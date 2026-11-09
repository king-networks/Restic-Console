# Restic Console

A small local web app for running [restic](https://restic.net) backups on a Linux desktop or server:
backup jobs on systemd timers, a snapshot browser with restore and download, run history with live logs,
and failure alerts by desktop notification, email or webhook.

No dependencies beyond **restic**, **Python 3.8+** (standard library only) and **systemd**.
It opens as its own app window in Chrome or Chromium, or in any browser at `http://127.0.0.1:8790`.

## Features

- **Multiple jobs**: each with its own folders, destination, schedule, retention, excludes and alert rules.
  For example, the whole OS nightly plus `~/Projects` every hour.
- **Multiple destinations**: local disks, NFS/SMB mounts, or any restic backend (`sftp:`, `rest:`, `s3:`, `b2:`, …).
  Create a new repository from the app, or connect an existing one.
- **Schedules** with a builder (every N minutes or hours, daily, weekly) or raw systemd calendar expressions,
  with a preview of the next run times. Catch-up after power-off and random delay are supported.
- **Snapshots**: browse any snapshot like a file manager, download files (or folders as a zip), restore into a
  new folder, compare a snapshot with the previous one, forget snapshots.
- **History**: every run read back from the systemd journal, with duration, data added and the full log, live while it runs.
- **Alerts**: desktop notification, email over SMTP, and Slack/Mattermost/Discord webhooks. Each job picks failure,
  warnings and/or success. A run that crashes or gets killed still sends a failure alert.
- **Maintenance** per destination: check (optionally re-reading a share of the data), size, prune, remove stale locks,
  retention preview.
- Network storage is checked before every run, so a dead NAS never makes restic write into the empty mountpoint.
- Jobs on the same repository wait for each other's locks (`--retry-lock`) instead of failing.
- Shows Linux software RAID (`/proc/mdstat`) health on the overview when present.

## Install

```sh
sudo apt install restic          # or your distro's package; needs restic 0.16+
git clone <this repo> restic-console && cd restic-console
sudo ./install.sh                # or: sudo ./install.sh --user alice
```

Then open **Restic Console** from the application menu (or run `restic-console`), add a destination, and create a job.
Re-run `install.sh` to upgrade; your config, jobs and keys are kept. `sudo ./uninstall.sh` removes the app
(add `--purge` to also delete `/etc/restic-console`). Backup repositories are never touched.

## How it works

```
restic-console.service    root, 127.0.0.1:8790     web UI + API (server.py)
restic-backup@<job>.timer                          one per job; schedule in a drop-in the app writes
restic-backup@<job>.service -> runner.py <job>     backup, forget/prune, check, alerts -> journal
restic-backup-failed@<job>.service                 OnFailure= alert if the runner died without reporting
```

| Path | What |
|------|------|
| `/opt/restic-console/` | program files |
| `/etc/restic-console/config.json` | destinations, jobs, settings (no secrets) |
| `/etc/restic-console/keys/` | repository and SMTP passwords, root-only |
| `/etc/restic-console/excludes/<job>.txt` | per-job exclude patterns |
| `/etc/restic-console/token` | UI sign-in token, readable by root and the desktop user's group |
| `/etc/systemd/system/restic-backup@<job>.timer.d/schedule.conf` | per-job schedule |

Snapshots are tagged with the job's tag and the host name. Retention only ever touches snapshots with that tag and host.

## Security model

- The service runs as **root** because restic needs to read everything it backs up and systemd needs root to manage timers.
  It listens on **127.0.0.1 only**.
- Every request needs a session cookie derived from `/etc/restic-console/token`. The launcher sends the token to the
  local service in a header and gets back a one-time code (valid 60 s) for the browser URL, so the token never appears
  on a command line. Requests with a foreign `Host` or `Origin` header are rejected (DNS-rebinding and CSRF protection).
- Restores only write into new or empty folders under the configured restore roots (default: the desktop user's home),
  and the restored files are given to the desktop user.
- Repository passwords created by the app live in `/etc/restic-console/keys`. **Copy them to a password manager**
  (Destinations → Show password): if the machine dies, the backups can't be restored without them.

## Development

Run the server unprivileged against scratch paths:

```sh
export RC_DEV=1 RC_PORT=8791 RC_ETC=$PWD/dev/etc RC_UNIT_DIR=$PWD/dev/units RC_CONFIG=$PWD/dev/etc/config.json
mkdir -p dev/etc/keys dev/etc/excludes && echo devtoken > dev/etc/token
python3 server.py      # then open http://127.0.0.1:8791 and paste "devtoken"
```

In dev mode nothing calls `systemctl` for writes, and "Run now" is disabled. `runner.py <job>` can be run directly
with `RC_CONFIG` and `RC_STATE` pointing at scratch paths.

## Limitations

- Linux with systemd only, one machine per install (it backs up the machine it runs on).
- No restore-in-place: restores always go to a new folder, and you copy back what you need.
- The Activity list of app-started tasks resets when the service restarts. Backup run history comes from the journal and persists.

## License

MIT. See [LICENSE](LICENSE).
