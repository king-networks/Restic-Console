# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-17

### Added
- **Windows support** (10, 11, Server 2019+): jobs run as Task Scheduler tasks under SYSTEM, the web UI runs as a boot
  task, alerts appear as Windows notifications, `install.ps1` / `uninstall.ps1`, and a Start menu launcher that opens the
  app in an Edge or Chrome window.
- Windows: back up open files with Volume Shadow Copy (`--use-fs-snapshot`), on by default.
- Windows: optional username and password for SMB share destinations, with a clear error for mapped drive letters.
- Run history is kept by Restic Console itself (`/var/lib/restic-console` or `C:\ProgramData\ResticConsole\state`), the
  same on both systems.
- A *Sign out* link. Sessions are now random per sign-in (30 days, in memory) instead of derived from the token.
- Linux installer works across distributions: Debian/Ubuntu, Fedora, RHEL family 9, Arch and openSUSE are installed
  and smoke-tested in CI. It finds a Python 3.9+ interpreter (including `python3.11` etc.), finds restic outside sudo's
  PATH, sets SELinux labels, names the right package when something is missing, and `--get-restic` downloads the
  official restic release (SHA-256 checked) when the distribution's package is missing or too old. `curl` is no longer
  needed.
- Linux launcher falls back to Edge, Brave or the default browser when Chrome/Chromium is missing.
- Tests (unit, integration with real restic, and a real install on Ubuntu and Windows in CI), contributor docs,
  security policy, and issue templates.

### Changed
- Python 3.9 or newer is required.
- Installers moved to `linux/` and `windows/`. On Linux, run `sudo ./linux/install.sh`.
- Linux: the token and config are readable by a dedicated `restic-console` group instead of the desktop user's primary
  group. Log out and back in once after upgrading.
- The restic and Python paths are found at install time and stored, instead of assuming `/usr/bin`.

### Security
- Linux restores are now written by a process running as the desktop user (root reads the snapshot with `restic dump`).
  Before, root wrote into a folder inside the user's home, and a symlink swapped in during the restore could redirect
  the write to a system location.
- Pages are sent with `X-Frame-Options: DENY` and `frame-ancestors 'none'`.

### Upgrade notes
- Run history starts empty after upgrading on Linux. Older runs are still in `journalctl -u 'restic-backup@*'`.

## [0.1.0] - 2026-11-09

First version: Linux with systemd. Multiple jobs and destinations, schedule builder, snapshot browser with restore,
download and diff, run history from the journal, maintenance tasks, and desktop / email / webhook alerts.

[Unreleased]: https://github.com/king-networks/Restic-Console/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/king-networks/Restic-Console/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/king-networks/Restic-Console/releases/tag/v0.1.0
