#!/usr/bin/env bash
# Install or upgrade Restic Console on Linux (any distribution that boots with systemd).
#
#   sudo ./linux/install.sh                     # desktop user = the user who ran sudo
#   sudo ./linux/install.sh --user alice        # pick the desktop user explicitly
#   sudo ./linux/install.sh --get-restic        # also download the official restic to /usr/local/bin
#   sudo ./linux/install.sh --restic PATH --python PATH   # use specific binaries
#
# Safe to re-run for upgrades: config, jobs, keys, run history and the token are kept.
set -euo pipefail

PREFIX=/opt/restic-console
ETC=/etc/restic-console
STATE=/var/lib/restic-console
UNITS=/etc/systemd/system
GROUP=restic-console
BIN=/usr/local/bin
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"
USER_NAME="${SUDO_USER:-}"
RESTIC="" PYTHON="" GET_RESTIC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --restic) RESTIC="${2:-}"; shift 2 ;;
    --python) PYTHON="${2:-}"; shift 2 ;;
    --get-restic) GET_RESTIC=1; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "   note: $*" >&2; }

# Package-manager hint for this distribution: pkg_hint <debian> <fedora> <suse> <arch>
# shellcheck disable=SC1091  # read at run time on the target system
DISTRO=$( (. /etc/os-release 2>/dev/null && echo " ${ID:-} ${ID_LIKE:-} ") || echo " ")
pkg_hint() {
  case "$DISTRO" in
    *" debian "*|*" ubuntu "*) echo "sudo apt install $1" ;;
    *" fedora "*|*" rhel "*|*" centos "*) echo "sudo dnf install $2" ;;
    *" suse "*|*" opensuse "*|*" opensuse-"*|*" sles "*) echo "sudo zypper install $3" ;;
    *" arch "*) echo "sudo pacman -S $4" ;;
    *) echo "install '$1' with your package manager" ;;
  esac
}

[ "$EUID" -eq 0 ] || die "run with sudo"
if [ -z "$USER_NAME" ] || ! id "$USER_NAME" >/dev/null 2>&1; then die "no desktop user; pass --user NAME"; fi
[ -d /run/systemd/system ] || die "this system is not running systemd, which Restic Console uses to schedule jobs"
for t in tar runuser timeout getent groupadd usermod; do
  command -v "$t" >/dev/null || die "'$t' is missing; install it with your package manager"
done

# sudo resets PATH (RHEL-family drops /usr/local/bin), so look in the usual places too.
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"

# ---------------------------------------------------------------- Python 3.9+
py_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; }
if [ -n "$PYTHON" ]; then
  py_ok "$PYTHON" || die "$PYTHON is not Python 3.9 or newer"
else
  for c in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    p=$(command -v "$c" 2>/dev/null) && py_ok "$p" && { PYTHON=$p; break; }
  done
  [ -n "$PYTHON" ] || die "Python 3.9 or newer is needed: $(pkg_hint python3 python3 python3 python)
       (RHEL/Rocky/Alma 8: sudo dnf install python3.11)"
fi
PYTHON=$(readlink -f "$PYTHON")

# ---------------------------------------------------------------- restic 0.16+
restic_ver() { "$1" version 2>/dev/null | awk '{print $2}'; }
restic_ok() { "$PYTHON" -c "import sys; v=tuple(int(x) for x in '$1'.split('.')[:2]); sys.exit(v < (0, 16))" 2>/dev/null; }
[ -n "$RESTIC" ] || RESTIC=$(command -v restic 2>/dev/null || true)
rv=""; [ -n "$RESTIC" ] && rv=$(restic_ver "$RESTIC")
if [ -n "$rv" ] && restic_ok "$rv"; then
  :
else
  why="restic is not installed"; [ -n "$rv" ] && why="restic $rv at $RESTIC is too old (0.16 or newer is needed)"
  if [ -z "$GET_RESTIC" ] && [ -t 0 ]; then
    read -r -p "$why. Download the official restic release to $BIN/restic? [Y/n] " a
    case "$a" in ""|y|Y|yes) GET_RESTIC=1 ;; esac
  fi
  [ -n "$GET_RESTIC" ] || die "$why.
       Re-run with --get-restic to download the official release to $BIN/restic,
       or install it yourself: $(pkg_hint restic restic restic restic)"
  echo "== Downloading the official restic release"
  install -d -m 755 "$BIN"
  "$PYTHON" -I "$HERE/get_restic.py" "$BIN/restic" >/dev/null || die "downloading restic failed"
  RESTIC="$BIN/restic"; rv=$(restic_ver "$RESTIC")
fi
RESTIC=$(readlink -f "$RESTIC")
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
echo "   python  $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"
echo "   restic  $rv at $RESTIC"
echo "   user    $USER_NAME ($USER_HOME)"

# ---------------------------------------------------------------- install
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

echo "== Launcher"
install -d -m 755 "$BIN" /usr/share/applications
sed -e "s|@PYTHON@|$PYTHON|g" "$HERE/restic-console" > "$BIN/restic-console"
chmod 755 "$BIN/restic-console"
install -m 644 "$HERE/restic-console.desktop" /usr/share/applications/restic-console.desktop
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications 2>/dev/null || true

# SELinux (Fedora, RHEL family): give the copied files their default labels.
if command -v restorecon >/dev/null && command -v selinuxenabled >/dev/null && selinuxenabled; then
  restorecon -RF "$PREFIX" "$ETC" "$STATE" "$UNITS"/restic-* "$BIN/restic-console" "$RESTIC" 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable restic-console.service >/dev/null
systemctl restart restic-console.service

command -v notify-send >/dev/null || \
  warn "notify-send is missing, so desktop alerts will not show. Install it: $(pkg_hint libnotify-bin libnotify libnotify-tools libnotify)"

# ---------------------------------------------------------------- check
status() {
  "$PYTHON" -I -c '
import urllib.error, urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    op.open("http://127.0.0.1:8790/api/status", timeout=3); print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")'
}
code=000
for _ in $(seq 15); do
  code=$(status)
  [ "$code" = 401 ] && break
  sleep 1
done
echo
if [ "$code" = 401 ]; then
  echo "Restic Console $("$PYTHON" -I "$PREFIX/server.py" --version) is running on http://127.0.0.1:8790"
  echo "Open it from the app menu (Restic Console) or run: restic-console"
  [ -n "${ADDED_GROUP:-}" ] && echo "NOTE: $USER_NAME was added to the '$GROUP' group. Log out and back in before opening the app."
  exit 0
else
  echo "WARNING: the service answered '$code'. See: journalctl -u restic-console -n 30"
  exit 1
fi
