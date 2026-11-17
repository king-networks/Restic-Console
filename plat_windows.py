"""Windows: Task Scheduler runs the jobs (as SYSTEM), toasts are shown through a per-user task,
SMB shares are logged in to with WNetAddConnection2. Standard library only (ctypes)."""
import base64
import ctypes
import os
import re
import shutil
import sys
import tempfile

import common
import schedule

NAME = "windows"
FOLDER = "\\ResticConsole\\"
FEATURES = {"one_file_system": False, "vss": True, "custom_calendar": False, "chown": False, "share_login": True}
DRIVE_REMOTE, DRIVE_NO_ROOT = 4, 1


def _k32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def schtasks(*args, timeout=60):
    if common.DEV:
        return 0, "", ""
    return common.sh(["schtasks.exe", *args], timeout=timeout)


def task_name(jid):
    return f"{FOLDER}Job-{jid}"


def python_exe():
    """pythonw.exe next to the running interpreter if present (no console window), else python.exe."""
    exe = os.path.realpath(sys.executable)
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return w if os.path.exists(w) else exe


def register_task(name, xml):
    """schtasks /create /xml wants a UTF-16 file."""
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        rc, out, err = schtasks("/create", "/tn", name, "/xml", path, "/f")
        if rc:
            raise RuntimeError(f"schtasks /create failed: {(err or out).strip()}")
    finally:
        os.unlink(path)


# ------------------------------------------------------------------ schedules
def calendar_preview(expr, n=5):
    if not isinstance(expr, str) or len(expr) > 200:
        raise ValueError("Invalid schedule")
    return schedule.preview(expr, n)


def install_schedule(jid, job):
    s = job["schedule"]
    triggers = [schedule.win_trigger(schedule.parse(c), s.get("randomized_delay_min", 0)) for c in s["calendars"]]
    xml = schedule.win_task_xml(f"Restic Console backup job '{job.get('name', jid)}'", triggers,
                                python_exe(), f'-I "{common.SCRIPT_DIR / "runner.py"}" {jid}', str(common.SCRIPT_DIR),
                                enabled=bool(job.get("enabled")), catch_up=bool(s.get("persistent", True)))
    register_task(task_name(jid), xml)


def remove_schedule(jid):
    schtasks("/delete", "/tn", task_name(jid), "/f")


def next_run(jid, job):
    if not job.get("enabled"):
        return None
    best = None
    for c in job["schedule"]["calendars"]:
        s = schedule.parse(c)
        if s:
            t = schedule.next_times(s, 1)
            if t and (best is None or t[0].timestamp() < best):
                best = t[0].timestamp()
    return best


def start_job(jid):
    if common.DEV:
        raise RuntimeError("dev mode: not starting scheduled tasks")
    rc, out, err = schtasks("/run", "/tn", task_name(jid))
    if rc:
        raise RuntimeError((err or out).strip() or f"schtasks failed ({rc})")


def stop_job(jid, rec):
    """Kill the runner and its restic child. Returns True: the caller must close the run record."""
    for pid in (rec.get("pid"), rec.get("child_pid")):
        if pid:
            common.sh(["taskkill.exe", "/PID", str(int(pid)), "/T", "/F"], timeout=30)
    return True


# ------------------------------------------------------------------ storage
def _drive_type(root):
    k = _k32()
    k.GetDriveTypeW.argtypes = (ctypes.c_wchar_p,)
    return k.GetDriveTypeW(root)


def mount_of(path):
    """(volume, kind) where kind is "network" or "local"."""
    drive, _ = os.path.splitdrive(path)
    if not drive:
        return ("", "")
    if drive.startswith("\\\\"):
        return (drive, "network")
    root = drive + "\\"
    t = _drive_type(root)
    return (root, "network" if t == DRIVE_REMOTE else "no-drive" if t == DRIVE_NO_ROOT else "local")


def network_mount(repo):
    return ""     # UNC paths need no mount; reachability is checked with a timeout instead


def probe_path(path, timeout=10):
    """0 = exists, 1 = missing, 124 = storage not responding."""
    r = common.with_timeout(lambda: os.path.exists(path), timeout, default=None)
    return 124 if r is None else (0 if r else 1)


def disk_usage(path):
    def du():
        try:
            u = shutil.disk_usage(path)
            return {"size": u.total, "used": u.used, "avail": u.free}
        except OSError:
            return None
    return common.with_timeout(du, 10)


def missing_drive_hint(path):
    root, kind = mount_of(path)
    if kind == "no-drive":
        return (f"Drive {root} does not exist for the Restic Console service. Mapped network drives belong to "
                "your login only: use the share's UNC path instead, like \\\\server\\share\\restic.")
    return ""


class _NETRESOURCE(ctypes.Structure):
    _fields_ = [("dwScope", ctypes.c_uint32), ("dwType", ctypes.c_uint32), ("dwDisplayType", ctypes.c_uint32),
                ("dwUsage", ctypes.c_uint32), ("lpLocalName", ctypes.c_wchar_p), ("lpRemoteName", ctypes.c_wchar_p),
                ("lpComment", ctypes.c_wchar_p), ("lpProvider", ctypes.c_wchar_p)]


def share_root(path):
    m = re.match(r"^(\\\\[^\\/]+[\\/][^\\/]+)", path or "")
    return m.group(1).replace("/", "\\") if m else ""


def share_login(unc, user, password):
    """Connect this logon session (SYSTEM) to \\\\server\\share with a username and password."""
    nr = _NETRESOURCE(dwType=1, lpRemoteName=unc)       # RESOURCETYPE_DISK
    mpr = ctypes.WinDLL("mpr")
    mpr.WNetAddConnection2W.argtypes = (ctypes.POINTER(_NETRESOURCE), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    rc = mpr.WNetAddConnection2W(ctypes.byref(nr), password, user, 0)
    if rc in (0, 1219):       # 1219: already connected (with these or other credentials)
        return ""
    return f"could not log in to {unc}: {ctypes.FormatError(rc).strip()} (error {rc})"


def prepare_dest(dest):
    repo = dest.get("repo", "")
    unc = share_root(repo)
    if unc and dest.get("share_user") and dest.get("share_password_file"):
        try:
            pw = common.read_text(dest["share_password_file"]).strip()
        except OSError as e:
            return f"cannot read the share password: {e}"
        if probe_path(repo, 5) != 0:
            return share_login(unc, dest["share_user"], pw)
    return missing_drive_hint(repo) if common.is_local_path(repo) and probe_path(repo, 5) else ""


def raid_status():
    return []


# ------------------------------------------------------------------ users
def user_exists(name):
    return bool(re.fullmatch(r"[A-Za-z0-9._ -]{1,64}(?:\\[A-Za-z0-9._ $-]{1,64})?", name or ""))


def config_saved(path, cfg):
    pass      # ProgramData\ResticConsole is locked down by install.ps1; files inherit its ACL


def as_user_cmd(user, cmd):
    return cmd


def restore_writer(user, target, path, is_dir):
    return None   # restores use `restic restore` directly; SYSTEM-created files inherit the folder's ACL


def desktop_notify(user, title, body, level):
    """Toasts can't be shown from SYSTEM's session, so (re)register a per-user task that shows one, then run it."""
    if not user:
        return False, "no desktop user set"
    script = common.SCRIPT_DIR / "windows" / "notify-toast.ps1"
    b64 = lambda s: base64.b64encode(s.encode("utf-8")).decode()  # noqa: E731
    args = (f'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{script}" '
            f'-Title {b64(title)} -Body {b64(body[:900])} -Level {level}')
    name = f"{FOLDER}Notify-{re.sub(r'[^A-Za-z0-9_-]', '_', user)}"
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    xml = schedule.win_task_xml("Restic Console desktop alert", [],
                                os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                                args, str(common.SCRIPT_DIR), user_id=user, interactive=True, priority=5)
    try:
        register_task(name, xml)
    except RuntimeError as e:
        return False, str(e)
    rc, out, err = schtasks("/run", "/tn", name)
    if rc:
        return False, (err or out).strip()
    return True, f"shown to {user} if they are signed in"


# ------------------------------------------------------------------ install (called by install.ps1)
SERVER_TASK = FOLDER + "Server"


def install_service():
    """Run the web server as SYSTEM at boot, restarting it if it dies."""
    xml = schedule.win_task_xml("Restic Console web UI on http://127.0.0.1:8790",
                                ["<BootTrigger><Enabled>true</Enabled></BootTrigger>"],
                                python_exe(), f'-I "{common.SCRIPT_DIR / "server.py"}"', str(common.SCRIPT_DIR),
                                restart=True, priority=6)
    register_task(SERVER_TASK, xml)
    schtasks("/end", "/tn", SERVER_TASK)
    rc, out, err = schtasks("/run", "/tn", SERVER_TASK)
    if rc:
        raise RuntimeError(f"could not start the server task: {(err or out).strip()}")
