#!/usr/bin/env bash
# Install or upgrade Restic Console.
#
#   sudo ./install.sh                 # desktop user = the user who ran sudo
#   sudo ./install.sh --user alice    # pick the desktop user explicitly
#
# Safe to re-run for upgrades: config, jobs, keys and the token are kept.
set -euo pipefail

PREFIX=/opt/restic-console
ETC=/etc/restic-console
UNITS=/etc/systemd/system
SRC="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
[ "$EUID" -eq 0 ] || die "run with sudo"
[ -n "$USER_NAME" ] && id "$USER_NAME" >/dev/null 2>&1 || die "no desktop user; pass --user NAME"
command -v restic >/dev/null || die "restic is not installed (apt install restic, or https://restic.net)"
command -v systemctl >/dev/null || die "systemd is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "python 3.8+ is required"
RESTIC=$(command -v restic)
rv=$("$RESTIC" version | awk '{print $2}')
python3 -c "import sys; v=tuple(int(x) for x in '$rv'.split('.')[:2]); sys.exit(v < (0, 16))" || die "restic $rv is too old; 0.16 or newer is needed (for --retry-lock)"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
USER_GROUP=$(id -gn "$USER_NAME")

echo "== Program files -> $PREFIX"
install -d -m 755 "$PREFIX" "$PREFIX/static"
install -m 644 "$SRC"/server.py "$SRC"/runner.py "$SRC"/notify.py "$PREFIX"/
install -m 644 "$SRC"/static/* "$PREFIX/static/"
[ "$RESTIC" = /usr/bin/restic ] || sed -i "s|RESTIC = os.environ.get(\"RC_RESTIC\", \"/usr/bin/restic\")|RESTIC = os.environ.get(\"RC_RESTIC\", \"$RESTIC\")|" "$PREFIX/runner.py"

echo "== Config in $ETC"
install -d -m 755 "$ETC"
install -d -m 700 "$ETC/keys"
install -d -m 755 "$ETC/excludes"
if [ ! -s "$ETC/token" ]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$ETC/token"
fi
chown root:"$USER_GROUP" "$ETC/token"; chmod 640 "$ETC/token"
if [ ! -f "$ETC/config.json" ]; then
  python3 - "$ETC/config.json" "$USER_NAME" "$USER_HOME" <<'PY'
import json, sys
path, user, home = sys.argv[1:]
cfg = {"version": 1,
       "settings": {"desktop_user": user, "restore_roots": [home],
                    "notifications": {"desktop": {"enabled": True, "user": user},
                                      "email": {"enabled": False, "host": "", "port": 587, "security": "starttls",
                                                "username": "", "from": "", "to": [], "password_file": ""},
                                      "webhook": {"enabled": False, "url": ""}}},
       "destinations": {}, "jobs": {}}
open(path, "w").write(json.dumps(cfg, indent=2) + "\n")
PY
  echo "   new config for desktop user $USER_NAME (restores allowed under $USER_HOME)"
fi
chown root:"$USER_GROUP" "$ETC/config.json"; chmod 640 "$ETC/config.json"

echo "== systemd units"
install -m 644 "$SRC"/systemd/*.service "$SRC"/systemd/*.timer "$UNITS"/
systemctl daemon-reload
systemctl enable restic-console.service >/dev/null
systemctl restart restic-console.service

echo "== Launcher"
install -m 755 "$SRC/restic-console" /usr/local/bin/restic-console
install -m 644 "$SRC/restic-console.desktop" /usr/share/applications/restic-console.desktop
update-desktop-database -q /usr/share/applications 2>/dev/null || true

sleep 1
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/status || true)
echo
if [ "$code" = 401 ]; then
  echo "Restic Console is running on http://127.0.0.1:8790"
  echo "Open it from the app menu (Restic Console) or run: restic-console"
else
  echo "WARNING: the service answered '$code'. See: journalctl -u restic-console -n 30"
fi
