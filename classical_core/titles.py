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
    r"^\s*(?:(?:mvt\.?|movement|satz)\s*)?(?P<num>[ivxlc]{1,6}|\d{1,2})\s*(?:[.)]|\s[-–—]\s|:)\s*(?P<name>\S.*)$",
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


# ---- opera acts and broadcast talk tracks -------------------------------------------------------------

_ORDINAL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "first": 1, "second": 2, "third": 3,
            "fourth": 4, "fifth": 5, "erster": 1, "zweiter": 2, "dritter": 3, "vierter": 4, "fünfter": 5,
            "erste": 1, "zweite": 2, "dritte": 3, "primo": 1, "secondo": 2, "terzo": 3, "quarto": 4,
            "premier": 1, "deuxième": 2, "troisième": 3, "quatrième": 4, "cinquième": 5}
_ACT_WORD = r"(?:act|acts|akt|aufzug|atto|acte|acto|aufz\.)"
_NUM = r"(?:[ivx]{1,4}|\d{1,2}|" + "|".join(_ORDINAL) + r")"
_TAIL = r"(?:\b|(?=[\s.,:;–—-]|$))[\s.,:;–—-]*(?P<rest>.*)$"
_ACT = (re.compile(rf"^{_ACT_WORD}\s+(?P<n>{_NUM}){_TAIL}", re.IGNORECASE),          # Act II, Akt 3, Act One
        re.compile(rf"^{_ACT_WORD}(?P<n>\d{{1,2}}){_TAIL}", re.IGNORECASE),                # Act1, Akt2
        re.compile(rf"^(?P<n>{_NUM})\.?\s+{_ACT_WORD}{_TAIL}", re.IGNORECASE),        # Erster Aufzug, 2. Akt
        re.compile(rf"^(?P<n>prolog(?:ue|o)?){_TAIL}", re.IGNORECASE))
_SEGMENT = re.compile(r"(?:^|[:;,–—]\s*|\s-\s)")


def parse_act(text: str | None) -> tuple[int | None, str]:
    """'Act II: Scene 3' -> (2, 'Scene 3'); 'Prologue' -> (0, ''); 'Overture' -> (None, 'Overture')."""
    t = (text or "").strip()
    for rx in _ACT:
        m = rx.match(t)
        if not m:
            continue
        tok = m.group("n").lower().rstrip(".")
        n = 0 if tok.startswith("prolog") else _ORDINAL.get(tok) or movement_number(tok)
        if n is not None:
            return n, m.group("rest").strip()
    return None, t


def find_act(text: str | None) -> tuple[int | None, str]:
    """The first act designation that starts a segment of `text` ('Tristan und Isolde: Act I: Prelude')."""
    t = text or ""
    for m in _SEGMENT.finditer(t):
        n, rest = parse_act(t[m.end():])
        if n is not None:
            return n, rest
    return None, t.strip()


# talk on broadcast recordings (§5.1): never a work, and transparent between two parts of one performance
_TALK = re.compile(
    r"\b(?:intermission|interval (?:feature|talk)|interview(?:ed)?|commentary|announc(?:er|ement|ements)|quiz|"
    r"synopsis|backstage|conversation with|in conversation|discussion|podcast|applause|curtain calls?|"
    r"host|wrap-?up|short gap|silence|pre-?concert|broadcast (?:opening|intro(?:duction)?|close|closing|outro)|"
    r"spoken (?:introduction|intro)|lead-?in|station id|sponsor credits|"
    r"program(?:me)? notes?|introduced by|introduction by|speaks? (?:about|with)|talks? (?:about|with))\b"
    r"|^\s*(?:intro(?:duction)?|interval|welcome|preview|outro|closing(?: remarks)?|sign[- ]off|news|"
    r"announcer|host)\s*$",
    re.IGNORECASE)


def is_talk(title: str | None) -> bool:
    return bool(title and _TALK.search(title))


# music is in the track even if talk is mentioned ('End of the opera, then the announcer names the cast')
_MUSIC_TOO = re.compile(r"\b(?:performance|recording|complete|end of|close of|conclusion|encore|opening of|"
                        r"movement [ivx\d]+|mvts?\.? ?[ivx\d]+|segment|part [ivx\d]+)\b", re.IGNORECASE)


def is_talk_tail(tail: str | None) -> bool:
    """A title tail past a work tag that describes only talk: 'Announcer introduction and interview
    excerpt', 'Applause, then host wrap-up'. The first clause must be talk, and nothing may say the music
    is there too."""
    if not tail:
        return False
    first = re.split(r"[,;:(]|\s(?:and|then|with)\s", tail, maxsplit=1)[0]
    return is_talk(first) and not _MUSIC_TOO.search(tail)


# ---- generic track titles (opera numbers, "Track 3") ----------------------------------------------------

# a title that names only its place in a larger work: never a work of its own (§5.4)
_NUMBER_PREFIX = re.compile(r"^\s*\d{1,3}\.?\s+(?=\D)")
_GENERIC_FULL = re.compile(
    r"^(?:track|titel|title|piste|pista|traccia|tr|cd|disc|disk|side|part|teil|parte|nummer|number)\s*\.?\s*"
    r"(?:\d{1,3}|[ivx]{1,4})$", re.IGNORECASE)
_GENERIC_NO = re.compile(r"^(?:nr|no|n°|num)\.?\s*\d{1,2}[a-z]?(?=$|[\s.,:;()\-–—])", re.IGNORECASE)
# vocal-number and stage forms: as the first word, the title names a number of an opera or oratorio
_NUMBER_FORMS = frozenset(["rezitativ", "recitativo", "recitative", "recitatif", "récitatif", "recit", "accompagnato", "duett", "duetto", "duet", "duo", "arie", "aria", "arietta", "arioso", "terzett", "terzetto", "quartett", "quartetto", "quintett", "quintetto", "sextett", "sestetto", "septett", "finale", "introduktion", "introduzione", "einleitung", "chor", "coro", "szene", "scena", "cavatina", "cabaletta", "melodram", "melodrama", "marsch", "marcia", "ensemble", "stretta", "romanze", "romanza", "lied", "couplet", "couplets", "kanon"])
# orchestral numbers that are generic only on their own ("Overture", "Ouvertüre (Orchester)")
_BARE_FORMS = frozenset(["overture", "ouverture", "ouvertüre", "ouvert", "vorspiel", "preludio", "prelude", "prologue", "prolog", "sinfonia", "intermezzo", "entracte", "zwischenspiel"])


def strip_track_number(title: str) -> str:
    """'001 Overture' -> 'Overture' (a leading track number, not a movement number)."""
    return _NUMBER_PREFIX.sub("", title or "").strip()


def is_generic_title(title: str | None) -> bool:
    """'Nr.1', 'Act 2', 'Act1 - No. 1 Aria', 'Duett Jaquino', 'Track 7', 'Overture': a title that only
    places the track inside a larger work. Catalogue numbers make a title specific ('Aria, BWV 988')."""
    t = strip_track_number(title or "")
    if not t or parse_catalogues(t):
        return False
    if _GENERIC_FULL.match(t) or _GENERIC_NO.match(t) or parse_act(t)[0] is not None:
        return True
    words = re.findall(r"[^\W\d_]+", t.lower())
    if not words:
        return True
    if words[0] in _NUMBER_FORMS:
        return True
    bare = re.sub(r"\([^)]*\)", "", t).strip(" .:-–—")
    bare_words = re.findall(r"[^\W\d_]+", bare.lower())
    return len(bare_words) == 1 and bare_words[0] in _BARE_FORMS or (
        bool(bare_words) and bare_words[0].startswith("ouvert") and len(bare_words) <= 2)


_SEGMENT_OF = re.compile(r"\b(?:segment|part|section|teil)\s+(\d{1,2})\s+(?:of|von)\s+\d{1,2}\b", re.IGNORECASE)
_TAIL_SEP = re.compile(r"^\s*(?::|[-–—/]|,)\s*")


def title_tail(title: str | None, work: str) -> str | None:
    """What a track title says past its work tag: 'War Requiem: Continuous segment 1 of 6' -> 'Continuous
    segment 1 of 6'. None when the title is just the work, or does not start with it."""
    t = (title or "").strip()
    if not t or not work:
        return None
    if t.lower().startswith(work.lower()):
        rest = t[len(work):]
        m = _TAIL_SEP.match(rest)
        if m and rest[m.end():].strip():
            return rest[m.end():].strip()
        if not rest.strip():
            return None
    p = parse_title(t)
    if p and p.movement_name:
        a, b = norm_key(p.work), norm_key(work)
        if a and b and (a.startswith(b) or b.startswith(a)):
            return (f"{p.movement_no}. " if p.movement_no else "") + p.movement_name
    return None


def tail_number(tail: str) -> int | None:
    """'Movement I – Andante mosso' -> 1; 'Continuous segment 3 of 6' -> 3."""
    m = _SEGMENT_OF.search(tail)
    if m:
        return int(m.group(1))
    return parse_movement(tail)[0]


# "Fidelio, Op. 72 - Act 1": an act designation at the end of a work title is a section, not a new work
_ACT_SUFFIX = re.compile(r"^(?P<work>.+?)\s*(?:[-–—:,/]\s*|\s)\(?(?P<act>" + _ACT_WORD + r"\s*" + _NUM + r")\)?\s*$",
                         re.IGNORECASE)


def split_act_suffix(work: str) -> tuple[str, int | None]:
    m = _ACT_SUFFIX.match(work or "")
    if m and len(m.group("work")) >= 3:
        n, _ = parse_act(m.group("act"))
        if n is not None:
            return m.group("work").strip(" ,:-–—"), n
    return work, None


# ---- album names -----------------------------------------------------------------------------------------

_DISC_MARKS = (
    re.compile(r"[\[(]\s*(?:disc|disk|cd|part|teil|akt|act)\s*(?:\d{1,3}|[ivx]{1,4})\s*[\])]", re.IGNORECASE),
    re.compile(r"\b(?:disc|disk|cd|teil|part|akt|act)\s*(?:\d{1,3}|[ivx]{1,4})\b", re.IGNORECASE),
    re.compile(r"\(\s*\d{1,2}\s*\)"),
    re.compile(r"(?<=[^\W\d_])\d$"),                    # "Fidelio1", "Hollander2"
)


def strip_disc_marks(name: str | None) -> str:
    """'Beethoven: Fidelio [Disc 1]' / 'Fidelio (Klemperer, disc 2)' / 'Fidelio1' -> without the disc."""
    s = name or ""
    for rx in _DISC_MARKS:
        s = rx.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" ,;:-–—")


_ALBUM_JUNK = re.compile(r"^(?:opera|oper|op[ée]ra|complete works?|complete|works|werke|live|stereo|mono|highlights|"
                         r"excerpts|auszüge|various(?: artists)?|unknown(?: album)?|untitled|título desconocido|"
                         r"met|recital|collection|edition|box|remastered)$", re.IGNORECASE)
_COLLECTION = re.compile(r"\b(?:arias|overtures|symphonies|concertos|sonatas|songs|lieder|works|duets|choruses|"
                         r"preludes|excerpts|highlights|scenes|collection|best of|greatest)\b", re.IGNORECASE)


def album_work_segments(name: str | None) -> list[str]:
    """Candidate work titles in an album name, in order: 'Beethoven: Fidelio; Furtwangler/VPO' ->
    ['Beethoven', 'Fidelio', 'Furtwangler', 'VPO']. The caller drops composer names."""
    s = re.sub(r"[\[(][^\])]*[\])]", " ", strip_disc_marks(name))
    out = []
    for seg in re.split(r"\s*(?::|/|;|\||—|\s[-–]\s)\s*", s):
        seg = strip_disc_marks(seg.split(",")[0])
        seg = re.sub(r"\s+\d{1,3}$", "", seg).strip(" .'\"")
        if len(seg) < 3 or not re.search(r"[^\W\d_]{3}", seg) or _ALBUM_JUNK.match(seg):
            continue
        if _COLLECTION.search(seg) or is_generic_title(seg):
            continue
        out.append(seg)
    return out
