"""A lightweight semantic-version value for comparing an app's version against a release
tag. Parses the leading ``major[.minor[.patch[.…]]]`` number from strings like ``"1.2.3"``,
``"v1.2.3"``, or ``"1.4.0-beta.2"``, ignoring any build-metadata suffix and treating a
pre-release as sorting *below* its final release (``1.2.0-beta < 1.2.0``)."""

from __future__ import annotations

from functools import total_ordering
from typing import List, Optional


@total_ordering
class SemanticVersion:
    __slots__ = ("components", "prerelease", "original")

    def __init__(self, components: List[int], prerelease: Optional[str], original: str):
        self.components = components
        self.prerelease = prerelease
        self.original = original

    @classmethod
    def parse(cls, raw: str) -> Optional["SemanticVersion"]:
        """Parses ``raw``, returning ``None`` if it has no leading numeric version."""
        trimmed = (raw or "").strip()
        if not trimmed:
            return None
        text = trimmed
        if text[0] in ("v", "V"):
            text = text[1:]
        # Drop build metadata (`+…`), then split off a pre-release (`-…`).
        text = text.split("+", 1)[0]
        number_part, dash, pre = text.partition("-")
        parts = number_part.split(".")
        components: List[int] = []
        for part in parts:
            if not part.isdecimal():  # isdigit() admits e.g. '²', which int() rejects
                return None
            components.append(int(part))
        if not components:
            return None
        return cls(components, pre if (dash and pre) else None, trimmed)

    def __str__(self) -> str:
        return self.original

    def __repr__(self) -> str:
        return f"SemanticVersion({self.original!r})"

    def _padded(self, count: int) -> List[int]:
        return self.components + [0] * (count - len(self.components))

    def __lt__(self, other: "SemanticVersion") -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        count = max(len(self.components), len(other.components))
        left, right = self._padded(count), other._padded(count)
        if left != right:
            return left < right
        # Equal numbers: a pre-release is older than the final release.
        if self.prerelease is None and other.prerelease is None:
            return False
        if self.prerelease is None:
            return False  # final > pre-release
        if other.prerelease is None:
            return True   # pre-release < final
        return self.prerelease < other.prerelease

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return not (self < other) and not (other < self)

    def __hash__(self) -> int:
        # `1.2` == `1.2.0`, so trailing zeros must not change the hash.
        components = list(self.components)
        while len(components) > 1 and components[-1] == 0:
            components.pop()
        return hash((tuple(components), self.prerelease))


SemanticVersion.ZERO = SemanticVersion.parse("0")
