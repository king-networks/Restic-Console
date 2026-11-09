#!/usr/bin/env python3
"""Restic Console: a local web UI for restic backup jobs, run by systemd.

* Destinations = restic repositories. Jobs = sources + destination + schedule + retention.
* Each job runs as restic-backup@<job>.service, fired by restic-backup@<job>.timer, which
  executes runner.py. This server edits /etc/restic-console/config.json and the timers,
  reads history back from the journal, and browses/restores snapshots.
* Runs as root (repo passwords, systemd), listens on 127.0.0.1 only, and needs a session
  cookie derived from the token in /etc/restic-console/token.

Standard library only. Dev mode: RC_DEV=1 plus the RC_* overrides below runs it
unprivileged against scratch paths; systemctl is never called for writes in dev mode.
"""
import collections
import hashlib
import hmac
import http.server
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402
sys.path.insert(0, SCRIPT_DIR)                 # python -I drops the script dir
import notify  # noqa: E402

E = os.environ.get
DEV = E("RC_DEV") == "1"
PORT = int(E("RC_PORT", "8790"))
BIND = "127.0.0.1"
ETC = Path(E("RC_ETC", "/etc/restic-console"))
CONFIG = Path(E("RC_CONFIG", str(ETC / "config.json")))
TOKEN_FILE = ETC / "token"
KEYS = ETC / "keys"
EXCLUDES_DIR = ETC / "excludes"
UNIT_DIR = Path(E("RC_UNIT_DIR", "/etc/systemd/system"))
STATIC = Path(__file__).resolve().parent / "static"
RESTIC = shutil.which("restic") or "/usr/bin/restic"
HOSTNAME = socket.gethostname()
NETFS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "fuse.rclone", "9p", "glusterfs", "ceph"}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
SNAP_RE = re.compile(r"^[0-9a-f]{8,64}$")
KEEP = ("last", "hourly", "daily", "weekly", "monthly", "yearly")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ------------------------------------------------------------------ helpers
def sh(cmd, timeout=30, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)


def systemctl(*args):
    if DEV:
        return 0, "", ""
    return sh(["systemctl", *args], timeout=60)


def systemctl_show(unit, props):
    _, out, _ = sh(["systemctl", "show", unit, "-p", ",".join(props)])
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    return d


def usec(v):
    try:
        n = int(v)
        return n / 1e6 if n > 0 else None
    except (TypeError, ValueError):
        return None


def ts_to_epoch(s):
    if not s or s == "n/a":
        return None
    rc, out, _ = sh(["date", "-d", s, "+%s"])
    return int(out) if rc == 0 and out.strip().isdigit() else None


def atomic_write(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def mount_of(path):
    """Longest mount prefix of path from /proc/mounts (never stats, so a dead NFS can't hang us)."""
    best = ("/", "")
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                p = line.split()
                mp, fs = p[1].replace("\\040", " "), p[2]
                if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best[0]):
                    best = (mp, fs)
    except OSError:
        pass
    return best


def is_mounted(mp):
    try:
        with open("/proc/self/mounts") as f:
            return any(line.split()[1] == mp for line in f)
    except OSError:
        return False


# ------------------------------------------------------------------ config
CFG_LOCK = threading.RLock()
DEFAULT_SETTINGS = {"desktop_user": "", "restore_roots": [],
                    "notifications": {"desktop": {"enabled": True, "user": ""},
                                      "email": {"enabled": False, "host": "", "port": 587, "security": "starttls",
                                                "username": "", "from": "", "to": [], "password_file": ""},
                                      "webhook": {"enabled": False, "url": ""}}}


def load_cfg():
    with CFG_LOCK:
        try:
            cfg = json.loads(CONFIG.read_text())
        except FileNotFoundError:
            cfg = {}
        cfg.setdefault("version", 1)
        cfg.setdefault("settings", {})
        for k, v in DEFAULT_SETTINGS.items():
            cfg["settings"].setdefault(k, json.loads(json.dumps(v)))
        for k, v in DEFAULT_SETTINGS["notifications"].items():
            cfg["settings"]["notifications"].setdefault(k, dict(v))
        cfg.setdefault("destinations", {})
        cfg.setdefault("jobs", {})
        return cfg


def save_cfg(cfg):
    with CFG_LOCK:
        atomic_write(CONFIG, json.dumps(cfg, indent=2) + "\n", 0o640)
        user = cfg["settings"].get("desktop_user")
        if user and not DEV:
            try:
                os.chown(CONFIG, 0, pwd.getpwnam(user).pw_gid)
            except (KeyError, PermissionError):
                pass


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


def job_units(jid, job=None):
    job = job or get_job(jid)
    return [f"restic-backup@{jid}.service"] + list(job.get("legacy_units", []))


def timer_unit(jid):
    return f"restic-backup@{jid}.timer"


def dropin_path(jid):
    return UNIT_DIR / f"restic-backup@{jid}.timer.d" / "schedule.conf"


# ------------------------------------------------------------------ restic
def restic_env(dest):
    env = dict(os.environ, RESTIC_REPOSITORY=dest["repo"], RESTIC_PASSWORD_FILE=dest["password_file"],
               RESTIC_PROGRESS_FPS="0.2")
    env.setdefault("HOME", "/root")
    return env


_health = {}


def dest_health(did, dest=None, fresh=False):
    dest = dest or get_dest(did)
    c = _health.get(did)
    if c and not fresh and time.time() - c[0] < 20:
        return c[1]
    v = {"mount": dest.get("mount") or None, "mounted": True, "reachable": False, "df": None, "error": ""}
    repo = dest["repo"]
    local = repo.startswith("/")
    if dest.get("mount"):
        if not is_mounted(dest["mount"]):
            sh(["timeout", "10", "ls", dest["mount"]], timeout=12)
            time.sleep(1)
        v["mounted"] = is_mounted(dest["mount"])
        if not v["mounted"]:
            v["error"] = f"{dest['mount']} is not mounted"
    if v["mounted"]:
        if local:
            rc, _, err = sh(["timeout", "10", "stat", "-t", os.path.join(repo, "config")], timeout=12)
            v["reachable"] = rc == 0
            if rc:
                v["error"] = "repository not found" if rc == 1 else ("storage not responding (timeout)" if rc == 124 else err.strip()[:200])
            rc, out, _ = sh(["timeout", "10", "df", "-B1", "--output=size,used,avail", repo], timeout=12)
            if rc == 0:
                v["df"] = dict(zip(["size", "used", "avail"], map(int, out.split()[-3:])))
        else:   # remote backend (sftp:, rest:, s3: ...): ask restic
            rc, _, err = sh([RESTIC, "cat", "config", "--no-lock"], timeout=30, env=restic_env(dest))
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
    rc, out, err = sh([RESTIC, *args, "--json", "--no-lock"], timeout=timeout, env=restic_env(dest))
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


def check_abs(p, what="path"):
    if not isinstance(p, str) or not p.startswith("/") or "\0" in p or "\n" in p or len(p) > 4096:
        raise ApiError(400, f"bad {what}: {p!r}")
    return os.path.normpath(p)


def ls(did, sid, path):
    path = check_abs(path)
    out = restic_json(did, ["ls", check_snap(sid), path], timeout=300)
    items = []
    for line in out.splitlines():
        try:
            n = json.loads(line)
        except ValueError:
            continue
        if "node" not in (n.get("struct_type"), n.get("message_type")):
            continue
        if n["path"] == path or os.path.dirname(n["path"].rstrip("/") or "/") != path:
            continue
        items.append({k: n.get(k) for k in ("name", "type", "path", "size", "mtime")})
    items.sort(key=lambda n: (n["type"] != "dir", n["name"].lower()))
    return items


# ------------------------------------------------------------------ schedule
def calendar_preview(expr, n=5):
    if not isinstance(expr, str) or not expr.strip() or "\n" in expr or len(expr) > 200:
        raise ApiError(400, "Invalid calendar expression")
    rc, out, err = sh(["systemd-analyze", "calendar", f"--iterations={n}", expr])
    if rc != 0:
        t = (err or out).strip()
        raise ApiError(400, t.splitlines()[-1] if t else "invalid expression")
    norm = re.search(r"Normalized form:\s*(.+)", out)
    nexts = re.findall(r"(?:Next elapse|Iter(?:ation|\.) #\d+):\s*(.+)", out)
    return {"normalized": norm.group(1).strip() if norm else expr,
            "next": [{"text": t.strip(), "epoch": ts_to_epoch(t.strip())} for t in nexts]}


def write_timer(jid, job):
    s = job["schedule"]
    text = ("# Managed by Restic Console. Edit the job in the app instead.\n[Timer]\nOnCalendar=\n"
            + "".join(f"OnCalendar={c}\n" for c in s["calendars"])
            + f"RandomizedDelaySec={int(s.get('randomized_delay_min', 0))}min\n"
            + f"Persistent={'true' if s.get('persistent', True) else 'false'}\n")
    atomic_write(dropin_path(jid), text)
    systemctl("daemon-reload")
    if job.get("enabled"):
        systemctl("enable", timer_unit(jid))
        systemctl("restart", timer_unit(jid))
    else:
        systemctl("disable", "--now", timer_unit(jid))


def next_run(jid, job):
    if not job.get("enabled"):
        return None
    t = systemctl_show(timer_unit(jid), ["NextElapseUSecRealtime"])
    n = usec(t.get("NextElapseUSecRealtime")) or ts_to_epoch(t.get("NextElapseUSecRealtime"))
    if n:
        return n
    best = None
    for c in job["schedule"]["calendars"]:
        try:
            e = calendar_preview(c, 1)["next"][0]["epoch"]
            best = e if best is None or (e and e < best) else best
        except (ApiError, IndexError):
            pass
    return best


# ------------------------------------------------------------------ runs (journal)
SUMMARY_RES = {
    "snapshot": re.compile(r"snapshot ([0-9a-f]{8}) saved"),
    "added": re.compile(r"Added to the repository: ([\d.]+ \w+)(?: \(([\d.]+ \w+) stored\))?"),
    "processed": re.compile(r"processed (\d+) files, ([\d.]+ \w+) in ([\d:]+)"),
    "files": re.compile(r"Files:\s+(\d+) new,\s+(\d+) changed,\s+(\d+) unmodified"),
}


def msg_text(e):
    m = e.get("MESSAGE", "")
    if isinstance(m, list):
        m = bytes(x for x in m if isinstance(x, int)).decode("utf-8", "replace")
    return m or ""


def journal(units, since=None, cursor=None):
    cmd = ["journalctl", "-o", "json", "--no-pager",
           "--output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID,INVOCATION_ID,PRIORITY"]
    for u in units:
        cmd += ["-u", u]
    cmd += ["--after-cursor", cursor] if cursor else ["--since", since or "-60d"]
    _, out, _ = sh(cmd, timeout=60)
    for line in out.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            pass


def running_invocations(units):
    inv = {}
    for u in units:
        s = systemctl_show(u, ["ActiveState", "InvocationID"])
        if s.get("ActiveState") in ("activating", "active", "deactivating") and s.get("InvocationID"):
            inv[s["InvocationID"]] = u
    return inv


_runs_cache = {}


def runs(jid, days=60, cache=0):
    c = _runs_cache.get((jid, days))
    if cache and c and time.time() - c[0] < cache:
        return c[1]
    units = job_units(jid)
    groups = collections.OrderedDict()
    for e in journal(units, since=f"-{days}d"):
        inv = e.get("_SYSTEMD_INVOCATION_ID") or e.get("INVOCATION_ID")
        if not inv:
            continue
        g = groups.setdefault(inv, {"id": inv, "job": jid, "start": None, "end": None, "status": "running",
                                    "warnings": 0, "errors": 0})
        t = int(e["__REALTIME_TIMESTAMP"]) / 1e6
        g["start"] = g["start"] or t
        g["end"] = t
        m = msg_text(e)
        for k, rx in SUMMARY_RES.items():
            r = rx.search(m)
            if r:
                g[k] = r.groups() if len(r.groups()) > 1 else r.group(1)
        if "WARNING" in m:
            g["warnings"] += 1
        if "ERROR" in m or "Fatal:" in m:
            g["errors"] += 1
            g["error_msg"] = m[-300:]
        if "Deactivated successfully" in m or re.search(r"^Finished .*restic", m):
            g["status"] = "warning" if g["warnings"] else "success"
        elif "Failed with result" in m or "Failed to start" in m:
            g["status"] = "failed"
        elif m.startswith("Stopped ") or m.startswith("Stopping "):
            g["status"] = "stopped"
    live = running_invocations(units)
    out = list(groups.values())
    for g in out:
        if g["status"] == "running" and g["id"] not in live:
            g["status"] = "unknown"
    out.reverse()
    _runs_cache[(jid, days)] = (time.time(), out)
    return out


def run_log(jid, inv, cursor=None):
    if not re.fullmatch(r"[0-9a-f]{32}", inv or ""):
        raise ApiError(400, "bad invocation id")
    lines, last = [], cursor
    for e in journal(job_units(jid), since="-60d", cursor=cursor):
        last = e.get("__CURSOR", last)
        if inv in (e.get("_SYSTEMD_INVOCATION_ID"), e.get("INVOCATION_ID")):
            lines.append({"t": int(e["__REALTIME_TIMESTAMP"]) / 1e6, "m": msg_text(e)})
    return {"lines": lines[-8000:], "cursor": last}


def job_running(jid, job=None):
    live = running_invocations(job_units(jid, job))
    return next(iter(live), None)


def dest_busy(did):
    """Name of a job currently backing up to this destination, if any."""
    for jid, j in load_cfg()["jobs"].items():
        if j["destination"] == did and job_running(jid, j):
            return j["name"]
    return None


# ------------------------------------------------------------------ status
def raid_status():
    try:
        txt = Path("/proc/mdstat").read_text()
    except OSError:
        return []
    arrays = []
    for m in re.finditer(r"^(md\w+) : (\w+) (\w+) (.+)\n\s+\d+ blocks.*\[(\d+)/(\d+)\] \[([U_]+)\](?:\n(.*))?", txt, re.M):
        prog = re.search(r"(recovery|resync|check)\s*=\s*([\d.]+)%.*finish=([\d.]+min)", m.group(8) or "")
        arrays.append({"name": m.group(1), "level": m.group(3), "members": re.findall(r"(\w+)\[\d+\]", m.group(4)),
                       "want": int(m.group(5)), "have": int(m.group(6)), "map": m.group(7),
                       "progress": {"op": prog.group(1), "pct": float(prog.group(2)), "eta": prog.group(3)} if prog else None})
    return arrays


def status():
    cfg = load_cfg()
    jobs = []
    for jid, j in cfg["jobs"].items():
        rs = runs(jid, days=10, cache=15)
        done = [r for r in rs if r["status"] not in ("running", "unknown")]
        jobs.append({"id": jid, "name": j["name"], "destination": j["destination"], "enabled": j.get("enabled", False),
                     "sources": j["sources"], "schedule": j["schedule"], "running": job_running(jid, j),
                     "next": next_run(jid, j), "last": done[0] if done else None,
                     "recent": [r["status"] for r in done[:24]]})
    dests = [{"id": did, "name": d["name"], "repo": d["repo"], "health": dest_health(did, d),
              "jobs": [jid for jid, j in cfg["jobs"].items() if j["destination"] == did]}
             for did, d in cfg["destinations"].items()]
    return {"host": HOSTNAME, "dev": DEV, "jobs": jobs, "destinations": dests, "raid": raid_status(),
            "tasks": [t.summary() for t in TASKS.recent(6)]}


# ------------------------------------------------------------------ jobs & destinations: edit
def clean_job(jid, b, existing):
    cfg = load_cfg()
    name = str(b.get("name", "")).strip()[:60]
    if not name:
        raise ApiError(400, "Give the job a name")
    if b.get("destination") not in cfg["destinations"]:
        raise ApiError(400, "Pick a destination")
    sources = [check_abs(s.strip(), "source") for s in b.get("sources", []) if str(s).strip()]
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
               exclude_caches=bool(b.get("exclude_caches", True)),
               notify={k: bool(b.get("notify", {}).get(k, k == "failure")) for k in ("failure", "warning", "success")})
    job.pop("notify_failure", None)
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
    write_timer(jid, job)
    _runs_cache.clear()
    return {"id": jid, **job}


def delete_job(jid):
    with CFG_LOCK:
        cfg = load_cfg()
        job = cfg["jobs"].pop(jid, None)
        if not job:
            raise ApiError(404, "no such job")
        if job_running(jid, job):
            raise ApiError(409, "The job is running; stop it first")
        save_cfg(cfg)
    systemctl("disable", "--now", timer_unit(jid))
    shutil.rmtree(dropin_path(jid).parent, ignore_errors=True)
    systemctl("daemon-reload")
    ex = Path(job.get("excludes_file", ""))
    if ex.parent == EXCLUDES_DIR and ex.exists():
        ex.unlink()
    return {"ok": True, "note": "Snapshots made by this job stay in the repository until you forget them."}


def read_excludes(job):
    try:
        return Path(job["excludes_file"]).read_text()
    except (FileNotFoundError, KeyError):
        return ""


def dest_from_body(b, did):
    name = str(b.get("name", "")).strip()[:60]
    repo = str(b.get("repo", "")).strip()
    if not name or not repo or "\n" in repo or len(repo) > 500:
        raise ApiError(400, "Name and repository location are required")
    mount = ""
    if repo.startswith("/"):
        repo = check_abs(repo, "repository path")
        mp, fs = mount_of(repo)
        if fs in NETFS or fs == "autofs":
            mount = mp
    return {"name": name, "repo": repo, "mount": mount}


def add_destination(b):
    did = str(b.get("id", "")).strip()
    if not ID_RE.fullmatch(did):
        raise ApiError(400, "Destination ID: lowercase letters, digits and dashes")
    if did in load_cfg()["destinations"]:
        raise ApiError(400, f"Destination '{did}' already exists")
    dest = dest_from_body(b, did)
    mode = b.get("mode")
    keyfile = KEYS / f"{did}.pw"
    if mode == "create":
        if dest["repo"].startswith("/") and os.path.exists(os.path.join(dest["repo"], "config")):
            raise ApiError(400, "There is already a restic repository there; use 'Connect existing'")
        atomic_write(keyfile, secrets.token_urlsafe(32), 0o600)
        dest["password_file"] = str(keyfile)
        if dest["repo"].startswith("/"):
            os.makedirs(dest["repo"], exist_ok=True)
        t = Task("init", f"Create repository {dest['name']}", [RESTIC, "init"], env=restic_env(dest),
                 after=lambda t: register_dest(did, dest))
        return TASKS.start(t).summary()
    if mode == "existing":
        if b.get("password_file"):
            pf = check_abs(b["password_file"], "password file")
            if not os.path.isfile(pf):
                raise ApiError(400, f"{pf} not found")
            dest["password_file"] = pf
        elif b.get("password"):
            tmp = KEYS / f".{did}.pw.check"
            atomic_write(tmp, str(b["password"]), 0o600)
            dest["password_file"] = str(tmp)
        else:
            raise ApiError(400, "Give the repository password or a password file")
        rc, _, err = sh([RESTIC, "cat", "config", "--no-lock"], timeout=60, env=restic_env(dest))
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
            if u:
                try:
                    pwd.getpwnam(u)
                except KeyError:
                    raise ApiError(400, f"No such user: {u}")
            st["desktop_user"] = u
        if "restore_roots" in b:
            st["restore_roots"] = [check_abs(r.strip(), "restore folder") for r in b["restore_roots"] if str(r).strip()]
        n = b.get("notifications")
        if n:
            d, e, w = n.get("desktop", {}), n.get("email", {}), n.get("webhook", {})
            st["notifications"]["desktop"] = {"enabled": bool(d.get("enabled")), "user": str(d.get("user") or st["desktop_user"]).strip()}
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
    def __init__(self, kind, title, cmd, env, exclusive=False, dest=None, after=None):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.title, self.cmd, self.env = kind, title, cmd, env
        self.exclusive, self.dest, self.after = exclusive, dest, after
        self.status, self.rc, self.started, self.ended, self.result = "queued", None, None, None, None
        self.lines = collections.deque(maxlen=20000)
        self.proc = None

    def summary(self):
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status, "rc": self.rc,
                "started": self.started, "ended": self.ended, "result": self.result, "dest": self.dest}

    def run(self):
        self.started, self.status = time.time(), "running"
        self.lines.append("$ " + " ".join(self.cmd))
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         env=self.env, bufsize=1)
            for line in self.proc.stdout:
                self.lines.append(line.rstrip("\n"))
            self.rc = self.proc.wait()
            self.status = "done" if self.rc == 0 else ("cancelled" if self.rc in (-15, -9) else "failed")
            if self.after and self.rc == 0:
                self.result = self.after(self)
        except Exception as e:  # noqa: BLE001
            self.lines.append(f"error: {e}")
            self.status = "failed"
        self.ended = time.time()
        if self.dest:
            _snaps.pop(self.dest, None)


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


def restore_roots():
    roots = settings().get("restore_roots") or []
    return [os.path.realpath(r) for r in roots]


def safe_target(target):
    t = os.path.realpath(target or "")
    roots = restore_roots()
    if not any(t == r or t.startswith(r.rstrip("/") + "/") for r in roots):
        raise ApiError(400, "Restore folder must be inside " + " or ".join(roots or ["(no restore_roots configured)"]))
    if os.path.exists(t) and (not os.path.isdir(t) or os.listdir(t)):
        raise ApiError(400, "That folder exists and is not empty")
    return t


def chown_user(path):
    user = settings().get("desktop_user")
    if DEV or not user:
        return
    pw = pwd.getpwnam(user)
    sh(["chown", "-R", f"{pw.pw_uid}:{pw.pw_gid}", path], timeout=900)


def new_task(b):
    kind, did = b.get("kind"), b.get("dest")
    if kind == "retention_preview" or kind == "retention_apply":
        jid = b.get("job")
        job = get_job(jid)
        did = job["destination"]
    dest = need_dest(did)
    env = restic_env(dest)
    R = lambda *a: [RESTIC, *a]  # noqa: E731
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
        sid, path = check_snap(b.get("snapshot")), check_abs(b.get("path", "/"))
        target = safe_target(b.get("target"))
        os.makedirs(target, exist_ok=True)
        chown_user(target)
        args = ["restore", sid, "--target", target] + (["--include", path] if path != "/" else [])
        return Task(kind, f"Restore {path} from {sid[:8]}", R(*args), env, False, did,
                    after=lambda t: (chown_user(target), {"target": target, "path": path})[1])
    raise ApiError(400, f"unknown task {kind!r}")


# ------------------------------------------------------------------ auth
def load_token():
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


TOKEN = load_token()
SESSION = hmac.new(TOKEN.encode(), b"restic-console-session", hashlib.sha256).hexdigest() if TOKEN else ""
CODES, CODES_LOCK = {}, threading.Lock()
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


# ------------------------------------------------------------------ HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ResticConsole/2"

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

    def authed(self):
        m = re.search(r"(?:^|;\s*)rc_session=([0-9a-f]+)", self.headers.get("Cookie", ""))
        return bool(SESSION and m and hmac.compare_digest(m.group(1), SESSION))

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
            if post and url.path == "/api/handoff":
                return self.handoff()
            if post and url.path == "/api/login":
                return self.login()
            if not self.authed():
                raise ApiError(401, "not logged in")
            if post:
                return self.send_json(200, self.post(url.path, self.body()))
            if url.path == "/api/dump":
                return self.dump(q)
            return self.send_json(200, self.get(url.path, q))
        except ApiError as e:
            self.send_json(e.code, {"error": e.msg})
        except (ValueError, KeyError, TypeError) as e:
            self.send_json(400, {"error": f"bad request: {e}"})
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def handoff(self):
        # The launcher trades the token (sent in a header, never on a command line) for a
        # one-time code that is safe to put in the browser URL.
        if not (TOKEN and hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN)):
            raise ApiError(401, "bad token")
        code = secrets.token_hex(32)
        with CODES_LOCK:
            now = time.time()
            for c in [c for c, t in CODES.items() if t < now]:
                del CODES[c]
            CODES[code] = now + 60
        self.send_json(200, {"code": code})

    def login(self):
        b = self.body()
        tok, code = str(b.get("token", "")), str(b.get("code", ""))
        with CODES_LOCK:
            ok = bool(code) and CODES.pop(code, 0) > time.time()
        if ok or (TOKEN and tok and hmac.compare_digest(tok, TOKEN)):
            return self.send_json(200, {"ok": True}, [("Set-Cookie",
                f"rc_session={SESSION}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")])
        raise ApiError(401, "bad token")

    def static(self, name, ctype):
        data = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(data)

    def get(self, path, q):
        if path == "/api/status":
            return status()
        if path == "/api/config":
            cfg = load_cfg()
            return {"settings": public_settings(cfg["settings"]), "destinations": cfg["destinations"],
                    "jobs": {k: {**v, "excludes": read_excludes(v)} for k, v in cfg["jobs"].items()}, "host": HOSTNAME}
        if path == "/api/snapshots":
            return snapshots(q.get("dest"), q.get("refresh") == "1")
        if path == "/api/ls":
            return ls(q.get("dest"), q.get("snapshot"), q.get("path", "/"))
        if path == "/api/runs":
            return runs(q.get("job"), int(q.get("days", 60)))
        if path == "/api/runlog":
            return run_log(q.get("job"), q.get("id"), q.get("cursor"))
        if path == "/api/calendar":
            return calendar_preview(q.get("expr", ""), int(q.get("n", 5)))
        if path == "/api/destination/password":
            d = get_dest(q.get("id"))
            return {"password": Path(d["password_file"]).read_text().strip(), "file": d["password_file"]}
        if path == "/api/pathinfo":
            p = check_abs(q.get("path", ""))
            return {"exists": os.path.exists(p), "dir": os.path.isdir(p), "mount": mount_of(p)}
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
            jid = b.get("id")
            job = get_job(jid)
            if job_running(jid, job):
                raise ApiError(409, "Already running")
            if DEV:
                raise ApiError(400, "dev mode: not starting systemd units")
            rc, _, err = sh(["systemctl", "start", "--no-block", f"restic-backup@{jid}.service"])
            if rc:
                raise ApiError(500, err.strip())
            time.sleep(1)
            _runs_cache.clear()
            return {"ok": True}
        if path == "/api/job/stop":
            jid = b.get("id")
            for u in job_units(jid):
                systemctl("stop", "--no-block", u)
            return {"ok": True}
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
            if t.proc and t.status == "running":
                t.proc.terminate()
            return t.summary()
        raise ApiError(404, "not found")

    def dump(self, q):
        did, sid, path = q.get("dest"), check_snap(q.get("snapshot")), check_abs(q.get("path", ""))
        dest = need_dest(did)
        is_dir = q.get("dir") == "1"
        args = [RESTIC, "dump", "--no-lock", sid, path] + (["--archive", "zip"] if is_dir else [])
        name = (os.path.basename(path) or "root") + (".zip" if is_dir else "")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=restic_env(dest))
        first = p.stdout.read(65536)
        if not first and p.wait() != 0:
            raise ApiError(502, "restic dump failed: " + p.stderr.read().decode(errors="replace")[-400:])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        try:
            self.wfile.write(first)
            while chunk := p.stdout.read(1 << 20):
                self.wfile.write(chunk)
        except BrokenPipeError:
            p.kill()
        finally:
            p.wait()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(f"No token in {TOKEN_FILE}; run install.sh")
    print(f"Restic Console on http://{BIND}:{PORT} (config {CONFIG}{', DEV MODE' if DEV else ''})", flush=True)
    Server((BIND, PORT), Handler).serve_forever()
