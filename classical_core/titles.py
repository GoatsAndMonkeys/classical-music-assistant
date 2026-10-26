"""Tier 3: track-title and album parsing (PLAN.md §5.4).

`parse_title()` splits one title into (work, movement number, movement name). `shared_prefix_runs()`
finds runs of consecutive tracks on one album that share a work prefix with increasing movement
numbers (the method of MA discussion #6151), which also covers localized titles such as
"Symphonie Nr. 1 C-dur, op. 21: 1. Adagio molto". Tier 3 never looks beyond one album.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from .catalogue import parse_catalogues, parse_key, work_type
from .text import norm_key

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
          "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18,
          "xix": 19, "xx": 20}
# "I. Allegro", "1. Allegro", "IV - Finale", "3) Presto", "No. 2: Andante" (a bare "No. 2" is a number,
# not a movement, in a work title like "Symphony No. 2")
_MOVEMENT = re.compile(
    r"^\s*(?:(?:mvt\.?|movement|satz)\s*)?(?P<num>[ivxlc]{1,6}|\d{1,2})\s*(?:[.)]|\s-\s|:)\s*(?P<name>\S.*)$",
    re.IGNORECASE,
)
# separators between work and movement, strongest first. ": " needs the space so "Hob. I:104" survives.
_SEPARATORS = (re.compile(r":\s+"), re.compile(r"\s+[-–—]\s+"), re.compile(r"\s+/\s+"))


@dataclass(frozen=True, slots=True)
class ParsedTitle:
    work: str
    movement_no: int | None
    movement_name: str | None
    composer_hint: str | None = None


def movement_number(token: str) -> int | None:
    t = token.strip().lower()
    if t.isdigit():
        return int(t)
    return _ROMAN.get(t)


def parse_movement(text: str) -> tuple[int | None, str]:
    """'III. Menuetto' -> (3, 'Menuetto'); 'Menuetto' -> (None, 'Menuetto')."""
    m = _MOVEMENT.match(text)
    if m:
        n = movement_number(m.group("num"))
        if n is not None:
            return n, m.group("name").strip()
    return None, text.strip()


def parse_title(title: str | None) -> ParsedTitle | None:
    """Split 'Work: I. Allegro' / 'Work - 2. Andante' / 'Work / Movement'. None if there is no split."""
    if not title:
        return None
    t = title.strip()
    for rx in _SEPARATORS:
        m = rx.search(t)
        if not m:
            continue
        work, rest = t[: m.start()].strip(), t[m.end():].strip()
        if len(work) < 3 or not rest:
            continue
        n, name = parse_movement(rest)
        if n is None and rx.pattern == r":\s+" and _looks_like_name_prefix(work, rest):
            # "Haydn: Trumpet Concerto in E flat, Hob. VIIe:1" -> a composer prefix, not a movement split
            inner = parse_title(rest)
            if inner is not None:
                return ParsedTitle(inner.work, inner.movement_no, inner.movement_name, composer_hint=work)
            return ParsedTitle(work=rest, movement_no=None, movement_name=None, composer_hint=work)
        return ParsedTitle(work=work, movement_no=n, movement_name=name or None)
    return None


def _looks_like_name_prefix(head: str, rest: str) -> bool:
    """A short, number-free head before ':' whose remainder carries the work's identity."""
    if any(ch.isdigit() for ch in head) or len(head.split()) > 4:
        return False
    return bool(parse_catalogues(rest) or parse_key(rest) or work_type(rest) != "Other")


def _common_prefix(a: str, b: str) -> str:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return a[:n]


def shared_prefix_runs(titles: list[str], min_len: int = 8) -> list[tuple[int, int, str]]:
    """Runs (start, end_exclusive, work prefix) of ≥2 consecutive titles sharing a prefix with
    increasing movement numbers after it. Titles already split by `parse_title` are handled there."""
    runs: list[tuple[int, int, str]] = []
    i = 0
    while i < len(titles) - 1:
        prefix = _common_prefix(titles[i], titles[i + 1])
        cut = _cut_prefix(prefix)
        if cut is None or len(cut) < min_len:
            i += 1
            continue
        j = i + 1
        nums = [_after_number(titles[i], cut), _after_number(titles[i + 1], cut)]
        while j + 1 < len(titles) and titles[j + 1].startswith(cut):
            nums.append(_after_number(titles[j + 1], cut))
            j += 1
        if all(n is not None for n in nums) and all(b > a for a, b in itertools.pairwise(nums)):
            runs.append((i, j + 1, cut.rstrip(" :,-–—/")))
            i = j + 1
        else:
            i += 1
    return runs


def _cut_prefix(prefix: str) -> str | None:
    """Trim a raw common prefix back to the last separator so it ends at a word boundary."""
    m = re.match(r"^(.*[:,\-–—/])\s*", prefix)
    if not m:
        return None
    return m.group(1)


def _after_number(title: str, prefix: str) -> int | None:
    rest = title[len(prefix):].strip()
    n, _ = parse_movement(rest)
    return n


def same_work_title(a: str, b: str) -> bool:
    return norm_key(a) == norm_key(b)
