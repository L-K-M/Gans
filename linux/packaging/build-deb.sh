#!/usr/bin/env bash
# Builds the Ubuntu/Debian package for Gans: dist/gans_<version>_all.deb.
#
#   packaging/build-deb.sh [--version X.Y.Z] [--output DIR] [--no-lintian]
#
# The version comes from --version, else $VERSION, else MARKETING_VERSION in the Xcode
# project — the git tag is the source of truth in CI (release.yml exports VERSION from
# it), and the pbxproj keeps local builds in step with the macOS app. It is stamped into
# /usr/share/gans/gans/VERSION so the installed `gans --version` reports the release, into
# the AppStream <release>, and into the Debian changelog.
#
# The tree is staged with explicit modes (dirs 755, files 644, programs 755) so the
# result does not depend on the caller's umask, then packed with dpkg-deb
# --root-owner-group so no fakeroot is needed. Runs from any cwd. lintian runs at the
# end when installed but never fails the build here; CI runs it separately with
# --fail-on error.
set -euo pipefail

LINUX_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPO_DIR=$(cd "$LINUX_DIR/.." && pwd)
PACKAGING_DIR="$LINUX_DIR/packaging"
DATA_DIR="$LINUX_DIR/gans/data"
APPICON_DIR="$REPO_DIR/Gans/Resources/Assets.xcassets/AppIcon.appiconset"

PACKAGE=gans
APP_ID=ch.lkmc.Gans
ICON_SIZES=(16 32 64 128 256 512)

usage() {
    cat <<USAGE
usage: packaging/build-deb.sh [--version X.Y.Z] [--output DIR] [--no-lintian]

  --version X.Y.Z  package version (default: \$VERSION, else MARKETING_VERSION from
                   Gans.xcodeproj/project.pbxproj)
  --output DIR     where to write gans_<version>_all.deb (default: linux/dist/)
  --no-lintian     skip the informational lintian run at the end
USAGE
}

die() {
    echo "build-deb: $*" >&2
    exit 1
}

# MARK: Arguments

version=""
output="$LINUX_DIR/dist"
run_lintian=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-lintian) run_lintian=0; shift ;;
        --version) [ $# -ge 2 ] || die "--version needs a value"; version=$2; shift 2 ;;
        --version=*) version=${1#--version=}; shift ;;
        --output) [ $# -ge 2 ] || die "--output needs a value"; output=$2; shift 2 ;;
        --output=*) output=${1#--output=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

if [ -z "$version" ]; then
    version=${VERSION:-}
fi
if [ -z "$version" ]; then
    version=$(sed -n 's/^[[:space:]]*MARKETING_VERSION = \([0-9][0-9A-Za-z.+~-]*\);.*/\1/p' \
        "$REPO_DIR/Gans.xcodeproj/project.pbxproj" | head -n 1)
fi
version=${version#v}
[ -n "$version" ] || die "cannot determine the version (pass --version or set VERSION)"
case "$version" in
    [0-9]*) ;;
    *) die "version must start with a digit: '$version'" ;;
esac
case "$version" in
    *[!0-9A-Za-z.+~-]*) die "version contains characters dpkg does not accept: '$version'" ;;
esac

command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb is required (apt install dpkg)"
command -v gzip >/dev/null 2>&1 || die "gzip is required"
[ -d "$APPICON_DIR" ] || die "app icons not found at $APPICON_DIR"

# Reproducible timestamps: honour SOURCE_DATE_EPOCH the way dpkg-buildpackage does.
build_epoch=${SOURCE_DATE_EPOCH:-$(date +%s)}
changelog_date=$(date -u -R -d "@$build_epoch")
release_date=$(date -u -d "@$build_epoch" +%Y-%m-%d)

# MARK: Staging

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/root"

# Copies one file with an explicit mode, creating the parent directories (755).
put() {  # put <mode> <source> <destination-under-root>
    local mode=$1 source=$2 destination="$root$3"
    install -D -m "$mode" "$source" "$destination"
}

# The Python package: every module plus the tray icons the app loads from the source
# tree (gans/ui/tray.py points the indicator at gans/data/icons). Tests, byte-code, and
# the data files that get installed into their proper system locations stay out.
(
    cd "$LINUX_DIR"
    find gans -type d \( -name __pycache__ -o -name tests \) -prune -o -type f -name '*.py' -print
    find gans/data/icons -type f -name '*.svg' -print
) | sort | while IFS= read -r relative; do
    put 644 "$LINUX_DIR/$relative" "/usr/share/gans/$relative"
done
printf '%s\n' "$version" > "$stage/VERSION"
put 644 "$stage/VERSION" /usr/share/gans/gans/VERSION

put 755 "$LINUX_DIR/bin/gans" /usr/bin/gans

put 644 "$DATA_DIR/$APP_ID.desktop" "/usr/share/applications/$APP_ID.desktop"
put 644 "$DATA_DIR/ch.lkmc.gans.policy" /usr/share/polkit-1/actions/ch.lkmc.gans.policy

sed -e "s/@VERSION@/$version/g" -e "s/@DATE@/$release_date/g" \
    "$DATA_DIR/$APP_ID.metainfo.xml" > "$stage/metainfo.xml"
put 644 "$stage/metainfo.xml" "/usr/share/metainfo/$APP_ID.metainfo.xml"

# Symbolic tray icons: whatever SVGs exist at build time. App icons: the macOS asset
# catalogue is the single source of truth, so the PNGs are not duplicated in git.
for svg in "$DATA_DIR"/icons/*.svg; do
    [ -e "$svg" ] || die "no tray icons found in $DATA_DIR/icons"
    put 644 "$svg" "/usr/share/icons/hicolor/symbolic/apps/$(basename "$svg")"
done
for size in "${ICON_SIZES[@]}"; do
    png="$APPICON_DIR/AppIcon-$size.png"
    [ -f "$png" ] || die "missing app icon $png"
    put 644 "$png" "/usr/share/icons/hicolor/${size}x${size}/apps/$APP_ID.png"
done

# gzip -9n: maximum compression, no timestamp/name in the header (reproducible, and what
# lintian expects for manpages and changelogs).
gzip -9n -c "$DATA_DIR/gans.1" > "$stage/gans.1.gz"
put 644 "$stage/gans.1.gz" /usr/share/man/man1/gans.1.gz

put 644 "$PACKAGING_DIR/debian/copyright" /usr/share/doc/gans/copyright
sed -e "s/@VERSION@/$version/g" -e "s/@DATE@/$changelog_date/g" \
    "$PACKAGING_DIR/debian/changelog.in" > "$stage/changelog"
gzip -9n -c "$stage/changelog" > "$stage/changelog.gz"
# The version carries no Debian revision, so the package is "native" and its changelog
# is changelog.gz (changelog.Debian.gz is for packages with a separate upstream).
put 644 "$stage/changelog.gz" /usr/share/doc/gans/changelog.gz

# MARK: Control files

# Installed-Size the way dpkg-gencontrol computes it: each regular file rounded up to
# whole KiB, plus 1 KiB per directory and symlink.
installed_size=$(cd "$root" && find . -mindepth 1 -type f -printf '%s\n' \
    | awk '{ kib += int(($1 + 1023) / 1024) } END { print kib + 0 }')
installed_size=$(( installed_size + $(cd "$root" && find . -mindepth 1 \( -type d -o -type l \) | wc -l) ))

install -d -m 755 "$root/DEBIAN"
sed -e "s/@VERSION@/$version/g" -e "s/@INSTALLED_SIZE@/$installed_size/g" \
    "$PACKAGING_DIR/debian/control.in" > "$root/DEBIAN/control"
chmod 644 "$root/DEBIAN/control"
install -m 755 "$PACKAGING_DIR/debian/postinst" "$root/DEBIAN/postinst"
install -m 755 "$PACKAGING_DIR/debian/prerm" "$root/DEBIAN/prerm"
(cd "$root" && find . -path ./DEBIAN -prune -o -type f -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' md5sum) \
    > "$root/DEBIAN/md5sums"
chmod 644 "$root/DEBIAN/md5sums"

# MARK: Build

mkdir -p "$output"
output=$(cd "$output" && pwd)
deb="$output/${PACKAGE}_${version}_all.deb"
dpkg-deb --build --root-owner-group "$root" "$deb"

echo
dpkg-deb --info "$deb"
echo
dpkg-deb --contents "$deb"
echo
if [ "$run_lintian" = 0 ]; then
    echo "lintian skipped (--no-lintian)"
elif command -v lintian >/dev/null 2>&1; then
    echo "lintian:"
    lintian --tag-display-limit 0 "$deb" || echo "(lintian reported issues; CI fails on errors)"
else
    echo "lintian not installed; skipping"
fi
echo
echo "built $deb"
