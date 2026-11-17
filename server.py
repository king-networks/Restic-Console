#!/usr/bin/env python3
"""Restic Console: a local web UI for restic backup jobs.

* Destinations = restic repositories. Jobs = sources + destination + schedule + retention.
* Each job runs runner.py on a schedule: a systemd timer on Linux, a Task Scheduler task on
  Windows. This server edits config.json and the schedules, reads run history back from the
  run records (runs.py), and browses/restores snapshots.
* Runs as root / SYSTEM, listens on 127.0.0.1 only, and needs a session obtained with the
  token in <config dir>/token.

Standard library only. Dev mode: RC_DEV=1 plus the RC_* overrides in common.py runs it
unprivileged against scratch paths; nothing touches systemd or Task Scheduler in dev mode.

    server.py                     run the web server
    server.py init [options]      create/upgrade config.json and the token (used by the installers)
    server.py install-tasks       Windows: register the server and job tasks (used by install.ps1)
    server.py --version
"""
import argparse
import collections
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # python -I drops the script dir
import common  # noqa: E402
import notify  # noqa: E402
import runs as runstore  # noqa: E402
from common import (CFG_LOCK, DEV, EXCLUDES_DIR, IS_WIN, KEYS, TOKEN_FILE, atomic_write, load_cfg,  # noqa: E402
                    plat, read_text, save_cfg, sh)

PORT = int(os.environ.get("RC_PORT", "8790"))
BIND = "127.0.0.1"
STATIC = common.SCRIPT_DIR / "static"
HOSTNAME = socket.gethostname()

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
SNAP_RE = re.compile(r"^[0-9a-f]{8,64}$")
KEEP = ("last", "hourly", "daily", "weekly", "monthly", "yearly")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def settings():
    return load_cfg()["settings"]


def get_dest(did):
    d = load_cfg()["destinations"].get(did or "")
    if not d:
        raise ApiError(404, f"no destination {did!r}")
    return d


def get_job(jid):
    j = load_cfg()["jobs"].get(jid or "")
    if not j:
        raise ApiError(404, f"no job {jid!r}")
    return j


def restic():
    return common.restic_bin()


def platform_info():
    return {"name": plat.NAME, "features": plat.FEATURES, "version": common.__version__,
            "keys_dir": str(KEYS), "token_file": str(TOKEN_FILE), "sep": "\\" if IS_WIN else "/"}


# ------------------------------------------------------------------ restic
_health = {}


def dest_health(did, dest=None, fresh=False):
    dest = dest or get_dest(did)
    c = _health.get(did)
    if c and not fresh and time.time() - c[0] < 20:
        return c[1]
    v = {"mount": dest.get("mount") or None, "mounted": True, "reachable": False, "df": None, "error": ""}
    repo = dest["repo"]
    err = common.prepare_dest(dest)
    if err:
        v["error"] = err
        v["mounted"] = not dest.get("mount")
    elif common.is_local_path(repo):
        rc = plat.probe_path(os.path.join(repo, "config"))
        v["reachable"] = rc == 0
        if rc:
            v["error"] = "storage not responding (timeout)" if rc == 124 else "repository not found"
        v["df"] = plat.disk_usage(repo)
    else:   # remote backend (sftp:, rest:, s3: ...): ask restic
        rc, _, err = sh([restic(), "cat", "config", "--no-lock"], timeout=30, env=common.restic_env(dest))
        v["reachable"] = rc == 0
        if rc:
            v["error"] = err.strip().splitlines()[-1][:200] if err.strip() else f"restic rc={rc}"
    _health[did] = (time.time(), v)
    return v


def need_dest(did):
    dest = get_dest(did)
    h = dest_health(did, dest)
    if not h["reachable"]:
        raise ApiError(503, f"{dest['name']}: {h['error'] or 'not reachable'}")
    return dest


def restic_json(did, args, timeout=120):
    dest = need_dest(did)
    rc, out, err = sh([restic(), *args, "--json", "--no-lock"], timeout=timeout, env=common.restic_env(dest))
    if rc != 0:
        raise ApiError(502, f"restic {args[0]} failed (rc={rc}): {err.strip()[-600:]}")
    return out


_snaps = {}


def snapshots(did, refresh=False):
    c = _snaps.get(did)
    if c and not refresh and time.time() - c[0] < 120:
        return c[1]
    data = json.loads(restic_json(did, ["snapshots"]) or "[]")
    data.sort(key=lambda s: s.get("time", ""), reverse=True)
    _snaps[did] = (time.time(), data)
    return data


def check_snap(s):
    if not SNAP_RE.fullmatch(s or ""):
        raise ApiError(400, "bad snapshot id")
    return s


def ls(did, sid, path):
    path = common.check_snapshot_path(path)
    out = restic_json(did, ["ls", check_snap(sid), path], timeout=300)
    items = []
    for line in out.splitlines():
        try:
            n = json.loads(line)
        except ValueError:
            continue
        if "node" not in (n.get("struct_type"), n.get("message_type")):
            continue
        parent = n["path"].rstrip("/").rsplit("/", 1)[0] or "/"
        if n["path"] == path or parent != path:
            continue
        items.append({k: n.get(k) for k in ("name", "type", "path", "size", "mtime")})
    items.sort(key=lambda n: (n["type"] != "dir", n["name"].lower()))
    return items


# ------------------------------------------------------------------ runs
def reap(rec):
    import runner
    return runner.reap(load_cfg(), rec)


def job_runs(jid, days=60):
    get_job(jid)
    return runstore.list_runs(jid, days, reap=reap)


def job_running(jid):
    rec = runstore.running(jid)
    return rec["id"] if rec else None


def dest_busy(did):
    """Name of a job currently backing up to this destination, if any."""
    for jid, j in load_cfg()["jobs"].items():
        if j["destination"] == did and job_running(jid):
            return j["name"]
    return None


def reaper_loop():
    """Close runs whose process died (Windows has no OnFailure=; on Linux this is a backstop)."""
    while True:
        time.sleep(60)
        try:
            for jid in load_cfg()["jobs"]:
                runstore.list_runs(jid, days=3, reap=reap)
        except Exception as e:  # noqa: BLE001
            print(f"reaper: {type(e).__name__}: {e}", flush=True)


# ------------------------------------------------------------------ status
def status():
    cfg = load_cfg()
    jobs = []
    for jid, j in cfg["jobs"].items():
        rs = runstore.list_runs(jid, days=10, reap=reap)
        done = [r for r in rs if r["status"] not in ("running", "unknown")]
        jobs.append({"id": jid, "name": j["name"], "destination": j["destination"], "enabled": j.get("enabled", False),
                     "sources": j["sources"], "schedule": j["schedule"], "running": job_running(jid),
                     "next": plat.next_run(jid, j), "last": done[0] if done else None,
                     "recent": [r["status"] for r in done[:24]]})
    dests = [{"id": did, "name": d["name"], "repo": d["repo"], "health": dest_health(did, d),
              "jobs": [jid for jid, j in cfg["jobs"].items() if j["destination"] == did]}
             for did, d in cfg["destinations"].items()]
    return {"host": HOSTNAME, "dev": DEV, "platform": platform_info(), "jobs": jobs, "destinations": dests,
            "raid": plat.raid_status(), "tasks": [t.summary() for t in TASKS.recent(6)]}


# ------------------------------------------------------------------ jobs & destinations: edit
def calendar_preview(expr, n=5):
    try:
        return plat.calendar_preview(expr, n)
    except ValueError as e:
        raise ApiError(400, str(e)) from None


def clean_job(jid, b, existing):
    cfg = load_cfg()
    name = str(b.get("name", "")).strip()[:60]
    if not name:
        raise ApiError(400, "Give the job a name")
    if b.get("destination") not in cfg["destinations"]:
        raise ApiError(400, "Pick a destination")
    sources = [common.check_local(s.strip(), "source") for s in b.get("sources", []) if str(s).strip()]
    if not 1 <= len(sources) <= 30:
        raise ApiError(400, "Add between 1 and 30 source folders")
    missing = [s for s in sources if not os.path.exists(s)]
    if missing:
        raise ApiError(400, "Source does not exist: " + ", ".join(missing))
    tag = str(b.get("tag") or (existing or {}).get("tag") or jid)
    if not TAG_RE.fullmatch(tag):
        raise ApiError(400, "Tag may use letters, digits and _ . : - only")
    for ojid, oj in cfg["jobs"].items():
        if ojid != jid and oj["destination"] == b["destination"] and oj["tag"] == tag:
            raise ApiError(400, f"Job '{oj['name']}' already uses tag '{tag}' on that destination; retention would mix them up")
    ret = {k: int(b.get("retention", {}).get(k, 0) or 0) for k in KEEP}
    if any(not 0 <= v <= 9999 for v in ret.values()):
        raise ApiError(400, "Retention values must be 0-9999")
    if not any(ret.values()):
        raise ApiError(400, "Set at least one retention rule above 0")
    s = b.get("schedule", {})
    cals = [calendar_preview(c, 1)["normalized"] for c in s.get("calendars", []) if str(c).strip()]
    if not 1 <= len(cals) <= 6:
        raise ApiError(400, "Give between 1 and 6 schedule times")
    delay = int(s.get("randomized_delay_min", 0))
    if not 0 <= delay <= 720:
        raise ApiError(400, "Random delay must be 0-720 minutes")
    pct = int(b.get("check_read_data_percent", 0) or 0)
    if not 0 <= pct <= 100:
        raise ApiError(400, "Read-data percent must be 0-100")
    job = dict(existing or {})
    job.update(name=name, destination=b["destination"], sources=sources, tag=tag, retention=ret,
               schedule={"calendars": cals, "randomized_delay_min": delay, "persistent": bool(s.get("persistent", True))},
               enabled=bool(b.get("enabled", True)), prune=bool(b.get("prune")), check=bool(b.get("check")),
               check_read_data_percent=pct, one_file_system=bool(b.get("one_file_system")),
               exclude_caches=bool(b.get("exclude_caches", True)), vss=bool(b.get("vss", True)),
               notify={k: bool(b.get("notify", {}).get(k, k == "failure")) for k in ("failure", "warning", "success")})
    for old in ("notify_failure", "legacy_units"):
        job.pop(old, None)
    job.setdefault("excludes_file", str(EXCLUDES_DIR / f"{jid}.txt"))
    return job


def save_job(b):
    jid = str(b.get("id", "")).strip()
    if not ID_RE.fullmatch(jid):
        raise ApiError(400, "Job ID: lowercase letters, digits and dashes, up to 32")
    with CFG_LOCK:
        cfg = load_cfg()
        existing = cfg["jobs"].get(jid)
        if b.get("new") and existing:
            raise ApiError(400, f"A job with ID '{jid}' already exists")
        job = clean_job(jid, b, existing)
        if "excludes" in b:
            text = str(b["excludes"])
            if len(text) > 64 * 1024 or "\0" in text:
                raise ApiError(400, "Excludes too large")
            ex = Path(job["excludes_file"])
            if ex.exists():
                shutil.copy2(ex, ex.with_name(ex.name + ".bak"))
            atomic_write(ex, text if text.endswith("\n") or not text else text + "\n")
        cfg["jobs"][jid] = job
        save_cfg(cfg)
    if not DEV:
        try:
            plat.install_schedule(jid, job)
        except RuntimeError as e:
            raise ApiError(500, f"Saved, but the schedule could not be installed: {e}") from None
    return {"id": jid, **job}


def delete_job(jid):
    with CFG_LOCK:
        cfg = load_cfg()
        job = cfg["jobs"].get(jid or "")
        if not job:
            raise ApiError(404, "no such job")
        if job_running(jid):
            raise ApiError(409, "The job is running; stop it first")
        del cfg["jobs"][jid]
        save_cfg(cfg)
    if not DEV:
        plat.remove_schedule(jid)
    ex = Path(job.get("excludes_file", ""))
    if ex.parent == EXCLUDES_DIR and ex.exists():
        ex.unlink()
    return {"ok": True, "note": "Snapshots made by this job stay in the repository until you forget them."}


def run_job_now(jid):
    get_job(jid)
    if job_running(jid):
        raise ApiError(409, "Already running")
    try:
        plat.start_job(jid)
    except RuntimeError as e:
        raise ApiError(400 if DEV else 500, str(e)) from None
    time.sleep(1)
    return {"ok": True}


def stop_job(jid):
    get_job(jid)
    rec = runstore.running(jid)
    if not rec:
        return {"ok": True}
    if plat.stop_job(jid, rec):
        runstore.finish_external(rec, "stopped", "Stopped from Restic Console.")
    return {"ok": True}


def read_excludes(job):
    try:
        return read_text(job["excludes_file"])
    except (FileNotFoundError, KeyError):
        return ""


def dest_from_body(b):
    name = str(b.get("name", "")).strip()[:60]
    repo = str(b.get("repo", "")).strip()
    if not name or not repo or "\n" in repo or len(repo) > 500:
        raise ApiError(400, "Name and repository location are required")
    mount = ""
    if common.is_local_path(repo):
        repo = common.check_local(repo, "repository path")
        mount = plat.network_mount(repo)
    elif repo.startswith("/") or re.match(r"^[A-Za-z]:", repo):
        raise ApiError(400, "Give the full path of the repository folder")
    return {"name": name, "repo": repo, "mount": mount}


def add_destination(b):
    did = str(b.get("id", "")).strip()
    if not ID_RE.fullmatch(did):
        raise ApiError(400, "Destination ID: lowercase letters, digits and dashes")
    if did in load_cfg()["destinations"]:
        raise ApiError(400, f"Destination '{did}' already exists")
    dest = dest_from_body(b)
    if plat.FEATURES["share_login"] and str(b.get("share_user", "")).strip():
        user = str(b["share_user"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9._@\\ -]{1,128}", user):
            raise ApiError(400, "Bad share username")
        spf = KEYS / f"{did}.share.pw"
        atomic_write(spf, str(b.get("share_password", "")), 0o600)
        dest.update(share_user=user, share_password_file=str(spf))
    err = common.prepare_dest(dest)
    if err:
        raise ApiError(400, err)
    mode = b.get("mode")
    keyfile = KEYS / f"{did}.pw"
    local = common.is_local_path(dest["repo"])
    if mode == "create":
        if local and os.path.exists(os.path.join(dest["repo"], "config")):
            raise ApiError(400, "There is already a restic repository there; use 'Connect existing'")
        atomic_write(keyfile, secrets.token_urlsafe(32), 0o600)
        dest["password_file"] = str(keyfile)
        if local:
            os.makedirs(dest["repo"], exist_ok=True)
        t = Task("init", f"Create repository {dest['name']}", [restic(), "init"], env=common.restic_env(dest),
                 after=lambda t: register_dest(did, dest))
        return TASKS.start(t).summary()
    if mode == "existing":
        if b.get("password_file"):
            pf = common.check_local(b["password_file"], "password file")
            if not os.path.isfile(pf):
                raise ApiError(400, f"{pf} not found")
            dest["password_file"] = pf
        elif b.get("password"):
            tmp = KEYS / f".{did}.pw.check"
            atomic_write(tmp, str(b["password"]), 0o600)
            dest["password_file"] = str(tmp)
        else:
            raise ApiError(400, "Give the repository password or a password file")
        rc, _, err = sh([restic(), "cat", "config", "--no-lock"], timeout=60, env=common.restic_env(dest))
        if dest["password_file"].endswith(".pw.check"):
            if rc == 0:
                os.replace(dest["password_file"], keyfile)
                dest["password_file"] = str(keyfile)
            else:
                os.unlink(dest["password_file"])
        if rc != 0:
            raise ApiError(400, "Could not open the repository: " + (err.strip().splitlines() or ["?"])[-1][:300])
        register_dest(did, dest)
        return {"ok": True, "id": did}
    raise ApiError(400, "mode must be create or existing")


def register_dest(did, dest):
    with CFG_LOCK:
        cfg = load_cfg()
        cfg["destinations"][did] = dest
        save_cfg(cfg)
    return {"destination": did}


def update_destination(b):
    did = b.get("id")
    with CFG_LOCK:
        cfg = load_cfg()
        d = cfg["destinations"].get(did)
        if not d:
            raise ApiError(404, "no such destination")
        d["name"] = str(b.get("name", d["name"])).strip()[:60] or d["name"]
        save_cfg(cfg)
    return d


def delete_destination(did):
    with CFG_LOCK:
        cfg = load_cfg()
        users = [j["name"] for j in cfg["jobs"].values() if j["destination"] == did]
        if users:
            raise ApiError(409, "Used by job(s): " + ", ".join(users))
        if not cfg["destinations"].pop(did, None):
            raise ApiError(404, "no such destination")
        save_cfg(cfg)
    return {"ok": True, "note": "Removed from the list. The repository and its password file were left in place."}


# ------------------------------------------------------------------ settings + notifications
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+$")


def save_settings(b):
    with CFG_LOCK:
        cfg = load_cfg()
        st = cfg["settings"]
        if "desktop_user" in b:
            u = str(b["desktop_user"]).strip()
            if u and not plat.user_exists(u):
                raise ApiError(400, f"No such user: {u}")
            st["desktop_user"] = u
        if "restore_roots" in b:
            st["restore_roots"] = [common.check_local(r.strip(), "restore folder") for r in b["restore_roots"] if str(r).strip()]
        n = b.get("notifications")
        if n:
            d, e, w = n.get("desktop", {}), n.get("email", {}), n.get("webhook", {})
            du = str(d.get("user") or st["desktop_user"]).strip()
            if du and not plat.user_exists(du):
                raise ApiError(400, f"No such user: {du}")
            st["notifications"]["desktop"] = {"enabled": bool(d.get("enabled")), "user": du}
            to = [a.strip() for a in e.get("to", []) if str(a).strip()]
            bad = [a for a in to + ([e["from"]] if e.get("from") else []) if not EMAIL_RE.match(a)]
            if bad:
                raise ApiError(400, "Not an email address: " + ", ".join(bad))
            sec = e.get("security", "starttls")
            if sec not in ("starttls", "ssl", "none"):
                raise ApiError(400, "security must be starttls, ssl or none")
            host = str(e.get("host", "")).strip()
            if host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                raise ApiError(400, "Bad SMTP host")
            if e.get("enabled") and not (host and to):
                raise ApiError(400, "Email needs an SMTP host and at least one recipient")
            pwfile = str(KEYS / "smtp.pw")
            if e.get("password") is not None and e.get("password") != "":
                atomic_write(pwfile, str(e["password"]), 0o600)
            if e.get("clear_password") and os.path.exists(pwfile):
                os.unlink(pwfile)
            st["notifications"]["email"] = {"enabled": bool(e.get("enabled")), "host": host,
                                            "port": int(e.get("port") or (465 if sec == "ssl" else 587)),
                                            "security": sec, "username": str(e.get("username", "")).strip(),
                                            "from": str(e.get("from", "")).strip(), "to": to,
                                            "password_file": pwfile if os.path.exists(pwfile) else ""}
            url = str(w.get("url", "")).strip()
            if url and not url.startswith(("https://", "http://")):
                raise ApiError(400, "Webhook URL must start with https://")
            if w.get("enabled") and not url:
                raise ApiError(400, "Webhook needs a URL")
            st["notifications"]["webhook"] = {"enabled": bool(w.get("enabled")), "url": url}
        save_cfg(cfg)
        return public_settings(cfg["settings"])


def public_settings(st):
    st = json.loads(json.dumps(st))
    e = st["notifications"]["email"]
    e["has_password"] = bool(e.get("password_file") and os.path.exists(e["password_file"]))
    return st


def test_notify(channel):
    if channel not in notify.CHANNELS:
        raise ApiError(400, "unknown channel")
    res = notify.send(settings(), "Test notification", f"Restic Console on {HOSTNAME} can reach you here.", "test", only=channel)
    ch, ok, detail = res[0]
    if not ok:
        raise ApiError(502, f"{ch}: {detail}")
    return {"ok": True, "detail": detail}


# ------------------------------------------------------------------ tasks (restic commands run from the UI)
class Task:
    def __init__(self, kind, title, cmd, env, exclusive=False, dest=None, after=None, pipe_to=None):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.title, self.cmd, self.env = kind, title, cmd, env
        self.exclusive, self.dest, self.after, self.pipe_to = exclusive, dest, after, pipe_to
        self.status, self.rc, self.started, self.ended, self.result = "queued", None, None, None, None
        self.lines = collections.deque(maxlen=20000)
        self.proc = None

    def summary(self):
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status, "rc": self.rc,
                "started": self.started, "ended": self.ended, "result": self.result, "dest": self.dest}

    def _drain(self, stream):
        for line in stream:
            self.lines.append(line.rstrip("\r\n"))

    def run(self):
        self.started, self.status = time.time(), "running"
        self.lines.append("$ " + " ".join(self.cmd) + (" | " + " ".join(self.pipe_to) if self.pipe_to else ""))
        opts = dict(text=True, encoding="utf-8", errors="replace", creationflags=common.NO_WINDOW)
        try:
            if self.pipe_to:    # restic dump ... | writer running as the desktop user
                p1 = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                                      creationflags=common.NO_WINDOW)
                p2 = subprocess.Popen(self.pipe_to, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **opts)
                p1.stdout.close()
                self.proc = p1
                err = threading.Thread(target=lambda: [self.lines.append(x.decode("utf-8", "replace").rstrip()) for x in p1.stderr],
                                       daemon=True)
                err.start()
                self._drain(p2.stdout)
                rc1, rc2 = p1.wait(), p2.wait()
                err.join(5)
                self.rc = rc1 if rc1 else rc2
            else:
                self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env,
                                             bufsize=1, **opts)
                self._drain(self.proc.stdout)
                self.rc = self.proc.wait()
            self.status = "done" if self.rc == 0 else "cancelled" if getattr(self, "cancelled", False) else "failed"
            if self.after and self.rc == 0:
                self.result = self.after(self)
        except Exception as e:  # noqa: BLE001
            self.lines.append(f"error: {e}")
            self.status = "failed"
        self.ended = time.time()
        if self.dest:
            _snaps.pop(self.dest, None)

    def cancel(self):
        if self.proc and self.status == "running":
            self.cancelled = True
            self.proc.terminate()


class Tasks:
    def __init__(self):
        self.tasks = collections.OrderedDict()
        self.lock = threading.Lock()

    def start(self, t):
        with self.lock:
            busy = [x for x in self.tasks.values() if x.status == "running" and x.dest == t.dest and (x.exclusive or t.exclusive)]
            if busy:
                raise ApiError(409, f"'{busy[0].title}' is still running on this destination")
            if t.exclusive and t.dest:
                b = dest_busy(t.dest)
                if b:
                    raise ApiError(409, f"Backup job '{b}' is running on this destination; try again when it finishes")
            self.tasks[t.id] = t
            while len(self.tasks) > 50:
                self.tasks.popitem(last=False)
        threading.Thread(target=t.run, daemon=True).start()
        return t

    def recent(self, n):
        return list(reversed(list(self.tasks.values())))[:n]

    def get(self, tid):
        t = self.tasks.get(tid)
        if not t:
            raise ApiError(404, "no such task")
        return t


TASKS = Tasks()


def _under(path, root):
    p, r = os.path.normcase(path), os.path.normcase(root.rstrip("\\/"))
    return p == r or p.startswith(r + os.sep)


def safe_target(target):
    """A new or empty folder under one of the restore roots."""
    t = os.path.realpath(common.check_local(target or "", "restore folder"))
    roots = [os.path.realpath(r) for r in settings().get("restore_roots") or []]
    if not any(_under(t, r) for r in roots):
        raise ApiError(400, "Restore folder must be inside " + " or ".join(roots or ["(no restore folders set in Settings)"]))
    if os.path.exists(t) and (not os.path.isdir(t) or os.listdir(t)):
        raise ApiError(400, "That folder exists and is not empty")
    return t


def restore_task(b, dest, did):
    sid, path = check_snap(b.get("snapshot")), common.check_snapshot_path(b.get("path", "/"))
    target = safe_target(b.get("target"))
    user = settings().get("desktop_user")
    title = f"Restore {path} from {sid[:8]}"
    result = {"target": target, "path": path}
    is_dir = b.get("type", "dir") == "dir"
    writer = plat.restore_writer(user, target, path, is_dir)
    if writer:
        # Linux: root reads the snapshot, the desktop user writes the files. Root never writes into
        # a folder the user controls, so swapping in a symlink can't redirect the restore.
        rc, _, err = sh(plat.as_user_cmd(user, ["mkdir", "-p", target]), timeout=30)
        if rc:
            raise ApiError(400, f"Cannot create {target}: {err.strip()}")
        dump = [restic(), "dump", "--no-lock", sid, path] + (["--archive", "tar"] if is_dir else [])
        return Task("restore", title, dump, common.restic_env(dest), False, did, after=lambda t: result, pipe_to=writer)
    os.makedirs(target, exist_ok=True)
    args = ["restore", sid, "--target", target] + (["--include", path] if path != "/" else [])
    return Task("restore", title, [restic(), *args], common.restic_env(dest), False, did, after=lambda t: result)


def new_task(b):
    kind, did = b.get("kind"), b.get("dest")
    job = None
    if kind in ("retention_preview", "retention_apply"):
        job = get_job(b.get("job"))
        did = job["destination"]
    dest = need_dest(did)
    env = common.restic_env(dest)
    R = lambda *a: [restic(), *a]  # noqa: E731
    lockwait = ["--retry-lock", "10m"]
    if kind == "check":
        return Task(kind, f"Check {dest['name']}", R("check", *lockwait), env, True, did)
    if kind == "check_data":
        pct = int(b.get("percent", 5))
        if not 1 <= pct <= 100:
            raise ApiError(400, "percent must be 1-100")
        return Task(kind, f"Check {dest['name']} + read {pct}%", R("check", f"--read-data-subset={pct}%", *lockwait), env, True, did)
    if kind == "stats":
        return Task(kind, f"Size of {dest['name']}", R("stats", "--mode", "raw-data", "--no-lock"), env, False, did)
    if kind == "prune":
        return Task(kind, f"Prune {dest['name']}", R("prune", *lockwait), env, True, did)
    if kind == "unlock":
        return Task(kind, f"Remove stale locks on {dest['name']}", R("unlock"), env, True, did)
    if kind in ("retention_preview", "retention_apply"):
        keep = [a for k in KEEP if job["retention"].get(k) for a in (f"--keep-{k}", str(job["retention"][k]))]
        base = ["forget", "--tag", job["tag"], "--host", HOSTNAME, *keep]
        if kind == "retention_preview":
            return Task(kind, f"Preview retention for {job['name']}", R(*base, "--dry-run", "--no-lock"), env, False, did)
        return Task(kind, f"Apply retention for {job['name']}", R(*base, "--prune", *lockwait), env, True, did)
    if kind == "forget_snapshot":
        sid = check_snap(b.get("snapshot"))
        return Task(kind, f"Forget snapshot {sid[:8]}", R("forget", sid, *lockwait), env, True, did)
    if kind == "diff":
        a, c = check_snap(b.get("a")), check_snap(b.get("b"))
        return Task(kind, f"Diff {a[:8]} → {c[:8]}", R("diff", "--no-lock", a, c), env, False, did)
    if kind == "restore":
        return restore_task(b, dest, did)
    raise ApiError(400, f"unknown task {kind!r}")


# ------------------------------------------------------------------ auth
def load_token():
    try:
        return read_text(TOKEN_FILE).strip()
    except OSError:
        return ""


TOKEN = load_token()
SESSION_DAYS = 30
SESSIONS, CODES, AUTH_LOCK = {}, {}, threading.Lock()
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def new_session():
    sid = secrets.token_hex(32)
    with AUTH_LOCK:
        now = time.time()
        for k in [k for k, exp in SESSIONS.items() if exp < now]:
            del SESSIONS[k]
        SESSIONS[sid] = now + SESSION_DAYS * 86400
    return sid


# ------------------------------------------------------------------ HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ResticConsole/" + common.__version__

    def log_message(self, fmt, *args):
        if DEV:
            super().log_message(fmt, *args)

    def send_json(self, code, obj, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def session_id(self):
        m = re.search(r"(?:^|;\s*)rc_session=([0-9a-f]{64})", self.headers.get("Cookie", ""))
        return m.group(1) if m else None

    def authed(self):
        sid = self.session_id()
        with AUTH_LOCK:
            return bool(sid and SESSIONS.get(sid, 0) > time.time())

    def guard(self, post):
        if self.headers.get("Host") not in ALLOWED_HOSTS:          # DNS-rebinding protection
            raise ApiError(403, "bad host")
        if post:
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in ALLOWED_HOSTS}:
                raise ApiError(403, "bad origin")
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise ApiError(415, "json only")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 256 * 1024:
            raise ApiError(413, "too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            self.guard(post)
            if not post and url.path in ("/", "/index.html"):
                return self.static("index.html", "text/html; charset=utf-8")
            if not post and url.path == "/icon.svg":
                return self.static("icon.svg", "image/svg+xml")
            if not post and url.path == "/api/hello":
                return self.send_json(200, {"platform": platform_info()})
            if post and url.path == "/api/handoff":
                return self.handoff()
            if post and url.path == "/api/login":
                return self.login()
            if post and url.path == "/api/logout":
                return self.logout()
            if not self.authed():
                raise ApiError(401, "not logged in")
            if post:
                return self.send_json(200, self.post(url.path, self.body()))
            if url.path == "/api/dump":
                return self.dump(q)
            return self.send_json(200, self.get(url.path, q))
        except ApiError as e:
            self.send_json(e.code, {"error": e.msg})
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except (KeyError, TypeError) as e:
            self.send_json(400, {"error": f"bad request: {e}"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def handoff(self):
        # The launcher trades the token (sent in a header, never on a command line) for a
        # one-time code that is safe to put in the browser URL.
        if not (TOKEN and hmac.compare_digest(self.headers.get("X-Token", "").strip(), TOKEN)):
            raise ApiError(401, "bad token")
        code = secrets.token_hex(32)
        with AUTH_LOCK:
            now = time.time()
            for c in [c for c, t in CODES.items() if t < now]:
                del CODES[c]
            CODES[code] = now + 60
        self.send_json(200, {"code": code})

    def login(self):
        b = self.body()
        tok, code = str(b.get("token", "")), str(b.get("code", ""))
        with AUTH_LOCK:
            ok = bool(code) and CODES.pop(code, 0) > time.time()
        if ok or (TOKEN and tok and hmac.compare_digest(tok, TOKEN)):
            return self.send_json(200, {"ok": True}, [("Set-Cookie",
                f"rc_session={new_session()}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_DAYS * 86400}")])
        raise ApiError(401, "bad token")

    def logout(self):
        sid = self.session_id()
        with AUTH_LOCK:
            SESSIONS.pop(sid, None)
        self.send_json(200, {"ok": True}, [("Set-Cookie", "rc_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])

    def static(self, name, ctype):
        data = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def get(self, path, q):
        if path == "/api/status":
            return status()
        if path == "/api/config":
            cfg = load_cfg()
            return {"settings": public_settings(cfg["settings"]), "destinations": cfg["destinations"],
                    "jobs": {k: {**v, "excludes": read_excludes(v)} for k, v in cfg["jobs"].items()},
                    "host": HOSTNAME, "platform": platform_info()}
        if path == "/api/snapshots":
            return snapshots(q.get("dest"), q.get("refresh") == "1")
        if path == "/api/ls":
            return ls(q.get("dest"), q.get("snapshot"), q.get("path", "/"))
        if path == "/api/runs":
            return job_runs(q.get("job"), int(q.get("days", 60)))
        if path == "/api/runlog":
            get_job(q.get("job"))
            return runstore.read_log(q.get("job"), q.get("id"), q.get("cursor"))
        if path == "/api/calendar":
            return calendar_preview(q.get("expr", ""), int(q.get("n", 5)))
        if path == "/api/destination/password":
            d = get_dest(q.get("id"))
            return {"password": read_text(d["password_file"]).strip(), "file": d["password_file"]}
        if path == "/api/pathinfo":
            p = common.check_local(q.get("path", ""))
            return {"exists": os.path.exists(p), "dir": os.path.isdir(p), "mount": plat.mount_of(p)}
        if path == "/api/tasks":
            return [t.summary() for t in TASKS.recent(50)]
        if path == "/api/task":
            t = TASKS.get(q.get("id"))
            lines = list(t.lines)
            return {**t.summary(), "lines": lines[int(q.get("from", 0)):]}
        raise ApiError(404, "not found")

    def post(self, path, b):
        if path == "/api/job/save":
            return save_job(b)
        if path == "/api/job/delete":
            return delete_job(b.get("id"))
        if path == "/api/job/run":
            return run_job_now(b.get("id"))
        if path == "/api/job/stop":
            return stop_job(b.get("id"))
        if path == "/api/settings":
            return save_settings(b)
        if path == "/api/notify/test":
            return test_notify(b.get("channel"))
        if path == "/api/destination/add":
            return add_destination(b)
        if path == "/api/destination/update":
            return update_destination(b)
        if path == "/api/destination/delete":
            return delete_destination(b.get("id"))
        if path == "/api/tasks":
            return TASKS.start(new_task(b)).summary()
        if path == "/api/task/cancel":
            t = TASKS.get(b.get("id"))
            t.cancel()
            return t.summary()
        raise ApiError(404, "not found")

    def dump(self, q):
        did, sid = q.get("dest"), check_snap(q.get("snapshot"))
        path = common.check_snapshot_path(q.get("path", ""))
        dest = need_dest(did)
        is_dir = q.get("dir") == "1"
        args = [restic(), "dump", "--no-lock", sid, path] + (["--archive", "zip"] if is_dir else [])
        name = (path.rstrip("/").rsplit("/", 1)[-1] or "root") + (".zip" if is_dir else "")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=common.restic_env(dest),
                             creationflags=common.NO_WINDOW)
        first = p.stdout.read(65536)
        if not first and p.wait() != 0:
            raise ApiError(502, "restic dump failed: " + p.stderr.read().decode(errors="replace")[-400:])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        try:
            self.wfile.write(first)
            while True:
                chunk = p.stdout.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            p.kill()
        finally:
            p.wait()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not IS_WIN      # on Windows this would let a second server bind the same port


# ------------------------------------------------------------------ init (installers)
def init(argv):
    """Create the config dirs, token and config.json; on upgrade only refresh settings.restic."""
    ap = argparse.ArgumentParser(prog="server.py init")
    ap.add_argument("--user", default="", help="desktop user (gets alerts; restored files belong to them)")
    ap.add_argument("--restore-root", default="", help="folder restores may write into (default: none)")
    ap.add_argument("--restic", default="", help="full path of the restic binary")
    a = ap.parse_args(argv)
    for d in (common.ETC, KEYS, EXCLUDES_DIR, common.STATE):
        d.mkdir(parents=True, exist_ok=True)
    if not IS_WIN:
        os.chmod(KEYS, 0o700)
    if not load_token():
        atomic_write(TOKEN_FILE, secrets.token_hex(32) + "\n", 0o640)
    new = not common.CONFIG.exists()
    with CFG_LOCK:
        cfg = load_cfg()
        st = cfg["settings"]
        if a.restic:
            st["restic"] = a.restic
        if new:
            st["desktop_user"] = a.user
            st["notifications"]["desktop"]["user"] = a.user
            st["restore_roots"] = [a.restore_root] if a.restore_root else []
        atomic_write(common.CONFIG, json.dumps(cfg, indent=2) + "\n", 0o640)
    print(("created " if new else "kept ") + str(common.CONFIG))


def install_tasks():
    """Windows installer step: (re)register the server task and every job's task with current paths."""
    if not hasattr(plat, "install_service"):
        raise SystemExit("install-tasks is only used on Windows; linux/install.sh sets up systemd")
    for jid, job in load_cfg()["jobs"].items():
        plat.install_schedule(jid, job)
        print(f"job task: {jid}")
    plat.install_service()
    print("server task registered and started")


def main():
    if sys.argv[1:2] == ["--version"]:
        print(common.__version__)
        return
    if sys.argv[1:2] == ["init"]:
        return init(sys.argv[2:])
    if sys.argv[1:2] == ["install-tasks"]:
        return install_tasks()
    if not TOKEN:
        raise SystemExit(f"No token in {TOKEN_FILE}; run the installer (or: server.py init)")
    print(f"Restic Console {common.__version__} on http://{BIND}:{PORT} ({plat.NAME}, config {common.CONFIG}"
          f"{', DEV MODE' if DEV else ''})", flush=True)
    threading.Thread(target=reaper_loop, daemon=True).start()
    Server((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
