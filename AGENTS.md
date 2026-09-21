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

<!-- shared-rules:start -->

## Working practices

- Follow explicit task instructions over the default workflow below.
- Writing the code is not finishing the task. A task is finished when
  its changes are merged to main through a PR that passed CI and review,
  or when the user explicitly accepts a different end state.
- Before editing, inspect the branch and working tree, fetch remote updates,
  and fast-forward where safe. Never overwrite existing work to update.
- Resolve ambiguity before making consequential changes. State low-risk
  assumptions; ask when scope, safety, or expected behavior is unclear.
- Keep changes focused. Do not modify unrelated code, formatting, or comments.
- Prefer surgical edits over whole-file rewrites when the result is equivalent.
- Stage only intended files. Inspect the diff before committing.

## Communication

- Be concise, factual, and direct. Preserve necessary context and uncertainty.
- Avoid praise, motivational filler, emojis, and em dashes in new prose.
- Address the reader directly in user-facing copy.
- Report what was verified and what remains unverified. Never imply that an
  unavailable check passed.

## Code design

- Prefer early returns and shallow nesting. Separate logical blocks with
  blank lines.
- Use descriptive constants or enums for meaningful or repeated values.
  Use existing standard definitions for protocol/specification constants.
  Keep obvious, one-off values inline.
- Use enums for behavioral modes that would otherwise require ambiguous
  boolean arguments.
- Default members to private. Widen visibility only for required consumers,
  and review the change as an API design decision.
- Follow the repository's declared dependency boundaries. UI and controllers
  must use application services rather than directly accessing databases,
  subprocesses, sockets, or other low-level mechanisms.
- Encapsulate low-level mechanics behind domain-oriented interfaces.
- Reuse genuinely shared logic. Avoid speculative abstractions and layers
  that only forward calls.
- Prefer pure functions for business rules and immutable data where practical.
  Isolate side effects; document non-obvious state ownership or synchronization.
- Explain non-obvious intent, constraints, and tradeoffs in comments.
  Do not narrate obvious code. Add examples or diagrams when they clarify it.

## Validation and errors

- Validate untrusted input at entry points. Where practical, represent valid
  states in types and enforce persistent invariants in database schemas.
- Represent absence and failure explicitly.
- Use assertions for internal programming invariants, not external-input
  validation or required runtime error handling.
- Prefer explicit, actionable errors over silent failure or undocumented
  fallback. Document intentional recovery behavior.
- Never report a skipped or failed operation as successful.

## Bug fixes

1. Identify the root cause and define an observable success criterion.
2. Add a regression test and observe the relevant failure before fixing it.
3. Implement the fix and observe the test passing.
4. Check surrounding behavior for regressions and architectural consistency.

If an automated regression test is impractical, document the reproduction
and verification procedure. State any inability to reproduce the failure.

## Verification

- Run relevant tests and lint after changes.
- Choose coverage by affected behavior and risk, not patch size.
- Use integration or end-to-end tests for critical workflows and boundaries;
  test isolated business rules at the lowest effective level.
- Run broader suites for cross-cutting or high-risk changes, and the full
  required release checks before releasing.
- Validate the requested command, options, platform, and configuration.
  Unrelated green CI is not proof that the reported problem is fixed.
- Recheck after the final edit. Distinguish local checks from CI results.

## Commit messages

- Use a capitalized, imperative subject without a final period.
- Target 50 characters; never exceed 72.
- Separate the subject and body with one blank line.
- Wrap body text at 72 characters.
- Explain what changed and why. Leave implementation mechanics to the code.

## Implementation and review

Unless explicitly instructed otherwise:

1. Work on a focused branch and open a PR against main before reporting
   the task as done.
2. Inspect CI results and completed review feedback for the latest commit.
   A successful reviewer job does not mean the review found no problems.
3. Address important findings or explain why they do not apply. Handle minor
   findings according to the stopping rules below.
4. Evaluate each fix in the surrounding project, add regression coverage,
   and rerun affected checks before pushing.
5. Repeat until a stopping criterion is met.
6. Merge without asking again once the stopping criterion is met, required
   checks pass on the latest commit, and no unresolved blockers or required
   human review requests remain.

### Reviewer context limits

The automated PR reviewer does not see the user's original prompt or
conversation. It may suggest changes that go against or beyond what the
user asked for. Do not implement such suggestions. Note each conflict and
report it to the user at the end of the thread.

### Automated review stopping rules

Judge findings by verified impact, not the reviewer's severity label.
Important findings concern correctness, security, data loss, broken builds,
or materially degraded behavior/performance.

Track completed review rounds and consecutive rounds without important
findings. Reruns of the same revision and integration failures do not count.

- No applicable actionable feedback: finish immediately.
- First minor-only round: optionally fix worthwhile, low-risk findings.
  Do not manufacture another push merely to obtain another review.
- Two consecutive rounds without important findings: stop responding to
  automated nitpicks, even if actionable minor suggestions remain.
  Defer worthwhile leftovers rather than continuing the cycle.
- A confirmed important finding resets the minor-only streak. Address it
  and verify the fix before continuing.

After ten completed rounds, enter stabilization:

- Stop optional cleanup, refactoring, and nitpick fixes.
- One completed review without confirmed important findings is sufficient
  to finish, even if minor suggestions remain.
- Continue only for confirmed important defects. If resolving them stalls,
  report the blockers rather than continuing indefinitely.

These limits end optional automated-feedback work. They do not waive
confirmed blockers, unresolved human review requests, or required checks.

### Reviewer integration failures

After two consecutive reviewer-integration failures, stop and report the
review gap. Do not treat failures as approval. An explicit user instruction
may waive review; report that waiver rather than claiming review passed.

## Ending a task

- A task ends with its changes merged to main — not with code written,
  and not with a PR merely opened. An open PR is work in progress:
  monitor CI on the latest commit, address review findings per the
  stopping rules, and merge once the criteria are met.
- Never finish with uncommitted changes or unpushed commits in the
  worktree. Commit, push, and open or update the PR first.
- If a step is impossible (missing push access, CI failure, reviewer
  outage), report the exact blocker instead. Never present unreviewed or
  unmerged work as finished.
- Before finishing, confirm: the requested behavior is implemented
  without unrelated changes; relevant checks pass on the latest code;
  important review findings are addressed or rejected with reasons;
  deferred suggestions, remaining risks, and validation gaps are
  disclosed.
- The final response states where the work stands: branch, PR, CI
  status, review rounds completed, and whether it is merged.

<!-- shared-rules:end -->

