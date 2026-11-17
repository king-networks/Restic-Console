"""Linux: systemd timers run the jobs, notify-send shows desktop alerts, /proc tells us about mounts."""
import os
import pwd
import re
import shutil
import time
from pathlib import Path

import common

NAME = "linux"
UNIT_DIR = Path(os.environ.get("RC_UNIT_DIR", "/etc/systemd/system"))
NETFS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "fuse.rclone", "9p", "glusterfs", "ceph"}
FEATURES = {"one_file_system": True, "vss": False, "custom_calendar": True, "chown": True, "share_login": False}


def systemctl(*args):
    if common.DEV:
        return 0, "", ""
    return common.sh(["systemctl", *args], timeout=60)


def systemctl_show(unit, props):
    _, out, _ = common.sh(["systemctl", "show", unit, "-p", ",".join(props)])
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    return d


def _usec(v):
    try:
        n = int(v)
        return n / 1e6 if n > 0 else None
    except (TypeError, ValueError):
        return None


def _ts_to_epoch(s):
    if not s or s == "n/a":
        return None
    rc, out, _ = common.sh(["date", "-d", s, "+%s"])
    return int(out) if rc == 0 and out.strip().isdigit() else None


# ------------------------------------------------------------------ schedules
def timer_unit(jid):
    return f"restic-backup@{jid}.timer"


def dropin_path(jid):
    return UNIT_DIR / f"restic-backup@{jid}.timer.d" / "schedule.conf"


def calendar_preview(expr, n=5):
    if not isinstance(expr, str) or not expr.strip() or "\n" in expr or len(expr) > 200:
        raise ValueError("Invalid calendar expression")
    rc, out, err = common.sh(["systemd-analyze", "calendar", f"--iterations={n}", expr])
    if rc == 127:      # no systemd-analyze (containers, CI): fall back to the portable subset
        import schedule
        return schedule.preview(expr, n)
    if rc != 0:
        t = (err or out).strip()
        raise ValueError(t.splitlines()[-1] if t else "invalid expression")
    norm = re.search(r"Normalized form:\s*(.+)", out)
    nexts = re.findall(r"(?:Next elapse|Iter(?:ation|\.) #\d+):\s*(.+)", out)
    return {"normalized": norm.group(1).strip() if norm else expr,
            "next": [{"text": t.strip(), "epoch": _ts_to_epoch(t.strip())} for t in nexts]}


def install_schedule(jid, job):
    s = job["schedule"]
    text = ("# Managed by Restic Console. Edit the job in the app instead.\n[Timer]\nOnCalendar=\n"
            + "".join(f"OnCalendar={c}\n" for c in s["calendars"])
            + f"RandomizedDelaySec={int(s.get('randomized_delay_min', 0))}min\n"
            + f"Persistent={'true' if s.get('persistent', True) else 'false'}\n")
    common.atomic_write(dropin_path(jid), text)
    systemctl("daemon-reload")
    if job.get("enabled"):
        systemctl("enable", timer_unit(jid))
        systemctl("restart", timer_unit(jid))
    else:
        systemctl("disable", "--now", timer_unit(jid))


def remove_schedule(jid):
    systemctl("disable", "--now", timer_unit(jid))
    shutil.rmtree(dropin_path(jid).parent, ignore_errors=True)
    systemctl("daemon-reload")


def next_run(jid, job):
    if not job.get("enabled"):
        return None
    if not common.DEV:
        t = systemctl_show(timer_unit(jid), ["NextElapseUSecRealtime"])
        n = _usec(t.get("NextElapseUSecRealtime")) or _ts_to_epoch(t.get("NextElapseUSecRealtime"))
        if n:
            return n
    best = None
    for c in job["schedule"]["calendars"]:
        try:
            e = calendar_preview(c, 1)["next"][0]["epoch"]
            best = e if best is None or (e and e < best) else best
        except (ValueError, IndexError):
            pass
    return best


def start_job(jid):
    if common.DEV:
        raise RuntimeError("dev mode: not starting systemd units")
    rc, _, err = common.sh(["systemctl", "start", "--no-block", f"restic-backup@{jid}.service"])
    if rc:
        raise RuntimeError(err.strip() or f"systemctl failed ({rc})")


def stop_job(jid, rec):
    """systemd sends SIGTERM to the runner and restic; the runner closes the run record itself."""
    systemctl("stop", "--no-block", f"restic-backup@{jid}.service")
    return False


# ------------------------------------------------------------------ storage
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
            return any(line.split()[1].replace("\\040", " ") == mp for line in f)
    except OSError:
        return False


def network_mount(repo):
    """The mountpoint to verify before each run, if the repo is on network storage."""
    mp, fs = mount_of(repo)
    return mp if fs in NETFS or fs == "autofs" else ""


def ensure_mount(mp):
    if is_mounted(mp):
        return True
    common.sh(["timeout", "10", "ls", mp], timeout=12)     # poke an automount
    time.sleep(1)
    return is_mounted(mp)


def probe_path(path, timeout=10):
    """0 = exists, 1 = missing, 124 = storage not responding."""
    rc, _, _ = common.sh(["timeout", str(timeout), "stat", "-t", path], timeout=timeout + 2)
    return rc if rc in (0, 124) else 1


def disk_usage(path):
    rc, out, _ = common.sh(["timeout", "10", "df", "-B1", "--output=size,used,avail", path], timeout=12)
    if rc == 0:
        try:
            return dict(zip(["size", "used", "avail"], map(int, out.split()[-3:])))
        except ValueError:
            pass
    return None


def prepare_dest(dest):
    if dest.get("mount") and not ensure_mount(dest["mount"]):
        return f"{dest['mount']} (destination storage) is not mounted"
    return ""


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


# ------------------------------------------------------------------ users
def user_exists(name):
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def config_saved(path, cfg):
    """config.json is readable by the restic-console group (the launcher does not need it, the UI user may)."""
    import grp
    try:
        os.chown(path, 0, grp.getgrnam("restic-console").gr_gid)
    except (KeyError, PermissionError):
        pass


def as_user_cmd(user, cmd):
    """Wrap cmd so it runs with the desktop user's privileges (restores are written this way)."""
    if common.DEV or not user or os.geteuid() != 0:
        return cmd
    return ["runuser", "-u", user, "--", *cmd]


def restore_writer(user, target, path, is_dir):
    """Command that reads `restic dump` output on stdin and writes it under target, as the desktop user."""
    if is_dir:
        cmd = ["tar", "-x", "--no-same-owner", "-C", target]
    else:
        # cat reads the pipe it inherits; re-opening /dev/stdin would fail because root owns the pipe.
        cmd = ["sh", "-c", 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"', "sh", os.path.join(target, path.lstrip("/"))]
    return as_user_cmd(user, cmd)


def desktop_notify(user, title, body, level):
    icons = {"failure": "dialog-error", "warning": "dialog-warning", "success": "emblem-default", "test": "dialog-information"}
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return False, f"no such user {user}"
    bus = f"/run/user/{uid}/bus"
    if not os.path.exists(bus):
        return False, f"{user} is not logged in to a desktop session"
    cmd = ["notify-send", "-u", "critical" if level == "failure" else "normal", "-a", "Restic Console",
           "-i", icons.get(level, "dialog-information"), title, body]
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", user, "--", "env", f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}", *cmd]
    rc, _, err = common.sh(cmd, timeout=15)
    return rc == 0, err.strip() or "sent"
