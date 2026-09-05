# CI/CD

Three GitHub Actions workflows. The macOS ones run on `macos-14` with Xcode pinned to
**16.2**; Swift packages (`swift-sodium`, `BigInt`) are resolved by `xcodebuild` on the
runner (which has network access); nothing is vendored. The Linux one runs on
`ubuntu-latest`.

## CI — `.github/workflows/ci.yml`

Runs on pull requests and pushes to `main`.

1. Checkout, select Xcode 16.2, install `xcbeautify`.
2. `xcodebuild -resolvePackageDependencies` (explicit, so resolution failures are obvious).
3. `xcodebuild clean test` with `CODE_SIGNING_ALLOWED=NO`.
4. On failure, upload `TestResults.xcresult`.

Local equivalent:

```bash
set -o pipefail
xcodebuild -project Gans.xcodeproj -scheme Gans \
  -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO clean test | xcbeautify
```

## Linux — `.github/workflows/linux.yml`

Runs on pull requests and pushes to `main` / `claude/**`, on `ubuntu-latest` (24.04):

1. Install the runtime dependencies from `linux/packaging/debian/control.in` plus the
   headless test rig (Xvfb, xdotool, a private session bus, GNOME's media-keys schema,
   libsodium-dev) and the package validators.
2. `python3 -m unittest discover -s tests -t . -v` in `linux/` (unit, Xvfb GUI, packaging
   and end-to-end tests).
3. `packaging/build-deb.sh`, `lintian --fail-on error`, then `apt install ./gans_*.deb`
   and a smoke run of the installed `gans --version`.
4. Upload the `.deb` as the `gans-deb` artifact.

Local equivalent: `cd linux && python3 -m unittest discover -s tests -t . && packaging/build-deb.sh`.

## Release — `.github/workflows/release.yml`

Triggered by pushing a `v*` tag (the tag is the source of truth for the version).

0. Job `linux-deb` (`ubuntu-latest`): build `gans_<version>_all.deb` with `VERSION` from
   the tag, run lintian, upload it as an artifact. The macOS job below `needs` it.
1. Derive `VERSION` from the tag (`v1.2.0` → `1.2.0`).
2. Resolve packages, then build Release unsigned with the version baked in.
3. **Ad-hoc sign** (`codesign --force --deep --sign -`) — required to launch on Apple
   Silicon, but **not** a Developer ID signature and **not** notarized.
4. Package a `.zip` (`ditto`) and a `.dmg` (`create-dmg`).
5. Download the Linux `.deb` artifact and publish a GitHub Release with all three assets
   (`.dmg`, `.zip`, `.deb`), Gatekeeper-bypass notes, and Linux install notes.

No secrets, certificates, or notarization credentials are used — distribution is
intentionally unsigned (same model as the sibling apps).

## Cutting a release

```bash
scripts/release.sh 1.2.0 --push
```

This bumps `MARKETING_VERSION` (and the README marker), commits, tags `v1.2.0`, and pushes
— which triggers the release workflow.
