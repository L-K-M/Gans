# awesome.md — a review of Gans, with ideas

A close read of the whole codebase (38 source files) after CI went green. Findings are
grouped into **bugs**, **general issues**, **missing features**, and **delightful ideas**.
Each entry has a confidence/priority tag. The ones I implemented in this pass are marked
✅ and summarized at the bottom.

Gans is in genuinely good shape: the Ente protocol work (SRP-6a, Argon2id, the secretbox /
sealed-box / secretstream chain) is careful and well-commented, the menu-bar agent
plumbing is idiomatic, and the update checker / semver / GitHub client trio is clean and
reusable. Most of what follows is polish, hardening, and delight rather than rescue.

---

## Bugs

### B1 — TOTP digit count can overflow and crash ✅ `high`
`TOTPGenerator.code` computes `let modulus = UInt32(pow(10.0, Double(digits)))`. `digits`
comes straight from the `otpauth://` URI via `Int(query["digits"] ?? "")` with no upper
bound. For `digits >= 10`, `10^digits` exceeds `UInt32.max` and the `UInt32(...)`
conversion **traps at runtime** — a malformed/hostile entry can crash code generation.
Fix: clamp digits to a sane range (1...9; RFC 4226/6238 use 6–8) at parse time and defend
in the generator.

### B2 — Sync diff pagination can loop forever ✅ `medium`
`EnteVault.refresh` advances the cursor with `cursor = max(cursor, updatedAt)` and loops
while `diff.count == limit`. If a full page ever comes back where no entity advances the
cursor (e.g. every row shares the page's `updatedAt`, or `updatedAt` is missing), the same
window is requested forever. Fix: break if a full page failed to advance the cursor.

### B3 — HOTP codes never advance `low / by-design`
`AuthEntry.code` for `.hotp` always uses the stored counter, so the shown HOTP code never
changes. This is *arguably* correct for a read-only mirror of Ente (the server owns the
counter), but it means HOTP entries are effectively non-functional for login. Documented
as a known limitation rather than "fixed", since incrementing locally would desync from
Ente and there's no write path. Worth a note in the README.

---

## General issues

### G1 — Clipboard is never cleared or restored ✅ `high (security)`
Copying from the menu, or `paste` delivery, leaves the plaintext OTP on the general
pasteboard indefinitely, and `paste` mode clobbers whatever the user had copied. Other
authenticators clear the clipboard after a short delay. Fix: an opt-in "clear clipboard N
seconds after copying a code" that only clears if the clipboard *still* holds our code
(so we never nuke something the user copied afterwards).

### G2 — Quick Search doesn't dismiss when it loses focus ✅ `medium`
The panel only hides on Esc or commit. Click another app (or another window) and it stays
floating. Spotlight-style panels dismiss on resign-key. Fix: hide on `windowDidResignKey`.

### G3 — Settings Accessibility status is read once and goes stale ✅ `medium`
`hasAccessibility` is captured in `@State` at view-creation. After the user grants the
permission in System Settings and returns, the row still shows the warning until the
window is reopened. Fix: re-check when the app/window reactivates.

### G4 — Dead `copyOnMenuSelect` defaults key ✅ `low`
`Preferences.Key.copyOnMenuSelect` is declared but never read or written. Remove it.

### G5 — `Base64.decodeURLSafe` is unused `low / keep`
Not called anywhere (the token is forwarded as-is). It's the logical pair of
`encodeURLSafe`, so keeping it is defensible; flagging only for awareness.

### G6 — Menu codes don't refresh while the menu stays open `low`
`menuNeedsUpdate` renders codes at open time; if the menu is held open across a 30s
boundary the codes go stale. AppKit makes live-updating an open `NSMenu` awkward; low
priority given Quick Search is the primary path and ticks live.

### G7 — `Keychain` items use the file keychain, not data-protection `low`
No `kSecUseDataProtectionKeychain`. Fine today; worth considering for consistency with
modern macOS keychain semantics.

---

## Missing features

### M1 — No expiry countdown in Quick Search ✅ `high`
`QuickSearchView`'s row doc says "live code + countdown on the right" and
`AuthEntry.secondsRemaining` exists, but **no countdown is actually shown**. A code can be
0.5s from rotating with no warning. Fix: a small countdown ring next to each time-based
code that shifts color as it nears expiry. (HOTP has no time component, so no ring.)

### M2 — No keyboard quick-select in Quick Search ✅ `medium (delight)`
You can only arrow + Return. Add ⌘1…⌘9 to instantly pick and commit the Nth result —
muscle-memory speed for "the one I always use".

### M3 — Search is prefix/substring only ✅ `medium (delight)`
`SearchFilter` matches prefix and substring but not subsequence, so "ghb" won't find
"GitHub". Add a lowest-priority subsequence (fuzzy) tier so loose typing still finds
things, without outranking real prefix/substring hits.

### M4 — No notion of "recently used" ✅ `medium (delight)`
Every launch of Quick Search shows the same alphabetical list. The codes you actually use
should float up. Track recent usage and bias ordering toward it (empty query → recent
first; ties → recent first).

### M5 — Can't copy from Quick Search without injecting `low`
There's no "copy and don't type" affordance from the panel (e.g. ⌘C / ⌥Return). Minor;
the menu already copies.

### M6 — No per-entry favicon / issuer glyph `low (delight)`
Rows are text-only. A small SF-Symbol or issuer initial chip would make scanning faster.
Left out to avoid bundling an icon set.

---

## Delightful / quirky ideas

- **D1 — Countdown ring** (≡ M1): a thin circular progress that depletes over the period
  and tints amber/red in the last few seconds. ✅
- **D2 — ⌘-number quick pick** (≡ M2). ✅
- **D3 — Fuzzy subsequence search** (≡ M3). ✅
- **D4 — Recently-used boosting** (≡ M4). ✅
- **D5 — Auto-clearing clipboard** (≡ G1) — both a security win and a quietly delightful
  "it cleaned up after itself" moment. ✅
- **D6 — "About to expire" guard**: optionally, committing a code with <N seconds left
  could grab the *next* window's code instead, so you never paste a code that dies
  mid-submit. (Not implemented — needs UX thought to avoid surprising users.)
- **D7 — Menu-bar glyph countdown**: subtly animate the menu-bar key icon with the active
  period. (Not implemented — fiddly and easy to make annoying.)

---

## Implemented in this pass

| Item | What changed |
|------|--------------|
| B1 ✅ | Clamp OTP digits to 1...9 at parse and in `TOTPGenerator.code`; new tests. |
| B2 ✅ | Break the sync loop if a full diff page doesn't advance the cursor. |
| G1 / D5 ✅ | Opt-in clipboard auto-clear (default 30s) that only clears if our code is still there; Settings toggle + interval. |
| G2 ✅ | Quick Search hides on resign-key. |
| G3 ✅ | Settings re-checks Accessibility on app reactivation. |
| G4 ✅ | Removed the dead `copyOnMenuSelect` key. |
| M1 / D1 ✅ | Countdown ring on time-based Quick Search rows. |
| M2 / D2 ✅ | ⌘1…⌘9 quick-select in Quick Search. |
| M3 / D3 ✅ | Subsequence fuzzy tier in `SearchFilter`; new tests. |
| M4 / D4 ✅ | Recently-used tracking in `Preferences`, biasing Quick Search ordering; new tests. |

Deferred (lower confidence or needs product/UX calls): B3, G5–G7, M5–M6, D6–D7.
