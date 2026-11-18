# Changelog

## Unreleased

- A hotkey press that arrives just as its shortcut is changed or turned off no
  longer fires the old action (Keycap 0.1.1).
- When GitHub's rate limit stops a Check for Updates, the alert says when to
  try again instead of showing a bare HTTP 403 (Updraft 0.2.0).
- Adopted Updraft for update checks. Version parsing is stricter and pre-releases
  order per SemVer, an unparseable version is reported instead of claiming you are up
  to date, and skipping a version survives a retag. A Check Now click during a
  background check is queued and reported instead of being dropped, the daily
  check runs at 90% of the interval so a punctual timer no longer skips a day, and
  Download only opens this repository's GitHub release pages.
- Adopted Keycap for the Quick Search hotkey. The recorder now refuses Shift-only
  and system-reserved shortcuts such as ⌘Q and ⌘Space, and a failed registration no
  longer leaves a Carbon event handler installed. A bare function key such as F12
  can now be recorded without a modifier, and Settings shows Keycap's key names,
  which follow the current keyboard layout (for example the return arrow instead
  of "Return").
