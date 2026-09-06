"""Completion stats from osucomplete.org (with manual override fallback).

The profile page is server-rendered, so the three numbers can be scraped
directly: maps passed, maps left, % completed. If the fetch fails, the
caller falls back to manually configured numbers (or skips the outro).
"""

from __future__ import annotations

import logging
import re
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)


class CompletionError(Exception):
    pass


@dataclass
class CompletionStats:
    passed: str  # display string, e.g. "1,133"
    left: str  # e.g. "147,163"
    pct: str  # e.g. "0.73%"

    @property
    def line1(self) -> str:
        return f"{self.passed}/{self.left}"

    @property
    def line2(self) -> str:
        return self.pct


# The page embeds a stale "% maps passed X.XX%" JS comment before the real
# figures, so plain ([\d,]+) would catch the decoy's "0". Anchor on a real
# thousands value NOT followed by more number chars, a dot, or a % sign.
_PATTERNS = {
    "passed": re.compile(r"maps\s+passed\s*([\d,]+)(?![\d.,%])", re.IGNORECASE),
    "left": re.compile(r"maps\s+left\s*([\d,]+)(?![\d.,%])", re.IGNORECASE),
    "pct": re.compile(r"%\s*completed\s*([\d.]+%)", re.IGNORECASE),
}


def _text(html: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", no_tags)


def fetch_completion(profile_url: str, timeout: int = 30) -> CompletionStats:
    req = urllib.request.Request(profile_url, headers={"User-Agent": "osu-completionist-pipeline/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = _text(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        raise CompletionError(f"osucomplete fetch failed: {exc}") from exc
    found = {}
    for key, rx in _PATTERNS.items():
        m = rx.search(text)
        if not m:
            raise CompletionError(f"osucomplete page missing {key!r} (layout changed?)")
        found[key] = m.group(1).replace(",", "") if key != "pct" else m.group(1)
    # Re-add thousands separators for display.
    passed = f"{int(found['passed']):,}"
    left = f"{int(found['left']):,}"
    return CompletionStats(passed=passed, left=left, pct=found["pct"])


def from_manual(passed: str | None, left: str | None, pct: str | None) -> CompletionStats | None:
    if not (passed and left and pct):
        return None
    return CompletionStats(passed=passed, left=left, pct=pct)
