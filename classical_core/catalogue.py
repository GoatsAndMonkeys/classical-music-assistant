"""Lossless catalogue numbers, keys and work types (PLAN.md §5.5).

`parse_catalogues("Symphony No. 9 in E minor, Op. 95, B. 178")` returns every catalogue reference in a
title, each with its namespace and *all* of its components, so "Op. 28 No. 4" and "Op. 28 No. 7" stay
different and "Op. 28" alone is recognisably a collection-level reference. Sethster's lossy
`catalogue_key()` (vendored in `sethster_canonical`) is used only to retrieve merge candidates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text import fold

# namespace spellings -> canonical namespace. Single letters only match when followed by a number.
_NAMESPACES = {
    "op": "op", "opus": "op", "bwv": "bwv", "k": "k", "kv": "k", "d": "d", "hob": "hob", "hwv": "hwv",
    "rv": "rv", "wwv": "wwv", "wab": "wab", "woo": "woo", "s": "s", "lw": "lw", "sz": "sz", "bb": "bb",
    "l": "l", "m": "m", "p": "p", "b": "b", "trv": "trv", "av": "av", "jw": "jw", "h": "h", "lwv": "lwv",
    "fs": "fs", "bv": "bv", "twv": "twv", "zwv": "zwv", "wq": "wq", "g": "g", "kk": "kk", "ms": "ms",
}
DISPLAY = {
    "op": "Op.", "bwv": "BWV", "k": "K.", "d": "D.", "hob": "Hob.", "hwv": "HWV", "rv": "RV", "wwv": "WWV",
    "wab": "WAB", "woo": "WoO", "s": "S.", "lw": "LW", "sz": "Sz.", "bb": "BB", "l": "L.", "m": "M.",
    "p": "P.", "b": "B.", "trv": "TrV", "av": "AV", "jw": "JW", "h": "H.", "lwv": "LWV", "fs": "FS",
    "bv": "BV", "twv": "TWV", "zwv": "ZWV", "wq": "Wq.", "g": "G.", "kk": "Kk.", "ms": "MS",
}
_NS_ALT = "|".join(sorted(_NAMESPACES, key=len, reverse=True))
# namespace, then a number ("67", "VIIe:1", "331/300i", "28 No. 4", "posth.")
_CAT = re.compile(
    rf"(?<![a-z0-9'’])(?P<ns>{_NS_ALT})\.?\s*"
    r"(?P<num>(?:[ivxlc]+[a-z]?\s*:\s*)?\d+[a-z]?(?:\s*[/.]\s*\d+[a-z]?)*)"
    r"(?P<nos>(?:\s*,?\s*(?:no|nr|n°|num)\.?\s*\d+[a-z]?)*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Catalogue:
    """One catalogue reference: namespace plus the full, ordered component tuple."""

    namespace: str
    components: tuple[str, ...]
    display: str = field(default="", compare=False)

    @property
    def key(self) -> str:
        return f"{self.namespace}:{'/'.join(self.components)}"

    def is_prefix_of(self, other: Catalogue) -> bool:
        """'Op. 28' is a collection-level prefix of 'Op. 28 No. 4'."""
        n = len(self.components)
        return (self.namespace == other.namespace and n < len(other.components)
                and other.components[:n] == self.components)


def parse_catalogues(text: str | None) -> list[Catalogue]:
    """Every catalogue reference in `text`, in order, without loss."""
    if not text:
        return []
    s = fold(text)
    out: list[Catalogue] = []
    for m in _CAT.finditer(s):
        raw_ns = m.group("ns").lower()
        ns = _NAMESPACES[raw_ns]
        before = s[: m.start()].rstrip()
        # "in D 960" is a catalogue, "in D, Op." is not reached (no digits); "No. 5 in B 1" is a key
        if len(raw_ns) == 1 and re.search(r"\bin$", before) and m.group(0)[1:2] != ".":
            continue
        num = re.sub(r"\s+", "", m.group("num"))
        head_parts = [num]
        if ":" not in num:
            # 'K. 331/300i': the alternative Köchel number is kept as its own component
            head_parts = [p for p in re.split(r"[/.]", num) if p]
        nos = re.findall(r"\d+[a-z]?", m.group("nos") or "")
        head = num
        if ":" in num:  # Hob. VIIe:1 -> roman group upper-case, its letter suffix lower-case
            grp, _, n = num.partition(":")
            roman = re.match(r"[ivxlc]+", grp)
            head = (roman.group(0).upper() + grp[roman.end():] if roman else grp) + ":" + n
        text = f"{DISPLAY.get(ns, ns.upper())} {head}" + "".join(f" No. {c}" for c in nos)
        out.append(Catalogue(ns, tuple(head_parts + nos), text))
    return out


def primary_catalogue(text: str | None) -> Catalogue | None:
    """The first catalogue reference, which is the one titles lead with."""
    cats = parse_catalogues(text)
    return cats[0] if cats else None


def catalogues_conflict(a: list[Catalogue], b: list[Catalogue]) -> bool:
    """True when two works carry different full numbers in a shared namespace."""
    by_ns = {c.namespace: c for c in a}
    for c in b:
        other = by_ns.get(c.namespace)
        if other is not None and other.components != c.components:
            return True
    return False


# ---- keys ----------------------------------------------------------------------------------------------

_KEY_EN = re.compile(r"\bin\s+([a-g])(?:[\s-]*(flat|sharp|♭|♯))?(?:\s+(major|minor))?\b", re.IGNORECASE)
_KEY_DE = re.compile(r"\b([a-h])(is|es|s)?[\s-](dur|moll)\b", re.IGNORECASE)


def parse_key(text: str | None) -> str | None:
    """'Symphony No. 5 in C minor' -> 'C minor'; 'Symphonie Nr. 1 C-dur' -> 'C major'."""
    if not text:
        return None
    m = _KEY_EN.search(text)
    if m:
        acc = {"flat": "-flat", "♭": "-flat", "sharp": "-sharp", "♯": "-sharp"}.get((m.group(2) or "").lower(), "")
        mode = (m.group(3) or "major").lower()
        return f"{m.group(1).upper()}{acc} {mode}"
    m = _KEY_DE.search(text)
    if m:
        note, acc, mode = m.group(1), (m.group(2) or "").lower(), m.group(3).lower()
        name = {"h": "B", "b": "B"}.get(note.lower(), note.upper())
        if note.lower() == "b" and not acc:
            name, acc = "B", "es"
        accidental = {"is": "-sharp", "es": "-flat", "s": "-flat"}.get(acc, "")
        return f"{name}{accidental} {'major' if mode == 'dur' else 'minor'}"
    return None


# ---- work types (folders under a composer) ------------------------------------------------------------

WORK_TYPES = ("Symphonies", "Concertos", "Orchestral", "Chamber", "Keyboard", "Opera & Stage",
              "Choral & Sacred", "Vocal", "Other")
_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("Opera & Stage", re.compile(r"\b(opera|operetta|singspiel|zarzuela|ballet|incidental music|melodrama)\b")),
    ("Concertos", re.compile(r"\b(concerto|konzert|concert(?:o|ante)|concertino|sinfonia concertante)\b")),
    ("Symphonies", re.compile(r"\b(symphon(?:y|ie|ies)|sinfonie|sinfonia\s+(?:no|nr)\.?\s*\d)\b")),
    ("Choral & Sacred", re.compile(r"\b(mass|messe|missa|requiem|cantata|kantate|oratorio|oratorium|motet|"
                                   r"magnificat|stabat mater|te deum|passion|vespers|gloria)\b")),
    ("Keyboard", re.compile(r"\b(piano sonatas?|klaviersonaten?|preludes?|etudes?|nocturnes?|ballades?|impromptus?|mazurkas?|"
                            r"polonaises?|waltz(?:es)?|bagatelles?|toccata|partita for keyboard|goldberg|well-tempered|"
                            r"for piano|for keyboard|for harpsichord|for organ)\b")),
    ("Chamber", re.compile(r"\b(quartet|quintet|sextet|septet|octet|trio|sonata|duo|serenade for|nonet|"
                           r"divertimento for)\b")),
    ("Vocal", re.compile(r"\b(lied(?:er)?|songs?|chanson|melodie|aria|liederkreis|winterreise)\b")),
    ("Orchestral", re.compile(r"\b(overture|suite|tone poem|symphonic poem|serenade|variations|rhapsody|"
                              r"adagio for strings|canon|march|dances?|divertimento|orchestral)\b")),
]
_MB_TYPES = {
    "symphony": "Symphonies", "concerto": "Concertos", "opera": "Opera & Stage", "ballet": "Opera & Stage",
    "operetta": "Opera & Stage", "zarzuela": "Opera & Stage", "incidental music": "Opera & Stage",
    "mass": "Choral & Sacred", "requiem": "Choral & Sacred", "cantata": "Choral & Sacred",
    "oratorio": "Choral & Sacred", "motet": "Choral & Sacred", "madrigal": "Vocal", "song": "Vocal",
    "song-cycle": "Vocal", "aria": "Vocal", "quartet": "Chamber", "sonata": "Chamber", "suite": "Orchestral",
    "overture": "Orchestral", "symphonic poem": "Orchestral", "étude": "Keyboard", "prelude": "Keyboard",
}


def work_type(title: str, mb_type: str | None = None, part_names: list[str] | None = None) -> str:
    """Folder name for a work: MB work type first, then title keywords, then part-name hints."""
    if mb_type and (t := _MB_TYPES.get(mb_type.lower())):
        if t == "Chamber" and re.search(r"piano sonata|klaviersonate", fold(title)):
            return "Keyboard"
        return t
    s = fold(title)
    for name, rx in _TYPE_RULES:
        if rx.search(s):
            return name
    if part_names and sum(bool(re.match(r"(act|akt|atto|acte)\s+[ivx\d]+", fold(p))) for p in part_names) >= 1:
        return "Opera & Stage"
    return "Other"
