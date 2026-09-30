#!/usr/bin/env bash
set -euo pipefail
# Build linux/dist/gans-amd64.flatpak by repacking the release .deb under
# flatpak's /app prefix — the bundle ships byte-for-byte what the .deb
# installs. Usage:
#   scripts/build-flatpak.sh [--install]   # build the .deb first, then repack
#   scripts/build-flatpak.sh path/to.deb   # repack an existing .deb (CI)
#
# The bundle names Flathub as its runtime's source, so installing it fetches
# the GNOME runtime from there. Needs flatpak and flatpak-builder.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
readonly APP_ID=ch.lkmc.Gans
readonly MANIFEST="$ROOT/flatpak/$APP_ID.yml"
readonly FLATHUB_REPO=https://dl.flathub.org/repo/flathub.flatpakrepo
readonly WORK="$ROOT/linux/dist/flatpak"

die() { echo "build-flatpak.sh: $*" >&2; exit 1; }

INSTALL=0
DEB=""
for argument in "$@"; do
  case "$argument" in
    --install) INSTALL=1 ;;
    -h|--help) awk 'NR==1&&/^#!/{next} /^set -euo pipefail/{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *.deb) [ -z "$DEB" ] || die "pass at most one .deb"; DEB="$argument" ;;
    *) echo "Unknown argument: $argument" >&2; exit 2 ;;
  esac
done

# A relative .deb argument names the caller's directory, not the
# repository root we cd'd into above.
if [[ -n "$DEB" && "$DEB" != /* ]]; then DEB="$OLDPWD/$DEB"; fi

command -v flatpak-builder >/dev/null 2>&1 ||
  die "flatpak-builder not found: install flatpak and flatpak-builder"
command -v dpkg-deb >/dev/null 2>&1 ||
  die "dpkg-deb not found: install dpkg"

if [[ -z "$DEB" ]]; then
  (cd linux && packaging/build-deb.sh)
  DEB="$(find linux/dist -maxdepth 1 -name 'gans_*_all.deb' -printf '%T@\t%p\n' 2>/dev/null | sort -rn | head -n1 | cut -f2- || true)"
fi
[ -f "$DEB" ] || die ".deb not found: $DEB"

# Debian /usr -> flatpak /app.
rm -rf "$WORK/debroot" "$WORK/stage" "$WORK/build" "$WORK/repo"
mkdir -p "$WORK"   # dpkg-deb creates the target dir but not its parents
dpkg-deb -x "$DEB" "$WORK/debroot"
mkdir -p "$WORK/stage"
cp -a "$WORK/debroot/usr/." "$WORK/stage/"

# The gans launcher and library paths hardcode /usr; inside flatpak the
# prefix is /app.
while IFS= read -r f; do
  sed -i '/^#!/!s|/usr/|/app/|g' "$f"
done < <(grep -rIl '/usr/' "$WORK/stage" 2>/dev/null || true)
while IFS= read -r f; do
  sed -i -e 's|Exec=/usr/bin/|Exec=|g' -e 's|Exec=/app/bin/|Exec=|g' -e '/^TryExec=/d' "$f"
done < <(find "$WORK/stage/share/applications" -type f -name '*.desktop' 2>/dev/null)

# The deb ships ch.lkmc.Gans.desktop and a 64px icon — flatpak's export
# names already; verify rather than rename.
DESKTOP="$(find "$WORK/stage/share/applications" -type f -name '*.desktop' -print -quit 2>/dev/null || true)"
[ -n "$DESKTOP" ] || die "no .desktop file inside $DEB"
[ "$(basename "$DESKTOP")" = "$APP_ID.desktop" ] ||
  mv "$DESKTOP" "$WORK/stage/share/applications/$APP_ID.desktop"
DESKTOP="$WORK/stage/share/applications/$APP_ID.desktop"
# The launcher resolves Icon= through flatpak's exported name.
sed -i "s|^Icon=.*|Icon=$APP_ID|" "$DESKTOP"
if ! find "$WORK/stage/share/icons" "$WORK/stage/share/pixmaps" -name "$APP_ID.*" -print -quit 2>/dev/null | grep -q .; then
  ICON="$(find "$WORK/stage/share/icons" "$WORK/stage/share/pixmaps" -name '*.png' -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -n1 | cut -f2- || true)"
  [ -n "$ICON" ] || die "no icon inside $DEB"
  mkdir -p "$WORK/stage/share/icons/hicolor/256x256/apps"
  cp "$ICON" "$WORK/stage/share/icons/hicolor/256x256/apps/$APP_ID.png"
fi

flatpak remote-add --user --if-not-exists flathub "$FLATHUB_REPO"
# --disable-rofiles-fuse: containers (CI) have no FUSE, and a copy-only build
# gains nothing from it.
flatpak-builder --user --install-deps-from=flathub --force-clean \
  --disable-rofiles-fuse \
  --state-dir="$WORK/state" --repo="$WORK/repo" \
  "$WORK/build" "$MANIFEST"

# Smoke check: the staged tree must leave an executable under
# /app/bin — catches a failed /usr->/app remap before the bundle
# ships.
COMMAND_NAME="$(sed -n '/^command:[[:space:]]*/{s///;p;q}' "$MANIFEST")"
[ -n "$COMMAND_NAME" ] || die "no command: key in $MANIFEST"
flatpak-builder --run "$WORK/build" "$MANIFEST" \
  sh -c 'bin="/app/bin/$1"; test -x "$bin" || { echo "missing $bin" >&2; ls -l /app/bin >&2; exit 1; }; bad="$(ldd "$bin" 2>/dev/null | grep "not found" || true)"; [ -z "$bad" ] || { printf 'unresolved libraries:\n%s\n' "$bad" >&2; exit 1; }' _ "$COMMAND_NAME"

BUNDLE="$ROOT/linux/dist/gans-amd64.flatpak"
flatpak build-bundle --runtime-repo="$FLATHUB_REPO" \
  "$WORK/repo" "$BUNDLE" "$APP_ID"
if ((INSTALL)); then
  flatpak install --user -y --noninteractive "$BUNDLE"
fi
echo "Built ${BUNDLE#"$ROOT"/}"
