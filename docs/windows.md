# Restic Console on Windows

Works on **Windows 10 and 11** (and Windows Server 2019+) with **restic 0.16+** and **Python 3.9+**.

## Install

Open **PowerShell as administrator** (Start → type *PowerShell* → *Run as administrator*).

```powershell
# 1. restic and Python, installed for all users so the background service can use them
winget install restic.restic --scope machine
winget install Python.Python.3.12 --scope machine
```

Close that window and open a new administrator PowerShell, so the new programs are on `PATH`. Then download
Restic Console: either the [latest release](https://github.com/king-networks/Restic-Console/releases) zip, unzipped,
or `git clone https://github.com/king-networks/Restic-Console.git`. In that folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
```

Options:

```powershell
.\windows\install.ps1 -User "DESKTOP-1\alice"                 # desktop user, if not you
.\windows\install.ps1 -Restic "C:\Tools\restic.exe"            # restic not on PATH
.\windows\install.ps1 -Python "C:\Python312\python.exe"        # a specific Python
```

The installer:

- copies the program to `C:\Program Files\ResticConsole`,
- creates `C:\ProgramData\ResticConsole` for config, passwords and run history, readable only by SYSTEM and
  Administrators, plus read access to the sign-in token for the desktop user,
- registers the scheduled task `\ResticConsole\Server`, which runs the web UI as SYSTEM from boot and restarts it if it
  stops,
- adds **Restic Console** to the Start menu.

Open **Restic Console** from the Start menu. It opens in its own Microsoft Edge window (or Chrome).

Scoop or Chocolatey work too (`scoop install restic python`, `choco install restic python`). The installer finds restic
and Python on `PATH` and stores their full paths.

## How jobs run on Windows

Each backup job is a Task Scheduler task, `\ResticConsole\Job-<id>`, running as **SYSTEM**. You can see them in
*Task Scheduler → Task Scheduler Library → ResticConsole*, but edit them in the app: it rewrites them on every save.

- **Back up open files**: with *Back up open files (Volume Shadow Copy)* ticked (the default), restic reads files
  from a shadow copy, so open Outlook PST files, databases and the like are backed up consistently.
- **Catch up**: with *Catch up a missed run* ticked, a run missed while the PC was off starts soon after it boots.
- **On battery**: jobs run on battery power too, and are not stopped when the laptop unplugs.

## Network shares (NAS)

The service runs as SYSTEM, which **cannot see your mapped drive letters** (like `Z:`) or the passwords saved in your
Windows account. Use the share's UNC path instead:

```
\\nas\backup\restic
```

If the share needs a login, enter the share's username and password when you add the destination. They are kept in
`C:\ProgramData\ResticConsole\keys`, and the service logs in to the share before each run.

Other options that work well: a USB disk, or a `rest:` / `sftp:` repository. For `sftp:`, SYSTEM needs an SSH key in
`C:\Windows\System32\config\systemprofile\.ssh\`.

## Upgrade

Download the new version, then in an administrator PowerShell in its folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
```

Config, jobs, passwords, the token and run history are kept. The job tasks are re-registered with the new paths.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1          # keeps C:\ProgramData\ResticConsole
powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1 -Purge   # deletes it too. Save your repository passwords first!
```

Backup repositories are never touched.

## Where things are

| Path | What |
|------|------|
| `C:\Program Files\ResticConsole\` | program files |
| `C:\ProgramData\ResticConsole\config.json` | destinations, jobs, settings (no secrets) |
| `C:\ProgramData\ResticConsole\keys\` | repository, share and SMTP passwords |
| `C:\ProgramData\ResticConsole\excludes\<job>.txt` | per-job exclude patterns |
| `C:\ProgramData\ResticConsole\token` | sign-in token (Administrators, SYSTEM and the desktop user) |
| `C:\ProgramData\ResticConsole\state\runs\<job>\` | run logs and summaries (kept 120 days) |
| Task Scheduler `\ResticConsole\` | `Server`, one `Job-<id>` per job, `Notify-<user>` for desktop alerts |

## Troubleshooting

- **"The Restic Console service is not answering"**: open Task Scheduler → ResticConsole → *Server*, check *Last Run
  Result*, and right-click → *Run*. To see errors, stop the task and run the server by hand in an administrator
  PowerShell: `& "C:\Path\To\python.exe" -I "C:\Program Files\ResticConsole\server.py"`.
- **"Drive Z: does not exist for the Restic Console service"**: use the UNC path (`\\server\share\...`), see above.
- **No desktop notifications**: they are shown only while the desktop user is signed in, and Windows Focus
  Assist / Do Not Disturb can hide them. Use *Settings → Desktop notifications → Send test*.
- **A job says "repository is already locked"**: a run was stopped mid-way. Use *Destinations → Remove stale locks*.
- **restic or Python moved** (for example after an upgrade to a new Python version): re-run `install.ps1`; it stores
  the new paths and re-registers the tasks.
