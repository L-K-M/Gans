# Gans for Linux

The native Linux / Ubuntu build of Gans: a tray-resident client for
[Ente Auth](https://ente.io/auth/) — your end-to-end encrypted 2FA codes, one keystroke
away. It ships as an architecture-independent `.deb`.

- **Quick Search** — press the global hotkey (default **Ctrl+Alt+Space**) to open a
  Spotlight-style search field anywhere. Type to filter, press **Return**, and Gans
  **types the current code straight into whatever field had focus**.
- **Tray menu** — click the key icon to see every entry; click an entry to copy its
  current code to the clipboard.

Everything is end-to-end encrypted with the same libsodium primitives the macOS build
uses (see [PLAN.md](PLAN.md) for the design and the macOS → Linux mapping).

## Install

Download `gans_<version>_all.deb` from
[Releases](https://github.com/L-K-M/Gans/releases/latest), then:

```bash
sudo apt install ./gans_<version>_all.deb    # resolves the dependencies from the archive
gans                                          # or launch "Gans" from your app menu
```

Supported: Ubuntu 22.04 LTS and newer (and other Debian-based distributions with
Python ≥ 3.10 and GTK 3). Ubuntu 24.04 on GNOME/Wayland is the primary target.

On first launch Gans opens the sign-in window (email + password; email code, TOTP 2FA
and passkeys are supported) and puts a key icon in the tray. A notification names the
hotkey and offers to try it.

### GNOME: the tray icon

GNOME shows tray icons only through the *AppIndicator and KStatusNotifierItem Support*
extension. Ubuntu preinstalls and enables it; on vanilla GNOME install
`gnome-shell-extension-appindicator` and enable it in Extensions. KDE, XFCE, Cinnamon
and MATE need nothing extra.

### The hotkey

Gans registers Ctrl+Alt+Space itself using whichever mechanism the session offers, and
Settings → Quick Search shows which one is active:

| Session | Mechanism |
|---|---|
| GNOME (X11 or Wayland) | A custom shortcut "Gans Quick Search" running `gans toggle`, added to Settings → Keyboard → Custom Shortcuts. Changing the hotkey in Gans updates it; signing out of GNOME's shortcut list removes it. |
| KDE Plasma (Wayland) and other portals | The XDG **GlobalShortcuts** portal (the desktop asks you to confirm the binding once). |
| Classic X11 desktops (XFCE, MATE, Cinnamon, i3…) | An X11 key grab. |
| Anything else | Bind the command `gans toggle` to a shortcut in your desktop's keyboard settings — Settings in Gans shows the exact instructions. |

`gans toggle`, `gans search`, `gans settings` and `gans quit` are forwarded to the
running instance over D-Bus, so a shortcut can simply run `gans toggle`.

### Typing codes into other apps (permissions)

Linux needs no Accessibility permission. Gans types codes through the X server (XTest):

- **X11 sessions:** works out of the box.
- **Wayland sessions (GNOME, KDE):** Gans runs on XWayland by default, and both Mutter
  and KWin route XTest input to the focused window — including native Wayland apps — so
  typing works there too. Where no X server is reachable, the code is copied to the
  clipboard instead and a notification says so.
- `GANS_GDK_BACKEND=wayland` forces native Wayland (typing and window placement are then
  up to the compositor).

Quick Search keys: **↑/↓** select · **Return** type the code · **Alt+Return** copy
instead · **Ctrl+C** copy · **Ctrl+1–9** pick the Nth result · hold **Alt** to peek at
masked codes · **Esc** clear, then dismiss · `#tag` filters by Ente tag.

### Where things live

| What | Where |
|---|---|
| Session token + authenticator key | The **Secret Service** (GNOME Keyring, KWallet, KeePassXC…) as items labelled "Gans — …". Without one, the session is kept in memory only and you sign in again after quitting — Settings and the tray say so. |
| Encrypted entity cache | `~/.local/share/gans/entities.json` (Ente's encrypted blobs only, mode 0600) |
| Preferences | `~/.config/gans/preferences.json` |
| Launch at login | `~/.config/autostart/ch.lkmc.Gans.desktop` (Settings → Startup) |
| App lock | polkit action `ch.lkmc.gans.unlock` — the system dialog asks for *your own* password (Settings → Security) |

The password is used once to unwrap the key hierarchy and then dropped. Plaintext TOTP
secrets exist in memory only. Nothing secret is logged. Copied codes are cleared from the
clipboard after 30 s (configurable) if nothing else was copied since, and are marked so
KDE's clipboard history doesn't keep them.

## Build & run from source

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
                 python3-nacl python3-xlib python3-secretstorage gir1.2-gstreamer-1.0
cd linux
./bin/gans                 # run from the source tree (GANS_DEBUG=1 for verbose logs)
./bin/gans --help
```

### Tests

```bash
cd linux
python3 -m unittest discover -s tests -t . -v
```

The suite covers RFC 4226/6238 vectors, otpauth parsing, search ranking, SRP against a
simulated go-srp server, libsodium interop vectors (regenerate with
`tests/tools/gen_vectors.c`), the vault sync pipeline against a fake Ente API, and —
when `Xvfb`, `xdotool` and `dbus-daemon` are installed — XTest typing, the clipboard,
the X11 hotkey grab, the Quick Search / login / settings windows, the tray menus, the
package build, and an end-to-end launch of the app. Each GUI test module starts its own
private Xvfb display and session bus.

### Package

```bash
cd linux
packaging/build-deb.sh                 # → dist/gans_<version>_all.deb
packaging/build-deb.sh --version 1.6.0 # override the version (CI passes the tag)
```

The version defaults to `MARKETING_VERSION` in `Gans.xcodeproj` so the Linux and macOS
builds stay in step; the release workflow derives it from the git tag. The script stages
the tree with fixed modes, runs `dpkg-deb --root-owner-group` (no fakeroot needed) and
`lintian` when available. App icons are copied from the macOS asset catalogue at build
time. See [../CICD.md](../CICD.md) for the CI and release pipeline.

## Troubleshooting

- **No tray icon on GNOME** — enable the AppIndicator extension (above).
- **The hotkey does nothing** — open Settings → Quick Search; it names the backend in use
  or tells you to bind `gans toggle` manually. On GNOME check Settings → Keyboard → Custom
  Shortcuts for "Gans Quick Search".
- **Codes are copied instead of typed** — no X server is reachable (native Wayland
  without XWayland, or `GANS_GDK_BACKEND=wayland`). Paste with Ctrl+V, or switch "On
  select" to *Paste the code*.
- **"No keyring available"** — install and log into a Secret Service provider
  (`gnome-keyring` on GNOME/Ubuntu, KWallet on KDE, or KeePassXC with its Secret Service
  integration enabled), then sign in again.
- **Logs** — `GANS_DEBUG=1 gans` in a terminal prints debug output; nothing secret is
  ever logged.

## Layout

`linux/gans` is the Python package (headless-testable core, `platform/` for X11/hotkey/
clipboard/polkit/autostart, `ui/` for the GTK 3 windows and tray), `linux/tests` the
unittest suite, `linux/packaging` the `.deb` build. [PLAN.md](PLAN.md) is the design doc
with the interface contracts.
