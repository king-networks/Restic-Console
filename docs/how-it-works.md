# How Restic Console works

## Pieces

```
                  ┌────────────────────────────── your machine ───────────────────────────────┐
 app window ──►   │ server.py  (root / SYSTEM, 127.0.0.1:8790)                                │
 (Chrome / Edge)  │   ├─ edits config.json, writes job schedules                              │
                  │   ├─ reads run records, browses / restores snapshots (restic)             │
                  │   └─ runs maintenance tasks (check, prune, diff, …)                       │
                  │                                                                           │
                  │ OS scheduler ──► runner.py <job>  (root / SYSTEM)                         │
                  │   Linux:   restic-backup@<job>.timer → restic-backup@<job>.service        │
                  │   Windows: Task Scheduler \ResticConsole\Job-<job>                        │
                  │     backup → forget/prune → check → alerts; writes the run record         │
                  └───────────────────────────────────────────────────────────────────────────┘
```

| File | Role |
|------|------|
| `server.py` | HTTP server and JSON API; serves `static/index.html` (the whole UI, no build step) |
| `runner.py` | runs one job: backup, retention, check, alerts |
| `runs.py` | run records: a `.log` and a `.json` summary per run, shared by server and runner |
| `schedule.py` | the portable schedule format, next-run calculation, Task Scheduler XML |
| `notify.py` | alert channels: desktop, email, webhook |
| `common.py` | paths, config, process helpers; picks the platform module |
| `plat_linux.py` / `plat_windows.py` | everything OS-specific: scheduling, mounts, notifications, restores |

Only the Python standard library is used, so there is nothing to `pip install`, and the installers just copy files.

## A backup run

1. The scheduler starts `runner.py <job>` as root or SYSTEM.
2. The runner writes a run record (`runs/<job>/<run-id>.json`, status `running`, its process ID) and starts the log.
3. It checks the destination: the mount on Linux, the share login on Windows. If storage is missing, it fails early.
4. `restic backup` runs with the job's tag and the host name, then `restic forget` applies retention (and `--prune` if
   enabled), then optionally `restic check`. Every output line goes to the run's log and to stdout (the journal on Linux).
5. The record is closed as `success`, `warning` (restic exit 3: some files unreadable) or `failed`, and alerts are
   sent according to the job's settings.

If the runner is killed before it closes the record, the server notices that the process is gone, marks the run
failed and sends the failure alert. On Linux, systemd's `OnFailure=` handler does the same thing straight away.
Each run is alerted at most once.

**Stopping a run** from the UI: on Linux, systemd sends SIGTERM and the runner stops restic and records `stopped`.
On Windows, the server ends the runner and restic process tree and records `stopped`.

## Retention is per job

Snapshots are tagged with the job's tag, and `forget` only ever matches that tag **and** this host name. Two jobs can
safely share a repository, and so can two machines. The app refuses two jobs with the same tag on one destination.

## Restores

Restores always go into a **new or empty folder** inside one of the configured restore folders (default: the desktop
user's home), and keep the file's full original path inside it. Nothing on the live system is overwritten.

- **Linux:** root runs `restic dump`, and a process running **as the desktop user** (`runuser … tar -x`) writes the
  files. Root never writes into a folder the user controls, so a symlink swapped in during the restore cannot
  redirect it to a system location. Restored files belong to the desktop user.
- **Windows:** `restic restore --include` writes the files as SYSTEM, and they inherit the permissions of the folder
  they are restored into.

Downloads (a file, or a folder as a zip) stream straight from `restic dump` to the browser.

## Security model

- The service runs as **root / SYSTEM** because restic must read everything it backs up, and because the service
  manages scheduled tasks.
- It listens on **127.0.0.1 only**. Requests whose `Host` header is not `127.0.0.1:8790` or `localhost:8790` are
  rejected (DNS rebinding), and so are POSTs from a foreign `Origin` or that aren't JSON (CSRF).
- **Sign-in:** the launcher reads the token file and trades it, in a request header, for a one-time code valid for
  60 seconds. Only that code goes in the browser URL; the page exchanges it for an `HttpOnly`, `SameSite=Strict`
  session cookie. Sessions live in memory for up to 30 days and end on *Sign out* or when the service restarts.
  You can also sign in by pasting the token.
- **Whoever can read the token is an administrator of your backups.** Through the app they can back up any file,
  download it, read repository passwords and restore files. On Linux the token is readable only by root and the
  `restic-console` group; on Windows, by SYSTEM, Administrators and the desktop user.
- **Secrets** (repository passwords, share passwords, SMTP password) are files in `keys/`, readable only by
  root / SYSTEM / Administrators, never in `config.json`. Copy the repository passwords to a password manager:
  without them, nobody can restore the backups.
- The UI is a single static page with a strict Content-Security-Policy and no third-party code.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).
