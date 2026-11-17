#!/usr/bin/env bash
# Install or upgrade Restic Console on Linux (systemd).
#
#   sudo ./linux/install.sh                 # desktop user = the user who ran sudo
#   sudo ./linux/install.sh --user alice    # pick the desktop user explicitly
#
# Safe to re-run for upgrades: config, jobs, keys, run history and the token are kept.
set -euo pipefail

PREFIX=/opt/restic-console
ETC=/etc/restic-console
STATE=/var/lib/restic-console
UNITS=/etc/systemd/system
GROUP=restic-console
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"
USER_NAME="${SUDO_USER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
[ "$EUID" -eq 0 ] || die "run with sudo"
if [ -z "$USER_NAME" ] || ! id "$USER_NAME" >/dev/null 2>&1; then die "no desktop user; pass --user NAME"; fi
command -v systemctl >/dev/null || die "systemd is required"
PYTHON=$(command -v python3) || die "python3 is not installed"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is required"
RESTIC=$(command -v restic) || die "restic is not installed (apt install restic, dnf install restic, or https://restic.net)"
rv=$("$RESTIC" version | awk '{print $2}')
"$PYTHON" -c "import sys; v=tuple(int(x) for x in '$rv'.split('.')[:2]); sys.exit(v < (0, 16))" \
  || die "restic $rv is too old; 0.16 or newer is needed (for --retry-lock)"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)

echo "== Group $GROUP (members can use the app)"
getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
if ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$GROUP"; then
  usermod -aG "$GROUP" "$USER_NAME"
  ADDED_GROUP=1
fi

echo "== Program files -> $PREFIX"
install -d -m 755 "$PREFIX" "$PREFIX/static"
rm -f "$PREFIX"/*.py
install -m 644 "$SRC"/server.py "$SRC"/runner.py "$SRC"/notify.py "$SRC"/common.py "$SRC"/runs.py \
               "$SRC"/schedule.py "$SRC"/plat_linux.py "$PREFIX"/
install -m 644 "$SRC"/static/* "$PREFIX/static/"

echo "== Config in $ETC, run history in $STATE"
install -d -m 755 "$ETC" "$ETC/excludes"
install -d -m 700 "$ETC/keys" "$STATE"
RC_ETC="$ETC" RC_STATE="$STATE" "$PYTHON" -I "$PREFIX/server.py" init \
  --user "$USER_NAME" --restore-root "$USER_HOME" --restic "$RESTIC"
chown root:"$GROUP" "$ETC/token" "$ETC/config.json"
chmod 640 "$ETC/token" "$ETC/config.json"

echo "== systemd units"
for f in "$HERE"/systemd/*; do
  sed -e "s|@PYTHON@|$PYTHON|g" -e "s|@PREFIX@|$PREFIX|g" "$f" > "$UNITS/$(basename "$f")"
  chmod 644 "$UNITS/$(basename "$f")"
done
systemctl daemon-reload
systemctl enable restic-console.service >/dev/null
systemctl restart restic-console.service

echo "== Launcher"
install -m 755 "$HERE/restic-console" /usr/local/bin/restic-console
install -m 644 "$HERE/restic-console.desktop" /usr/share/applications/restic-console.desktop
update-desktop-database -q /usr/share/applications 2>/dev/null || true

code=000
for _ in 1 2 3 4 5 6 7 8 9 10; do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/status || true)
  [ "$code" = 401 ] && break
  sleep 1
done
echo
if [ "$code" = 401 ]; then
  echo "Restic Console $("$PYTHON" -I "$PREFIX/server.py" --version) is running on http://127.0.0.1:8790"
  echo "Open it from the app menu (Restic Console) or run: restic-console"
  [ -n "${ADDED_GROUP:-}" ] && echo "NOTE: $USER_NAME was added to the '$GROUP' group. Log out and back in before opening the app."
else
  echo "WARNING: the service answered '$code'. See: journalctl -u restic-console -n 30"
  exit 1
fi
