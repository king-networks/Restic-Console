"""Shared paths, config and helpers for the Restic Console server and job runner.

Everything here works on Linux and Windows. OS-specific work (scheduling, notifications,
mounts) lives in plat_linux.py / plat_windows.py, imported below as `plat`.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

__version__ = "0.2.0"

IS_WIN = os.name == "nt"
E = os.environ.get
DEV = E("RC_DEV") == "1"

if IS_WIN:
    _data = Path(E("ProgramData", r"C:\ProgramData")) / "ResticConsole"
    DEFAULT_ETC, DEFAULT_STATE = _data, _data / "state"
else:
    DEFAULT_ETC, DEFAULT_STATE = Path("/etc/restic-console"), Path("/var/lib/restic-console")

ETC = Path(E("RC_ETC", str(DEFAULT_ETC)))
CONFIG = Path(E("RC_CONFIG", str(ETC / "config.json")))
STATE = Path(E("RC_STATE", str(DEFAULT_STATE)))
TOKEN_FILE = ETC / "token"
KEYS = ETC / "keys"
EXCLUDES_DIR = ETC / "excludes"
SCRIPT_DIR = Path(__file__).resolve().parent

if IS_WIN:
    import plat_windows as plat  # noqa: E402,F401
else:
    import plat_linux as plat  # noqa: E402,F401


# ------------------------------------------------------------------ processes
NO_WINDOW = 0x08000000 if IS_WIN else 0     # CREATE_NO_WINDOW: no console flashes from the service


def sh(cmd, timeout=30, env=None, input=None):
    """Run a command; return (rc, stdout, stderr). Never raises for a missing binary or timeout."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, input=input, creationflags=NO_WINDOW)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except (FileNotFoundError, PermissionError) as e:
        return 127, "", str(e)


def with_timeout(fn, timeout, default=None):
    """Call fn() in a thread; give up after `timeout` seconds (for stat()s on dead network shares)."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else default


# ------------------------------------------------------------------ files
def atomic_write(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if not IS_WIN:
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return
    for i in range(40):     # Windows refuses to replace a file another process has open (e.g. being read)
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 39:
                raise
            time.sleep(0.05)


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


# ------------------------------------------------------------------ paths
_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)")


def is_local_path(p):
    """An absolute path on this machine (as opposed to a restic backend like sftp:host:/x)."""
    if not isinstance(p, str):
        return False
    return bool(_WIN_ABS.match(p)) if IS_WIN else p.startswith("/")


def check_local(p, what="path"):
    """Validate and normalise an absolute local filesystem path."""
    if not is_local_path(p) or "\0" in p or "\n" in p or len(p) > 4096:
        raise ValueError(f"bad {what}: {p!r}" + (" (use a full path like C:\\Users\\you or \\\\server\\share)" if IS_WIN else ""))
    return os.path.normpath(p)


def check_snapshot_path(p, what="path"):
    """A path inside a snapshot. restic always uses /-style paths there (/C/Users/... on Windows)."""
    if not isinstance(p, str) or not p.startswith("/") or "\0" in p or "\n" in p or len(p) > 4096:
        raise ValueError(f"bad {what}: {p!r}")
    p = re.sub(r"/+", "/", p)
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


# ------------------------------------------------------------------ config
CFG_LOCK = threading.RLock()
DEFAULT_SETTINGS = {"desktop_user": "", "restore_roots": [], "restic": "",
                    "notifications": {"desktop": {"enabled": True, "user": ""},
                                      "email": {"enabled": False, "host": "", "port": 587, "security": "starttls",
                                                "username": "", "from": "", "to": [], "password_file": ""},
                                      "webhook": {"enabled": False, "url": ""}}}


def load_cfg():
    with CFG_LOCK:
        try:
            cfg = json.loads(read_text(CONFIG))
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
        if not DEV:
            plat.config_saved(CONFIG, cfg)


def restic_bin(cfg=None):
    """restic binary: RC_RESTIC env, then settings.restic (written by the installer), then PATH."""
    if E("RC_RESTIC"):
        return E("RC_RESTIC")
    s = ((cfg or load_cfg()).get("settings") or {}).get("restic")
    if s and os.path.exists(s):
        return s
    return shutil.which("restic") or ("restic.exe" if IS_WIN else "/usr/bin/restic")


def restic_env(dest, fps="0.2"):
    """Environment for running restic against a destination."""
    env = dict(os.environ, RESTIC_REPOSITORY=dest["repo"], RESTIC_PASSWORD_FILE=dest["password_file"],
               RESTIC_PROGRESS_FPS=fps)
    if not IS_WIN:
        env.setdefault("HOME", "/root")
    return env


def prepare_dest(dest):
    """Make the destination reachable (e.g. log in to an SMB share on Windows). Returns an error or ''."""
    return plat.prepare_dest(dest)
