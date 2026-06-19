# CI/CD

Two GitHub Actions workflows, both on `macos-14` with Xcode pinned to **16.2**. Swift
packages (`swift-sodium`, `BigInt`) are resolved by `xcodebuild` on the runner (which has
network access); nothing is vendored.

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

## Release — `.github/workflows/release.yml`

Triggered by pushing a `v*` tag (the tag is the source of truth for the version).

1. Derive `VERSION` from the tag (`v1.2.0` → `1.2.0`).
2. Resolve packages, then build Release unsigned with the version baked in.
3. **Ad-hoc sign** (`codesign --force --deep --sign -`) — required to launch on Apple
   Silicon, but **not** a Developer ID signature and **not** notarized.
4. Package a `.zip` (`ditto`) and a `.dmg` (`create-dmg`).
5. Publish a GitHub Release with both assets and Gatekeeper-bypass notes.

No secrets, certificates, or notarization credentials are used — distribution is
intentionally unsigned (same model as the sibling apps).

## Cutting a release

```bash
scripts/release.sh 1.2.0 --push
```

This bumps `MARKETING_VERSION` (and the README marker), commits, tags `v1.2.0`, and pushes
— which triggers the release workflow.
