"""Pure ranking/filter for Quick Search — no GTK, so it's fully unit-testable.
Matches case- and diacritic-insensitively against the issuer, account, and combined
display name; ranks prefix matches above interior substring matches, then alphabetically."""

from __future__ import annotations

import unicodedata
from typing import Iterable, List, Optional, Sequence

from .entry import AuthEntry

__all__ = ["fold", "filter", "is_subsequence", "next_index"]


def fold(string: str) -> str:
    """Case- and diacritic-insensitive form (NFKD, combining marks stripped, casefolded)."""
    decomposed = unicodedata.normalize("NFKD", string)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def filter(entries: Sequence[AuthEntry], query: str, recent_ids: Iterable[str] = ()) -> List[AuthEntry]:
    """Returns the entries matching ``query``, best matches first.

    The query is split on whitespace and every token must match (AND), so "github alice"
    and "alice github" both find the GitHub/alice entry. A single token ranks prefix →
    substring → subsequence (fuzzy); a multi-token query ranks by its *worst* token so a
    real prefix hit always beats a fuzzy one.

    A token beginning with ``#`` is a **tag filter**: ``#work`` keeps only entries carrying a
    matching Ente tag, combinable with text ("github #work"). Tag tokens filter but don't
    contribute to the text rank.

    Ordering within a rank: pinned entries first, then recently used (``recent_ids``, most
    recent first), then name. An empty query returns everything in that order.
    """
    recent = list(recent_ids)
    recency = {entry_id: index for index, entry_id in reversed(list(enumerate(recent)))}

    def base_key(entry: AuthEntry):
        return (0 if entry.pinned else 1, recency.get(entry.id, len(recent) + 1), fold(entry.display_name))

    trimmed = query.strip()
    if not trimmed:
        return sorted(entries, key=base_key)

    all_tokens = fold(trimmed).split()
    tag_needles = [token[1:] for token in all_tokens if token.startswith("#") and len(token) > 1]
    text_tokens = [token for token in all_tokens if not token.startswith("#")]
    # A query of just "#" (or "#" plus whitespace) has nothing to match on.
    if not tag_needles and not text_tokens:
        return sorted(entries, key=base_key)

    scored = []
    for entry in entries:
        # Every tag needle must match one of the entry's tags (prefix or substring).
        if tag_needles:
            entry_tags = [fold(tag) for tag in entry.tags]
            if not all(any(tag.startswith(needle) or needle in tag for tag in entry_tags) for needle in tag_needles):
                continue

        issuer = fold(entry.issuer)
        account = fold(entry.account)
        display = fold(entry.display_name)

        worst = 0
        matched = True
        for token in text_tokens:
            rank = _token_rank(token, issuer, account, display)
            if rank is None:
                matched = False  # every text token must match somewhere
                break
            worst = max(worst, rank)
        if matched:
            scored.append((worst, base_key(entry), entry))

    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored]


def _token_rank(needle: str, issuer: str, account: str, display: str) -> Optional[int]:
    """How well one (already-folded) token matches: 0 issuer/display prefix, 1 account
    prefix, 2 substring anywhere, 3 in-order subsequence ("ghb" → "GitHub"), None none."""
    if issuer.startswith(needle) or display.startswith(needle):
        return 0
    if account.startswith(needle):
        return 1
    if needle in issuer or needle in account or needle in display:
        return 2
    if is_subsequence(needle, display) or is_subsequence(needle, account):
        return 3
    return None


def is_subsequence(needle: str, haystack: str) -> bool:
    """Whether every character of ``needle`` appears in ``haystack`` in order (not
    necessarily contiguously). Both are expected to be already folded."""
    iterator = iter(haystack)
    return all(any(char == candidate for candidate in iterator) for char in needle)


def next_index(count: int, current: Optional[int], down: bool) -> Optional[int]:
    """The next selection index when pressing up/down through ``count`` rows. Clamps at the
    ends; with no current selection, picks the first (down) or last (up)."""
    if count <= 0:
        return None
    if current is None or current < 0:
        return 0 if down else count - 1
    return min(max(current + (1 if down else -1), 0), count - 1)
