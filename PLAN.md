# Gans — Implementation Plan

**Gans** is a native macOS menu‑bar app that logs into [Ente Auth](https://ente.io/auth/)
(the open‑source 2FA authenticator), syncs your TOTP secrets, and puts your one‑time
codes one keystroke away:

1. A **menu‑bar item** lists every entry; selecting one copies its current code to the
   clipboard.
2. A **global hotkey** opens a **Spotlight‑style floating search field**. You type to
   filter your entries; pressing Return on a result **types the current code straight
   into whatever field had focus** before the panel appeared.

This document is the full design. It is intentionally detailed because the app is built
end‑to‑end in one pass and most of it can't be exercised on the CI box (a Linux
container with no Xcode); the real build/sign happens on GitHub's macOS runners, exactly
like the sibling apps **Zap** and **MacDring** in this workspace.

---

## 1. Constraints & guiding decisions

| Decision | Rationale |
|---|---|
| **AppKit `NSApplication` agent app** (`@main enum`, `.accessory` policy, `LSUIElement = YES`) | Matches Zap/MacDring; no Dock icon; full lifecycle control. SwiftUI is used only for view content hosted in AppKit windows. |
| **Deployment target macOS 13.0**, Swift 5.0, Xcode 16.2 (pinned in CI) | Same as the sibling apps. |
| **File‑system‑synchronized Xcode groups** (`PBXFileSystemSynchronizedRootGroup`) | New `.swift` files under `Gans/` are picked up automatically — no `project.pbxproj` churn. |
| **Crypto via libsodium** through the `swift-sodium` SPM package | Ente's E2EE needs Argon2id, `crypto_kdf`, `crypto_secretbox`, `crypto_box_seal`, and the XChaCha20‑Poly1305 secretstream — none of which CryptoKit provides. This is the **only** required dependency. |
| **Login: email‑OTP first, SRP‑6a as the fast path** | Email‑OTP (`/users/ott` → `/users/verify-email`) needs no SRP math or bignum and returns the same key material + token, so it is the robust default that I can reason about confidently without a live account to test against. SRP‑6a (4096‑bit) is added as the no‑email‑roundtrip path. Account‑level 2FA (TOTP) and email‑MFA are handled in both. |
| **Not sandboxed; Hardened Runtime ON** | Typing a code into another app via `CGEventPost` and reading the frontmost app require the app to be outside the sandbox; Accessibility is a runtime TCC grant, not an entitlement (same model as Zap). Network + Keychain work without entitlements when unsandboxed. |
| **Secrets at rest:** Keychain holds the Ente auth token + the 32‑byte authenticator key; disk (Application Support) holds only Ente's *already‑encrypted* entity blobs | The password is never persisted. Plaintext TOTP secrets live only in memory. The on‑disk cache is useless without the Keychain key, and lets the menu populate instantly/offline at launch. |
| **Unsigned, ad‑hoc release** built on tag push | Identical to Zap/MacDring: no Developer ID / notarization; Gatekeeper bypass documented in the release notes. |

---

## 2. Module map

```
Gans/
├── App/
│   ├── GansApp.swift              @main enum → NSApplication(.accessory)
│   └── AppDelegate.swift          status item, coordinators, login gate, update checker
├── Model/
│   ├── AuthEntry.swift            parsed otpauth entry (issuer/account/secret/type/algo/digits/period/counter)
│   ├── OTPType.swift              .totp / .hotp / .steam
│   ├── Preferences.swift          ObservableObject over UserDefaults
│   └── HotkeySpec.swift           keyCode + Carbon modifier flags (Codable)
├── Crypto/
│   ├── EnteCrypto.swift           libsodium wrappers (argon2id, kdf, secretbox open, box seal open, secretstream pull)
│   └── Base64.swift               standard vs URL-safe helpers
├── OTP/
│   ├── Base32.swift               RFC 4648 base32 decode
│   └── TOTPGenerator.swift        RFC 6238 / RFC 4226 / Steam code generation (HMAC via CryptoKit)
├── Ente/
│   ├── EnteModels.swift           Codable DTOs (SRPAttributes, KeyAttributes, AuthorizationResponse, AuthKey, AuthEntity, diff)
│   ├── EnteAPI.swift              URLSession client: endpoints, X-Auth-Token, X-Client-Package
│   ├── EnteSRP.swift              SRP-6a 4096 handshake (uses derived loginKey as the SRP password)
│   ├── EnteLogin.swift            orchestrates SRP / email-OTP / 2FA → AuthorizationResponse
│   ├── KeyUnwrap.swift            AuthorizationResponse + password → master/secret/auth keys + token
│   └── EnteVault.swift            high-level: login, persist session, fetch+decrypt entities → [AuthEntry]
├── Store/
│   ├── Keychain.swift             tiny Security.framework wrapper (token + authKey + session meta)
│   └── EntityCache.swift          encrypted-entity blob cache in Application Support
├── MenuBar/
│   └── StatusItemController.swift NSStatusItem + dynamic entries menu (click = copy code)
├── QuickSearch/
│   ├── SearchFilter.swift         pure ranking/filter (unit-tested)
│   ├── QuickSearchModel.swift     ObservableObject: query, results, selection
│   ├── QuickSearchView.swift      SwiftUI search field + results list
│   └── QuickSearchPanel.swift     borderless key NSPanel, centered, escape/arrows/return
├── Hotkey/
│   ├── KeyCodes.swift             virtual key codes + Carbon modifier mapping
│   └── CarbonHotkey.swift         RegisterEventHotKey wrapper (no Accessibility needed)
├── Paste/
│   └── CodeInjector.swift         restore prev-frontmost app, then type code via CGEvent (or ⌘V)
├── Auth/
│   ├── LoginWindowController.swift
│   └── LoginView.swift            email → (password | code) → optional 2FA
├── Settings/
│   ├── SettingsWindowController.swift
│   └── SettingsView.swift         hotkey, paste-vs-type, launch-at-login, account, updates
├── Common/
│   ├── ActivationPolicy.swift     accessory↔regular handoff helper
│   └── Logging.swift              os.Logger wrapper
├── Updates/                       ported verbatim from Zap (GitHub release checker)
│   ├── GitHubRelease.swift  GitHubReleaseClient.swift  SemanticVersion.swift
│   ├── UpdateChecker.swift
└── Resources/Assets.xcassets      AppIcon + AccentColor

GansTests/
├── Base32Tests.swift              RFC 4648 vectors
├── TOTPGeneratorTests.swift       RFC 6238 vectors (SHA1/256/512), Steam, HOTP (RFC 4226)
├── OtpAuthURITests.swift          otpauth:// parsing → AuthEntry
├── SearchFilterTests.swift        ranking (prefix > substring), tie-breaks
├── Base64Tests.swift              std vs url round-trips
├── PreferencesTests.swift         defaults + encode/decode
├── HotkeySpecTests.swift          Codable + display string
└── SemanticVersionTests.swift     ported from Zap
```

---

## 3. Ente protocol (as implemented)

Host: `https://api.ente.io`. Every authenticated request carries `X-Auth-Token: <token>`
and `X-Client-Package: io.ente.auth`. All crypto base64 fields are **standard** base64;
the token is **URL‑safe** base64.

### 3.1 Login

**Email‑OTP (default):**
1. `POST /users/ott` `{email, purpose:"login"}` → Ente emails a code.
2. `POST /users/verify-email` `{email, ott}` → `AuthorizationResponse`.

**SRP‑6a (fast path):**
1. `GET /users/srp/attributes?email=` → `{srpUserID, srpSalt, kekSalt, memLimit, opsLimit, isEmailMFAEnabled}`.
2. Derive `kek = Argon2id(password, kekSalt, opsLimit, memLimit)`, then
   `loginKey = crypto_kdf_derive_from_key(id:1, ctx:"loginctx", key:kek)[0..<16]`.
3. `POST /users/srp/create-session {srpUserID, srpA}` → `{sessionID, srpB}`.
4. `POST /users/srp/verify-session {srpUserID, sessionID, srpM1}` → `AuthorizationResponse`.
   SRP identity = `srpUserID` (UUID string), SRP password = the 16‑byte `loginKey`,
   group = RFC 5054 4096‑bit, hash = SHA‑256.

**2FA branch (either path):** if the response carries `twoFactorSessionID`,
`POST /users/two-factor/verify {sessionID, code}` → final `AuthorizationResponse`.
A `passkeySessionID` triggers the passkey flow: open
`‹accountsUrl›/passkeys/verify?passkeySessionID=…&redirect=ente-cli://passkey&clientPackage=…`
in the browser, then poll `GET /users/two-factor/passkeys/get-token?sessionID=…` until the
ceremony completes and the server returns the `AuthorizationResponse`.

### 3.2 Key unwrap (libsodium)
```
kek       = Argon2id(password, kekSalt, opsLimit, memLimit_bytes)            // 32B
masterKey = secretbox_open(encryptedKey,        keyDecryptionNonce,        kek)        // 32B
secretKey = secretbox_open(encryptedSecretKey,  secretKeyDecryptionNonce,  masterKey)  // 32B
token     = box_seal_open(encryptedToken,       publicKey, secretKey)       // → base64url for header
```

### 3.3 Authenticator entities
```
GET /authenticator/key            → {encryptedKey, header}
authKey = secretbox_open(encryptedKey, header /*24B nonce*/, masterKey)     // 32B

GET /authenticator/entity/diff?sinceTime=0&limit=500 → {diff:[AuthEntity]}  // paginate on updatedAt (ms)
for each non-deleted entity:
  plaintext = secretstream_pull(encryptedData, header /*24B*/, authKey)     // single chunk; accept tag 0 or FINAL
  uri       = JSON.decode(plaintext as String)                             // "otpauth://..." (quoted)
  entry     = parse(uri)                                                    // totp | hotp | steam
```

### 3.4 libsodium mapping
| Step | libsodium | notes |
|---|---|---|
| KEK | `crypto_pwhash` | ALG `ARGON2ID13`, out 32, opslimit = `opsLimit`, **memlimit = `memLimit` bytes**, salt 16B |
| loginKey | `crypto_kdf_derive_from_key` | id 1, ctx `"loginctx"` (8B), len 32 → take 16 |
| key unwrap | `crypto_secretbox_open_easy` | nonce 24B, key 32B |
| token | `crypto_box_seal_open` | recipient pub/priv 32B, 48B overhead |
| entity | `crypto_secretstream_xchacha20poly1305_*` | header 24B, key 32B; auth payload may end on tag 0 |

These map to swift‑sodium's `PWHash`, `KeyDerivation`, `SecretBox`, `Box`, and
`SecretStream.XChaCha20Poly1305`.

---

## 4. OTP generation

- **Base32** decode of the `secret` (RFC 4648, no padding required).
- **TOTP** (RFC 6238): `T = floor(now/period)`, HMAC over big‑endian 8‑byte counter,
  dynamic truncation, `code mod 10^digits`. Algorithms SHA1/SHA256/SHA512 via CryptoKit
  `HMAC`. Defaults: SHA1, 6 digits, 30 s.
- **HOTP** (RFC 4226): same truncation, counter from the URI.
- **Steam**: SHA1, period 30, 5‑char output over the 26‑char Steam alphabet.
- Each `AuthEntry` exposes `code(at:)` and `secondsRemaining(at:)` for the UI ring.
- Verified against published RFC test vectors in `TOTPGeneratorTests`.

---

## 5. UX flows

### Menu bar
A key‑shaped template icon. The menu is rebuilt on open: a row per entry
(`Issuer (account) ····· 123 456`) sorted by issuer; clicking copies the live code to
the pasteboard (and shows a brief confirmation). Footer: **Quick Search…**, **Refresh**,
**Settings…**, **Check for Updates…**, **Sign Out**, **Quit**.

### Quick search (Spotlight‑style)
- Global Carbon hotkey (default **⌃⌥Space**, configurable) → centered borderless
  `NSPanel` that becomes key so the search field has focus.
- **Before** activating, we record `NSWorkspace.shared.frontmostApplication` as the
  paste target.
- Type → `SearchFilter` ranks entries (prefix matches first). ↑/↓ move selection, Return
  commits, Esc dismisses (first Esc clears a non‑empty query).
- On commit: order the panel out, re‑activate the recorded target app, then **type the
  current code** into the focused field via synthesized Unicode key events
  (`CGEventKeyboardSetUnicodeString`) — clean, no clipboard clobber. A preference can
  switch to **⌘V paste** (copies, then synthesizes ⌘V). Both require Accessibility.

### Login
On first launch (no Keychain session) the login window opens: email → choose **Email
code** (default) or **Password (SRP)** → optional **2FA code** → success persists the
token + authKey to Keychain and triggers the first sync. Sign Out clears the Keychain
and cache.

---

## 6. Permissions
- **Accessibility** (runtime TCC): only needed to *type/paste* into other apps. The
  menu‑bar copy flow needs nothing. Prompted lazily with a clear explanation; the app
  degrades to "copied to clipboard" if denied.
- **Global hotkey**: Carbon `RegisterEventHotKey` — no permission required.
- **Network**: outbound HTTPS to `api.ente.io` and `api.github.com` (update check).

---

## 7. CI/CD (mirrors Zap/MacDring)
- `.github/workflows/ci.yml`: `macos-14`, Xcode 16.2, `xcodebuild clean test`
  (`CODE_SIGNING_ALLOWED=NO`), packages auto‑resolved by xcodebuild.
- `.github/workflows/release.yml`: on `v*` tag → Release build, ad‑hoc `codesign -s -`,
  `.zip` + `.dmg`, GitHub Release with Gatekeeper‑bypass notes.
- `scripts/release.sh` bumps `MARKETING_VERSION` + README marker, commits, tags.
- `clean-build.sh` resets the Swift build service for local clean builds.

---

## 8. Testing
Pure logic is unit‑tested (Base32, TOTP/HOTP/Steam vectors, otpauth parsing, search
ranking, base64, preferences, hotkey spec, semantic version). Crypto round‑trips are
tested where deterministic. Login/SRP against the live server, global hotkey, the
floating panel, and CGEvent typing require a real signed‑in Mac and are manual‑verify
items (listed in README).

---

## 9. Known risks (cannot be exercised on the Linux CI box)
1. **SRP‑6a interop** is the single most fragile area (exact `k`/`x`/padding/encoding).
   It is isolated in `EnteSRP.swift`; email‑OTP is the fallback that needs none of it.
2. **swift‑sodium argon2 memlimit units** (bytes, not KiB) — encoded explicitly.
3. **Secretstream final‑tag leniency** for auth payloads (accept tag 0).
4. **SPM resolution** happens on the macOS runner (it has network); the Linux box can't.
