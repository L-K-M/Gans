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

### B3 — HOTP codes never advance `low / by-design`
`AuthEntry.code` for `.hotp` always uses the stored counter, so the shown HOTP code never
changes. This is *arguably* correct for a read-only mirror of Ente (the server owns the
counter), but it means HOTP entries are effectively non-functional for login. Documented
as a known limitation rather than "fixed", since incrementing locally would desync from
Ente and there's no write path. Worth a note in the README.

---

## General issues

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

### M5 — Can't copy from Quick Search without injecting `low`
There's no "copy and don't type" affordance from the panel (e.g. ⌘C / ⌥Return). Minor;
the menu already copies.

### M6 — No per-entry favicon / issuer glyph `low (delight)`
Rows are text-only. A small SF-Symbol or issuer initial chip would make scanning faster.
Left out to avoid bundling an icon set.

---

## Delightful / quirky ideas

- **D6 — "About to expire" guard**: optionally, committing a code with <N seconds left
  could grab the *next* window's code instead, so you never paste a code that dies
  mid-submit. (Not implemented — needs UX thought to avoid surprising users.)
- **D7 — Menu-bar glyph countdown**: subtly animate the menu-bar key icon with the active
  period. (Not implemented — fiddly and easy to make annoying.)