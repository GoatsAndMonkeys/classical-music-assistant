"""Accolades: awards, reviews and ratings for recordings, and the acclaim weight of a Play pick (docs/accolades.md,
PLAN.md §5.8, §5.9). MA-free.

Sources, all legal and none scraped:
- the bundled dataset `data/accolades.json` (award facts compiled from public Wikipedia lists, sources cited in
  the file; no review prose). Matched offline against the index by `refresh`.
- MusicBrainz release-group url relations (AllMusic, Discogs, Wikidata, review pages): links only.
- Opt-in network sources: Wikidata P166 ("award received") on the release group's item, CritiqueBrainz reviews
  (CC BY-SA / BY-NC-SA: an attributed excerpt and a link), the Discogs community rating (token; refreshed
  within 6 hours or hidden). These attach to the album; they count for a recording only when the album holds
  exactly one recording.

Matching is conservative: the composer, the work and the principal performers must agree, and the album's year
must fit the award year. A recording whose release is outside that window may still carry the award, labelled
as given to another release of the same recording, only on stronger performer evidence (or a label match).
Anything less certain goes to the review queue and is shown only after a user confirms it. Rejected matches
stay rejected across rebuilds (decisions are keyed by the dataset's stable entry ids).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any
from urllib.parse import quote, quote_plus

from . import boxes
from .text import ENSEMBLE_NAME, fold, norm_key, surname

COUNTED_GRADES = ("this_release", "other_release", "confirmed")
SHOWN_GRADES = COUNTED_GRADES
NET_SOURCES = ("wikidata", "critiquebrainz", "discogs")
DISCOGS_TTL = 6 * 3600
NET_REFRESH = 30 * 86400
EXCERPT_CHARS = 280

# ---- data files ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    award: str
    category: str | None
    year: int
    result: str                      # winner | nominee
    composer: str
    work: str
    performers: tuple[str, ...]
    label: str | None
    url: str | None                  # the cited list
    source: str                      # the cited source's title
    historical: bool = False


@lru_cache(maxsize=1)
def tiers() -> dict:
    return json.loads(resources.files("classical_core.data").joinpath("acclaim_tiers.json").read_text("utf-8"))


@lru_cache(maxsize=1)
def dataset() -> dict:
    return json.loads(resources.files("classical_core.data").joinpath("accolades.json").read_text("utf-8"))


def _split_people(names: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for n in names:
        out += [p.strip() for p in re.split(r"\s+with\s+", n) if p.strip()]
    return tuple(out)


@lru_cache(maxsize=1)
def entries() -> tuple[Entry, ...]:
    d = dataset()
    cats, srcs = d["categories"], d["sources"]
    out = []
    for row in d["entries"]:
        eid, ci, year, result, composer, work, performers = row[:7]
        c = cats[ci]
        out.append(Entry(eid, c["award"], c.get("category"), int(year), result, composer, work,
                         _split_people(performers), (row[7] if len(row) > 7 else None) or None,
                         row[8] if len(row) > 8 and row[8] else c.get("url"),     # a per-entry page, else the list
                         srcs[c["source"]]["title"], bool(c.get("historical"))))
    return tuple(out)


def dataset_info() -> dict:
    d = dataset()
    return {"compiled": d.get("compiled"), "entries": len(d["entries"]), "sources": d["sources"]}


# ---- normalisation ------------------------------------------------------------------------------------


def _set(words: str) -> frozenset[str]:
    return frozenset(words.split())


_STOP = _set("""the a an of in on for and with from to by at op opus no nr nos n number major minor dur moll flat
sharp bemol diese majeur mineur der die das des dem den ein eine le la les l du de d un une il lo gli i di del della
dello e ed et und y el los las en from complete version versions excerpts orig original""")
_PLURAL = _set("""symphonies concertos concerti sonatas sonaten sonates quartets quartette quatuors songs lieder
works pieces etudes preludes nocturnes mazurkas waltzes trios quintets cantatas motets madrigals suites overtures
arias duets dances etc""")
_GENRE = _set("""symphony concerto sonata quartet quintet trio sextet octet mass requiem suite overture serenade
string piano violin viola cello flute oboe clarinet horn trumpet orchestra orchestral act scene part chamber vocal
choral songs song lieder cantata motet variations""")
_CATALOGUE = re.compile(
    r"\b(?:op|opus|bwv|kv|k|wwv|hob|d|rv|hwv|sz|bb|trv|woo|jw|fs|s|l|m|cd|anh)\s*\.?\s*(?:[ivxlc]+\s*:\s*)?\d+[a-z]?"
    r"(?:\s*[/.]\s*\d+)?\b", re.IGNORECASE)
_KEY = re.compile(r"\b([a-h])(?:\s*[- ]?\s*(flat|sharp|b|bemol|dur|moll|is|es))?\s*[- ]?\s*(major|minor|dur|moll)\b")
_NUM = re.compile(r"(?<![\d.])\b(\d{1,3})\b(?![\d.])")
_TOK_SPLIT = re.compile(r"[^a-z0-9']+")


@lru_cache(maxsize=1)
def _phrases() -> list[tuple[str, str]]:
    t = {norm_key(k): norm_key(v) for k, v in tiers()["titles"].items() if k != "about"}
    return sorted(t.items(), key=lambda kv: -len(kv[0]))


@lru_cache(maxsize=1)
def _words() -> dict[str, list[str]]:
    return {norm_key(k): norm_key(v).split() for k, v in tiers()["words"].items() if k != "about"}


def _phrase_rewrite(s: str) -> str:
    s = f" {s} "
    for k, v in _phrases():
        s = s.replace(f" {k} ", f" {v} ")
    return s.strip()


def _map_words(toks: Iterable[str]) -> list[str]:
    words = _words()
    out: list[str] = []
    for t in toks:
        out += words.get(t, [t])
    return out


@dataclass(frozen=True, slots=True)
class WorkKey:
    tokens: frozenset[str]       # content words, mapped and singular
    numbers: frozenset[int]
    key: str | None              # 'c minor'
    plural: bool                 # 'Symphonies', 'Sonatas', '... etc': more than one work
    core: frozenset[str] = frozenset()   # tokens without quoted nicknames ('From the New World')

    @property
    def generic(self) -> bool:
        return bool(self.tokens) and self.tokens <= _GENRE


_QUOTED = re.compile(r"[\"“”„«»]([^\"“”„«»]{2,60})[\"“”„«»]|(?<!\w)'([^']{2,60})'(?!\w)")


@lru_cache(maxsize=8192)
def work_key(title: str) -> WorkKey:
    k = _work_key(title)
    stripped = _QUOTED.sub(" ", title or "")
    core = _work_key(stripped).tokens if stripped != title else k.tokens
    return WorkKey(k.tokens, k.numbers, k.key, k.plural, core)


def _work_key(title: str) -> WorkKey:
    s = fold(re.sub(r"\([^)]*\)|\[[^]]*\]", " ", title or ""))
    s = s.replace("n°", "no ").replace("nº", "no ").replace("№", "no ")
    s = _CATALOGUE.sub(" ", s)
    s = re.sub(r"\b(?:1[5-9]|20)\d\d\b", " ", s)                     # years
    km = _KEY.search(s)
    key = None
    if km:
        acc = {"flat": "flat", "b": "flat", "bemol": "flat", "es": "flat", "sharp": "sharp", "is": "sharp"}.get(
            km.group(2) or "", "")
        mode = "minor" if km.group(3) in ("minor", "moll") else "major"
        key = " ".join(x for x in (km.group(1).replace("h", "b"), acc, mode) if x)
        s = s[:km.start()] + " " + s[km.end():]
    nums = frozenset(int(n) for n in _NUM.findall(s))
    s = _phrase_rewrite(norm_key(s))
    raw = [t for t in _TOK_SPLIT.split(s) if t]
    plural = any(t in _PLURAL for t in raw)
    toks = _map_words(raw)
    toks = [t for t in toks if len(t) > 1 and not t.isdigit() and t not in _STOP]
    toks = [t[:-1] if t in _PLURAL and t.endswith("s") else t for t in toks]
    return WorkKey(frozenset(toks), nums, key, plural)


def split_parts(work: str) -> list[str]:
    """'La Mer; Prelude a l'Apres-Midi d'un Faune' -> two works. A single title stays whole."""
    parts = [p.strip() for p in re.split(r";|\s+/\s+|\s+&\s+(?=[A-Z])", work) if p.strip()]
    return parts or [work]


def work_match(entry_title: str, rec_title: str) -> str | None:
    """'exact', 'weak' (the entry names no number but the recording's work has one) or None."""
    e, r = work_key(entry_title), work_key(rec_title)
    if not e.tokens or not r.tokens:
        return None
    subset = e.tokens <= r.tokens or (len(r.tokens) >= 2 and r.tokens <= e.tokens)
    # a nickname in one language only ('From the New World', 'Z Nového světa'): the numbered core decides
    if not subset and not (e.numbers and e.numbers == r.numbers and e.core and e.core == r.core):
        return None
    if e.generic and not r.tokens <= _GENRE:
        return None                       # 'Mass in C' is not 'Missa solemnis'
    if e.key and r.key and e.key != r.key:
        return None
    if e.numbers:
        return "exact" if r.numbers == e.numbers or (r.numbers and e.numbers <= r.numbers and not e.generic) \
            else None
    if r.numbers and e.generic:
        return "weak"                     # 'Piano Concerto' vs 'Piano Concerto No. 2'
    return "exact"


# ---- names --------------------------------------------------------------------------------------------

_PARTICLES = {"de", "del", "della", "di", "da", "von", "van", "der", "den", "le", "la", "du", "dos", "das", "ter",
              "ten", "sir", "dame"}
_ENS_GENERIC = _set("""orchestra the of and de des du la le les der die das di del della dell l et und members
soloists ensemble company""")
_ENS_TYPES = _set("""chorus opera symphony philharmonic chamber radio state festival house boys children
youth national royal""")
_ENS_HINT = re.compile(r"\b(?:opera|philharmon\w*|symphon\w*|staatskapelle|festival|sinfonia|kapelle|consort|"
                       r"players|choir|chorus|chor|orchest\w*|ensemble|quartet\w*|trio|soloists)\b", re.IGNORECASE)
_SPLIT_NAMES = re.compile(r"\s*(?:/|;|,|&|\+|\s-\s|\s–\s|\band\b|\bwith\b|\bconducting\b|\bconducts\b)\s*")


def is_group(name: str) -> bool:
    return bool(ENSEMBLE_NAME.search(name) or _ENS_HINT.search(name))


def ens_tokens(name: str) -> frozenset[str]:
    toks = _map_words(norm_key(_phrase_rewrite(norm_key(name))).split())
    return frozenset(t for t in toks if t not in _ENS_GENERIC and len(t) > 1)


def ens_distinctive(name: str) -> frozenset[str]:
    """Tokens that make an ensemble name specific; empty for 'Orchestra', 'Chorus', 'Opera Chorus'."""
    t = ens_tokens(name)
    return t if t - _ENS_TYPES else frozenset()


@dataclass(frozen=True, slots=True)
class Person:
    given: tuple[str, ...]
    sur: str                         # surname tokens joined by spaces, folded


def person(name: str) -> Person | None:
    name = re.sub(r"\([^)]*\)", " ", name)
    sur = norm_key(surname(name, particles=False))
    if not sur:
        return None
    toks = norm_key(name).split()
    stoks = sur.split()
    given = tuple(t for t in toks[: max(0, len(toks) - len(stoks))] if t not in _PARTICLES)
    return Person(given, sur)


def _initial_ok(a: tuple[str, ...], b: tuple[str, ...]) -> str:
    """'full' (same first given name), 'initial' (same initial or one side has none), 'conflict'."""
    if not a or not b:
        return "initial"
    if a[0] == b[0]:
        return "full"
    return "initial" if a[0][0] == b[0][0] and (len(a[0]) == 1 or len(b[0]) == 1) else "conflict"


@dataclass(slots=True)
class Pool:
    """Everything that says who plays on a recording: credits, track and album artists, the album name."""
    persons: list[tuple[Person, str]] = field(default_factory=list)       # (person, role)
    groups: list[tuple[frozenset[str], str]] = field(default_factory=list)  # (tokens, role)
    loose: str = " "                                                       # ' fidelio klemperer disc 1 '
    conductors: list[Person] = field(default_factory=list)
    orchestras: list[frozenset[str]] = field(default_factory=list)

    def add(self, name: str, role: str = "") -> None:
        name = (name or "").strip()
        if not name or norm_key(name) in _PLACEHOLDERS:
            return
        group = role in ("orchestra", "chorus", "choir", "ensemble") or is_group(name)
        if group:
            t = ens_tokens(name)
            if t:
                self.groups.append((t, role))
                if (role == "orchestra" or "chorus" not in t) and ens_distinctive(name):
                    self.orchestras.append(t)
            return
        p = person(name)
        if p:
            self.persons.append((p, role))
            if role == "conductor":
                self.conductors.append(p)

    def add_text(self, text: str, names: bool = True) -> None:
        """A free-text artist or album string: every piece as a name (when `names`), and all of it as loose
        words for surname-only matches ('Fidelio (Klemperer, disc 1)')."""
        if not text:
            return
        if names:
            for piece in _SPLIT_NAMES.split(re.sub(r"[()\[\]]", ",", text)):
                if piece and not re.search(r"\d", piece) and len(piece) < 60:
                    self.add(piece)
        self.loose += " ".join(_map_words(norm_key(_phrase_rewrite(norm_key(text))).split())) + " "


_PLACEHOLDERS = frozenset({"various artists", "various", "unknown", "unknown artist", "anonymous", "va", "lectures"})


@dataclass(slots=True)
class PerfMatch:
    n: int = 0                       # specific performers the entry names
    m: int = 0                       # of them, found on the recording
    full: int = 0                    # found by full name / full ensemble name
    principal: bool = False          # the first specific performer was found
    contradiction: str | None = None
    stranger: bool = False           # an entry performer is missing and the recording names someone else instead
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def match_performers(names: Iterable[str], pool: Pool) -> PerfMatch:
    pm = PerfMatch()
    entry_orch: list[tuple[str, frozenset[str]]] = []
    entry_persons: list[Person] = []
    first = True
    for name in names:
        if is_group(name):
            d = ens_distinctive(name)
            if not d:
                continue                                  # 'Orchestra', 'Chorus'
            pm.n += 1
            if "chorus" not in d:
                entry_orch.append((name, d))
            hit = any(d <= t and (("chorus" in d) == ("chorus" in t)) for t, _ in pool.groups)
            loose = not hit and all(f" {x} " in pool.loose for x in d)
            if hit or loose:
                pm.m += 1
                pm.full += hit
                pm.matched.append(name)
                pm.principal = pm.principal or first
            else:
                pm.missing.append(name)
            first = False
            continue
        p = person(name)
        if p is None or len(p.sur) < 3:
            continue
        pm.n += 1
        entry_persons.append(p)
        how = None
        for q, _ in pool.persons:
            if q.sur == p.sur:
                ok = _initial_ok(p.given, q.given)
                if ok != "conflict":
                    how = "full" if ok == "full" else (how or "surname")
                    if how == "full":
                        break
        if how is None and f" {p.sur} " in pool.loose:
            how = "surname"
        if how:
            pm.m += 1
            pm.full += how == "full"
            pm.matched.append(name)
            pm.principal = pm.principal or first
        else:
            pm.missing.append(name)
        first = False
    # contradictions: a credited conductor or orchestra the entry does not name
    if entry_orch and pool.orchestras and not any(d <= t for _, d in entry_orch for t in pool.orchestras):
        pm.contradiction = "orchestra"
    if entry_orch and pool.conductors and entry_persons and not any(
            c.sur == p.sur and _initial_ok(p.given, c.given) != "conflict" for c in pool.conductors
            for p in entry_persons):
        pm.contradiction = "conductor"
    # an uncredited name in an artist string ('Perlman / Chicago SO / Barenboim' for Perlman, Giulini, CSO):
    # no contradiction, but the match is not strong enough to badge without a look
    if entry_orch and entry_persons and pm.m <= 2 and any(n for n in pm.missing if not is_group(n)):
        pm.stranger = any(role in ("", "conductor") and not any(
            q.sur == p.sur and _initial_ok(p.given, q.given) != "conflict" for p in entry_persons)
            for q, role in pool.persons)
    return pm


def same_composer(entry_name: str, names: Iterable[str]) -> bool:
    e = person(entry_name)
    if e is None:
        return False
    for n in names:
        q = person(n)
        if q and (q.sur == e.sur or q.sur.endswith(" " + e.sur) or e.sur.endswith(" " + q.sur)) \
                and _initial_ok(e.given, q.given) != "conflict":
            return True
    return False


# ---- grading --------------------------------------------------------------------------------------------


def year_window(award: str, year: int) -> tuple[int, int]:
    y = tiers()["years"]
    before, after = y.get(award, y["default"])
    return year - before, year + after


def _year(v: Any) -> int | None:
    try:
        return int(str(v)[:4]) if v not in (None, "") else None
    except ValueError:
        return None


_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
          r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATED = re.compile(
    r"\b(?P<a>1[89]\d\d|20\d\d)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"                 # 2012-11-17
    r"|\b(?:0?[1-9]|[12]\d|3[01])[./](?:0?[1-9]|1[0-2])[./](?P<b>1[89]\d\d|20\d\d)\b"          # 17.11.2012
    rf"|\b(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)? {_MONTH}\.?,? (?P<c>1[89]\d\d|20\d\d)\b"   # 17 November 2012
    rf"|\b{_MONTH}\.? (?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,? (?P<d>1[89]\d\d|20\d\d)\b",   # November 17, 2012
    re.IGNORECASE)


def performance_year(text: str | None) -> int | None:
    """The year of a performance date written out to the day in an album or recording name ("Carnegie Hall,
    2012-11-17"): a live recording's own date, so it is when the music was recorded. A bare year is not
    enough (it may be the release or a reissue)."""
    m = _DATED.search(text or "")
    return int(next(g for g in m.group("a", "b", "c", "d") if g)) if m else None


def label_match(a: str | None, b: str | None) -> bool:
    ta = {t for t in norm_key(a).split() if len(t) > 2 and t not in {"records", "classics", "classical", "music", "cd"}}
    tb = {t for t in norm_key(b).split() if len(t) > 2}
    return bool(ta & tb)


@dataclass(slots=True)
class Facts:
    """One recording as the matcher sees it."""
    id: str
    work_id: str
    album_id: str
    titles: list[str]
    composers: list[str]
    recorded: int | None
    released: int | None
    label: str | None
    pool: Pool


def grade(e: Entry, wm: str | None, pm: PerfMatch, f: Facts) -> tuple[str | None, dict]:
    """(grade, why). Grade: this_release | other_release | uncertain | None (no match)."""
    lo, hi = year_window(e.award, e.year)
    why: dict[str, Any] = {"work": wm, "performers": pm.matched, "missing": pm.missing[:6],
                           "album_year": f.released, "recorded": f.recorded, "window": [lo, hi]}
    if wm is None or pm.m == 0 or pm.contradiction:
        if pm.contradiction:
            why["contradiction"] = pm.contradiction
        return None, why
    if f.recorded is not None and f.recorded > max(hi, e.year - 1):        # recorded after the awarded disc
        return None, why
    if f.recorded is not None and not e.historical and f.recorded < e.year - tiers()["years"]["recorded_before"]:
        return None, why
    if f.released is not None and not e.historical and f.released < lo - 5 and f.recorded is None:
        return None, why                       # an album long before the award: another recording
    lab = bool(e.label and label_match(e.label, f.label))
    why["label"] = lab
    strong = (pm.m >= 2 or (pm.n == 1 and pm.full >= 1)) and not pm.stranger
    in_window = f.released is not None and lo <= f.released <= hi
    recorded_fits = f.recorded is not None and lo - 1 <= f.recorded <= hi
    # a known session well before the window: a reissue of an older recording, or the same artists' remake
    old_session = f.recorded is not None and not e.historical and f.recorded < lo - 2
    if pm.stranger:
        why["stranger"] = True
    if old_session:
        why["old_session"] = True
    if wm == "exact" and strong and pm.principal and not old_session:
        if in_window:
            return "this_release", why
        # another release of this recording: the session itself must fit the award (a label or a long cast
        # list is not enough; the same artists re-recorded works for the same label)
        if recorded_fits and (f.released is None or f.released > hi):
            return "other_release", why
    if pm.principal or pm.m >= 2:
        return "uncertain", why
    return None, why


def _acc_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def entry_title(e: Entry) -> str:
    return f"{e.composer}: {e.work}"


def badge_text(award: str | None, result: str | None, year: int | None) -> str:
    a = award or "Award"
    if result == "nominee":
        a += " nominee"
    elif result == "winner" and a == "Grammy":
        a += " winner"
    return f"{a} {year}" if year else a


# ---- dataset refresh (writer thread) ----------------------------------------------------------------------


def _facts(con) -> dict[str, Facts]:
    rows = con.execute(
        "SELECT r.id, r.work_id, r.album_library_id AS aid, r.recorded_start, r.release_year, r.label, "
        "a.name AS album, a.artist AS album_artist, a.year AS album_year, a.label AS album_label, w.title, "
        "pw.title AS parent_title, "
        "(SELECT GROUP_CONCAT(c.name, ';') FROM work_composer wc JOIN composer c ON c.id=wc.composer_id "
        " WHERE wc.work_id IN (r.work_id, w.parent_work_id)) AS composers "
        "FROM recording r JOIN work w ON w.id=r.work_id LEFT JOIN work pw ON pw.id=w.parent_work_id "
        "LEFT JOIN album_info a ON a.album_library_id=r.album_library_id "
        "WHERE r.joined_to IS NULL").fetchall()       # a later disc's recording is part of its lead's
    out: dict[str, Facts] = {}
    # a box set's album artist and name speak for the whole box (several conductors, 'Karajan: The Complete EMI
    # Recordings 1946-1984'), not for one recording on it: only the disc's own subtitle counts, and the album
    # artist only for a recording with no credits or track artists of its own (boxes.py)
    in_box = boxes.of_albums(con, sorted({r["aid"] for r in rows if r["aid"]}))
    box_artist: dict[str, str] = {}
    for r in rows:
        pool = Pool()
        album = r["album"] or ""
        if r["aid"] in in_box:
            album = "" if in_box[r["aid"]][0].single else (boxes.subtitle(album) or "")
            if r["album_artist"]:
                box_artist[r["id"]] = r["album_artist"]
        out[r["id"]] = Facts(
            r["id"], r["work_id"], r["aid"], [t for t in (r["title"], r["parent_title"]) if t],
            [c for c in (r["composers"] or "").split(";") if c],
            _year(r["recorded_start"]) or performance_year(album),
            _year(r["release_year"]) or _year(r["album_year"]), r["label"] or r["album_label"], pool)
        if r["id"] not in box_artist:
            pool.add_text(r["album_artist"] or "")
        pool.add_text(album, names=False)
    for r in con.execute(
            "SELECT s.recording_id AS rid, p.name, p.kind, tc.role FROM recording_slot s "
            "JOIN track_credit tc ON tc.occurrence_id=s.occurrence_id JOIN person p ON p.id=tc.person_id "
            "GROUP BY s.recording_id, p.id, tc.role"):
        f = out.get(r["rid"])
        if f:
            role = r["role"] if r["kind"] == "person" or r["role"] in ("orchestra", "chorus", "ensemble") \
                else ("orchestra" if r["kind"] == "orchestra" else "ensemble")
            f.pool.add(r["name"], role)
    for r in con.execute(
            "SELECT DISTINCT s.recording_id AS rid, t.artist FROM recording_slot s JOIN occurrence o "
            "ON o.id=s.occurrence_id JOIN track_info t ON t.library_track_id=o.library_track_id "
            "WHERE t.artist IS NOT NULL AND t.artist<>''"):
        f = out.get(r["rid"])
        if f:
            f.pool.add_text(r["artist"])
    for rid, artist in box_artist.items():
        f = out[rid]
        if not (f.pool.persons or f.pool.groups):
            f.pool.add_text(artist)
    # the composer's own name is no performer evidence
    for f in out.values():
        csur = {p.sur for p in (person(c) for c in f.composers) if p}
        f.pool.persons = [(p, role) for p, role in f.pool.persons if p.sur not in csur or role]
        for s in csur:
            f.pool.loose = f.pool.loose.replace(f" {s} ", " ")
    return out


@dataclass(slots=True)
class Match:
    entry: Entry
    grade: str
    target: str                      # recording | album
    recording_id: str | None
    album_id: str
    work_id: str | None
    why: dict


def _by_surname(es: Iterable[Entry]) -> dict[str, list[Entry]]:
    out: dict[str, list[Entry]] = defaultdict(list)
    for e in es:
        p = person(e.composer)
        if p:
            out[p.sur.split()[-1]].append(e)
    return out


def match_all(facts: dict[str, Facts], es: Iterable[Entry] | None = None,
              decisions: dict[tuple[str, str, str], str] | None = None,
              resolve=lambda wid: wid) -> list[Match]:
    """Every dataset match for these recordings. `decisions`: (entry id, album, work id) -> confirm|reject."""
    decisions = decisions or {}
    by_sur = _by_surname(entries() if es is None else es)
    out: list[Match] = []
    by_album: dict[str, list[Facts]] = defaultdict(list)
    for f in facts.values():
        by_album[f.album_id].append(f)
    album_done: set[tuple[str, str]] = set()
    for f in sorted(facts.values(), key=lambda f: f.id):
        cands: dict[str, Entry] = {}
        for c in f.composers:
            p = person(c)
            if p:
                for e in by_sur.get(p.sur.split()[-1], ()):
                    cands[e.id] = e
        for e in cands.values():
            if not same_composer(e.composer, f.composers):
                continue
            parts = split_parts(e.work)
            multi = len(parts) > 1 or work_key(e.work).plural
            if multi:
                key = (e.id, f.album_id)
                if key not in album_done:
                    album_done.add(key)
                    m = _album_match(e, parts, by_album[f.album_id])
                    if m:
                        out.append(m)
                continue
            wm = max((work_match(e.work, t) for t in f.titles), key=lambda x: {"exact": 2, "weak": 1, None: 0}[x])
            if wm is None:
                continue
            pm = match_performers(e.performers, f.pool)
            g, why = grade(e, wm, pm, f)
            dec = decisions.get((e.id, f.album_id, resolve(f.work_id)))
            if dec == "reject":
                continue
            if dec == "confirm" and (g is not None or (pm.m and not pm.contradiction)):
                g = "confirmed"
            if g:
                out.append(Match(e, g, "recording", f.id, f.album_id, f.work_id, why))
    return out


def _album_match(e: Entry, parts: list[str], recs: list[Facts]) -> Match | None:
    """A multi-work or 'Complete ...' entry: for the album, never for one recording."""
    pool = Pool()
    # a part naming another composer ('...; Haydn: English Songs') is not checked; every other part must be
    # on the album: a numbered plural ('Symphonies Nos. 5 & 7') needs each number, an unnumbered one
    # ('Symphonies', 'String Quartets') at least two of the works
    own = [p for p in parts if ":" not in p] or parts
    hits: dict[int, set[str]] = defaultdict(set)
    nums: dict[int, set[int]] = defaultdict(set)
    for f in recs:
        if not same_composer(e.composer, f.composers):
            continue
        for i, part in enumerate(own):
            pk = work_key(part)
            for t in f.titles:
                if work_match(part, t) == "exact" and not pk.plural:
                    hits[i].add(f.id)
                elif pk.plural and pk.tokens:
                    rk = work_key(t)
                    if pk.tokens <= rk.tokens and (not pk.numbers or rk.numbers & pk.numbers):
                        hits[i].add(f.id)
                        nums[i] |= rk.numbers
        pool.persons += f.pool.persons
        pool.groups += f.pool.groups
        pool.loose += f.pool.loose
        pool.conductors += f.pool.conductors
        pool.orchestras += f.pool.orchestras
    for i, part in enumerate(own):
        pk = work_key(part)
        if not hits[i]:
            return None
        if pk.plural and (not pk.numbers <= nums[i] if pk.numbers else len(hits[i]) < 2):
            return None
    pm = match_performers(e.performers, pool)
    f0 = recs[0]
    facts = Facts(f0.id, f0.work_id, f0.album_id, [], [], None, f0.released, f0.label, pool)
    g, why = grade(e, "exact", pm, facts)
    if g != "this_release" or pm.m < 2:
        return None
    why["recordings"] = sorted(set().union(*hits.values()))
    return Match(e, "this_release", "album", None, f0.album_id, None, why)


def refresh(con, now: float | None = None, use_dataset: bool = True) -> dict:
    """Rebuild the dataset accolades and their review items from the whole index (writer thread). With
    `use_dataset` off the dataset's rows are only cleared."""
    now = time.time() if now is None else now

    def resolve(wid: str) -> str:
        seen = set()
        while wid not in seen:
            seen.add(wid)
            r = con.execute("SELECT new_id FROM redirect WHERE entity='work' AND old_id=?", (wid,)).fetchone()
            if not r:
                break
            wid = r[0]
        return wid

    decisions = {(r["entry_ref"], r["album_library_id"], resolve(r["work_id"])): r["decision"]
                 for r in con.execute("SELECT * FROM accolade_decision")}
    matches = match_all(_facts(con), decisions=decisions, resolve=resolve) if use_dataset else []
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM accolade WHERE source='dataset'")
        con.execute("DELETE FROM review_item WHERE reason='accolade_match'")
        for m in matches:
            e = m.entry
            aid = _acc_id("dataset", e.id, m.recording_id or f"album:{m.album_id}")
            con.execute(
                "INSERT OR REPLACE INTO accolade(id, target, recording_id, album_library_id, work_id, kind, award, "
                "category, year, result, url, source, source_ref, grade, confidence, why_json, fetched_at) "
                "VALUES (?,?,?,?,?,'award',?,?,?,?,?,'dataset',?,?,?,?,?)",
                (aid, m.target, m.recording_id, m.album_id, m.work_id, e.award, e.category, e.year, e.result, e.url,
                 e.id, m.grade, 1.0 if m.grade != "uncertain" else 0.5, json.dumps(m.why), now))
            if m.grade == "uncertain":
                w = m.why
                bits = []
                if w.get("performers"):
                    bits.append("found " + ", ".join(w["performers"]))
                if w.get("missing"):
                    bits.append("not found " + ", ".join(w["missing"][:3]))
                bits.append(f"album year {w.get('album_year') or 'unknown'} (award fits {w['window'][0]}–"
                            f"{w['window'][1]})")
                if w.get("work") == "weak":
                    bits.append("the award names no number")
                if w.get("old_session"):
                    bits.append(f"recorded {w.get('recorded')}, before the award's years: a reissue or another version?")
                if w.get("stranger"):
                    bits.append("the artists also name someone the award does not")
                con.execute(
                    "INSERT OR REPLACE INTO review_item(id, reason, album_library_id, entity, entity_id, detail_json) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"acc:{e.id}:{m.recording_id}", "accolade_match", m.album_id, "recording", m.recording_id,
                     json.dumps({"title": f"{badge_text(e.award, e.result, e.year)}: {entry_title(e)}",
                                 "award": badge_text(e.award, e.result, e.year), "category": e.category,
                                 "entry": e.id, "performers": list(e.performers[:8]), "why": "; ".join(bits),
                                 "source_url": e.url})))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    grades: dict[str, int] = defaultdict(int)
    for m in matches:
        grades[f"{m.target}:{m.grade}"] += 1
    return dict(grades)


def decide(con, entry_ref: str, recording_id: str, decision: str, user: str | None) -> dict:
    """Record a user's decision on one dataset match ('confirm', 'reject' or 'clear'). Writer thread, inside
    the caller's transaction."""
    if decision not in ("confirm", "reject", "clear"):
        raise ValueError("decision must be confirm, reject or clear")
    if not any(e.id == entry_ref for e in entries()):
        raise ValueError("unknown accolade entry")
    r = con.execute("SELECT album_library_id, COALESCE(source_work_id, work_id) AS wid FROM recording WHERE id=?",
                    (recording_id,)).fetchone()
    if r is None:
        raise ValueError("unknown recording")
    if decision == "clear":
        con.execute("DELETE FROM accolade_decision WHERE entry_ref=? AND album_library_id=? AND work_id=?",
                    (entry_ref, r["album_library_id"], r["wid"]))
    else:
        con.execute("INSERT OR REPLACE INTO accolade_decision(entry_ref, album_library_id, work_id, decision, by_user, "
                    "at) VALUES (?,?,?,?,?,?)", (entry_ref, r["album_library_id"], r["wid"], decision, user,
                                                 time.time()))
    con.execute("INSERT INTO correction_log(op, payload_json, user, at) VALUES (?,?,?,?)",
                ("accolade_decide", json.dumps({"entry": entry_ref, "recording": recording_id,
                                                "decision": decision}), user, time.time()))
    return {"entry": entry_ref, "recording_id": recording_id, "decision": decision}


# ---- reading: badges and acclaim ----------------------------------------------------------------------------


def tier_weight(kind: str, award: str | None, result: str | None, rating: float | None = None,
                rating_count: int | None = None) -> float:
    t = tiers()
    if kind == "review":
        award = "CritiqueBrainz review"
        if rating is None or rating < 4:
            return 1.0
    elif kind == "rating":
        award = "Discogs rating"
        if rating is None or rating < 4.0 or (rating_count or 0) < t["min_ratings"]:
            return 1.0
    tier = t["awards"].get(award or "")
    if tier is None and award:
        tier = next((v for k, v in t["awards"].items() if k.lower() in award.lower()), None)
    w = t["tiers"].get(tier or "B" if kind == "award" else tier or "", 1.0)
    if result == "nominee":
        w = min(w, t["nomination"])
    return float(w)


def acclaim(accs: Iterable[dict]) -> float:
    """The §5.9 multiplier: 1.0 without counted accolades, the strongest weight plus a little for each further
    one, capped."""
    t = tiers()
    ws = sorted((a["weight"] for a in accs if a.get("counts") and a.get("weight", 1.0) > 1.0), reverse=True)
    if not ws:
        return 1.0
    return round(min(t["cap"], ws[0] + sum(t["extra"] * (w - 1) for w in ws[1:])), 3)


def pick_score(rec: dict, accs: Iterable[dict]) -> float:
    """How strongly to prefer a recording for a pick (Play, seasonal curation): acclaim x evidence."""
    ev = 1 + 0.15 * bool(rec.get("complete")) + 0.1 * (rec.get("status") == "confirmed") \
        + 0.02 * min(int(rec.get("evidence_fields") or 0), 10)
    return round(acclaim(accs) * ev, 4)


def best_pick(recs: list[dict], accs: dict[str, list[dict]] | None = None) -> tuple[dict, str] | None:
    """§6 ranking with acclaim: complete set first, then acclaim, then confirmed, most evidence, lowest id.
    The reason names the deciding accolade when acclaim decided."""
    if not recs:
        return None
    accs = accs or {}
    score = {r["id"]: acclaim(accs.get(r["id"], [])) for r in recs}
    r = min(recs, key=lambda r: (-r["complete"], -score[r["id"]], r["status"] != "confirmed", -r["evidence_fields"],
                                 r["id"]))
    if len(recs) == 1:
        return r, "only recording"
    rivals = [x for x in recs if x["id"] != r["id"] and x["complete"] == r["complete"]]
    if score[r["id"]] > 1.0 and all(score[x["id"]] < score[r["id"]] for x in rivals):
        top = max((a for a in accs.get(r["id"], []) if a.get("counts")), key=lambda a: a["weight"])
        return r, top["text"] + (f" ({top['category']})" if top.get("category") else "")
    return r, "complete movement set" if r["complete"] else "most evidence"


def _badge(row: dict, single: bool) -> dict:
    """One accolade as the UI shows it. `single`: the album holds just this recording."""
    kind, grade_ = row["kind"], row["grade"]
    album_level = row["target"] == "album"
    counts = grade_ in COUNTED_GRADES and (not album_level or (single and row["source"] in NET_SOURCES))
    if kind == "award":
        text = badge_text(row["award"], row["result"], row["year"])
    elif kind == "rating":
        text = f"{row['publication'] or 'Rating'} {row['rating']:.1f}/{row['rating_scale']:.0f}"
    else:
        text = f"{row['publication'] or 'Review'}" + (f" {row['rating']:.0f}/{row['rating_scale']:.0f}"
                                                      if row.get("rating") else "")
    note = None
    if album_level and not counts:
        note = "for the album"
    elif grade_ == "other_release":
        note = f"awarded to another release of this recording ({row['year']})" if row.get("year") \
            else "for another release of this recording"
    elif grade_ == "confirmed":
        note = "match confirmed by a user"
    out = {"id": row["id"], "kind": kind, "text": text, "award": row["award"], "category": row["category"],
           "year": row["year"], "result": row["result"], "grade": grade_, "scope": row["target"], "note": note,
           "url": row["url"], "source": row["source"], "counts": counts,
           "weight": tier_weight(kind, row["award"], row["result"], row.get("rating"), row.get("rating_count"))
           if counts else 1.0}
    if kind in ("review", "rating"):
        out.update(publication=row["publication"], reviewer=row["reviewer"], rating=row["rating"],
                   rating_scale=row["rating_scale"], rating_count=row["rating_count"], excerpt=row["excerpt"],
                   excerpt_license=row["excerpt_license"])
    return out


def for_recordings(con, recs: list[dict], now: float | None = None) -> dict[str, list[dict]]:
    """Shown accolades per recording id (recording-level, then the album's), for recordings as `Index.recordings`
    returns them. Uncertain and expired rows are left out."""
    if not recs:
        return {}
    now = time.time() if now is None else now
    rids = [r["id"] for r in recs]
    # a recording joined across discs also shows what was found for its later discs' albums
    albums_of: dict[str, list[tuple[str, str]]] = {r["id"]: [(r["id"], r["album_library_id"])] for r in recs
                                                   if r.get("album_library_id")}
    for x in con.execute("SELECT joined_to, id, album_library_id FROM recording WHERE joined_to IN "
                         "(SELECT value FROM json_each(?))", (json.dumps(rids),)):
        albums_of.setdefault(x[0], []).append((x[1], x[2]))
    aids = sorted({a for v in albums_of.values() for _, a in v})
    rows = [dict(x) for x in con.execute(
        "SELECT * FROM accolade WHERE grade<>'uncertain' AND (expires_at IS NULL OR expires_at>?) AND "
        "(recording_id IN (SELECT value FROM json_each(?)) OR (target='album' AND album_library_id IN "
        "(SELECT value FROM json_each(?)))) ORDER BY year IS NULL, year, award", (now, json.dumps(rids),
                                                                                  json.dumps(aids)))]
    if not rows:
        return {}
    single = {r[0]: r[1] == 1 for r in con.execute(
        "SELECT album_library_id, COUNT(DISTINCT COALESCE(joined_to, id)) FROM recording "
        "WHERE album_library_id IN (SELECT value FROM json_each(?)) GROUP BY album_library_id", (json.dumps(aids),))}
    out: dict[str, list[dict]] = defaultdict(list)
    by_album: dict[str, list[dict]] = defaultdict(list)
    rec_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["target"] == "recording":
            out[row["recording_id"]].append(_badge(row, False))
            rec_rows[row["recording_id"]].append(row)
        else:
            by_album[row["album_library_id"]].append(row)
    for r in recs:
        # the same entry found on two discs of one recording, or on the recording and an album: one badge
        seen: set[tuple[str, str]] = {(b["source"], b["source_ref"]) for b in rec_rows.get(r["id"], [])}
        for own, aid in albums_of.get(r["id"], []):
            for row in by_album.get(aid, []):
                only = (json.loads(row["why_json"] or "{}") or {}).get("recordings") if row["source"] == "dataset" \
                    else None
                if only is not None and own not in only and r["id"] not in only:
                    continue              # a dataset album award shows on the recordings it was matched on
                if (row["source"], row["source_ref"]) in seen:
                    continue
                seen.add((row["source"], row["source_ref"]))
                out[r["id"]].append(_badge(row, single.get(aid, False)))
    return dict(out)


def links_for(con, album_id: str | None) -> list[dict]:
    if not album_id:
        return []
    return [dict(r) for r in con.execute("SELECT kind, url, source FROM link WHERE entity='album' AND entity_id=? "
                                         "ORDER BY kind, url", (album_id,))]


# ---- review links (search URLs only) ---------------------------------------------------------------------


def _work_core(title: str) -> str:
    t = re.sub(r"\([^)]*\)", " ", title or "")
    t = t.split(":")[0] if ":" in t and len(t.split(":")[0]) > 3 else t
    t = _CATALOGUE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" ,;-")


_LINK_SITES = {"allmusic": "AllMusic", "discogs": "Discogs", "wikidata": "Wikidata", "wikipedia": "Wikipedia",
               "review": "Review", "other databases": "Other database"}


def review_links(composer: str | None, work_title: str, performer: str | None,
                 mb_links: Iterable[dict] = ()) -> list[dict]:
    """'Read reviews': search pages of classical review sites for this recording, plus AllMusic/Discogs pages
    MusicBrainz links to the album (else their searches). Links only: nothing is fetched."""
    comp = surname(composer, particles=False) if composer else ""
    who = surname(performer, particles=False) if performer and not is_group(performer) else (performer or "")
    q = " ".join(x for x in (comp, _work_core(work_title), who) if x).strip()
    out = [
        {"site": "Presto Music", "kind": "search",
         "url": f"https://www.prestomusic.com/classical/search?search_query={quote_plus(q)}"},
        {"site": "Gramophone", "kind": "search",
         "url": f"https://duckduckgo.com/?q={quote_plus('site:gramophone.co.uk ' + q)}"},
        {"site": "MusicWeb International", "kind": "search",
         "url": "https://www.musicweb-international.com/Search_engines.htm?cx=partner-pub-7131039333392991"
                f"%3Astvzk0-7fwo&cof=FORID%3A10&ie=ISO-8859-1&q={quote_plus(q)}"},
    ]
    have = set()
    for link in mb_links:
        kind = link.get("kind") or ""
        site = _LINK_SITES.get(kind)
        url = link.get("url") or ""
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]       # these sites all serve https; never link plain http
        if site and url.startswith("https://") and not re.search(r"[\s\"'<>]", url):
            out.append({"site": site, "kind": "page", "url": url})
            have.add(kind)
    if "allmusic" not in have:
        out.append({"site": "AllMusic", "kind": "search", "url": f"https://www.allmusic.com/search/albums/{quote(q)}"})
    if "discogs" not in have:
        out.append({"site": "Discogs", "kind": "search",
                    "url": f"https://www.discogs.com/search/?q={quote_plus(q)}&type=release"})
    return out


# ---- network sources (parsers are pure; fetching goes through MBClient instances) -------------------------

MB_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def parse_url_rels(data: dict) -> list[tuple[str, str]]:
    out = []
    for r in data.get("relations") or []:
        if r.get("target-type") == "url" and isinstance(r.get("url"), dict) and r["url"].get("resource"):
            out.append((r.get("type") or "url", r["url"]["resource"]))
    return out


def discogs_release_id(links: Iterable[tuple[str, str]]) -> str | None:
    for _, url in links:
        m = re.search(r"discogs\.com/(?:[a-z-]+/)?release/(\d+)", url)
        if m:
            return m.group(1)
    return None


def wikidata_qid(links: Iterable[tuple[str, str]]) -> str | None:
    for _, url in links:
        m = re.search(r"wikidata\.org/wiki/(Q\d+)$", url)
        if m:
            return m.group(1)
    return None


def parse_wikidata_awards(data: dict, qid: str) -> list[tuple[str, int | None]]:
    """(award item id, year) from P166 ('award received') with its P585 ('point in time') qualifier."""
    ent = (data.get("entities") or {}).get(qid) or {}
    out = []
    for c in (ent.get("claims") or {}).get("P166") or []:
        try:
            award = c["mainsnak"]["datavalue"]["value"]["id"]
        except (KeyError, TypeError):
            continue
        year = None
        for q in (c.get("qualifiers") or {}).get("P585") or []:
            m = re.match(r"[+-]?(\d{4})", ((q.get("datavalue") or {}).get("value") or {}).get("time") or "")
            if m:
                year = int(m.group(1))
        out.append((award, year))
    return out


def parse_wikidata_labels(data: dict) -> dict[str, str]:
    out = {}
    for qid, ent in (data.get("entities") or {}).items():
        lab = ((ent.get("labels") or {}).get("en") or {}).get("value")
        if lab:
            out[qid] = lab
    return out


def excerpt(text: str, n: int = EXCERPT_CHARS) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"[#*_>`]+", " ", text or "")).strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "…"


def parse_critiquebrainz(data: dict) -> list[dict]:
    out = []
    for r in data.get("reviews") or []:
        if not re.fullmatch(r"[0-9A-Za-z-]{1,64}", str(r.get("id") or "")) or r.get("is_hidden") or r.get("is_draft"):
            continue
        lic = r.get("license") or {}
        lic_name = lic.get("full_name") or lic.get("id") or r.get("license_id") or ""
        out.append({"id": r["id"], "rating": r.get("rating"), "text": excerpt(r.get("text") or ""),
                    "license": lic_name, "author": ((r.get("user") or {}).get("display_name") or ""),
                    "url": f"https://critiquebrainz.org/review/{r['id']}",
                    "year": _year(r.get("published_on") or r.get("created"))})
    return out


def parse_discogs(data: dict) -> tuple[float, int] | None:
    rating = ((data.get("community") or {}).get("rating") or {})
    try:
        return float(rating["average"]), int(rating["count"])
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(slots=True)
class NetResult:
    links: list[tuple[str, str]] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)          # accolade rows (album-level)
    sources: set[str] = field(default_factory=set)          # sources that answered: their old rows are replaced
    release_group: str | None = None


async def fetch_album(album: dict, mb=None, wikidata=None, critiquebrainz=None, discogs=None) -> NetResult:
    """Network facts for one album (no database access). `album`: album_info row plus `release` from evidence.
    Each client is an MBClient (rate-limited, cached) or None when that source is off."""
    from .mb import MBError, MBTransientError

    async def get(client, url: str) -> dict | None:
        """The JSON, or None for a permanent miss (404, 400); transient failures propagate so the album is
        retried on a later run."""
        try:
            return await client.get(url)
        except MBTransientError:
            raise
        except MBError:
            return None

    res = NetResult()
    now = time.time()
    aid = album["album_library_id"]
    rg = album.get("mb_release_group")
    release = album.get("mb_release") or album.get("release")
    if mb is not None:
        base = mb.base_url
        if release and MB_UUID.fullmatch(release):
            data = await get(mb, f"{base}/release/{release}?inc=url-rels+release-groups&fmt=json")
            if data:
                res.links += parse_url_rels(data)
                rg = rg or ((data.get("release-group") or {}).get("id"))
        if rg and MB_UUID.fullmatch(rg):
            data = await get(mb, f"{base}/release-group/{rg}?inc=url-rels&fmt=json")
            if data:
                res.links += parse_url_rels(data)
        res.sources.add("musicbrainz")
    res.release_group = rg
    links = list(dict.fromkeys(res.links))
    res.links = links
    if wikidata is not None:
        qid = wikidata_qid(links)
        res.sources.add("wikidata")
        data = await get(wikidata, f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json") if qid else None
        awards = parse_wikidata_awards(data, qid) if data and qid else []
        if awards:
            ids = "|".join(sorted({a for a, _ in awards}))
            labels = parse_wikidata_labels(await get(
                wikidata, f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={ids}&props=labels"
                "&languages=en&format=json") or {})
            for award, year in awards:
                res.rows.append({"id": _acc_id("wikidata", aid, award, str(year)), "kind": "award",
                                 "award": labels.get(award, award), "year": year, "result": "winner",
                                 "url": f"https://www.wikidata.org/wiki/{qid}", "source": "wikidata",
                                 "source_ref": f"{qid}/P166/{award}", "fetched_at": now})
    if critiquebrainz is not None and rg and MB_UUID.fullmatch(rg):
        res.sources.add("critiquebrainz")
        data = await get(critiquebrainz, f"https://critiquebrainz.org/ws/1/review/?entity_id={rg}"
                         "&entity_type=release_group&limit=5&sort=popularity")
        for r in parse_critiquebrainz(data or {}):
            res.rows.append({"id": _acc_id("critiquebrainz", aid, r["id"]), "kind": "review",
                             "publication": "CritiqueBrainz", "reviewer": r["author"], "rating": r["rating"],
                             "rating_scale": 5 if r["rating"] is not None else None, "year": r["year"],
                             "url": r["url"], "source": "critiquebrainz", "source_ref": r["id"],
                             "excerpt": r["text"], "excerpt_license": r["license"], "fetched_at": now})
    if discogs is not None:
        did = album.get("discogs_release") or discogs_release_id(links)
        res.sources.add("discogs")
        got = None
        if did and str(did).isdigit():
            got = parse_discogs(await get(discogs, f"https://api.discogs.com/releases/{did}") or {})
        if got:
            res.rows.append({"id": _acc_id("discogs", aid, str(did)), "kind": "rating",
                             "publication": "Discogs", "rating": got[0], "rating_scale": 5,
                             "rating_count": got[1], "url": f"https://www.discogs.com/release/{did}",
                             "source": "discogs", "source_ref": str(did), "fetched_at": now,
                             "expires_at": now + DISCOGS_TTL})
    return res


def album_for_enrichment(con, limit: int = 20, now: float | None = None) -> list[dict]:
    """Albums whose network facts are missing or older than NET_REFRESH (writer or reader thread)."""
    now = time.time() if now is None else now
    rows = con.execute(
        "SELECT a.album_library_id, a.mb_release, a.mb_release_group, a.discogs_release, "
        "(SELECT substr(e.edge, 9) FROM evidence e JOIN occurrence o ON o.library_track_id=e.entity_id "
        " WHERE e.entity='release_track' AND e.edge LIKE 'release:%' AND e.retracted_at IS NULL "
        " AND o.album_library_id=a.album_library_id LIMIT 1) AS release "
        "FROM album_info a WHERE a.enriched_at IS NULL OR a.enriched_at<? ORDER BY a.enriched_at IS NOT NULL, "
        "a.enriched_at, a.album_library_id LIMIT ?", (now - NET_REFRESH, limit)).fetchall()
    return [dict(r) for r in rows]


def store_net(con, album_id: str, res: NetResult, now: float | None = None) -> None:
    """Replace one album's network facts (writer thread)."""
    now = time.time() if now is None else now
    con.execute("BEGIN IMMEDIATE")
    try:
        if "musicbrainz" in res.sources:
            con.execute("DELETE FROM link WHERE entity='album' AND entity_id=? AND source='musicbrainz'", (album_id,))
            for kind, url in res.links:
                con.execute("INSERT OR IGNORE INTO link(entity, entity_id, kind, url, source) VALUES "
                            "('album',?,?,?,'musicbrainz')", (album_id, kind, url))
        for src in res.sources & set(NET_SOURCES):
            con.execute("DELETE FROM accolade WHERE album_library_id=? AND source=?", (album_id, src))
        for r in res.rows:
            con.execute(
                "INSERT OR REPLACE INTO accolade(id, target, album_library_id, kind, award, year, result, publication, "
                "reviewer, rating, rating_scale, rating_count, url, source, source_ref, grade, excerpt, "
                "excerpt_license, fetched_at, expires_at) VALUES (?,'album',?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "'this_release',?,?,?,?)",
                (r["id"], album_id, r["kind"], r.get("award"), r.get("year"), r.get("result"), r.get("publication"),
                 r.get("reviewer"), r.get("rating"), r.get("rating_scale"), r.get("rating_count"), r.get("url"),
                 r["source"], r["source_ref"], r.get("excerpt"), r.get("excerpt_license"), r["fetched_at"],
                 r.get("expires_at")))
        con.execute("UPDATE album_info SET enriched_at=?, mb_release_group=COALESCE(mb_release_group, ?) "
                    "WHERE album_library_id=?", (now, res.release_group, album_id))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def set_album_ids(con, album_id: str, label: str | None, barcode: str | None, mb_release: str | None,
                  mb_release_group: str | None, discogs_release: str | None) -> bool:
    """Store MA's identifiers for an album (kept out of the album signature, so no reindex); a change makes the
    album due for enrichment again. Returns whether anything changed."""
    r = con.execute("SELECT label, barcode, mb_release, mb_release_group, discogs_release FROM album_info "
                    "WHERE album_library_id=?", (album_id,)).fetchone()
    if r is None:
        return False
    new = (label, barcode, mb_release, mb_release_group, discogs_release)
    if tuple(r) == new:
        return False
    con.execute("UPDATE album_info SET label=?, barcode=?, mb_release=?, mb_release_group=?, discogs_release=?, "
                "enriched_at=NULL WHERE album_library_id=?", (*new, album_id))
    return True
