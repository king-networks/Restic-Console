#!/usr/bin/env python3
"""Run one Restic Console backup job.

    runner.py <job-id>               run the job (restic-backup@<job>.service)
    runner.py --on-failure <job-id>  alert for a run that died without reporting
                                     (restic-backup-failed@<job>.service, via OnFailure=)

Reads /etc/restic-console/config.json, backs up the job's sources to its destination,
applies retention, optionally checks the repo, and sends notifications.
Output goes to the journal, where Restic Console reads it back.
restic exit 3 (snapshot saved, some files unreadable) counts as a warning.
"""
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # python -I drops the script dir
import notify  # noqa: E402

CONFIG = os.environ.get("RC_CONFIG", "/etc/restic-console/config.json")
RESTIC = os.environ.get("RC_RESTIC", "/usr/bin/restic")
STATE = os.environ.get("RC_STATE", "/run/restic-console")
LOCK_WAIT = "30m"          # wait for another job's exclusive lock (e.g. a prune) instead of failing


def log(msg):
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}", flush=True)


def mounted(path):
    with open("/proc/self/mounts") as f:
        return any(line.split()[1] == path for line in f)


def ensure_mount(path):
    if mounted(path):
        return True
    subprocess.run(["timeout", "10", "ls", path], capture_output=True)   # poke an automount
    time.sleep(2)
    return mounted(path)


def restic(args, env):
    log("+ restic " + " ".join(args))
    return subprocess.run([RESTIC, *args], env=env).returncode


def alert(cfg, job_id, job, level, detail):
    if not notify.wanted(job, level):
        return
    title = {"failure": "Backup failed", "warning": "Backup finished with warnings",
             "success": "Backup succeeded"}[level] + f": {job.get('name', job_id)}"
    body = f"Job '{job_id}' on {socket.gethostname()}.\n{detail}\n\nDetails: Restic Console → Backup runs, or journalctl -u restic-backup@{job_id}"
    for ch, ok, d in notify.send(cfg.get("settings"), title, body, level):
        log(f"notify {ch}: {'ok' if ok else 'FAILED'} ({d})")


def marker(job_id):
    return os.path.join(STATE, f"{job_id}.alerted")


def on_failure(cfg, job_id, job):
    """Called by systemd when the unit failed. Skip if the runner already sent the alert."""
    m = marker(job_id)
    if os.path.exists(m) and time.time() - os.path.getmtime(m) < 600:
        os.unlink(m)
        return
    alert(cfg, job_id, job, "failure", "The backup process stopped unexpectedly (killed, crashed or timed out).")


def main():
    args = sys.argv[1:]
    mode = "run"
    if args and args[0] == "--on-failure":
        mode, args = "on-failure", args[1:]
    if len(args) != 1:
        sys.exit("usage: runner.py [--on-failure] <job-id>")
    job_id = args[0]
    with open(CONFIG) as f:
        cfg = json.load(f)
    job = cfg["jobs"].get(job_id)
    if not job:
        sys.exit(f"no job {job_id!r} in {CONFIG}")
    if mode == "on-failure":
        return on_failure(cfg, job_id, job)

    dest = cfg["destinations"][job["destination"]]
    host = socket.gethostname()
    env = dict(os.environ, RESTIC_REPOSITORY=dest["repo"], RESTIC_PASSWORD_FILE=dest["password_file"],
               RESTIC_PROGRESS_FPS="0.05")
    env.setdefault("HOME", "/root")
    os.makedirs(STATE, exist_ok=True)

    def fail(rc, why):
        log(f"ERROR: {why}")
        alert(cfg, job_id, job, "failure", why)
        open(marker(job_id), "w").close()          # tell the OnFailure handler we already alerted
        sys.exit(rc or 1)

    log(f"=== restic job {job_id} START ({job.get('name', job_id)} -> {dest.get('name', job['destination'])}, host={host}) ===")
    if dest.get("mount") and not ensure_mount(dest["mount"]):
        fail(1, f"{dest['mount']} (destination storage) is not mounted")
    missing = [s for s in job["sources"] if not os.path.exists(s)]
    if missing:
        fail(1, f"source path(s) missing: {', '.join(missing)}")

    warned = None
    a = ["backup", *job["sources"], "--tag", job["tag"], "--host", host, "--retry-lock", LOCK_WAIT]
    if job.get("one_file_system"):
        a.append("--one-file-system")
    if job.get("exclude_caches", True):
        a.append("--exclude-caches")
    ex = job.get("excludes_file")
    if ex and os.path.exists(ex):
        a.append(f"--exclude-file={ex}")
    log("Running backup...")
    rc = restic(a, env)
    if rc == 3:
        warned = "restic exited 3: some source files could not be read. The snapshot was saved without them."
        log("WARNING: " + warned)
    elif rc != 0:
        fail(rc, f"restic backup failed (exit {rc})")

    keep = []
    for k in ("last", "hourly", "daily", "weekly", "monthly", "yearly"):
        n = int(job.get("retention", {}).get(k, 0) or 0)
        if n > 0:
            keep += [f"--keep-{k}", str(n)]
    if keep:
        a = ["forget", "--tag", job["tag"], "--host", host, *keep, "--retry-lock", LOCK_WAIT]
        if job.get("prune"):
            a.append("--prune")
        log("Applying retention policy...")
        rc = restic(a, env)
        if rc != 0:
            fail(rc, f"restic forget failed (exit {rc})")
    else:
        log("WARNING: no retention rules set, keeping every snapshot.")

    if job.get("check"):
        pct = int(job.get("check_read_data_percent", 0) or 0)
        a = ["check", "--retry-lock", LOCK_WAIT] + ([f"--read-data-subset={pct}%"] if pct > 0 else [])
        log("Checking repository" + (f" + reading {pct}% of the data..." if pct else " structure..."))
        rc = restic(a, env)
        if rc != 0:
            fail(rc, f"restic check found problems (exit {rc})")

    log(f"=== restic job {job_id} DONE ===")
    if warned:
        alert(cfg, job_id, job, "warning", warned)
    else:
        alert(cfg, job_id, job, "success", "Snapshot saved" + (", retention applied" if keep else "") +
              (", repository checked." if job.get("check") else "."))


if __name__ == "__main__":
    main()
