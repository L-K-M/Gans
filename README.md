# Gans

A native macOS menu-bar client for [Ente Auth](https://ente.io/auth/) — your end-to-end
encrypted 2FA codes, one keystroke away.

**Version:** <!-- version -->0.1.0<!-- /version -->

## What it does

- **Menu bar** — click the key icon to see every entry with its live code; click an entry
  to copy the code to the clipboard.
- **Quick Search** — press the global hotkey (default **⌃⌥Space**) to open a Spotlight-style
  search field anywhere. Type to filter, press **Return**, and Gans **types the current
  code straight into whatever field had focus**.
- **End-to-end encrypted** — Gans logs into Ente, syncs your encrypted authenticator
  entities, and decrypts them locally with libsodium. Your password is never stored;
  plaintext secrets never touch disk.

## How it works

Gans speaks Ente's real protocol (reverse-engineered from the official open-source CLI):

- **Login** via SRP-6a (4096-bit) with an automatic **email-code** fallback, plus
  account-level **2FA**. The account password is used locally to derive the key-encryption
  key (Argon2id) and unwrap the master key.
- **Sync** pulls `/authenticator/entity/diff`; each entity is decrypted with the
  authenticator key (XChaCha20-Poly1305 secretstream) into its `otpauth://` URI.
- **Codes** are generated locally per RFC 6238 (TOTP), RFC 4226 (HOTP), and Steam Guard.

At rest, the Keychain holds only the auth token + the 32-byte authenticator key; the disk
cache holds only Ente's already-encrypted blobs.

## Permissions

- **Accessibility** — required *only* to type/paste a code into another app from Quick
  Search. Copying from the menu needs nothing. Grant it in Settings.
- The global hotkey uses Carbon and needs no special permission.

## Install

Download the latest `.dmg` from [Releases](https://github.com/L-K-M/Gans/releases). The
app is **unsigned** (no Developer ID / notarization), so on first launch:

- **Right-click** the app → **Open** → **Open**, or
- run `xattr -dr com.apple.quarantine /Applications/Gans.app`

Requires macOS 13 (Ventura) or later.

## Build

```bash
# Requires Xcode 16.2+. Swift packages (swift-sodium, BigInt) resolve automatically.
xcodebuild -project Gans.xcodeproj -scheme Gans -destination 'platform=macOS' build

# or a clean Release build that reveals the product in Finder:
./clean-build.sh
```

## Dependencies

- [swift-sodium](https://github.com/jedisct1/swift-sodium) — libsodium (Argon2id,
  secretbox, sealed box, secretstream). Gans uses the `Clibsodium` C API directly.
- [attaswift/BigInt](https://github.com/attaswift/BigInt) — big-integer math for SRP-6a.

## Releasing

```bash
scripts/release.sh 1.2.0 --push    # bump version, tag v1.2.0, push → CI builds & publishes
```

See [CICD.md](CICD.md) for the pipeline.

## Status & manual verification

The pure logic (OTP generation, otpauth parsing, search, base64/base32, crypto wrappers)
is unit-tested against RFC vectors. The following require a real signed-in Mac and are
verified manually:

- SRP / email-code login against the live Ente server.
- The global hotkey, the floating panel, and typing/pasting a code into another app.

## License

[The Unlicense](LICENSE) — public domain.
