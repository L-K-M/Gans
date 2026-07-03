# fable-is-awesome.md — a deep review of Gans

A file-by-file read of the whole project (every Swift file, the tests, the CI/release
pipeline, the project config, and the screenshot). Claude 4.8 left this in genuinely good
shape — the crypto layer is careful, the SRP notes are excellent, and the architecture is
clean. What follows is what a second, fresh pair of eyes finds: real bugs (including two
crashers-in-waiting and a login failure for a whole class of email addresses), the
concrete cause of the popup sizing weirdness, escaping problems in exactly the place you
suspected, performance issues, and a pile of feature and delight ideas.

Each entry has an ID, severity, confidence, and a **Plan** line. Entries marked
`Plan: branch <X>` are implemented in their own PR branch; the branch grouping is chosen
so PRs touch disjoint files wherever possible.

Severity: 🔴 high · 🟠 medium · 🟡 low. Confidence is how sure I am the issue is real
*as described* given that I reviewed on Linux and could not run the app.

---

## 1. Bugs

### F-B1 🔴 Crash on duplicate query keys in an `otpauth://` URI
`Gans/Model/AuthEntry.swift:85` — `Dictionary(uniqueKeysWithValues:)` has a documented
precondition that keys are unique and **traps at runtime** on duplicates. A URI like
`otpauth://totp/x?secret=A&secret=A` (duplicated params happen in real-world exports and
re-imports), or even `?digits=6&DIGITS=8` (keys are lowercased first, manufacturing a
collision), crashes the app while decrypting the vault — at launch, every launch, until
the offending entry is removed server-side. Fix: `Dictionary(_:uniquingKeysWith:)`,
keeping the first value.
**Confidence: high · Plan: branch A**

### F-B2 🔴 Names are percent-decoded twice
`Gans/Model/AuthEntry.swift:93` — `URLComponents.path` is *already* percent-decoded;
calling `removingPercentEncoding` on it decodes a second time. An account or issuer whose
name contains a literal `%`-sequence (e.g. `Rate %20 Club`, stored encoded as `%2520`)
displays as `Rate   Club`. Names where the second decode fails (`100% Legit`) survive
only because `removingPercentEncoding` returns `nil` and the code falls back — i.e. it's
correct by accident. Fix: split the **raw** `percentEncodedPath` on `:` (so encoded
colons `%3A` inside names can't confuse the issuer/account split either), then decode
each part exactly once.
**Confidence: high · Plan: branch A**

### F-B3 🔴 `+` in query values becomes a literal plus — issuer names and codes disagree with Ente
`Gans/Model/AuthEntry.swift:85` — `URLComponents` decodes `%XX` but leaves `+` alone.
Ente's own web client parses the query with `URLSearchParams`, which form-decodes `+` as
a space. So an entry whose issuer was stored as `issuer=My+Bank` shows as `My+Bank` in
Gans and `My Bank` in Ente Auth. Worse, if a **secret** ever arrives with `+` as an
encoded space, Base32-decoding silently produces the wrong secret → wrong codes. Fix:
form-decode query values the way `URLSearchParams` does (replace `+` with space in the
*raw* value, then percent-decode).
**Confidence: high (semantics), medium (how often Ente data contains `+`) · Plan: branch A**

### F-B4 🔴 Plus-addressed emails can't log in
`Gans/Ente/EnteAPI.swift:53-57` — the SRP attributes request puts the email in the query
via `URLComponents.queryItems`, which leaves `+` unescaped (RFC 3986 allows it there).
Ente's server is Go; `url.ParseQuery` decodes `+` as a space, so `alice+ente@example.com`
arrives as `alice ente@example.com` → 404 "account may not exist" for a perfectly valid
account. Fix: percent-encode `+` in query values (`percentEncodedQueryItems`).
**Confidence: high · Plan: branch E**

### F-B5 🟠 Ente's `codeDisplay` metadata is ignored — deleted (trashed) entries still show up
`Gans/Model/AuthEntry.swift:79` — Ente Auth appends a `codeDisplay` query param (JSON)
carrying `trashed`, `pinned`, `note`, `tags`, `position`. Gans ignores it entirely, so an
entry the user moved to Ente's trash **keeps appearing (and typing codes!) in Gans**, and
pinned entries get no priority. Fix: parse `codeDisplay`; drop trashed entries; float
pinned entries to the top of Quick Search; keep the note for future display.
**Confidence: high · Plan: branch A**

### F-B6 🟠 Valid-but-unencoded URIs are silently dropped
`Gans/Model/AuthEntry.swift:80` — `URLComponents(string:)` returns `nil` for URIs
containing raw spaces or other unencoded characters (`otpauth://totp/My Bank:alice?...`),
which real exporters produce all the time. The entry silently vanishes from Gans with
only a log line. Fix: pre-encode invalid characters before parsing (a small, dependency-
free sanitizer; the `encodingInvalidCharacters:` initializer needs macOS 14.4+).
**Confidence: high · Plan: branch A**

### F-B7 🟠 Expired sessions are invisible
`Gans/Ente/EnteVault.swift:133-139` — when a refresh fails with HTTP 401 and cached
entries exist, the error is logged and the UI keeps showing (aging) codes forever. TOTP
codes keep generating from cached secrets, so the user may not notice for weeks that
nothing has synced. Fix: detect 401, set a visible "session expired" state, and offer
"Sign In Again…" in the menu.
**Confidence: high · Plan: branch F**

### F-B8 🟠 Likely per-open memory leak of every menu item
`Gans/MenuBar/StatusItemController.swift:5-14` — `ActionMenuItem` sets `target = self`.
AppKit retains an `NSMenuItem`'s target on modern macOS, so a self-targeting item is a
retain cycle: every `menuNeedsUpdate` allocates N items that are never deallocated. With
many entries and frequent menu opens this adds up. Fix: route the action through a small
separate trampoline object (safe under either retain semantic).
**Confidence: medium (depends on AppKit internals I can't run here; the fix is safe
either way) · Plan: branch C**

### F-B9 🟡 Rapid double-copy leaves the menu-bar checkmark stuck
`Gans/MenuBar/StatusItemController.swift:147-155` — `flashCopied` captures the *current*
button image and restores it 0.9s later. Copy twice within 0.9s and the second flash
captures the checkmark as "original", restoring… the checkmark, permanently (until the
lock state redraws it). Also races with the lock-state glyph. Fix: a generation counter
plus restoring via `configureButton(locked:)` instead of a captured image.
**Confidence: high · Plan: branch C**

### F-B10 🟡 Login failure can strand the vault in `.loading`
`Gans/Ente/EnteVault.swift:70-92` — `completeLogin` sets `state = .loading` first; if key
unwrap or the authenticator-key fetch throws, nothing resets the state. Mostly cosmetic
today (menus key off `isSignedIn`), but any future UI reading `state` inherits a lie.
Also: a fresh Ente account that has never used Ente **Auth** gets a 404 from
`/authenticator/key`, surfaced as "Not found (HTTP 404). The account may not exist." —
confusing and wrong. Fix: reset state on failure; map that 404 to "This account has no
authenticator data yet — add codes in Ente Auth first."
**Confidence: high · Plan: branch D**

### F-B11 🟡 Recording a hotkey like ⌘Q quits the app
`Gans/Settings/HotkeyRecorderView.swift:41-51` — the recorder captures chords in
`keyDown`, but menu key equivalents are dispatched *before* `keyDown`. While recording,
pressing ⌘Q quits Gans, ⌘C triggers Copy, etc. — those chords can never be recorded and
some actively fire. Esc doesn't cancel recording either. Fix: intercept in
`performKeyEquivalent` while recording; Esc cancels.
**Confidence: high · Plan: branch G**

### F-B12 🟡 `⌘⇧1`/`⌘⌥1` also trigger the quick-commit shortcut
`Gans/QuickSearch/QuickSearchPanel.swift:134` — the ⌘-digit check only asks whether ⌘ is
held, so any extra modifier still commits row N. Harmless-ish but sloppy chording; also
digits-behind-shift layouts (AZERTY) never match. Fix: require ⌘ as the only modifier.
**Confidence: high · Plan: branch B**

---

## 2. The Quick Search popup — sizing & search (your headline complaint)

### F-Q1 🔴 The panel is always max-height: ~84pt of dead space below short result lists
`Gans/QuickSearch/QuickSearchView.swift:59` — `ScrollView` is greedy: `.frame(maxHeight:
320)` makes the list area **exactly 320pt whenever there is at least one result**. Your
own screenshot shows it: five 44pt rows (≈236pt of content) floating above ~84pt of
empty panel. Fix: compute the content height exactly (rows are fixed 44pt + 2pt spacing
+ padding) and set `height = min(contentHeight, 320)` so the panel hugs its content.
**Confidence: high (the screenshot is the proof) · Plan: branch B**

### F-Q2 🔴 Panel height changes are unanchored and never repositioned
`Gans/QuickSearch/QuickSearchPanel.swift:104-119` — the panel is positioned once at
`show()` using whatever `fittingSize` says at that instant. As you type, results change
between "many" (381pt) and "no matches" (~126pt) and back; the hosted SwiftUI view
resizes the borderless window with **no controlled anchor**, so the search field / panel
edges jump around instead of the panel growing downward from a fixed top edge like
Spotlight. Fix: the controller owns the frame — observe result changes, compute the
target height deterministically, and `setFrame` keeping the top edge (and horizontal
center) pinned.
**Confidence: high that sizing is uncontrolled; the exact jump direction needs a real
Mac to observe · Plan: branch B**

### F-Q3 🟠 Multi-word queries barely work
`Gans/QuickSearch/SearchFilter.swift:36-48` — `"github alice"` only matches via the
last-resort subsequence rank (the literal string `"github alice"` is not a substring of
anything), and `"alice github"` doesn't match at all (subsequence requires order). Every
serious launcher treats the query as tokens AND-matched across fields. Fix: split the
query on whitespace; every token must match issuer/account/display; rank by the best
per-token match quality.
**Confidence: high · Plan: branch A (SearchFilter lives with the ranking changes)**

### F-Q4 🟠 No way to copy from Quick Search without typing
(previously noted as M5 in awesome.md, still true) — Return always injects into the
previous app. ⌥Return / ⌘C should copy instead — sometimes the target field is in the
same browser tab you need to click first, and typing-into-whatever-had-focus is exactly
wrong. Fix: ⌥Return and ⌘C copy the selected code; footer hint teaches it.
**Plan: branch B**

### F-Q5 🟠 Silent failure when Accessibility permission is missing
`Gans/QuickSearch/QuickSearchPanel.swift:183-192` + `CodeInjector.deliver` — commit
returns `.copiedOnly` when injection isn't permitted, and the caller ignores the result.
The user presses Return, the panel closes, **nothing appears in the target field**, and
nobody says the code actually went to the clipboard. Feels broken on first run. Fix: a
small transient toast ("Copied — grant Accessibility to type codes") when delivery
degrades.
**Confidence: high · Plan: branch B**

### F-Q6 🟡 ⌘1–⌘9 exists but is undiscoverable; no hover state; empty states are misleading
- Nothing in the UI hints that ⌘1…⌘9 commits row N. Fix: show index badges while ⌘ is held.
- Rows don't react to the mouse at all (no hover highlight, though they are clickable).
- A signed-in user with zero entries sees "Type to search your codes" — there's nothing
  to search. Say so ("No codes yet — add them in Ente Auth").
**Plan: branch B**

### F-Q7 🟡 The countdown ring ticks in 1-second jumps
`QuickSearchView.swift:137-163` — `fractionRemaining` is integer-seconds, animated with a
0.25s ease over a 1s timer: the ring stutters. Fix: sub-second fraction + a 1s linear
animation → a continuous, Spotlight-smooth sweep. (Delight-adjacent, but it's the thing
your eye rests on while waiting.)
**Plan: branch B**

### F-Q8 🟡 Dismissing the panel doesn't return focus
`QuickSearchPanel.swift:73-77` — on commit, the target app is explicitly re-activated;
on Esc/click-away, Gans stays active with no window. Fix: re-activate `previousApp` when
hiding without a commit.
**Confidence: medium (needs on-device confirmation of the focus limbo) · Plan: branch B**

---

## 3. Performance & stuttering

### F-P1 🔴 Argon2id runs on the main thread during login — twice
`Gans/Ente/EnteVault.swift:72` — `EnteVault` is `@MainActor`, and `completeLogin` calls
`KeyUnwrap.unwrap` synchronously: a memory-hard Argon2id derivation (server-specified
memLimit, typically ≥64 MiB) **freezes the UI** — spinner stops, beachball — for however
long the derivation takes. And it's the *second* derivation of the same KEK (SRP already
derived it inside the `EnteLogin` actor, off-main). Fix now: run unwrap off the main
actor. Fix later: reuse the KEK from the SRP step and halve login time.
**Confidence: high · Plan: branch D (off-main); KEK reuse documented, not implemented**

### F-P2 🟠 The whole vault is re-decrypted and re-parsed on every refresh, on the main actor
`EnteVault.swift:161-180` — every sync (even a no-change one) secretstream-opens and
re-parses every entity on the main actor. Per-entity cost is small; at hundreds of
entries it's a visible hitch each refresh. Reasonable fix when it matters: decrypt off
the main actor and/or only when the diff actually changed something.
**Confidence: medium (needs profiling on-device) · Plan: documented only**

### F-P3 🟡 Every visible row recomputes its HMAC every second — even masked
`QuickSearchView.swift:115` — the 1 Hz `tick` invalidates all rows; each computes
`entry.formattedCode(at:)` even when `showCode == false` renders dots. HMACs are
microseconds so this is energy, not stutter — but it's free to skip.
**Plan: branch B (compute code only when shown)**

### F-P4 🟡 Timers use `.common` mode at 1 Hz while the panel is open — fine — but the update timer never stops
`UpdateChecker.start()` schedules a daily repeating timer with 10% tolerance; fine.
The quick-search tick timer is correctly torn down on hide. No action needed; noted so
you don't hunt for it.

---

## 4. General issues

### F-G1 🟠 No periodic or event-driven sync — new codes never appear
`Gans/App/AppDelegate.swift` — the vault refreshes at launch, at login, and via the
manual "Refresh Now". A code added on your phone shows up in Gans **never**, unless you
remember the menu item. Fix: refresh when Quick Search opens (throttled), on a periodic
timer, and on wake from sleep.
**Confidence: high · Plan: branch H**

### F-G2 🟠 App lock never re-engages on its own
`Gans/Common/AppLock.swift` — `requireUnlock` locks at launch and via "Lock Now", but a
Mac that sleeps, locks its screen, or sits idle keeps Gans unlocked indefinitely. An
"auto-lock when the screen locks / after N minutes" option is the natural companion.
**Plan: documented (needs a product decision on defaults)**

### F-G3 🟡 Concurrent `refresh()` calls race harmlessly but wastefully
Two refreshes (menu + launch) run the full diff/pipeline twice and both save. Add an
in-flight guard. **Plan: branch F (one-line guard alongside the 401 work)**

### F-G4 🟡 `signOut()` doesn't cancel an in-flight refresh
The refresh can re-save the cache after `signOut` cleared it. Benign today (next login
clears again); worth a cancellation token eventually. **Plan: documented**

### F-G5 🟡 `.gitignore` claims `Package.resolved` is committed — it isn't
The comment says pinning is supply-chain hardening, but no `Package.resolved` exists in
the repo (deps are pinned to exact *versions* in the pbxproj, which a moved tag could
still change). Commit the resolved file (revision hashes) to make the claim true.
**Plan: documented (needs a Mac to generate the file honestly)**

### F-G6 🟡 Assorted small ones
- `EnteLogin.startSRP` never wipes `kek`/`loginKey` (the vault path wipes carefully;
  this path forgot). *(branch D)*
- `UpdateChecker` line 139 uses `NSLog` instead of the `Log` loggers. *(documented)*
- `SemanticVersion` compares pre-release identifiers lexically (`beta.10 < beta.9`) —
  fine for this repo's tags. *(documented)*
- `waitForPasskeyToken` swallows *all* errors via `try?` and will poll a dead network
  for the full 180s timeout. *(documented)*
- The login window keeps stale state when reopened mid-flow (e.g. abandoned at the
  email-code stage). *(documented)*

---

## 5. Visual & layout

### F-V1 🟠 The dead space below short result lists (see F-Q1) is *the* visual bug — fixed by branch B.

### F-V2 🟡 Long names truncate with no tooltip
Rows `lineLimit(1)` both lines; add `.help(...)`/truncation tooltips. **Plan: branch B**

### F-V3 🟡 The selected row's subtitle contrast is computed, the ring tint too — nice —
but the *unselected* secondary text on the vibrancy material can get muddy on loud
wallpapers (`.hudWindow` + `behindWindow`). Consider `withinWindow` blending or a
slightly higher-emphasis secondary. **Plan: documented (needs eyes on real hardware)**

### F-V4 🟡 The Settings window is fixed-size with `.formStyle(.grouped)`
At large accessibility text sizes the fixed `width: 460 / minHeight: 520` will clip.
Making it resizable is one style-mask flag. **Plan: documented**

### F-V5 🟡 Mask dots reveal the digit count
`maskedCode` prints `digits` dots — a 6- vs 8-digit distinction leaks. Deliberate design
(the comment says "a code lives here"); flagging that a constant 6 dots would be
strictly more private. **Plan: documented**

---

## 6. Missing features

### F-M1 Self-hosted / custom server URL
`EnteAPI` already takes a `baseURL`; there's just no UI. Ente's self-hosting community
is exactly the crowd that installs a third-party menu-bar client. One "Advanced" field
in Settings + persisting it. **Plan: documented — say the word and I'll add it**

### F-M2 Pinned & trashed awareness — covered by F-B5 (branch A).

### F-M3 Next-code preview near expiry
When <5s remain, show the *next* code dimmed next to the current one, so you can decide
to wait or paste-and-pray. (awesome.md's D6, still a good idea.) **Plan: documented**

### F-M4 Issuer icons / initial chips
Rows are text-only. Even a deterministic colored-initial chip (hash the issuer → hue)
would make scanning much faster, with zero bundled assets. **Plan: documented**

### F-M5 Notes & tags
`codeDisplay` carries them (branch A starts parsing); showing a note as a row tooltip
and filtering by `#tag` in the query would be cheap follow-ups. **Plan: documented**

### F-M6 Menu scalability
200 entries → a 200-row NSMenu. Cap at ~30 + "… 170 more — use Quick Search (⌃⌥Space)".
**Plan: branch C**

### F-M7 VoiceOver / accessibility pass
Rows are loose Text stacks; combine into one accessibility element per row with a
sensible label ("GitHub, alice, 23 seconds remaining"). **Plan: documented**

### F-M8 Localization readiness
All strings are hardcoded English. Fine for now; worth `String(localized:)` before any
audience growth. **Plan: documented**

---

## 7. Delightful / quirky ideas

### F-D1 🪿 Lean into the goose
The app is called *Gans*. When a code is committed, the menu-bar key could blink into a
tiny goose for half a second. Optional "Honk on copy" toggle (off by default, obviously)
using a bundled honk. Peak Untitled-Goose-Software. The kind of thing that gets a repo
starred.

### F-D2 ⌘-hold reveals everything
Holding ⌘ in Quick Search shows the index badges (F-Q6). Extend the idea: holding ⌥
could temporarily *reveal* masked codes ("peek"), releasing re-masks. Fast, discoverable,
no settings round-trip. *(Peek = documented; badges ship in branch B.)*

### F-D3 Type-ahead commit
If the query narrows to exactly one result, a subtle "↩ to fill into <AppName>" hint
appears under the field, naming the actual target app captured at open. Reinforces the
core magic trick. **Plan: documented**

### F-D4 "About to die" code protection
If the selected code has <2s left, briefly hold the commit until the fresh window ticks
over (with the ring flashing) — never paste a code that expires mid-submit. Needs care
to not feel laggy; combine with F-M3. **Plan: documented**

### F-D5 Per-entry usage sparkline
`recentlyUsedIDs` already exists; storing counts per entry would let Settings show a tiny
"most used" list — and Quick Search's empty-query ordering could learn frequency, not
just recency (frecency, the Firefox trick). **Plan: documented**

### F-D6 Wordless onboarding
First launch: the menu opens itself with a 3-line explainer and a "Press ⌃⌥Space" nudge;
first Quick Search commit without Accessibility shows the toast from F-Q5 with a "Grant…"
button. Two moments, zero windows. **Plan: partially in branch B (toast); rest documented**

---

## 8. What I'm implementing (branch → PR map)

| Branch | Contents | Files touched |
|---|---|---|
| A `claude/otpauth-parsing-and-search` | F-B1, F-B2, F-B3, F-B5, F-B6, F-Q3 + tests | `AuthEntry.swift`, `SearchFilter.swift`, `EnteVault.swift` (2 lines), tests |
| B `claude/quicksearch-panel-polish` | F-Q1, F-Q2, F-Q4, F-Q5, F-Q6, F-Q7, F-Q8, F-B12, F-P3, F-V2 | `QuickSearch/*` |
| C `claude/menu-bar-fixes` | F-B8, F-B9, F-M6 | `StatusItemController.swift` |
| D `claude/login-offmain-and-errors` | F-P1 (off-main), F-B10, F-G6 (kek wipe) | `EnteVault.swift` (completeLogin), `EnteLogin.swift` |
| E `claude/fix-plus-in-query` | F-B4 + test | `EnteAPI.swift`, new test |
| F `claude/session-expiry` | F-B7, F-G3 | `EnteVault.swift` (refresh), `StatusItemController.swift` (1 row) |
| G `claude/hotkey-recorder-capture` | F-B11 | `HotkeyRecorderView.swift` |
| H `claude/auto-refresh` | F-G1 | `AppDelegate.swift` |

Branches D and F both touch `EnteVault.swift` but in different functions
(`completeLogin` vs `refresh`); C and F both touch `StatusItemController.swift` in
different regions. Everything else is disjoint. All branches are cut from `main` after
this document lands, so the PRs are independent.

Caveat, stated plainly: this environment is Linux — I cannot build or run macOS code
here. Every change is written against macOS 13 APIs and the repo's conventions, and CI
(macOS 14 + Xcode 16.2, which runs on `claude/**` pushes and PRs) is the compile/test
gate. Findings marked medium confidence (F-B8, F-Q2's exact jump direction, F-Q8, F-V3)
deserve one minute of on-device eyeballing.

*— Fable, having fun as instructed* 🪿
