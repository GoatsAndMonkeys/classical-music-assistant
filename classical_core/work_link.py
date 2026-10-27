"""Linking library works to MusicBrainz works (DEVIATIONS.md, "MusicBrainz work links"), MA-free.

Most works are identified from their titles alone, so composition dates (`dates`), cross-album identity
(`work_identity`) and anything else keyed on a MusicBrainz work id never reach them. This job proposes the
link, conservatively:

* only for a work whose first composer has a MusicBrainz artist id: the index's own (`alias`), the authority
  list's, or, for a listed composer without one, a Person artist search whose name and life dates agree
  (`names.pick_mb_artist`);
* candidates are that artist's works, read by browsing `work?artist=` (paged, 100 a page, cached like every
  MusicBrainz answer) or, when the composer has far more works on MusicBrainz than the library asks about,
  by a search per work (`arid:` plus the title's words) with a lookup of the work it picks;
* a link needs strong evidence: the same catalogue number (Op./BWV/K./D./Hob./WoO/RV/S./WWV…, all
  components) with agreeing titles, or the same form, number and key, or a distinctive name ('Kindertotenlieder')
  that only one of the composer's works carries. Parts of a work, arrangements and works the artist did not
  compose are never candidates;
* anything weaker or ambiguous becomes a review item ("Link to MusicBrainz work?") listing the candidates;
  the job never guesses;
* a person's decision (an `override` row, op 'mb_link') wins and survives re-index; a rejected candidate is
  never proposed again, and after a rejection the job only proposes.

A link is an `alias(work, 'mbid')` row, exactly what a file's MusicBrainz work tag gives, so the dates job
and the cross-album rules use it with no further change. `refresh` is offline (the library and cached
answers) and returns the requests it still needs; `fetch` is the rate-limited network step, a few requests a
job run. With MusicBrainz off nothing is fetched; cached answers are still used.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import urllib.parse
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import catalogue as cat
from .composers import lookup
from .mb import DEFAULT_BASE, WORK_INC, MBClient, MBError, MBTransientError, is_mbid
from .names import NameCandidate, mb_search_url, pick_mb_artist
from .text import fold, norm_key
from .titles import strip_track_number
from .work_identity import core_name, has_act_suffix, is_arrangement, title_sig, titles_agree

REASON = "Link to MusicBrainz work?"
MATCHER = "3"                   # bump when the rules change: every work the job decided is looked at again
PAGE = 100                      # MusicBrainz's largest browse page
BROWSE_INC = "aliases+artist-rels+work-rels"
MAX_PAGES = 60                  # a composer with more works than this many pages is searched work by work
SEARCH_LIMIT = 25
CHUNK = 10                      # requests per network job run (1 req/s on the shared client: ~10 s)
MAX_LISTED = 6                  # more weak candidates than this is no proposal at all
_ARRANGED_RELS = {"arrangement", "orchestration"}
_EXCERPT = re.compile(r"\b(?:excerpts?|extraits?|auszuge?|ausschnitte?|estratti|selections?)\b")
_GENERIC = {"adagio", "allegro", "andante", "largo", "presto", "scherzo", "minuet", "menuet", "overture",
            "prelude", "fugue", "aria", "song", "songs", "lieder", "dances", "march", "romance", "romanze",
            "serenade", "nocturne", "fantasia", "fantasy", "variations", "suite", "sonata", "symphony",
            "concerto", "quartet", "trio", "mass", "requiem", "te deum", "magnificat", "gloria", "stabat mater",
            "ave maria", "lullaby", "berceuse", "elegy", "elegie", "impromptu", "waltz", "polonaise", "mazurka"}


# ---- URLs ---------------------------------------------------------------------------------------------------

def browse_url(base: str, arid: str, page: int) -> str:
    return (f"{base.rstrip('/')}/work?artist={arid}&inc={BROWSE_INC}&limit={PAGE}&offset={page * PAGE}"
            "&fmt=json")


def search_url(base: str, arid: str, title: str) -> str:
    words = re.findall(r"[a-z0-9]+", fold(strip_track_number(title)))
    q = f"arid:{arid} AND work:({' '.join(dict.fromkeys(words))})"
    return f"{base.rstrip('/')}/work?query={urllib.parse.quote(q)}&limit={SEARCH_LIMIT}&fmt=json"


def lookup_url(base: str, mbid: str) -> str:
    """The indexer's own work lookup URL: its cached answer is shared with the hierarchy walk and dates."""
    return f"{base.rstrip('/')}/work/{mbid}?inc={WORK_INC}&fmt=json"


# ---- cached answers -------------------------------------------------------------------------------------

_MISSING = object()


class _Cache:
    """Cached MusicBrainz answers by URL: parsed JSON for a 200 (stale ones too: works hardly change),
    None for an unexpired error, missing otherwise (the URL is due)."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.memo: dict[str, Any] = {}

    def get(self, url: str) -> Any:
        if url in self.memo:
            return self.memo[url]
        r = self.con.execute("SELECT status, body_zlib, expires_at FROM mb_cache WHERE url=?", (url,)).fetchone()
        out: Any = _MISSING
        if r is not None and "/artist?" in url and r["expires_at"] < time.time():
            pass                        # a person search goes stale (an empty answer after a week): ask again
        elif r is not None:
            if r["status"] == 200 and r["body_zlib"]:
                try:
                    out = json.loads(zlib.decompress(r["body_zlib"]))
                except (ValueError, zlib.error):
                    out = _MISSING
            elif r["status"] != 200 and r["expires_at"] >= time.time():
                out = None
        self.memo[url] = out
        return out

    def has(self, url: str) -> bool:
        """Whether `url` has a usable cached answer, without reading it."""
        if url in self.memo:
            return self.memo[url] is not _MISSING
        r = self.con.execute("SELECT status, expires_at FROM mb_cache WHERE url=?", (url,)).fetchone()
        return r is not None and (r["status"] == 200 or r["expires_at"] >= time.time())


# ---- MusicBrainz candidates -----------------------------------------------------------------------------

@dataclass
class Candidate:
    mbid: str
    title: str
    names: list[str]                    # the title, then its aliases
    key: str | None
    cats: list[cat.Catalogue]
    composer: bool                      # the artist is credited as its composer
    part: bool                          # a part of another work
    arranged: bool                      # an arrangement or orchestration of another work
    revision: bool = False              # one edition or version of another work (Bruckner's 'ed. Haas')
    verified: bool = True               # read from a browse page or a lookup (relations complete)
    note: str = ""                      # MusicBrainz's disambiguation ('1877 version'), shown to a person


def candidate(w: dict, arid: str, verified: bool = True) -> Candidate | None:
    mbid = str(w.get("id") or "").lower()
    title = str(w.get("title") or "")
    if not is_mbid(mbid) or not title:
        return None
    rels = w.get("relations") or []
    composer = any(r.get("type") == "composer" and str((r.get("artist") or {}).get("id") or "").lower() == arid
                   for r in rels)
    part = any(r.get("type") == "parts" and r.get("direction") == "backward" and r.get("target-type", "work") == "work"
               for r in rels)
    # MusicBrainz often says it only in the disambiguation: 'Symphony no. 5, op. 82' (transcr. for piano by Ekman)
    arranged = is_arrangement(title) or is_arrangement(str(w.get("disambiguation") or "")) or any(r.get("type") in _ARRANGED_RELS and r.get("direction") == "backward"
                                            for r in rels)
    revs = {r.get("direction") for r in rels if r.get("type") == "revision of" and r.get("target-type", "work") == "work"}
    # one version of a work that has others: an edition (it revises another), or the first of several (others
    # revise it: Tchaikovsky's 1868 Symphony No. 1, Mozart's 40 without clarinets), unless MusicBrainz calls it
    # the one to use when the version is not known (Bruckner's 'generic, use if unsure')
    revision = "backward" in revs or ("forward" in revs and not _GENERIC_NOTE.search(str(w.get("disambiguation") or "")))
    # a title says it too, relations or not: 'Op. 67: IV. Allegro', 'Mass in E minor…: Kyrie', '(excerpt)'
    part = part or ": " in title or (not verified and ":" in title) or bool(_EXCERPT.search(fold(title)))
    key = next((a.get("value") for a in w.get("attributes") or [] if (a.get("type") or "").lower() == "key"), None)
    key = key or cat.parse_key(title)
    names = [title] + [a["name"] for a in w.get("aliases") or []
                       if a.get("name") and (a.get("type") or "") != "Search hint"]
    cats = cat.parse_catalogues(title)
    if not cats:
        cats = next((c for c in (cat.parse_catalogues(n) for n in names[1:]) if c), [])
    return Candidate(mbid, title, list(dict.fromkeys(names)), key, cats, composer, part, arranged, revision,
                     verified, str(w.get("disambiguation") or ""))


def usable(c: Candidate) -> bool:
    return c.composer and not c.part and not c.arranged


# ---- matching -------------------------------------------------------------------------------------------

@dataclass
class LibWork:
    id: str
    title: str
    key: str | None
    catalogue: str | None
    composer_id: str | None
    composer: str | None


@dataclass
class Decision:
    state: str                          # 'linked', 'review', 'none'
    mbid: str | None = None
    how: str | None = None
    candidates: list[dict] = field(default_factory=list)
    why: str | None = None


def _split_nos(c: cat.Catalogue) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(the number with its alternatives, the 'No.' components): 'K. 439b No. 2' -> (('439b',), ('2',))."""
    n = c.display.count(" No. ")
    return (c.components[:-n], c.components[-n:]) if n else (c.components, ())


def _same_cat(a: cat.Catalogue, b: cat.Catalogue) -> bool:
    if a.namespace != b.namespace:
        return False
    if a.components == b.components:
        return True
    # Köchel's alternative numbers: 'K. 427' is 'K. 427/417a'; the 'No.' after it must still be the same
    # ('K. 439b No. 1' is not 'K. 439b No. 2', nor the set 'K. 439b')
    if a.namespace != "k":
        return False
    (ah, an), (bh, bn) = _split_nos(a), _split_nos(b)
    return an == bn and bool(ah) and bool(bh) and (ah[0] in bh or bh[0] in ah)


def _cat_relation(mine: cat.Catalogue | None, theirs: list[cat.Catalogue]) -> bool | None:
    """True: the same number; False: a different number in the same catalogue; None: nothing to compare."""
    if mine is None:
        return None
    shared = [c for c in theirs if c.namespace == mine.namespace]
    if not shared:
        return None
    return any(_same_cat(mine, c) for c in shared)


_GENERIC_NOTE = re.compile(r"generic|unsure|uncertain|catch-all|unknown edition", re.IGNORECASE)
_ORCH = re.compile(r"\borch")
_ORCHESTRAL = {"symphony", "concerto", "overture"}
_COUNT = re.compile(r"\s*\d+\s")
_TUTTI = {"orchestra", "orchester"}
_NO_TUTTI = re.compile(r"\b(?:orchestra|orchester)\b", re.IGNORECASE)


def _same_scoring(a: str, b: str) -> bool:
    """Both titles name the same instruments, or neither does ('orchestra' aside): 'Sonata No. 1 in C' is
    not certainly 'Piano Sonata no. 1 in C major', while 'Piano Concerto No. 1' is 'Concerto for Piano and
    Orchestra no. 1'."""
    return title_sig(a).scoring - _TUTTI == title_sig(b).scoring - _TUTTI


def _agree(a: str, b: str) -> bool:
    """titles_agree, except that 'and Orchestra' alone is no different instrument."""
    why = titles_agree(a, b)
    return why is None or (why == "different instruments" and _same_scoring(a, b)
                           and titles_agree(_NO_TUTTI.sub("", a), _NO_TUTTI.sub("", b)) is None)


def lib_catalogue(w: LibWork) -> cat.Catalogue | None:
    return cat.primary_catalogue(w.title) or cat.primary_catalogue(w.catalogue)


def distinctive(title: str) -> bool:
    """A name on its own ('Kindertotenlieder', 'Das Lied von der Erde'), not a genre with a number."""
    sig = title_sig(title)
    core = core_name(title)
    return bool(sig.names) and not sig.numbers and len(core) >= 5 and core not in _GENERIC


def eligible(w: LibWork) -> str | None:
    """Why a work is not looked up at all, or None."""
    t = strip_track_number(w.title)
    if is_arrangement(t):
        return "an arrangement is its own work"
    if ": " in t or ";" in t or has_act_suffix(t):
        return "a part or a selection, not a whole work"
    if not norm_key(t):
        return "no title"
    return None


def decide(w: LibWork, cands: list[Candidate], complete: bool, rejected: set[str] = frozenset(),
           auto: bool = True) -> Decision:
    """The decision for one library work against one composer's MusicBrainz works. `complete`: every work
    of the composer is in `cands` (a browse), so uniqueness means something; a search answer is not."""
    pool = [c for c in cands if usable(c) and c.mbid not in rejected]
    mine = lib_catalogue(w)
    sig = title_sig(w.title)
    key = sig.key or w.key          # the title's own key first: a stored key may predate a parser fix
    core = core_name(w.title)
    strong: dict[str, tuple[Candidate, str]] = {}
    weak: dict[str, tuple[Candidate, str]] = {}
    by_core: dict[str, set[str]] = defaultdict(set)
    for c in pool:
        for n in c.names:
            by_core[core_name(n)].add(c.mbid)
    for c in pool:
        rel = _cat_relation(mine, c.cats)
        if rel is False:
            continue                                    # Op. 67 is not Op. 68
        keys_conflict = bool(key and c.key and key != c.key)
        agree = any(_agree(w.title, n) for n in c.names)
        if rel:
            why = f"same catalogue number ({mine.display})"
            if agree and not keys_conflict and not c.revision:
                strong[c.mbid] = (c, why)
            elif agree and not keys_conflict:
                weak[c.mbid] = (c, why + ", one edition of it")
            else:
                weak[c.mbid] = (c, why + ", but the titles differ" if not agree else why + ", but a different key")
            continue
        if keys_conflict:
            continue
        same_shape = sig.forms and any((s := title_sig(n)).forms == sig.forms and s.numbers == sig.numbers
                                       for n in c.names)
        if same_shape and agree:
            if sig.numbers and key and c.key and not c.revision and any(_same_scoring(w.title, n)
                                                                         for n in c.names):
                strong[c.mbid] = (c, f"same title, number and key ({key})")
            else:
                weak[c.mbid] = (c, "same kind of work and number" if sig.numbers else "same kind of work")
            continue
        if core and any(core_name(n) == core for n in c.names):
            # the name alone links only a work that is the name too: an alias 'Leonore' on the overture
            # 'Leonore no. 3' must not make the overture the opera 'Leonore'
            own = title_sig(c.title)
            plain = not own.numbers and own.forms <= sig.forms
            # '3 Hungarian Folk Tunes' found by its alias is not certainly the library's 'Hungarian Folk Songs'
            plain = plain and bool(_COUNT.match(c.title)) <= bool(_COUNT.match(w.title))
            if plain and distinctive(w.title) and complete and len(by_core[core]) == 1 and not c.revision:
                strong[c.mbid] = (c, "the same distinctive title, and the only such work")
            else:
                weak[c.mbid] = (c, "the same title")

    def listing(d: dict[str, tuple[Candidate, str]]) -> list[dict]:
        return [{"mbid": c.mbid, "title": c.title + (f" ({c.note})" if c.note else ""), "why": why}
                for c, why in d.values()]

    # an orchestration of a work not already orchestral exists (Schoenberg's of Brahms's Op. 25, Ravel's
    # Tzigane): an orchestra's recording titled as the original may be it, so the original is proposed, not
    # linked. A symphony's chamber or piano versions are no such doubt.
    shadow = not (sig.forms & _ORCHESTRAL) and any(
        c.composer and c.arranged and not c.part and _ORCH.search(fold(c.note))
        and not (key and c.key and key != c.key) and (
        _cat_relation(mine, c.cats) or (sig.forms and any(title_sig(n).forms == sig.forms
                                                          and title_sig(n).numbers == sig.numbers for n in c.names))
        or (core and any(core_name(n) == core for n in c.names))) for c in cands)
    if shadow:
        weak.update({m: (c, why + "; an arrangement of it exists too") for m, (c, why) in strong.items()})
        strong = {}
    if len(strong) == 1 and auto:
        (m, (c, why)), = strong.items()
        return Decision("linked", m, why, listing(strong))
    if strong:
        return Decision("review", candidates=listing(strong)[:MAX_LISTED],
                        why="several MusicBrainz works match" if len(strong) > 1 else
                        "a match, but you turned down another proposal for this work")
    if weak and len(weak) <= MAX_LISTED:
        return Decision("review", candidates=listing(weak), why="a possible match, not certain enough to link")
    return Decision("none", why="too many possible matches" if weak else "no matching MusicBrainz work")


# ---- the library side -----------------------------------------------------------------------------------

def _works(con: sqlite3.Connection) -> list[LibWork]:
    """Live works with recordings and no MusicBrainz work id yet."""
    rows = con.execute(
        "SELECT w.id, w.title, w.key, w.catalogue_display, c.id AS cid, c.name AS cname FROM work w "
        "LEFT JOIN work_composer wc ON wc.work_id=w.id AND wc.ord=(SELECT MIN(ord) FROM work_composer x "
        "  WHERE x.work_id=w.id) "
        "LEFT JOIN composer c ON c.id=wc.composer_id "
        "WHERE NOT EXISTS (SELECT 1 FROM redirect d WHERE d.entity='work' AND d.old_id=w.id) "
        "AND EXISTS (SELECT 1 FROM recording r WHERE r.work_id=w.id) "
        "AND NOT EXISTS (SELECT 1 FROM alias a LEFT JOIN redirect d ON d.entity='work' AND d.old_id=a.entity_id "
        "  WHERE a.entity='work' AND a.kind='mbid' AND COALESCE(d.new_id, a.entity_id)=w.id) "
        "ORDER BY w.id").fetchall()
    return [LibWork(r["id"], r["title"], r["key"], r["catalogue_display"], r["cid"], r["cname"]) for r in rows]


def _resolve(con: sqlite3.Connection, wid: str) -> str:
    for _ in range(10):
        r = con.execute("SELECT new_id FROM redirect WHERE entity='work' AND old_id=?", (wid,)).fetchone()
        if not r:
            break
        wid = r[0]
    return wid


def decisions(con: sqlite3.Connection) -> dict[str, dict]:
    """Live work id → {'accepted': mbid | None, 'rejected': set} from people's decisions (later ones win)."""
    out: dict[str, dict] = defaultdict(lambda: {"accepted": None, "rejected": set()})
    for r in con.execute("SELECT entity_id, payload_json FROM override WHERE entity='work' AND op='mb_link' "
                         "ORDER BY created_at, id"):
        try:
            p = json.loads(r["payload_json"])
        except ValueError:
            continue
        d = out[_resolve(con, r["entity_id"])]
        m = p.get("mbid")
        if p.get("decision") == "accept":
            d["accepted"] = m
            d["rejected"].discard(m)
        elif p.get("decision") == "reject":
            d["rejected"].add(m)
            if d["accepted"] == m:
                d["accepted"] = None
    return out


def composer_mbid(con: sqlite3.Connection, cache: _Cache, base: str, cid: str | None,
                  name: str | None) -> tuple[str | None, str | None]:
    """(the composer's MusicBrainz artist id, or None; a URL still to fetch, or None)."""
    if cid is None:
        return None, None
    r = con.execute("SELECT value FROM alias WHERE entity='composer' AND kind='mbid' AND entity_id=? "
                    "ORDER BY value LIMIT 1", (cid,)).fetchone()
    if r and is_mbid(r[0]):
        return r[0].lower(), None
    k = lookup(name)
    if k is not None and k.mbid:
        return k.mbid, None
    if k is not None:
        cand = NameCandidate(k.name, k.name, norm_key(k.name), k.born, k.died)
    else:
        # an unlisted composer the index's own search left without an id (an answer cached before the search
        # read aliases: 'Mikhail Glinka' is 'Михаил Иванович Глинка' on MusicBrainz): the same strict pick
        row = con.execute("SELECT born, died FROM composer WHERE id=?", (cid,)).fetchone()
        if not name or row is None:
            return None, None
        cand = NameCandidate(name, name, norm_key(name), _year(row["born"]), _year(row["died"]))
        if not cand.key or " " not in cand.key:
            return None, None           # a lone surname or nothing: never searched
    url = mb_search_url(base, cand.name)
    data = cache.get(url)
    if data is _MISSING:
        return None, url
    if not data:
        return None, None
    art, _why = pick_mb_artist(cand, data)
    return (art.mbid if art else None), None


def _year(v: Any) -> int | None:
    m = re.match(r"\s*(\d{3,4})", v) if isinstance(v, str) else None
    return int(m.group(1)) if m else (v if isinstance(v, int) else None)


def _sig(w: LibWork, arid: str | None) -> str:
    raw = json.dumps([MATCHER, w.title, w.key, w.catalogue, arid])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---- the job ----------------------------------------------------------------------------------------------

def refresh(con: sqlite3.Connection, base: str = DEFAULT_BASE, limit: int = CHUNK,
            ) -> tuple[dict[str, int], list[str]]:
    """Decide every work whose answers are cached, and list up to `limit` URLs still to fetch (composers
    with the fewest requests left first). Offline; writes in one transaction; the caller is the writer."""
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        out = _refresh(con, base, limit)
        if own:
            con.execute("COMMIT")
        return out
    except BaseException:
        if own:
            con.execute("ROLLBACK")
        raise


def _refresh(con: sqlite3.Connection, base: str, limit: int) -> tuple[dict[str, int], list[str]]:
    cache = _Cache(con)
    stats: dict[str, int] = defaultdict(int)
    due: list[tuple[int, str]] = []        # (requests the composer still needs, url)
    prior = {r["work_id"]: r for r in con.execute("SELECT work_id, sig, state, mbid FROM mb_work_match")}
    users = decisions(con)
    _unlink_stale(con, base, cache, prior)
    by_comp: dict[tuple[str | None, str | None], list[LibWork]] = defaultdict(list)
    live = _works(con)
    for w in live:
        by_comp[(w.composer_id, w.composer)].append(w)
    # a proposal for a work that has since gained an id (a file's tag) or gone (merged, removed) is moot
    ids = {w.id for w in live}
    for r in con.execute("SELECT entity_id FROM review_item WHERE reason=? AND id LIKE 'mbw:%'", (REASON,)).fetchall():
        if r[0] not in ids:
            con.execute("DELETE FROM review_item WHERE id=?", (f"mbw:{r[0]}",))
    writes: list[tuple[LibWork, str, str, Decision]] = []
    for (cid, cname), works in sorted(by_comp.items(), key=lambda kv: (kv[0][1] or "", kv[0][0] or "")):
        arid, need = composer_mbid(con, cache, base, cid, cname)
        if need:
            due.append((1, need))
            continue
        pending = []
        for w in works:
            s = _sig(w, arid)
            u = users.get(w.id)
            if u and u["accepted"]:
                # a person's link; a live work here has lost its alias (a rebuilt row): put it back
                if _holder(con, u["accepted"], w.id) is None:
                    con.execute("INSERT OR IGNORE INTO alias(entity, entity_id, kind, value) "
                                "VALUES ('work', ?, 'mbid', ?)", (w.id, u["accepted"]))
                continue
            p = prior.get(w.id)
            # a 'linked' work listed here has lost its alias: decided again (and linked again)
            if p is not None and p["sig"] == s and p["state"] not in ("user", "linked"):
                continue
            why = eligible(w) or (None if arid else "the composer has no MusicBrainz artist id")
            if why:
                writes.append((w, s, "skip", Decision("none", why=why)))
                continue
            pending.append((w, s))
        if not pending or arid is None:
            continue
        first = cache.get(browse_url(base, arid, 0))
        if first is _MISSING:
            due.append((1, browse_url(base, arid, 0)))
            continue
        if not first:
            stats["errors"] += 1
            continue
        pages = max(1, math.ceil(int(first.get("work-count") or 0) / PAGE))
        urls = [browse_url(base, arid, i) for i in range(pages)]
        # which pages are still due, without reading (decompressing) the ones that are not
        missing = [u for u in urls if not cache.has(u)]
        eligible_n = sum(1 for w in works if not eligible(w))
        if not missing or (pages <= MAX_PAGES and pages - 1 <= 2 * eligible_n):
            if missing:
                due += [(len(missing), u) for u in missing]
                continue
            got = [cache.get(u) for u in urls]
            if any(g is None or g is _MISSING for g in got):
                stats["errors"] += 1
                continue
            cands = [c for g in got for x in g.get("works") or [] if (c := candidate(x, arid))]
            for w, s in pending:
                d = decide(w, cands, True, users[w.id]["rejected"] if w.id in users else set(),
                           auto=not (w.id in users and users[w.id]["rejected"]))
                writes.append((w, s, "browse", d))
            continue
        # search mode: a request per work, then a lookup of the work(s) it would link or list
        for w, s in pending:
            su = search_url(base, arid, w.title)
            data = cache.get(su)
            if data is _MISSING:
                due.append((2, su))
                continue
            if data is None:
                stats["errors"] += 1
                continue
            rej = users[w.id]["rejected"] if w.id in users else set()
            auto = not rej
            cands = [c for x in data.get("works") or [] if (c := candidate(x, arid, verified=False))]
            d = decide(w, cands, False, rej, auto)
            todo = [c["mbid"] for c in d.candidates]
            looked = {m: cache.get(lookup_url(base, m)) for m in todo}
            wait = [lookup_url(base, m) for m, v in looked.items() if v is _MISSING]
            if wait:
                due += [(1, u) for u in wait]
                continue
            verified = {m: c for m, v in looked.items() if v and (c := candidate(v, arid))}
            cands = [verified.get(c.mbid, c) for c in cands if c.mbid not in looked or c.mbid in verified]
            writes.append((w, s, "search", decide(w, cands, False, rej, auto)))
    for w, s, mode, d in writes:
        _apply(con, w, s, mode, d, stats)
    due.sort(key=lambda x: x[0])
    return dict(stats), list(dict.fromkeys(u for _, u in due))[:limit]


def _unlink_stale(con: sqlite3.Connection, base: str, cache: _Cache, prior: dict) -> None:
    """An automatic link made under other rules (or from a title, key or catalogue since changed) is taken
    back, so the work is decided again now: a rule fixed after a wrong link undoes the link."""
    for wid, p in list(prior.items()):
        if p["state"] != "linked" or not p["mbid"]:
            continue
        r = con.execute(
            "SELECT w.title, w.key, w.catalogue_display, c.id AS cid, c.name AS cname FROM work w "
            "LEFT JOIN work_composer wc ON wc.work_id=w.id AND wc.ord=(SELECT MIN(ord) FROM work_composer x "
            "  WHERE x.work_id=w.id) LEFT JOIN composer c ON c.id=wc.composer_id WHERE w.id=? AND NOT EXISTS "
            "(SELECT 1 FROM redirect d WHERE d.entity='work' AND d.old_id=w.id)", (wid,)).fetchone()
        if r is None:
            continue
        arid, need = composer_mbid(con, cache, base, r["cid"], r["cname"])
        if need:
            continue                    # the composer's id is not known yet: decided when it is
        w = LibWork(wid, r["title"], r["key"], r["catalogue_display"], r["cid"], r["cname"])
        if _sig(w, arid) == p["sig"]:
            continue
        con.execute("DELETE FROM alias WHERE entity='work' AND kind='mbid' AND entity_id=? AND value=?",
                    (wid, p["mbid"]))
        con.execute("UPDATE mb_work_match SET state='none', mbid=NULL WHERE work_id=?", (wid,))
        prior[wid] = {"work_id": wid, "sig": None, "state": "none", "mbid": None}


def _review(con: sqlite3.Connection, w: LibWork, d: Decision) -> None:
    con.execute("INSERT OR REPLACE INTO review_item(id, reason, album_library_id, entity, entity_id, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (f"mbw:{w.id}", REASON, None, "work", w.id,
                 json.dumps({"title": w.title, "composer": w.composer, "why": d.why, "candidates": d.candidates},
                            ensure_ascii=False)))


def _holder(con: sqlite3.Connection, mbid: str, wid: str) -> str | None:
    """The other library work already carrying this MusicBrainz work id, or None."""
    for r in con.execute("SELECT entity_id FROM alias WHERE entity='work' AND kind='mbid' AND value=?", (mbid,)):
        other = _resolve(con, r[0])
        if other != wid:
            return other
    return None


def _apply(con: sqlite3.Connection, w: LibWork, sig: str, mode: str, d: Decision, stats: dict) -> None:
    state, how = (d.state, d.how) if mode != "skip" else ("skip", d.why)
    if state in ("linked", "review"):
        # a MusicBrainz work another library work already carries is not offered again: two library works
        # for one piece are a merge question (the cross-album rules), not a link to make twice
        taken = {c["mbid"]: o for c in d.candidates if (o := _holder(con, c["mbid"], w.id))}
        if taken:
            free = [c for c in d.candidates if c["mbid"] not in taken]
            if state == "linked" or not free:
                d = Decision("none", candidates=[dict(c, taken_by=taken[c["mbid"]]) for c in d.candidates
                                                  if c["mbid"] in taken],
                             why="the matching MusicBrainz work is already linked to another work in your library")
                state, how = "none", None
            else:
                d = Decision("review", candidates=free, why=d.why)
    if state == "linked":
        con.execute("INSERT OR IGNORE INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)",
                    (w.id, d.mbid))
    if state == "review":
        _review(con, w, d)
    else:
        con.execute("DELETE FROM review_item WHERE id=?", (f"mbw:{w.id}",))
    con.execute("INSERT OR REPLACE INTO mb_work_match(work_id, sig, state, mbid, how, detail_json, at) "
                "VALUES (?,?,?,?,?,?,?)",
                (w.id, sig, state, d.mbid if state == "linked" else None, f"{mode}: {how}" if how else mode,
                 json.dumps({"why": d.why, "candidates": d.candidates}, ensure_ascii=False), time.time()))
    stats[state] += 1


async def fetch(mb: MBClient, urls: list[str]) -> tuple[int, str | None]:
    """Fetch the answers (the client caches them); (fetched, deferral reason or None). A transient failure
    stops the chunk: the rest wait for the next run."""
    n = 0
    for u in urls:
        try:
            await mb.get(u)
            n += 1
        except MBTransientError as e:
            return n, str(e)
        except MBError:
            n += 1                  # a 404/400 is cached as an error for a day
    return n, None


# ---- a person's decision --------------------------------------------------------------------------------

def proposed(con: sqlite3.Connection, wid: str) -> set[str]:
    r = con.execute("SELECT mbid, detail_json FROM mb_work_match WHERE work_id=?", (wid,)).fetchone()
    if r is None:
        return set()
    out = {r["mbid"]} if r["mbid"] else set()
    try:
        out |= {c["mbid"] for c in json.loads(r["detail_json"] or "{}").get("candidates") or []}
    except (ValueError, TypeError, KeyError):
        pass
    return out


def set_user_link(con: sqlite3.Connection, wid: str, user: str | None, decision: str,
                  mbids: list[str]) -> dict:
    """A person accepts one MusicBrainz work for a work, or rejects one or more. Only an id this job linked
    or proposed for the work can be decided here (a file's own MusicBrainz tag is not this job's to undo).
    The caller holds the write transaction."""
    wid = _resolve(con, wid)
    if con.execute("SELECT 1 FROM work WHERE id=?", (wid,)).fetchone() is None:
        raise ValueError("unknown work")
    if decision not in ("accept", "reject"):
        raise ValueError("decision must be 'accept' or 'reject'")
    mbids = [m.lower() for m in mbids if isinstance(m, str)]
    if not mbids or not all(is_mbid(m) for m in mbids) or (decision == "accept" and len(mbids) != 1):
        raise ValueError("give one MusicBrainz work id to accept, or the ids to reject")
    allowed = proposed(con, wid)
    if any(m not in allowed for m in mbids):
        raise ValueError("not a MusicBrainz work proposed for this work")
    now = time.time()
    if decision == "accept":
        m = mbids[0]
        holder = con.execute("SELECT entity_id FROM alias WHERE entity='work' AND kind='mbid' AND value=?",
                             (m,)).fetchone()
        if holder is not None and _resolve(con, holder[0]) != wid:
            raise ValueError("another work in your library is already linked to this MusicBrainz work")
        con.execute("DELETE FROM alias WHERE entity='work' AND kind='mbid' AND entity_id=? AND value IN "
                    "(SELECT mbid FROM mb_work_match WHERE work_id=? AND state='linked' AND mbid IS NOT NULL)",
                    (wid, wid))
        con.execute("INSERT OR IGNORE INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)",
                    (wid, m))
    else:
        for m in mbids:
            # only a link this job made or a person accepted goes (a tag's id would come back on the next
            # index anyway)
            con.execute("DELETE FROM alias WHERE entity='work' AND kind='mbid' AND entity_id=? AND value=? AND "
                        "EXISTS (SELECT 1 FROM mb_work_match WHERE work_id=? AND state IN ('linked', 'user') "
                        "AND mbid=?)", (wid, m, wid, m))
    for m in mbids:
        con.execute("INSERT INTO override(entity, entity_id, op, payload_json, created_at, by_user) "
                    "VALUES (?,?,?,?,?,?)", ("work", wid, "mb_link",
                                             json.dumps({"mbid": m, "decision": decision}, sort_keys=True), now, user))
    con.execute("INSERT INTO correction_log(op, payload_json, user, at) VALUES (?,?,?,?)",
                ("work_mb_link", json.dumps({"work_id": wid, "decision": decision, "mbids": mbids},
                                            sort_keys=True), user, now))
    con.execute("DELETE FROM review_item WHERE id=?", (f"mbw:{wid}",))
    con.execute("UPDATE mb_work_match SET state='user', mbid=?, at=? WHERE work_id=?",
                (mbids[0] if decision == "accept" else None, now, wid))
    return {"work_id": wid, "decision": decision, "mbids": mbids}
