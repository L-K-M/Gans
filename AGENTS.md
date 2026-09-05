# Working on Gans

Gans is a native macOS **menu-bar agent** (AppKit `NSApplication`, `.accessory`, no Dock
icon) that's an end-to-end-encrypted client for Ente Auth. SwiftUI is used only for view
content hosted inside AppKit windows/panels.

See [PLAN.md](PLAN.md) for the full design and the Ente protocol spec.

## Layout

- `Gans/App` — entry point + `AppDelegate` (wires everything).
- `Gans/Crypto` — libsodium wrappers (`EnteCrypto`) + base64.
- `Gans/OTP` — base32 + TOTP/HOTP/Steam generation.
- `Gans/Ente` — API client, DTOs, SRP, login orchestration, key unwrap, the vault.
- `Gans/Store` — Keychain + encrypted-entity disk cache.
- `Gans/MenuBar`, `Gans/QuickSearch`, `Gans/Hotkey`, `Gans/Paste`, `Gans/Auth`,
  `Gans/Settings` — UI/interaction.
- `Gans/Updates` — reusable GitHub release checker (shared verbatim with the sibling apps).
- `GansTests` — pure-logic tests (RFC vectors, parsing, search, base64).

The Xcode project uses **file-system-synchronized groups**: new `.swift` files under
`Gans/` or `GansTests/` are picked up automatically — no `project.pbxproj` edits needed
(except when adding a Swift Package).

## Conventions

- **One type per file**; the filename matches the primary type. Use `// MARK:` sections.
- Avoid force-unwraps outside tests.
- **Keep crypto exact.** `EnteCrypto` calls the libsodium C API directly so byte lengths,
  the Argon2 memlimit *unit* (bytes), and the secretstream final-tag leniency are explicit.
  Don't "simplify" it to the higher-level wrapper without re-checking the contract.
- **Never persist the password or plaintext secrets.** Keychain = token + authKey only;
  disk = Ente's encrypted blobs only.
- **Never log secrets, tokens, or codes.** Use the `Log` loggers.
- `LSUIElement = YES` stays true. Windows (Login/Settings) flip the app to `.regular`
  while visible and back to `.accessory` on close via `ActivationPolicy`.
- Permissions: the global hotkey uses Carbon (no permission). Typing into other apps uses
  `CGEventPost` and needs **Accessibility** — degrade to clipboard copy when it's denied.

## SRP (read before touching `EnteSRP.swift`)

Ente uses SRP-6a, group 4096, SHA-256, with the **simple** proof `M1 = H(A | B | S)` (not
the RFC 2945 form — that's why the common Swift SRP libraries don't interoperate). `k`/`u`
pad to N's length; `A`/`B`/`S` are hashed into `M1` as minimal big-endian bytes to match
the server (`ente-io/go-srp`). If login proof fails, the leading-zero padding of `S`/`A` in
`M1` is the first thing to check.

## Linux port (`linux/`)

A Python 3 + GTK 3 tray app packaged as a `.deb`; design and binding interface contracts
in [linux/PLAN.md](linux/PLAN.md), user docs in [linux/README.md](linux/README.md).

- `linux/gans/` — the package. Everything below `ui/` is headless-testable and imports no
  GTK: `crypto.py` (libsodium via PyNaCl's low-level bindings — **keep crypto exact**, same
  rules as `EnteCrypto.swift`), `otp.py`, `entry.py`, `search.py`, `prefs.py`, `ente/`
  (API, SRP, login, key unwrap, vault), `store/` (Secret Service keyring, encrypted cache).
  `platform/` wraps X11/XTest, the hotkey backends, clipboard, polkit lock, autostart;
  `ui/` holds the GTK windows, tray and `app.py` (the `AppDelegate` equivalent).
- Conventions: Python 3.10-compatible, one primary type per module with `# MARK:`
  sections, blocking work on threads marshalled back with the `dispatch` callable, never
  persist secrets in plaintext (Secret Service or memory only), never log secrets/codes.
- Tests: `cd linux && python3 -m unittest discover -s tests -t .` (GUI tests start their
  own Xvfb + private session bus via `tests/harness.py`; they skip without Xvfb).
- Package: `linux/packaging/build-deb.sh` → `linux/dist/gans_<version>_all.deb`; lintian
  must stay clean. CI: `.github/workflows/linux.yml`.
- Ente-specific gotchas (SRP padding, base64 flavours, memlimit units, secretstream tag
  leniency) apply identically — the Python and Swift implementations must agree byte for
  byte; `tests/vectors/libsodium.json` pins the crypto against libsodium itself.

## Testing

`xcodebuild -project Gans.xcodeproj -scheme Gans -destination 'platform=macOS' clean test`

Unit-test pure logic only. Login against the live server, the global hotkey, the floating
panel, and CGEvent typing need a real Mac and are manual-verify items (see README).
