#!/usr/bin/env python3
"""Run one Restic Console backup job.

    runner.py <job-id>               run the job (systemd restic-backup@<job>.service, or the
                                     Windows scheduled task \\ResticConsole\\Job-<job>)
    runner.py --on-failure <job-id>  alert for a run that died without reporting
                                     (Linux: restic-backup-failed@<job>.service, via OnFailure=)

Reads config.json, backs up the job's sources to its destination, applies retention,
optionally checks the repository, and sends notifications. Every output line goes to the
run's log file (see runs.py) and to stdout (the systemd journal on Linux).
restic exit 3 (snapshot saved, some files unreadable) counts as a warning.
"""
import os
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # python -I drops the script dir
import common  # noqa: E402
import notify  # noqa: E402
import runs  # noqa: E402

LOCK_WAIT = "30m"          # wait for another job's exclusive lock (e.g. a prune) instead of failing


class Stopped(Exception):
    pass


def alert(cfg, job_id, job, level, detail, log=print):
    if not notify.wanted(job, level):
        return
    title = {"failure": "Backup failed", "warning": "Backup finished with warnings",
             "success": "Backup succeeded"}[level] + f": {job.get('name', job_id)}"
    body = f"Job '{job_id}' on {socket.gethostname()}.\n{detail}\n\nDetails: Restic Console → Run history."
    for ch, ok, d in notify.send(cfg.get("settings"), title, body, level):
        log(f"notify {ch}: {'ok' if ok else 'FAILED'} ({d})")


def reap(cfg, rec):
    """Close a run whose process died, and alert once. Used by the server and by --on-failure."""
    job = cfg["jobs"].get(rec["job"], {"name": rec["job"]})
    rec = runs.finish_external(rec, "failed", "ERROR: the backup process ended without finishing "
                                              "(killed, crashed, or the computer shut down).")
    if not rec.get("alerted"):
        rec["alerted"] = True
        runs.write(rec)
        alert(cfg, rec["job"], job, "failure", "The backup process stopped unexpectedly (killed, crashed or shut down).",
              log=lambda m: None)
    return rec


def on_failure(cfg, job_id):
    """systemd OnFailure=: the unit failed. Alert only if the runner did not finish its record."""
    time.sleep(1)
    runs.list_runs(job_id, days=2, reap=lambda r: reap(cfg, r), grace=0)


def run_job(cfg, job_id, job):
    dest = cfg["destinations"][job["destination"]]
    host = socket.gethostname()
    env = common.restic_env(dest, fps="0.05")
    restic = common.restic_bin(cfg)

    live = runs.running(job_id)
    if live:
        print(f"job {job_id} is already running (pid {live.get('pid')}); not starting another run")
        return 0
    runs.prune(job_id)
    run = runs.RunLog(job_id)
    child = {"p": None}

    def log(msg):
        run.line(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}")

    def on_term(signum, frame):
        raise Stopped()

    if not common.IS_WIN:
        signal.signal(signal.SIGTERM, on_term)

    def restic_cmd(args):
        log("+ restic " + " ".join(args))
        p = subprocess.Popen([restic, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=common.NO_WINDOW)
        child["p"] = p
        run.set_child(p.pid)
        for line in p.stdout:
            run.line(line)
        rc = p.wait()
        child["p"] = None
        return rc

    def fail(rc, why):
        log(f"ERROR: {why}")
        alert(cfg, job_id, job, "failure", why, log)
        run.finish("failed", alerted=True)
        return rc or 1

    try:
        log(f"=== restic job {job_id} START ({job.get('name', job_id)} -> {dest.get('name', job['destination'])}, host={host}) ===")
        err = common.prepare_dest(dest)
        if err:
            return fail(1, err)
        missing = [s for s in job["sources"] if not os.path.exists(s)]
        if missing:
            return fail(1, f"source path(s) missing: {', '.join(missing)}")

        warned = None
        a = ["backup", *job["sources"], "--tag", job["tag"], "--host", host, "--retry-lock", LOCK_WAIT]
        if job.get("one_file_system") and not common.IS_WIN:
            a.append("--one-file-system")
        if job.get("vss", True) and common.IS_WIN:
            a.append("--use-fs-snapshot")
        if job.get("exclude_caches", True):
            a.append("--exclude-caches")
        ex = job.get("excludes_file")
        if ex and os.path.exists(ex):
            a.append(f"--exclude-file={ex}")
        log("Running backup...")
        rc = restic_cmd(a)
        if rc == 3:
            warned = "restic exited 3: some source files could not be read. The snapshot was saved without them."
            log("WARNING: " + warned)
        elif rc != 0:
            return fail(rc, f"restic backup failed (exit {rc})")

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
            rc = restic_cmd(a)
            if rc != 0:
                return fail(rc, f"restic forget failed (exit {rc})")
        else:
            log("WARNING: no retention rules set, keeping every snapshot.")

        if job.get("check"):
            pct = int(job.get("check_read_data_percent", 0) or 0)
            a = ["check", "--retry-lock", LOCK_WAIT] + ([f"--read-data-subset={pct}%"] if pct > 0 else [])
            log("Checking repository" + (f" + reading {pct}% of the data..." if pct else " structure..."))
            rc = restic_cmd(a)
            if rc != 0:
                return fail(rc, f"restic check found problems (exit {rc})")

        log(f"=== restic job {job_id} DONE ===")
        if warned:
            alert(cfg, job_id, job, "warning", warned, log)
            run.finish("warning")
        else:
            alert(cfg, job_id, job, "success", "Snapshot saved" + (", retention applied" if keep else "") +
                  (", repository checked." if job.get("check") else "."), log)
            run.finish("success")
        return 0
    except (Stopped, KeyboardInterrupt):
        if child["p"]:
            child["p"].terminate()
            try:
                child["p"].wait(30)
            except subprocess.TimeoutExpired:
                child["p"].kill()
        log("Stopped from Restic Console.")
        run.finish("stopped")
        return 0
    except Exception as e:  # noqa: BLE001
        return fail(1, f"Restic Console runner crashed: {type(e).__name__}: {e}")


def main():
    args = sys.argv[1:]
    mode = "run"
    if args and args[0] == "--on-failure":
        mode, args = "on-failure", args[1:]
    if len(args) != 1:
        sys.exit("usage: runner.py [--on-failure] <job-id>")
    job_id = args[0]
    cfg = common.load_cfg()
    job = cfg["jobs"].get(job_id)
    if not job:
        sys.exit(f"no job {job_id!r} in {common.CONFIG}")
    if mode == "on-failure":
        return on_failure(cfg, job_id)
    sys.exit(run_job(cfg, job_id, job))


if __name__ == "__main__":
    main()
