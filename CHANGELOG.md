# Changelog

## Unreleased

- Adopted Keycap for the Quick Search hotkey. The recorder now refuses Shift-only
  and system-reserved shortcuts such as ⌘Q and ⌘Space, and a failed registration no
  longer leaves a Carbon event handler installed. A bare function key such as F12
  can now be recorded without a modifier, and Settings shows Keycap's key names,
  which follow the current keyboard layout (for example the return arrow instead
  of "Return").
