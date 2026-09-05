# Gans for Linux — Implementation Plan

This is the native **Linux / Ubuntu** port of Gans: a tray-resident agent that logs into
[Ente Auth](https://ente.io/auth/), syncs your end-to-end-encrypted 2FA codes, and puts
them one keystroke away. It ships as an architecture-independent **`.deb`**.

It mirrors [../PLAN.md](../PLAN.md) feature-for-feature; this document records what maps
to what on Linux and why, and pins the module interfaces so the pieces fit together.

---

## 1. Stack & guiding decisions

| Decision | Rationale |
|---|---|
| **Python 3.10+, GTK 3 via PyGObject** | GTK 3 is the toolkit the tray protocol (AppIndicator) supports; Ubuntu 22.04 and 24.04 both ship it. PyGObject gives full GTK/GLib/Gio access with no compile step, so the `.deb` is `Architecture: all` and builds anywhere (including the macOS-less CI box). |
| **Crypto via libsodium through PyNaCl** (`python3-nacl`) | Exactly the same C library the macOS build uses through `swift-sodium`, called through its low-level `nacl.bindings` — Argon2id (`crypto_pwhash`), `crypto_secretbox`, `crypto_box_seal`, XChaCha20-Poly1305 `secretstream`, and BLAKE2b with salt/personal for `crypto_kdf_derive_from_key`. Byte-exact by construction. |
| **Tray icon: Ayatana AppIndicator** (`gir1.2-ayatanaappindicator3-0.1`) with a `Gtk.StatusIcon` fallback | AppIndicator/StatusNotifierItem is what Ubuntu GNOME (via the preinstalled `ubuntu-appindicators` extension), KDE, XFCE, Cinnamon and MATE all render. The fallback covers desktops that only have a legacy XEmbed tray. |
| **Session secrets in the Secret Service** (`python3-secretstorage` → GNOME Keyring / KWallet) | The Linux equivalent of the macOS Keychain. Disk holds only Ente's *already-encrypted* entity blobs. Without a Secret Service the session lives in memory only — the password and keys are **never** written in plaintext. |
| **GDK runs on X11 (XWayland when the session is Wayland)** | Supports tray clipboard ownership and popup placement. XWayland does not expose native Wayland focus; Wayland sessions copy codes rather than injecting them. `GANS_GDK_BACKEND=wayland` overrides the toolkit backend, not this safety rule. |
| **Global hotkey: one backend at a time** — GNOME custom keybinding (gsettings) → XDG GlobalShortcuts portal → X11 `XGrabKey` → manual | GNOME (X11 *and* Wayland) has no API for grabbing keys except its own custom-shortcut mechanism, so Gans registers a "Gans Quick Search" custom shortcut running `gans toggle`. KDE Wayland and others get the portal; classic X11 desktops get `XGrabKey`. Exactly one backend is active so a press never toggles twice. |
| **Typing a code: X11 XTest** (`python3-xlib`) | Layout-independent typing on native X11, using the live keymap and a scratch keycode when needed. Wayland or missing XTest means clipboard-only delivery. |
| **App lock: polkit `auth_self`** (`pkcheck --allow-user-interaction`) | The Linux stand-in for Touch ID / device password: the desktop's polkit agent asks for the user's own password. Installed as `/usr/share/polkit-1/actions/ch.lkmc.gans.policy`. |
| **Single instance + CLI via `Gtk.Application`** (`ch.lkmc.Gans` on the session bus) | `gans toggle`, `gans settings`, `gans quit` are forwarded to the running instance through GApplication's D-Bus activation — no bespoke IPC. |
| **Toasts are desktop notifications** (`Gio.Notification`) | Native on every desktop, supports action buttons ("Try it", "Grant…"), and needs no window placement. |
| **Preferences in `$XDG_CONFIG_HOME/gans/preferences.json`** | Mirrors `UserDefaults`; trivially unit-testable with a temp path. |

Ubuntu 22.04 LTS (GTK 3.24, Python 3.10) is the floor; 24.04 LTS (GNOME 46, Wayland by
default) is the primary target.

---

## 2. Module map

```
linux/
├── bin/gans                          launcher (usable from the source tree and as /usr/bin/gans)
├── gans/
│   ├── version.py                    app_version(): VERSION file (stamped by packaging) → pbxproj MARKETING_VERSION → "0.0.0-dev"
│   ├── cli.py                        argv → GDK backend choice → GansApplication.run()
│   ├── log.py                        namespaced loggers (app/ente/hotkey/paste); never log secrets
│   ├── crypto.py                     EnteCrypto: libsodium primitives via nacl.bindings (exact)
│   ├── b64.py                        Base64: standard vs URL-safe (+padded) helpers
│   ├── base32.py                     RFC 4648 base32 decode (lenient)
│   ├── otp.py                        OTPAlgorithm + TOTPGenerator (TOTP / HOTP / Steam)
│   ├── entry.py                      AuthEntry + otpauth:// parsing (Ente escaping rules, codeDisplay)
│   ├── search.py                     SearchFilter (pure ranking; tags, frecency, subsequence)
│   ├── hotkeyspec.py                 HotkeySpec (GTK accelerator form; display / portal / X11 conversions)
│   ├── prefs.py                      Preferences + DeliveryMode (JSON-backed, observable)
│   ├── semver.py                     SemanticVersion
│   ├── ente/
│   │   ├── api.py                    EnteAPI (urllib, sync — call from worker threads)
│   │   ├── models.py                 DTO dataclasses
│   │   ├── srp.py                    EnteSRP (SRP-6a 4096 / SHA-256 / M1 = H(A|B|S))
│   │   ├── login.py                  EnteLogin (SRP → email-OTP → 2FA / passkey)
│   │   ├── keyunwrap.py              KeyUnwrap
│   │   └── vault.py                  EnteVault (session, sync, decrypt; observable)
│   ├── store/
│   │   ├── keyring.py                Keyring: SecretServiceKeyring | MemoryKeyring
│   │   └── cache.py                  EntityCache ($XDG_DATA_HOME/gans/entities.json)
│   ├── updates/
│   │   ├── github.py                 GitHubRelease + GitHubReleaseClient
│   │   └── checker.py                UpdateChecker (GTK dialogs)
│   ├── platform/
│   │   ├── session.py                display/session/desktop detection
│   │   ├── x11.py                    X11Session (XTest typing, active window, key grabs)
│   │   ├── hotkey.py                 HotkeyManager (backend selection)
│   │   ├── gnome.py                  GNOME custom keybinding via Gio.Settings
│   │   ├── portal.py                 org.freedesktop.portal.GlobalShortcuts
│   │   ├── clipboard.py              Clipboard (GTK; clear-after; password-manager hints)
│   │   ├── inject.py                 CodeInjector (type / paste / copy-only)
│   │   ├── applock.py                AppLock (polkit)
│   │   ├── autostart.py              LaunchAtLogin (XDG autostart)
│   │   └── honk.py                   🪿 synthesized honk (GStreamer, else paplay/aplay)
│   ├── ui/
│   │   ├── app.py                    GansApplication (Gtk.Application) — the AppDelegate
│   │   ├── css.py                    stylesheet
│   │   ├── tray.py                   StatusItemController (AppIndicator / StatusIcon)
│   │   ├── toast.py                  Toast (Gio.Notification)
│   │   ├── issuerchip.py             IssuerChip drawing + hue
│   │   ├── quicksearch_model.py      QuickSearchModel
│   │   ├── quicksearch.py            QuickSearchController + window
│   │   ├── login_model.py            LoginViewModel
│   │   ├── login.py                  LoginWindow
│   │   └── settings.py               SettingsWindow + HotkeyRecorder
│   └── data/                         tray icons (SVG), app icons, desktop file, polkit policy, metainfo
├── tests/                            unittest suite (RFC vectors, parsing, search, crypto KATs, SRP, Xvfb UI)
├── packaging/build-deb.sh            stages the tree and runs dpkg-deb → gans_<version>_all.deb
└── README.md
```

---

## 3. macOS → Linux mapping

| macOS | Linux |
|---|---|
| `NSStatusItem` + `NSMenu` (built on open) | `AyatanaAppIndicator3.Indicator` + `Gtk.Menu` (rebuilt whenever vault/lock state changes — SNI menus can't be built lazily) |
| Carbon `RegisterEventHotKey` | `HotkeyManager`: GNOME gsettings custom shortcut → portal → `XGrabKey` |
| `NSPanel` Quick Search (borderless, floating, non-activating) | undecorated `Gtk.Window`, type hint DIALOG, keep-above, skip taskbar; hides on focus-out |
| `CGEventPost` Unicode typing / ⌘V | XTest keystrokes / Ctrl+V |
| `NSPasteboard` + concealed/transient types | `Gtk.Clipboard` + `x-kde-passwordManagerHint=secret` target; clear-after if unchanged |
| Keychain (`kSecClassGenericPassword`) | Secret Service item, attributes `{application: gans, account: <key>}` |
| `~/Library/Application Support/Gans/entities.json` | `$XDG_DATA_HOME/gans/entities.json` (0600) |
| `UserDefaults` | `$XDG_CONFIG_HOME/gans/preferences.json` |
| `LAContext` (Touch ID / password) | polkit action `ch.lkmc.gans.unlock` (`auth_self`) via `pkcheck -u` |
| `SMAppService` launch at login | `~/.config/autostart/ch.lkmc.Gans.desktop` |
| `ToastPanel` | `Gio.Notification` with optional action |
| `NSSound` honk | GStreamer `playbin` (fallback `paplay` / `pw-play` / `aplay`) |
| `NSAlert` update dialog | `Gtk.MessageDialog`; "Download" opens the release page |
| `ente-cli://passkey` URL scheme | `.desktop` `MimeType=x-scheme-handler/ente-cli` → `Gtk.Application` `open` presents the login window |
| ⌘1–9 / ⌘C / ⌥↩ / ⌥ peek / ⌘ badges | Ctrl+1–9 / Ctrl+C / Alt+↩ / Alt peek / Ctrl badges |

---

## 4. Interface contracts

Everything below the `ui/` layer is plain Python with **no GTK import** so it stays unit-
testable headless. Blocking work (network, Argon2id) runs on worker threads; observers
are always invoked on the GTK main thread through the `dispatch` callable a component is
constructed with (`GLib.idle_add` in the app, a direct call in tests).

### 4.1 Core

```python
# crypto.py — all inputs/outputs are bytes; raises CryptoError(kind, what)
initialize() -> bool
derive_key_encryption_key(password: str, salt: bytes, mem_limit: int, ops_limit: int, out_len=32) -> bytes
derive_login_key(kek: bytes) -> bytes                      # 16 bytes: kdf(id=1, ctx="loginctx")[:16]
secret_box_open(cipher_text, nonce, key) -> bytes
sealed_box_open(cipher_text, public_key, secret_key) -> bytes
secret_stream_open_single_chunk(cipher_text, header, key) -> bytes  # any tag accepted

# b64.py
decode_standard(s) -> bytes | None ; encode_standard(b) -> str
decode_url_safe(s) -> bytes | None ; encode_url_safe(b) -> str ; encode_url_safe_padded(b) -> str

# base32.py
decode(s) -> bytes | None

# otp.py
class OTPAlgorithm(Enum): SHA1, SHA256, SHA512 ; OTPAlgorithm.lenient(str | None)
code(secret, counter, digits, algorithm) -> str ; totp(secret, time=None, period=30, digits=6, algorithm=SHA1)
steam(secret, time=None, period=30) ; seconds_remaining(time=None, period=30)

# entry.py
@dataclass AuthEntry(id, kind: Kind, issuer, account, secret, algorithm, digits, period,
                     pinned=False, is_trashed=False, note="", tags=())
  Kind = TOTP | HOTP(counter) | STEAM  (AuthEntry.Kind with .totp/.hotp/.steam constructors)
  display_name, code(at=None), formatted_code(at=None), seconds_remaining(at=None),
  is_time_based, fraction_remaining(at=None)
AuthEntry.parse(uri, id) -> AuthEntry | None

# search.py
fold(s) -> str ; filter(entries, query, recent_ids=()) -> list[AuthEntry]
is_subsequence(needle, haystack) -> bool ; next_index(count, current, down) -> int | None

# hotkeyspec.py
@dataclass(frozen=True) HotkeySpec(key: str, control=False, alt=False, shift=False, super_=False)
  DEFAULT = <Control><Alt>space ; accelerator ; from_accelerator(str) ; display_string ("Ctrl+Alt+Space")
  portal_trigger ("CTRL+ALT+space") ; x11_keysym_name ; to_json / from_json

# prefs.py
class DeliveryMode(Enum): TYPE, PASTE ; .label
class Preferences(path=None):  # path=None → $XDG_CONFIG_HOME/gans/preferences.json
  hotkey, delivery_mode, also_copy_when_typing, clear_clipboard_enabled, clear_clipboard_seconds,
  require_unlock, show_codes_in_quick_search, honk_on_copy, has_completed_onboarding
  recently_used_ids, usage_counts, record_usage(id), frecency_ranked_ids, most_used(limit=5)
  clipboard_clear_delay -> float | None ; on_change(callback) ; save()

# semver.py
class SemanticVersion: parse(str) -> SemanticVersion | None ; comparable; ZERO
```

### 4.2 Ente

```python
# ente/api.py
class APIError(Exception): kind in {"http","decoding","transport"}, status, body ; str() = user message
class EnteAPI(base_url=DEFAULT_BASE_URL):
  set_auth_token(token | None) ; get(path, query=[(name, value)], authenticated) -> dict
  post(path, body: dict, authenticated) -> dict ; encode_query_component(raw) -> str (static)
  CLIENT_PACKAGE = "io.ente.auth"

# ente/models.py — dataclasses with from_json(dict)
SRPAttributes, KeyAttributes, AuthorizationResponse(requires_two_factor, requires_passkey),
CreateSRPSessionResponse, AuthenticatorKey, AuthEntity, AuthEntityDiff

# ente/srp.py
class EnteSRP: begin(identity, salt, login_key) -> Session
class Session: srp_a_base64 ; compute_m1(server_b_base64) -> str ; verify_server_proof(m2_base64, m1_base64) -> bool

# ente/login.py
Step = Authorized(auth) | NeedsEmailCode() | NeedsTwoFactor(session_id) | NeedsPasskey(passkey_session_id, accounts_url)
class LoginError(Exception): SRP_UNAVAILABLE / PASSKEY_TIMED_OUT
class EnteLogin(api):
  start_srp(email, password) -> Step ; send_email_otp(email) ; verify_email_otp(email, code)
  verify_two_factor(session_id, code) ; wait_for_passkey_token(session_id, timeout=180, poll=2, is_cancelled=lambda: False)
  passkey_verification_url(accounts_url, session_id, client_package) ; sanitized_accounts_base(raw) ; DEFAULT_ACCOUNTS_URL

# ente/keyunwrap.py
@dataclass UnwrappedKeys(master_key, secret_key, token) ; unwrap(authorization, password) -> UnwrappedKeys

# ente/vault.py
class VaultState(Enum): SIGNED_OUT, LOADING, READY, ERROR
class EnteVault(api, keyring, cache, dispatch=lambda f: f()):
  entries, state, error_message, account_email, last_sync, session_expired, is_signed_in
  restore() ; complete_login(authorization, password, email) ; refresh() ; sign_out()   # blocking
  on_change(callback) ; keyring_persistent
```

### 4.3 Store / updates

```python
# store/keyring.py
class Keyring(Protocol): get(account) -> bytes | None ; set(account, data) ; remove(account) ; persistent: bool
open_keyring() -> Keyring    # SecretServiceKeyring if reachable else MemoryKeyring
# store/cache.py
class EntityCache(path=None): load() -> Snapshot(entities, since_time) ; save(snapshot) ; clear()
# updates/github.py
@dataclass GitHubRelease(tag_name, name, body, html_url, prerelease, draft, published_at) ; release_notes(max_length=600)
class GitHubReleaseClient(owner, repo): latest_release(include_prereleases) -> GitHubRelease
# updates/checker.py
class UpdateChecker(configuration: Configuration, prefs: Preferences, dispatch, client=None):   # client: a GitHubReleaseClient stand-in
  start() ; check_in_background() ; check_now() ; automatic_checks_enabled ; last_check_date ; is_checking ; on_change(cb)
  # is_checking holds until the outcome dialog closes (the macOS alert is modal), so a repeat check raises it rather than stacking one
```

### 4.4 Platform

```python
# platform/session.py
session_type() -> "x11" | "wayland" | "none" ; desktop() -> str ; is_gnome() ; has_x_display()
# platform/x11.py
class X11Session:  # lazily connects to $DISPLAY; safe to construct without one (available == False)
  available, has_xtest, can_inject ; active_window() -> int | None ; activate_window(window_id) ; window_name(window_id) -> str | None
  type_text(text) ; send_ctrl_v() ; close()
class X11HotkeyGrabber(x11, dispatch): register(spec, on_pressed) -> bool ; unregister()
# platform/hotkey.py
@dataclass HotkeyStatus(backend: str, ok: bool, detail: str)   # backend ∈ gnome | portal | x11 | none
class HotkeyManager(on_pressed, dispatch, x11=None): register(spec) -> HotkeyStatus ; unregister() ; status
# platform/gnome.py
GnomeKeybinding.available() ; install(spec, command="gans toggle") ; remove() ; current() -> HotkeySpec | None
# platform/portal.py
class GlobalShortcutsPortal(on_pressed): bind(spec) -> bool ; close()
# platform/clipboard.py
class Clipboard: copy(text, clear_after=None) ; clear_if_still(text)
# platform/inject.py
class DeliveryResult(Enum): DELIVERED, COPIED_ONLY
class CodeInjector(clipboard, x11): can_inject ; deliver(code, target_window, mode, also_copy, clear_clipboard_after=None, completion=None) -> DeliveryResult
# platform/applock.py
class AppLock(prefs, dispatch): is_locked, is_enabled ; lock_if_enabled() ; lock() ; authenticate(reason=…, completion=None) ; on_change(cb)
# platform/autostart.py
LaunchAtLogin.is_enabled() ; set(enabled)
# platform/honk.py
Honk.play()
```

### 4.5 UI

```python
# ui/app.py — GansApplication(Gtk.Application, application_id="ch.lkmc.Gans")
#   command line: gans | gans toggle | gans search | gans settings | gans quit | gans --version | gans ente-cli://…
# ui/tray.py — StatusItemController(vault, prefs, app_lock, on_quick_search, on_settings, on_login,
#              on_check_for_updates, on_unlock, on_quit) ; confirm_copy() ; rebuild()
# ui/quicksearch_model.py — QuickSearchModel (query, results, selected_id, tick, show_codes, peek, show_indices,
#              target_app_name, recent_ids, set_entries, reset, selected_entry, move_selection, on_change)
# ui/quicksearch.py — QuickSearchController(prefs, injector, x11, app): entries_provider, is_signed_in,
#              on_needs_login, is_locked, on_locked, on_committed ; show() hide() toggle() is_visible ; model
# ui/login_model.py — LoginViewModel(vault, api, dispatch): stage, email/password/code, is_busy, error_message,
#              sign_in_with_password(), send_email_code(), submit_code(), restart(), wait_for_passkey(), on_signed_in
# ui/login.py — LoginWindowController(vault, api, app): show()
# ui/settings.py — SettingsWindowController(prefs, vault, update_checker, hotkey_manager, app_lock, injector, app):
#              on_sign_in, on_hotkey_changed ; show()
# ui/toast.py — Toast(app): show(message, duration=2.4, action_title=None, action=None)
```

---

## 5. Security notes

- The password exists only for the duration of login; it is dropped once the key
  hierarchy is unwrapped. Plaintext TOTP secrets live only in memory.
- Secret Service items are created with `application=gans` attributes and human-readable
  labels so users can inspect/delete them in Seahorse or KWalletManager.
- Without a Secret Service, nothing secret touches disk: the session is memory-only and
  the tray/Settings say so.
- The clipboard copy carries `x-kde-passwordManagerHint=secret` and is cleared after the
  configured delay if it is still the most recent clipboard content.
- Logs never contain secrets, tokens, or codes.
- App lock fails closed: only explicit polkit authorization unlocks; missing agents,
  cancellation, and checker failures leave it locked.

## 6. Known limits

- On Wayland, the global hotkey needs a desktop-level mechanism (GNOME custom shortcut,
  the GlobalShortcuts portal, or a manual binding of `gans toggle`); `XGrabKey` only sees
  keys while an X11 window is focused.
- Typing requires native X11 with XTest. Wayland sessions copy codes to the clipboard;
  XWayland cannot reliably identify or activate native Wayland targets.
- Popup placement on Wayland is up to the compositor (GNOME centers new windows).
