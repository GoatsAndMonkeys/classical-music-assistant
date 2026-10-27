"""Name resolution (PLAN.md §5.11): raw credit strings -> person identities.

Role-generic by design (composers now; performers and conductors later), MA-free and mostly pure:

(a) `parse_names` splits a multi-value field, drops role-tagged entries that are not the wanted role, keeps
    life dates as matching evidence, turns "Last, First" around and dedupes;
(b) `authority_match` matches the bundled composer list exactly; a lone surname resolves only when it is
    unambiguous in the list and either the album context supports it or nothing else is plausible;
(c) `mb_search_url` / `pick_mb_artist` resolve a still-unknown full name through a MusicBrainz artist search,
    accepting only a high-confidence Person whose name or alias matches and whose life span agrees;
(d) `compatible` is the conservative fuzzy test used to *propose* merges for review, never to apply them;
(e) `LLMClient` is an optional OpenAI-compatible fallback whose answers are cached proposals;
(f) user corrections are applied by the index before any of this (they win over everything).

The index stores what (c) and (e) return as evidence (entity 'name', entity_id = the parsed name's key), so
derivation stays synchronous and reproducible.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .composers import PERFORMING, WORLD_AMBIGUOUS, KnownComposer, by_surname, lookup
from .mb import is_mbid
from .text import _PARTICLES, _SUFFIXES, norm_key, surname

log = logging.getLogger("classical.names")

# ---- (a) parsing ---------------------------------------------------------------------------------------

_SPLIT = re.compile(r"\s*(?:/|;|\x00|\s&\s)\s*")
_ROLE_TAG = re.compile(r"\s*\[(?P<r>[^\]\d]{2,30})\]\s*")
_DATES = re.compile(r"\s*[(\[]\s*(?:b\.\s*|c\.\s*|ca\.\s*)?(?P<b>\d{3,4})?\s*(?:[-–—]|\bto\b)\s*"
                    r"(?:c\.\s*)?(?P<d>\d{3,4})?\s*[)\]]\s*")
_ARR = re.compile(r"\s*(?:,\s*|\(\s*|\s)(?:arr\.|arranged by|arr\b|orch\.|orchestrated by|transcr\.|"
                  r"transcribed by|bearb\.)\s*(?P<who>[^)]*)\)?\s*$", re.IGNORECASE)
# role tags that are not the composer ("Sonnleithner, Joseph von [Author]")
NOT_COMPOSER = {"author", "lyricist", "librettist", "libretto", "text", "words", "writer", "poet", "translator",
                "arranger", "arr", "orchestrator", "editor", "lyrics", "book", "adapter", "performer", "conductor"}
COMPOSER_TAGS = {"composer", "music", "komponist", "compositeur", "compositore"}
# credits that name nobody ('Unknown', 'Various Artists', 'N/A'): no composer at all, so the track takes the
# usual no-composer path (context composer, else not classical) instead of creating a composer 'Unknown'.
# A parenthetical qualifier does not change that: 'Various (traditional carols and hymns, with readings)'.
PLACEHOLDER_NAMES = frozenset({
    "various", "various artists", "various artist", "various composers", "various composer", "va", "v a",
    "unknown", "unknown artist", "unknown artists", "unknown composer", "unknown composers", "unknown author",
    "n a", "na", "none", "no artist", "not applicable", "tba", "tbc", "tbd", "unidentified",
    "artista desconocido", "compositeur inconnu", "unbekannt",
})
# credits for real but unnamed authorship: one shared non-person composer each, never looked up or merged
# as a person ('Anon.', '[anonymous]', 'Anonymous (plainsong)' -> 'Anonymous'; 'Trad.' -> 'Traditional')
NON_PERSON = {"anonymous": "Anonymous", "anon": "Anonymous", "anonym": "Anonymous", "anonyme": "Anonymous",
              "anonimo": "Anonymous", "anonymus": "Anonymous", "traditional": "Traditional", "trad": "Traditional",
              "traditionnel": "Traditional", "tradicional": "Traditional", "traditionell": "Traditional",
              "tradizionale": "Traditional"}
NON_PERSON_NAMES = frozenset(NON_PERSON.values())


@dataclass(frozen=True, slots=True)
class NameCandidate:
    raw: str                  # the segment as written
    name: str                 # display form: dates and role tag removed, "Last, First" turned around
    key: str                  # norm_key(name): the identity key used for evidence and aliases
    born: int | None = None   # life dates found in the string (evidence for matching, never displayed)
    died: int | None = None
    role_tag: str | None = None


def _display(name: str) -> str:
    """Tidy case: an all-lower or all-upper name is title-cased; inner particles ('Van') go lower."""
    if name.islower() or name.isupper():
        name = " ".join(w[:1].upper() + w[1:].lower() for w in name.split())
    parts = name.split()
    out = [p.lower() if 0 < i < len(parts) - 1 and p.lower() in _PARTICLES else p for i, p in enumerate(parts)]
    return " ".join(out)


def _depths(s: str) -> list[int]:
    """Bracket depth before each character of `s` (round and square brackets); [] when they do not balance,
    so callers fall back to the bracket-blind reading rather than swallowing the rest of the string."""
    out, d = [], 0
    for ch in s:
        out.append(d)
        if ch in "([":
            d += 1
        elif ch in ")]":
            d -= 1
            if d < 0:
                return []
    return out if d == 0 else []


def split_names(value: str) -> list[str]:
    """A multi-value field's segments, split on '/', ';', NUL and ' & ' outside brackets only:
    'Various (carols / hymns)' is one segment."""
    depth = _depths(value)
    out, start = [], 0
    for m in _SPLIT.finditer(value):
        if depth and depth[m.start()]:
            continue
        out.append(value[start:m.start()])
        start = m.end()
    out.append(value[start:])
    return out


def _invert(name: str) -> str:
    """'Beethoven, Ludwig van' -> 'Ludwig van Beethoven'; 'Strauss II, Johann' -> 'Johann Strauss II'.
    Only one comma outside brackets, and not before a suffix ('Sammy Davis, Jr.'); a comma inside a
    parenthetical ('Various (carols and hymns, with readings)') is not a name separator."""
    depth = _depths(name)
    commas = [i for i, ch in enumerate(name) if ch == "," and (not depth or not depth[i])]
    if len(commas) != 1 or (not depth and "(" in name):
        return name
    last, first = name[:commas[0]].strip(), name[commas[0] + 1:].strip()
    if not last or not first or first.lower().rstrip(".") in {s.rstrip(".") for s in _SUFFIXES}:
        return name
    if any(ch.isdigit() for ch in first) or len(first.split()) > 4:
        return name
    return f"{first} {last}"


def parse_one(segment: str) -> NameCandidate | None:
    s = segment.strip()
    if not s:
        return None
    if norm_key(s) in NON_PERSON:       # '[anonymous]', '[traditional]' (MusicBrainz's spellings): not role tags
        name = NON_PERSON[norm_key(s)]
        return NameCandidate(s, name, norm_key(name))
    role = None
    m = _ROLE_TAG.search(s)
    if m:
        role = m.group("r").strip().lower().rstrip(".")
        s = (s[: m.start()] + " " + s[m.end():]).strip()
    born = died = None
    m = _DATES.search(s)
    if m:
        born = int(m.group("b")) if m.group("b") else None
        died = int(m.group("d")) if m.group("d") else None
        s = (s[: m.start()] + " " + s[m.end():]).strip()
    m = _ARR.search(s)
    if m and m.start() > 0:
        s = s[: m.start()].strip()
    s = re.sub(r"\s+", " ", _invert(s.strip(" ,;"))).strip()
    if not s:
        return None
    head = norm_key(re.sub(r"\s*[(\[].*$", "", s)) or norm_key(s)   # 'Anonymous (plainsong)' -> 'anonymous'
    name = NON_PERSON.get(head) or _display(s)
    return NameCandidate(segment.strip(), name, norm_key(name), born, died, role)


def parse_names(raw: str | Iterable[str] | None, role: str = "composer") -> list[NameCandidate]:
    """Every distinct name of `role` in one field (or a list of values)."""
    values = [raw] if isinstance(raw, str) else list(raw or [])
    out: list[NameCandidate] = []
    for v in values:
        if is_placeholder(v):
            continue        # 'N/A' is one placeholder, not the names 'N' and 'A'
        for seg in split_names(v or ""):
            c = parse_one(seg)
            if c is not None and c.key and not is_placeholder(c.name):
                out.append(c)
    if role == "composer":
        if any(c.role_tag in COMPOSER_TAGS for c in out):
            out = [c for c in out if c.role_tag in COMPOSER_TAGS]
        out = [c for c in out if c.role_tag not in NOT_COMPOSER]
    merged: dict[str, NameCandidate] = {}
    for c in out:
        prev = merged.get(c.key)
        if prev is None:
            merged[c.key] = c
        elif (prev.born is None and c.born) or (prev.died is None and c.died):
            merged[c.key] = NameCandidate(prev.raw, prev.name, prev.key, prev.born or c.born, prev.died or c.died,
                                          prev.role_tag or c.role_tag)
    return list(merged.values())


def is_placeholder(name: str | None) -> bool:
    """'Unknown', '[unknown]', 'Various Artists', 'N/A', 'Various (traditional carols, with readings)'."""
    s = (name or "").strip()
    if not norm_key(s):
        return bool(s)      # '?', '-', '[]'
    head = re.sub(r"\s*[(\[].*$", "", s) if not s.startswith(("(", "[")) else s
    return norm_key(s) in PLACEHOLDER_NAMES or norm_key(head) in PLACEHOLDER_NAMES


_ALBUM_PREFIX = re.compile(r"^\s*(?P<c>[^:/]+?)\s*(?::|/|\s-\s|-\s)\s*\S")


def album_name_prefix(album_name: str | None) -> str | None:
    """'Beethoven: Fidelio Disc 1', 'Beethoven/Fidelio', 'Beethoven- Fidelio' -> 'Beethoven' (a short
    name-like prefix only; resolution decides whether it is a composer)."""
    m = _ALBUM_PREFIX.match(album_name or "")
    if not m:
        return None
    c = m.group("c").strip()
    if not _depths(c) and any(ch in c for ch in "()[]"):
        return None     # 'readings): A Festival…' — the tail of a parenthetical cut off upstream, not a name
    return c if 0 < len(c.split()) <= 4 and not any(ch.isdigit() for ch in c) else None


# ---- (b) authority list --------------------------------------------------------------------------------


@dataclass(slots=True)
class NameContext:
    """What one album says about its names: catalogue prefixes on its titles (K. -> mozart) and the full
    names already resolved on it (a sibling track credited 'Ludwig van Beethoven')."""
    catalogue_surnames: set[str] = field(default_factory=set)
    siblings: set[str] = field(default_factory=set)       # KnownComposer names resolved by full name
    titles: list[str] = field(default_factory=list)       # norm_key of the album, track and work titles


def dates_agree(c: NameCandidate, born: int | None, died: int | None) -> bool:
    for a, b in ((c.born, born), (c.died, died)):
        if a is not None and b is not None and abs(a - b) > 1:
            return False
    return True


# initials shared by several well-known bearers of a listed surname (Johann I, Johann II, Josef Strauss)
AMBIGUOUS_INITIALS = {"j strauss", "j strauss ii", "j c bach", "j m haydn"}


def is_lone_surname(name: str) -> bool:
    toks = [t for t in name.split() if t.lower() not in _PARTICLES]
    return len(toks) == 1


def authority_match(c: NameCandidate, ctx: NameContext | None = None) -> tuple[KnownComposer | None, str]:
    """(composer, how) — how is 'exact', 'surname+context', 'surname only', or the abstain reason."""
    k = lookup(c.name)
    if k is not None:
        return (k, "exact") if dates_agree(c, k.born, k.died) else (None, "life dates disagree with the list")
    if not is_lone_surname(c.name):
        # initials only ('Strauss, R.', 'W. A. Mozart'): one listed bearer of the surname must fit them
        given = _given(c.name)
        if given and all(len(g) == 1 for g in given) and norm_key(c.name) not in AMBIGUOUS_INITIALS:
            fits = [h for h in by_surname(c.name) if dates_agree(c, h.born, h.died) and _initials_fit(given, h.name)]
            if len(fits) == 1:
                return fits[0], "initials"
            return None, "ambiguous initials" if fits else "not in the list"
        return None, "not in the list"
    hits = [h for h in by_surname(c.name) if dates_agree(c, h.born, h.died)]
    if len(hits) != 1:
        return None, "ambiguous surname" if hits else "not in the list"
    h = hits[0]
    sk = norm_key(surname(h.name, particles=False))
    ctx = ctx or NameContext()
    if sk in ctx.catalogue_surnames or h.name in ctx.siblings:
        return h, "surname+context"
    if sk in WORLD_AMBIGUOUS:
        return None, "surname shared outside the list"
    return h, "surname only"


def is_performing_composer(k: KnownComposer | None) -> bool:
    return k is not None and k.name in PERFORMING


# ---- (d) conservative compatibility (review proposals only) --------------------------------------------


def _given(name: str) -> list[str]:
    toks = [t for t in norm_key(name).split() if t not in _PARTICLES and t not in {"jr", "sr", "ii", "iii"}]
    return toks[:-1]


def _initials_fit(initials: list[str], full: str) -> bool:
    g = _given(full)
    return len(initials) == len(g) and all(full_tok.startswith(i) for i, full_tok in zip(initials, g, strict=False))


def compatible(a: str, b: str) -> str | None:
    """Why two different names could be one person ('same surname, compatible initials'), or None.
    Same surname is required; given names must agree token by token where both have them, an initial
    matching a full name. Never used to merge on its own."""
    if norm_key(a) == norm_key(b):
        return None
    sa, sb = norm_key(surname(a, particles=False)), norm_key(surname(b, particles=False))
    if not sa or sa != sb:
        return None
    ga, gb = _given(a), _given(b)
    if not ga or not gb:
        return "same surname, one name has no given name"
    ta, tb = set(ga), set(gb)
    if ta <= tb or tb <= ta:
        return "same surname, given names are a subset"
    for x, y in zip(ga, gb, strict=False):
        if not (x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))):
            return None
    return "same surname, compatible initials"


def _alike(x: str, y: str) -> bool:
    return x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y)) or (
        min(len(x), len(y)) >= 4 and SequenceMatcher(None, x, y).ratio() >= 0.8)


def respelling_fits(c: NameCandidate, canonical: str, born: int | None = None, died: int | None = None) -> bool:
    """Could `canonical` be the person credited as `c`: the surname alike (a slip of a letter or two,
    'Beethofen'), the given names alike or initials of them in order, the life dates agreeing? The guard on
    language-model answers: 'Alexander Prokofiev' -> 'Sergei Prokofiev' does not fit (§5.11 d)."""
    if not dates_agree(c, born, died):
        return False
    sa, sb = norm_key(surname(c.name, particles=False)), norm_key(surname(canonical, particles=False))
    if not sa or not sb or not _alike(sa, sb):
        return False
    ga, gb = _given(c.name), _given(canonical)
    if not ga:
        return True
    if not gb:
        return False
    short, long_ = (ga, gb) if len(ga) <= len(gb) else (gb, ga)
    it = iter(long_)
    return all(any(_alike(x, y) for y in it) for x in short)


def given_name_conflict(c: NameCandidate) -> KnownComposer | None:
    """'Alexander Prokofiev': a full name whose surname is unique in the list but whose given name differs
    (Sergei). Proposed for review: tags sometimes copy a word of the work title into the name."""
    if is_lone_surname(c.name) or lookup(c.name):
        return None
    hits = by_surname(c.name)
    if len(hits) != 1:
        return None
    return hits[0] if compatible(c.name, hits[0].name) is None else None


# real composers whose given name is also a famous work of the listed bearer of their surname
_TITLE_NAMESAKES = {"siegfried wagner"}


def given_name_from_title(c: NameCandidate, k: KnownComposer, ctx: NameContext | None) -> bool:
    """'Alexander Prokofiev' on 'Alexander Nevsky, Op. 78': the one given name is a word of a listed work of
    the known composer ('Alexander Nevsky', work_identity) that a title on the album names, the name never
    appears there followed by the surname (that would make it a real credit), the surname is unique in the
    list (given_name_conflict) and not shared by other notable composers (WORLD_AMBIGUOUS: Leopold Mozart),
    and the life dates agree. The tag copied a word of the work title."""
    if ctx is None or not dates_agree(c, k.born, k.died):
        return False
    given = _given(c.name)
    if len(given) != 1 or len(given[0]) < 4:
        return False
    g, sk = given[0], norm_key(surname(c.name, particles=False))
    if sk in WORLD_AMBIGUOUS or norm_key(c.name) in _TITLE_NAMESAKES:
        return False
    from .work_identity import known_names

    word = re.compile(rf"\b{re.escape(g)}\b")
    works = [n for n in known_names(k.name) if word.search(n)]
    if not works:
        return False
    named = False
    for t in ctx.titles:
        for m in word.finditer(t):
            if t[m.end():].lstrip().startswith(sk):
                return False        # 'Alexander Prokofiev' written out in a title: a real name
        named = named or any(re.search(rf"\b{re.escape(n)}\b", t) for n in works)
    return named


# ---- (c) MusicBrainz artist search ---------------------------------------------------------------------

MB_MIN_SCORE = 90


def _lucene(s: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


def mb_search_url(base_url: str, name: str) -> str:
    """A Person search on the name *and* its aliases: MusicBrainz names many composers in their own script
    or order ('Дмитрий Дмитриевич Шостакович', 'Bartók Béla'), so `artist:` alone finds no one. A changed
    query is a new URL, so answers cached for an older query are never reused."""
    n = _lucene(name)
    q = f'(artist:"{n}" OR alias:"{n}") AND type:person'
    return f"{base_url.rstrip('/')}/artist?query={urllib.parse.quote(q)}&limit=5&fmt=json"


def _year(v: Any) -> int | None:
    m = re.match(r"\s*(\d{3,4})", v or "") if isinstance(v, str) else None
    return int(m.group(1)) if m else None


def _artist_keys(a: dict) -> set[str]:
    keys = {norm_key(a.get("name"))}
    sn = a.get("sort-name") or ""
    keys.add(norm_key(_invert(sn)))
    for al in a.get("aliases") or []:
        keys.add(norm_key(al.get("name")))
        keys.add(norm_key(_invert(al.get("sort-name") or "")))
    keys.discard("")
    return keys


@dataclass(frozen=True, slots=True)
class MBArtist:
    mbid: str
    name: str
    born: int | None
    died: int | None
    score: int


def pick_mb_artist(c: NameCandidate, data: dict) -> tuple[MBArtist | None, str]:
    """The one high-confidence Person match in an artist search response, or (None, why)."""
    found = []
    for a in data.get("artists") or []:
        if (a.get("type") or "").lower() != "person" or not is_mbid(a.get("id")):
            continue
        try:
            score = int(a.get("score") or 0)
        except (TypeError, ValueError):
            continue
        if score < MB_MIN_SCORE or c.key not in _artist_keys(a):
            continue
        ls = a.get("life-span") or {}
        art = MBArtist(a["id"].lower(), a.get("name") or c.name, _year(ls.get("begin")), _year(ls.get("end")), score)
        if not dates_agree(c, art.born, art.died):
            continue
        found.append(art)
    if not found:
        return None, "no confident MusicBrainz match"
    if len({f.mbid for f in found}) > 1:
        return None, "several MusicBrainz artists match"
    return found[0], "musicbrainz"


def mb_artist_confirms(data: dict, canonical: str, c: NameCandidate | None = None) -> bool:
    """An artist lookup (artist/{mbid}?inc=aliases) whose name or an alias is `canonical` and, given the
    credit `c`, that is plausibly the person credited: an alias spelled like it, or a fitting respelling."""
    if (data.get("type") or "").lower() != "person":
        return False
    keys = _artist_keys(data)
    if norm_key(canonical) not in keys:
        return False
    if c is None:
        return True
    ls = data.get("life-span") or {}
    born, died = _year(ls.get("begin")), _year(ls.get("end"))
    return dates_agree(c, born, died) and (c.key in keys or respelling_fits(c, canonical))


# ---- (e) optional LLM fallback -------------------------------------------------------------------------

LLM_AUTO_CONFIDENCE = 0.9
_SYSTEM = (
    "You identify classical-music composers from messy credit strings. For each item, give the composer's "
    "canonical full name as MusicBrainz spells it, their MusicBrainz artist id only if you are certain of it, "
    "and your confidence from 0 to 1. If the string is not a composer or you are unsure, give canonical null "
    "and a low confidence. Reply with JSON only, exactly: "
    '{"results": [{"id": "<item id>", "canonical": "<name or null>", "mbid": "<id or null>", '
    '"confidence": <number>}]}')

Fetch = Callable[[str, dict[str, str], bytes], Awaitable[tuple[int, bytes]]]


@dataclass(frozen=True, slots=True)
class LLMProposal:
    canonical: str | None
    mbid: str | None
    confidence: float


class LLMClient:
    """OpenAI-compatible chat completions client. Sends only the names and a little album context."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None, fetch: Fetch | None = None,
                 batch_size: int = 20, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or None
        self.batch_size = batch_size
        self.timeout = timeout
        self._fetch = fetch or self._aiohttp_fetch

    async def _aiohttp_fetch(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        import aiohttp  # shipped with MA

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s, \
                s.post(url, data=body, headers=headers) as resp:
            return resp.status, await resp.read()

    async def resolve(self, items: list[dict]) -> dict[str, LLMProposal]:
        """items: {"id", "name", "album", "tracks"}; returns {id: proposal} for the answers that parse."""
        out: dict[str, LLMProposal] = {}
        for i in range(0, len(items), self.batch_size):
            batch = items[i: i + self.batch_size]
            try:
                out.update(await self._one_batch(batch))
            except Exception as e:  # noqa: BLE001 - an LLM failure only means no proposals
                log.warning("LLM name resolution failed: %r", e)
        return out

    async def _one_batch(self, batch: list[dict]) -> dict[str, LLMProposal]:
        body = json.dumps({
            "model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": json.dumps({"items": batch}, ensure_ascii=False)}],
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status, raw = await self._fetch(f"{self.base_url}/chat/completions", headers, body)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return parse_llm_reply(json.loads(raw), {b["id"] for b in batch})


def parse_llm_reply(data: dict, ids: set[str]) -> dict[str, LLMProposal]:
    """Strict: anything that is not the agreed shape is dropped."""
    content = data["choices"][0]["message"]["content"]
    content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content)
    reply = json.loads(content)
    out: dict[str, LLMProposal] = {}
    for r in reply.get("results") or []:
        if not isinstance(r, dict) or r.get("id") not in ids:
            continue
        canon = r.get("canonical")
        canon = canon.strip() if isinstance(canon, str) and canon.strip() else None
        mbid = r.get("mbid") if isinstance(r.get("mbid"), str) and is_mbid(r.get("mbid")) else None
        try:
            conf = min(1.0, max(0.0, float(r.get("confidence"))))
        except (TypeError, ValueError):
            continue
        out[r["id"]] = LLMProposal(canon, mbid.lower() if mbid else None, conf)
    return out

