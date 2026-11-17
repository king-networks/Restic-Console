# Contributing to Restic Console

Thanks for helping! Bug reports, fixes and new features are all welcome. For anything bigger than a small fix,
please open an issue first so we can agree on the approach.

## Ground rules

- **Standard library only.** The app must run on a stock Python 3.9+ with nothing to `pip install`.
- **No build step.** The UI is one file, `static/index.html`, with plain JavaScript and CSS.
- **OS-specific code goes in `plat_linux.py` / `plat_windows.py`**, behind the same function names in both.
  Everything else should work unchanged on both systems.
- Keep the security model intact (see [docs/how-it-works.md](docs/how-it-works.md#security-model)): 127.0.0.1 only,
  token sign-in, secrets only in `keys/`, restores never overwrite.

## Run it in development mode

Dev mode runs the server as your normal user against scratch folders. It never touches systemd or Task Scheduler
(*Run now* is disabled), but you can create real repositories, browse snapshots and restore.

Linux shell:

```sh
export RC_DEV=1 RC_PORT=8791 RC_ETC=$PWD/dev/etc RC_STATE=$PWD/dev/state RC_UNIT_DIR=$PWD/dev/units
python3 server.py init --restore-root "$HOME"
python3 server.py                  # open http://127.0.0.1:8791 and paste the token from dev/etc/token
```

Windows PowerShell:

```powershell
$env:RC_DEV="1"; $env:RC_PORT="8791"; $env:RC_ETC="$PWD\dev\etc"; $env:RC_STATE="$PWD\dev\state"
python server.py init --restore-root $HOME
python server.py
```

To make a backup run in dev mode, start the runner by hand with the same environment: `python3 runner.py <job-id>`.
`dev/` is in `.gitignore`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_units.py`: schedules, Windows task XML, path checks, run records.
- `tests/test_integration.py`: starts the real server in dev mode, creates a repository with the real restic, runs a
  backup through `runner.py`, then browses, downloads and restores. Skipped if restic is not installed.
- `tests/smoke.py`: run by CI against a **real install** on Ubuntu and Windows (the installer, systemd / Task
  Scheduler, Run now, restore, uninstall). Don't run it on your own machine unless you want a test job and repository
  left in your install.

Lint the way CI does:

```sh
pip install ruff && ruff check .
shellcheck linux/install.sh linux/uninstall.sh linux/restic-console
```

## Pull requests

1. Fork, then create a branch from `main`.
2. Make the change, with a test where one makes sense.
3. Run the tests and the linters.
4. Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md).
5. Open the pull request and describe what you changed and how you tested it, especially on the OS you could test.

CI runs everything on Linux and Windows, including a full install and uninstall on both.

## Releasing (maintainers)

1. Update `__version__` in `common.py` and `version` in `pyproject.toml`.
2. Move the **Unreleased** notes in `CHANGELOG.md` under the new version and date.
3. Tag `vX.Y.Z` on `main` and push the tag; create a GitHub release from it with those notes.
