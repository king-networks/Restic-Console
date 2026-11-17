# Restic Console on Linux

Works on any distribution that boots with **systemd** and has **Python 3.9+**. restic 0.16+ comes from your
package manager, or the installer downloads the official release for you.

| Distribution | Tested in CI | Notes |
|---|---|---|
| Ubuntu 22.04+ | yes | |
| Debian 12+ | yes | Debian 12's restic is too old; use `--get-restic` |
| Fedora | yes | SELinux stays enforcing |
| Rocky / Alma / RHEL 9 | yes | no restic package; use `--get-restic` |
| RHEL family 8 | no | needs a newer Python: `sudo dnf install python3.11` |
| Arch, Manjaro, EndeavourOS | yes (Arch) | |
| openSUSE Tumbleweed / Leap | yes (Tumbleweed) | |
| Linux Mint, Pop!_OS, Zorin, elementary | via Ubuntu | |

Not supported: distributions without systemd (Alpine, Void, Devuan, Gentoo with OpenRC) and NixOS, which manages
`/etc/systemd` itself.

## Install

```sh
# 1. Python, and restic if your distribution has a recent one
sudo apt install python3 restic                  # Debian / Ubuntu / Mint
sudo dnf install python3 restic                  # Fedora
sudo pacman -S python restic                     # Arch
sudo zypper install python3 restic               # openSUSE

# 2. Restic Console
git clone https://github.com/king-networks/Restic-Console.git
cd Restic-Console
sudo ./linux/install.sh                          # desktop user = you (the user running sudo)
```

Installer options:

| Option | What it does |
|---|---|
| `--user NAME` | desktop user who gets alerts and whose home is the default restore folder |
| `--get-restic` | download the latest official restic to `/usr/local/bin/restic` and check its SHA-256 (asked automatically when restic is missing or older than 0.16) |
| `--restic PATH` | use this restic binary |
| `--python PATH` | use this Python 3.9+ interpreter |

For desktop alerts you also need `notify-send` (package `libnotify-bin` on Debian/Ubuntu, `libnotify` elsewhere); the
installer tells you if it is missing.

The installer:

- copies the program to `/opt/restic-console`,
- creates `/etc/restic-console` (config, keys, token) and `/var/lib/restic-console` (run history),
- creates the `restic-console` group and adds the desktop user to it,
- installs and starts `restic-console.service`, and the `restic-backup@.service` / `.timer` templates,
- adds **Restic Console** to the application menu, with `restic-console` as the command-line launcher.

**Log out and back in** after the first install so your session picks up the new group. Then open the app.

The launcher opens the app in its own Chrome, Chromium, Edge or Brave window. Without one of those, it opens a tab in
your default browser. You can also open <http://127.0.0.1:8790> directly and paste the token from
`/etc/restic-console/token`.

## Upgrade

```sh
cd Restic-Console && git pull
sudo ./linux/install.sh
```

Config, jobs, keys, the token and run history are kept.

> **Upgrading from 0.1:** run history now lives in `/var/lib/restic-console` instead of being read from the journal,
> so the Run history page starts empty. Older runs are still in `journalctl -u 'restic-backup@*'`. The token is now
> readable by the `restic-console` group instead of your own group, so log out and back in once.

## Uninstall

```sh
sudo ./linux/uninstall.sh            # keeps /etc/restic-console and the run history
sudo ./linux/uninstall.sh --purge    # deletes them too. Save your repository passwords first!
```

Backup repositories are never touched. A restic downloaded by `--get-restic` stays in `/usr/local/bin/restic`.

## Where things are

| Path | What |
|------|------|
| `/opt/restic-console/` | program files |
| `/etc/restic-console/config.json` | destinations, jobs, settings (no secrets) |
| `/etc/restic-console/keys/` | repository, share and SMTP passwords; root only |
| `/etc/restic-console/excludes/<job>.txt` | per-job exclude patterns |
| `/etc/restic-console/token` | sign-in token, root and the `restic-console` group |
| `/var/lib/restic-console/runs/<job>/` | run logs and summaries (kept 120 days) |
| `/etc/systemd/system/restic-backup@<job>.timer.d/schedule.conf` | per-job schedule, written by the app |

## Useful commands

```sh
systemctl status restic-console                    # the web UI service
systemctl list-timers 'restic-backup@*'            # every job's next run
sudo systemctl start restic-backup@projects        # run the "projects" job now
journalctl -u restic-backup@projects -f            # follow a job's output
journalctl -u restic-console -n 50                 # web UI errors
```

## Network storage

For a repository on an NFS or SMB mount (including autofs), the app notices the mount when you add the destination
and checks that it is mounted before every run. If it is not, the run fails with a clear alert instead of writing
into the empty mountpoint on your local disk.

For `sftp:` destinations, restic runs as root, so root needs SSH access: put the key in `/root/.ssh/` and accept the
host key once with `sudo ssh user@host`.

## Troubleshooting

- **"Cannot read /etc/restic-console/token"** when opening the app: you are not in the `restic-console` group yet.
  Log out and back in, or run `sudo usermod -aG restic-console $USER` first.
- **Desktop notifications never appear**: they reach the desktop user only while they are logged in to a graphical
  session. Use *Settings → Desktop notifications → Send test*.
- **A job says "repository is already locked"**: a run was killed mid-way. Use *Destinations → Remove stale locks*.
