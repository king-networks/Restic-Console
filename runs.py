"""Run records: one log file and one JSON summary per backup run.

    <STATE>/runs/<job>/<run-id>.log    "<epoch>\\t<line>" per output line
    <STATE>/runs/<job>/<run-id>.json   {"id", "job", "status", "start", "end", "pid", ...}

The runner writes them; the server reads them. A run whose status is still "running"
but whose process is gone is "reaped": marked failed, and an alert is sent once.
"""
import json
import os
import re
import secrets
import time

import common

RUN_ID_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{6}$")
KEEP_DAYS = 120
KEEP_MAX = 2000

SUMMARY_RES = {
    "snapshot": re.compile(r"snapshot ([0-9a-f]{8}) saved"),
    "added": re.compile(r"Added to the repository: ([\d.]+ \w+)(?: \(([\d.]+ \w+) stored\))?"),
    "processed": re.compile(r"processed (\d+) files, ([\d.]+ \w+) in ([\d:]+)"),
    "files": re.compile(r"Files:\s+(\d+) new,\s+(\d+) changed,\s+(\d+) unmodified"),
}


def job_dir(jid):
    return common.STATE / "runs" / jid


def _paths(jid, rid):
    if not RUN_ID_RE.fullmatch(rid or ""):
        raise ValueError("bad run id")
    d = job_dir(jid)
    return d / f"{rid}.log", d / f"{rid}.json"


def read(jid, rid):
    try:
        return json.loads(common.read_text(_paths(jid, rid)[1]))
    except (FileNotFoundError, ValueError):
        return None


def write(rec):
    common.atomic_write(_paths(rec["job"], rec["id"])[1], json.dumps(rec, indent=1) + "\n", 0o644)


def pid_alive(pid):
    if not pid:
        return False
    if common.IS_WIN:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:    # guard against pid reuse after a reboot
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return b"python" in f.read().split(b"\0")[0]
    except OSError:
        return True


# ------------------------------------------------------------------ writing (runner side)
class RunLog:
    """Context for one run: appends log lines, tracks the summary, finishes the record."""

    def __init__(self, jid, echo=True):
        os.makedirs(job_dir(jid), exist_ok=True)
        self.id = time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        self.log_path, _ = _paths(jid, self.id)
        self.echo = echo
        self.rec = {"id": self.id, "job": jid, "status": "running", "start": time.time(), "end": None,
                    "pid": os.getpid(), "child_pid": None, "warnings": 0, "errors": 0, "alerted": False}
        self.f = open(self.log_path, "a", encoding="utf-8", newline="\n")
        write(self.rec)

    def line(self, msg):
        msg = msg.rstrip("\r\n")
        now = time.time()
        self.f.write(f"{now:.3f}\t{msg}\n")
        self.f.flush()
        if self.echo:
            try:
                print(msg, flush=True)
            except (OSError, ValueError):
                pass
        for k, rx in SUMMARY_RES.items():
            r = rx.search(msg)
            if r:
                self.rec[k] = r.groups() if len(r.groups()) > 1 else r.group(1)
        if "WARNING" in msg:
            self.rec["warnings"] += 1
        if "ERROR" in msg or "Fatal:" in msg:
            self.rec["errors"] += 1
            self.rec["error_msg"] = msg[-300:]

    def set_child(self, pid):
        self.rec["child_pid"] = pid
        write(self.rec)

    def finish(self, status, alerted=False):
        self.rec.update(status=status, end=time.time(), child_pid=None, alerted=alerted or self.rec["alerted"])
        write(self.rec)
        self.f.close()


def finish_external(rec, status, note):
    """Close a run from outside the runner (stopped from the UI, or found dead)."""
    path, _ = _paths(rec["job"], rec["id"])
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{time.time():.3f}\t{note}\n")
    rec.update(status=status, end=time.time(), child_pid=None)
    if status == "failed":
        rec["error_msg"] = note
        rec["errors"] = rec.get("errors", 0) + 1
    write(rec)
    return rec


def prune(jid):
    """Drop run records older than KEEP_DAYS, or beyond the newest KEEP_MAX."""
    d = job_dir(jid)
    try:
        ids = sorted({p.stem for p in d.iterdir() if RUN_ID_RE.fullmatch(p.stem)})
    except FileNotFoundError:
        return
    cutoff = time.strftime("%Y%m%dT%H%M%S", time.localtime(time.time() - KEEP_DAYS * 86400))
    drop = [r for r in ids if r < cutoff] + ids[:-KEEP_MAX]
    for rid in set(drop):
        for ext in (".log", ".json"):
            try:
                (d / (rid + ext)).unlink()
            except OSError:
                pass


# ------------------------------------------------------------------ reading (server side)
def list_runs(jid, days=60, reap=None, grace=120):
    """Newest first. `reap(rec)` is called for runs whose process died without finishing, once
    their log has been quiet for `grace` seconds (on Linux, systemd's OnFailure= handler gets
    the first chance, so the alert is not sent twice). Until then such a run shows as "unknown"."""
    d = job_dir(jid)
    cutoff = time.strftime("%Y%m%dT%H%M%S", time.localtime(time.time() - days * 86400))
    try:
        ids = sorted((p.stem for p in d.glob("*.json") if RUN_ID_RE.fullmatch(p.stem) and p.stem >= cutoff), reverse=True)
    except FileNotFoundError:
        return []
    out = []
    for rid in ids:
        rec = read(jid, rid)
        if not rec:
            continue
        if rec.get("status") == "running" and not pid_alive(rec.get("pid")):
            try:
                quiet = time.time() - os.path.getmtime(_paths(jid, rid)[0])
            except OSError:
                quiet = grace
            if quiet < grace:
                rec = dict(rec, status="unknown")
            elif reap:
                rec = reap(rec)
            else:
                rec = finish_external(rec, "failed", "ERROR: the backup process ended without finishing.")
        if rec.get("status") == "running" and rec.get("end") is None:
            rec = dict(rec, end=time.time())
        out.append(rec)
    return out


def running(jid):
    """The run record of the live run of this job, or None."""
    d = job_dir(jid)
    try:
        ids = sorted((p.stem for p in d.glob("*.json") if RUN_ID_RE.fullmatch(p.stem)), reverse=True)[:5]
    except FileNotFoundError:
        return None
    for rid in ids:
        rec = read(jid, rid)
        if rec and rec.get("status") == "running" and pid_alive(rec.get("pid")):
            return rec
    return None


def read_log(jid, rid, cursor=None, max_lines=8000):
    """Log lines from byte offset `cursor`; returns {"lines": [{"t", "m"}], "cursor": new offset}."""
    path, _ = _paths(jid, rid)
    try:
        off = int(cursor or 0)
    except ValueError:
        off = 0
    lines = []
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except FileNotFoundError:
        return {"lines": [], "cursor": str(off)}
    end = data.rfind(b"\n") + 1          # only hand out complete lines
    for raw in data[:end].decode("utf-8", "replace").splitlines():
        t, _, m = raw.partition("\t")
        try:
            lines.append({"t": float(t), "m": m})
        except ValueError:
            lines.append({"t": None, "m": raw})
    return {"lines": lines[-max_lines:], "cursor": str(off + end)}
