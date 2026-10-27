"""Composition and premiere dates of works (DEVIATIONS.md, "Work dates"), MA-free.

Each kind of date ('composed', 'premiered') is chosen per work from, in order:

1. a user's correction (an `override` row: entity 'work', op 'dates'); it survives re-index, and
   'unknown' hides the automatic value;
2. the files' own COMPOSITIONDATE-style tag (composed only; ORIGINALDATE is a release date and is never
   read as the work's);
3. MusicBrainz, only for works already linked to an MB work: the composer relationship's begin/end
   (or the span of its parts' when the work itself has none) and the earliest premiere event;
4. the bundled premieres list (curation's premieres.json, premiered only).

Recording and release years are never used. Each automatic source's answer is kept in `work_date`; the
chosen values (with their source) are materialised into the `work` row, which the views and curation
read. `refresh` is offline and cheap (the library's tags and cached MusicBrainz answers); `mb_due` and
`fetch_mb` are the rate-limited network step, a few works per job run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from . import composers
from .mb import DEFAULT_BASE, WORK_INC, MBClient, MBError, MBTransientError
from .text import norm_key

KINDS = ("composed", "premiered")
PRIORITY = ("user", "tags", "musicbrainz", "dataset")
SOURCE_LABEL = {"user": "your correction", "tags": "file tags", "musicbrainz": "MusicBrainz",
                "dataset": "premieres list"}
DATES_INC = "artist-rels+event-rels"      # composer dates and premiere events in one response
# how the cached MusicBrainz answers are read; a change re-reads every work's once (job row 'dates:mb_reader')
MB_READER = "2"
MB_CHUNK = 10                             # works per network job run (1 req/s: ~10 s on the shared client)
UNKNOWN = "unknown"
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
# (id, label, first year, last year), by the composition's first year
PERIODS = (("early", "Early", None, 1599), ("baroque", "Baroque", 1600, 1749),
           ("classical", "Classical", 1750, 1819), ("romantic", "Romantic", 1820, 1899),
           ("20th", "20th century", 1900, 1974), ("contemporary", "Contemporary", 1975, None))
PERIOD_IDS = tuple(p[0] for p in PERIODS)
MIN_YEAR = 800


# ---- parsing and formatting -----------------------------------------------------------------------------

_ISO = r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?"
_CIRCA = re.compile(r"^(?:c\.|ca\.?|circa|approx\.?|~)\s*", re.IGNORECASE)
_DECADE = re.compile(r"^(\d{3})(?:0'?s|x|\?)$", re.IGNORECASE)
_SINGLE = re.compile(rf"^{_ISO}$")
_HYPHEN_YY = re.compile(r"^(\d{4})-(\d{2})$")      # 1804-05: a range or a month (parse_partial)
# a range: 1804-1805 (a hyphen before two digits is _HYPHEN_YY's), 1804–05, 1804/05,
# 1804 to 1805, 1804-11-20/1805-02
_RANGE = re.compile(rf"^{_ISO}\s*(?:[–—/]|\bto\b|-(?=\s*\d{{4}}))\s*(\d{{4}}|\d{{2}})(?:-(\d{{1,2}})(?:-(\d{{1,2}}))?)?$",
                    re.IGNORECASE)


def _ymd(y: str, m: str | None, d: str | None) -> str | None:
    year = int(y)
    if not MIN_YEAR <= year <= dt.date.today().year + 1:
        return None
    if not m:
        return f"{year:04d}"
    month = int(m)
    if not 1 <= month <= 12:
        return None
    if not d:
        return f"{year:04d}-{month:02d}"
    try:
        return dt.date(year, month, int(d)).isoformat()
    except ValueError:
        return None


def _precision(value: str) -> str:
    return {4: "year", 7: "month", 10: "day"}[len(value)]


def parse_partial(text: Any, ranges: bool = True) -> tuple[str, str | None, str] | None:
    """(date_from, date_to or None, precision) for a written date, or None when it is not one.

    Accepts '1804', '1805-11-20', ranges ('1804-1805', '1804–05', '1804/05', '1804 to 1805'), decades
    ('1860s', '186x') and 'c. 1805' (precision 'circa'). Years outside 800..next year are refused.

    'YYYY-NN' with an ASCII hyphen is a range when `ranges` (a composition date) and NN is later than the
    year's own last two digits: '1804-05' is 1804–1805, '1805-11' is 1805–1811, '1898-1902' stays a range.
    Otherwise NN is a month ('1898-03' is March 1898; a premiere, `ranges=False`, reads '1805-11' as
    November 1805), and a NN that is neither ('1850-20') is not a date."""
    if not isinstance(text, (str, int)):
        return None
    s = str(text).strip().rstrip(".").strip()
    circa = bool(_CIRCA.match(s))
    s = _CIRCA.sub("", s).strip()
    if ranges and (m := _HYPHEN_YY.match(s)) and int(m.group(2)) > int(m.group(1)[2:]):
        a, b = _ymd(m.group(1), None, None), _ymd(m.group(1)[:2] + m.group(2), None, None)
        return (a, b, "circa" if circa else "year") if a and b else None
    if m := _DECADE.match(s):
        start = _ymd(m.group(1) + "0", None, None)
        return (start, f"{int(start) + 9:04d}", "decade") if start else None
    if m := _SINGLE.match(s):
        v = _ymd(*m.groups())
        return (v, None, "circa" if circa else _precision(v)) if v else None
    if m := _RANGE.match(s):
        a = _ymd(m.group(1), m.group(2), m.group(3))
        y2 = m.group(4)
        if len(y2) == 2:
            y2 = m.group(1)[:2] + y2
        b = _ymd(y2, m.group(5), m.group(6))
        if not a or not b or b[:len(a)] < a[:len(b)] or int(b[:4]) - int(a[:4]) > 100:
            return None
        return (a, None if a == b else b, "circa" if circa else _precision(a))
    return None


def year(value: str | None) -> int | None:
    return int(value[:4]) if value and value[:4].isdigit() else None


def year_range(a: int, b: int | None) -> str:
    """'1805', '1804–05', '1840–41', '1898–1902'."""
    if b is None or b == a:
        return str(a)
    if a // 100 == b // 100:
        return f"{a}–{b % 100:02d}"
    return f"{a}–{b}"


def composed_text(start: str | None, end: str | None, precision: str | None) -> str | None:
    a = year(start)
    if a is None:
        return None
    if precision == "decade":
        return f"{a // 10 * 10}s"
    s = year_range(a, year(end))
    return f"c. {s}" if precision == "circa" else s


def day_text(value: str | None, precision: str | None = None) -> str | None:
    """'20 November 1805', 'November 1805', '1805' ('c. 1805', '1800s')."""
    if not value:
        return None
    if precision == "decade":
        return f"{year(value) // 10 * 10}s"
    if precision == "circa":
        return f"c. {year(value)}"
    parts = value.split("-")
    if len(parts) == 3:
        return f"{int(parts[2])} {MONTHS[int(parts[1]) - 1]} {parts[0]}"
    if len(parts) == 2:
        return f"{MONTHS[int(parts[1]) - 1]} {parts[0]}"
    return parts[0]


def period_of(y: int | None) -> str | None:
    if y is None:
        return None
    for pid, _, lo, hi in PERIODS:
        if (lo is None or y >= lo) and (hi is None or y <= hi):
            return pid
    return None


def period_bounds(pid: str) -> tuple[int | None, int | None]:
    for p, _, lo, hi in PERIODS:
        if p == pid:
            return lo, hi
    raise ValueError(f"period must be one of {', '.join(PERIOD_IDS)}")


# ---- the automatic sources ------------------------------------------------------------------------------


def _live_works(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT w.id, w.title, w.catalogue_display, w.relation, w.work_type, w.composed_start, w.composed_end, "
        "w.composed_precision, w.composed_source, w.premiered, w.premiered_precision, w.premiered_place, "
        "w.premiered_source, w.dates_mb_at, "
        "(SELECT c.name FROM work_composer wc JOIN composer c ON c.id=wc.composer_id WHERE wc.work_id=w.id "
        " ORDER BY wc.ord LIMIT 1) AS composer FROM work w "
        "WHERE NOT EXISTS (SELECT 1 FROM redirect d WHERE d.entity='work' AND d.old_id=w.id)").fetchall()


def _from_tags(con: sqlite3.Connection) -> dict[str, tuple[str, str | None, str, str]]:
    """work id → (from, to, precision, raw) of its tracks' commonest parseable COMPOSITIONDATE tag."""
    rows = con.execute(
        "SELECT r.work_id, e.value, COUNT(*) AS n FROM evidence e "
        "JOIN mapping_state ms ON ms.provider_instance || '/' || ms.item_id = e.entity_id AND ms.status != 'gone' "
        "JOIN occurrence o ON o.library_track_id = ms.library_track_id "
        "JOIN recording_slot s ON s.occurrence_id = o.id JOIN recording r ON r.id = s.recording_id "
        "WHERE e.entity='mapping' AND e.field='composition_date' AND e.retracted_at IS NULL "
        "GROUP BY r.work_id, e.value").fetchall()
    votes: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        try:
            raw = json.loads(r["value"])
        except ValueError:
            continue
        if parse_partial(raw):
            votes[r["work_id"]][str(raw).strip()] += r["n"]
    out = {}
    for wid, c in votes.items():
        raw = min(c.items(), key=lambda x: (-x[1], x[0]))[0]
        out[wid] = (*parse_partial(raw), raw)
    return out


# a work derived from the premiered one, or a piece of it (a suite, excerpts, an act, the overture, a
# paraphrase): the stage premiere is not its own. Over norm_key text.
_DERIVED = re.compile(
    r"\b(?:suite|excerpts?|extracts?|selections?|highlights|scenes?|szenen?|fragments?|"
    r"arr|arranged|arrangement|transcription|transcribed|paraphrase|fantas(?:y|ia|ie)|variations|"
    r"reminiscences|potpourri|medley|"
    r"acts?|akt|aufzug|acte|atto|prelude|preludes|vorspiel|overture|ouverture|intermezzo|interlude|entr acte|"
    r"aria|arias|duet|chorus|march|dances?|ballet music|music from|good friday|karfreitagszauber|liebestod)\b")
# works joined in one title ("Blanc et noir; Prélude à l'après-midi d'un faune", "Cavalleria / Pagliacci"):
# split at a top-level ';' or ' / ' (not inside brackets: "Daphnis et Chloé (complete ballet; possible)")
_JOIN = re.compile(r"\s*;\s*|\s+/\s+")


def _segments(title: str) -> list[str]:
    out, depth, cur = [], 0, []
    i = 0
    while i < len(title):
        ch = title[i]
        depth += ch in "([{"
        depth -= ch in ")]}" and depth > 0
        if depth == 0 and (m := _JOIN.match(title, i)) and m.end() > i:
            out.append("".join(cur))
            cur = []
            i = m.end()
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [norm_key(x) for x in out if re.search(r"[^\W\d_]{2}", x)]


def _same_type(a: str | None, b: str) -> bool:
    """Both known and different ('Emperor' in a cantata's title is not the Emperor concerto): no match."""
    return not a or a == "Other" or b == "Other" or a == b


def dataset_hit(title: str, text: str, work_type: str | None, p: Any) -> bool:
    """Is the premieres.json entry `p` (already the work's composer's) this work's own premiere? Its pattern
    matches `text` (norm_key of the title and catalogue), the types agree, the title names one work (every joined segment
    matches), and the work is not a suite, excerpt or arrangement unless the entry is one too. Shared
    with curation's "On this day" for works the dates job has not settled."""
    from . import catalogue as cat

    if not p.match.search(text):
        return False
    segs = _segments(title)
    if len(segs) > 1 and not all(p.match.search(x) for x in segs):
        return False
    if _DERIVED.search(norm_key(title)) and not _DERIVED.search(norm_key(p.work)):
        return False
    return _same_type(work_type, cat.work_type(p.work))


def _from_dataset(works: list[sqlite3.Row]) -> dict[str, Any]:
    """work id → the premieres.json entry that matches it (dataset_hit, after curation's composer match),
    when exactly one date matches and the work is an "original" one."""
    from . import curation

    by_composer: dict[str, list] = defaultdict(list)
    for p in curation.premieres():
        by_composer[p.composer].append(p)
    mine: dict[str, list] = {}         # a composer credit → the premieres of that composer
    out = {}
    for w in works:
        if not w["composer"] or (w["relation"] or "original") != "original":
            continue
        if w["composer"] not in mine:
            kc = composers.lookup(w["composer"]) or composers.find_in_credit(w["composer"])
            mine[w["composer"]] = [p for who, ps in by_composer.items()
                                   if curation._same_composer(who, kc.name if kc else None, w["composer"]) for p in ps]
        text = norm_key(f"{w['title']} {w['catalogue_display'] or ''}")
        hits = [p for p in mine[w["composer"]] if dataset_hit(w["title"], text, w["work_type"], p)]
        if len({p.date for p in hits}) == 1:
            out[w["id"]] = hits[0]
    return out


def _cached(con: sqlite3.Connection, urls: list[str], bodies: bool) -> dict[str, tuple[float, Any]]:
    """url → (fetched_at, parsed JSON or None) for cached 200 answers (expired ones too: dates hardly
    change, and a stale answer beats none while MusicBrainz is off)."""
    out: dict[str, tuple[float, Any]] = {}
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        cols = "url, fetched_at" + (", body_zlib" if bodies else "")
        for r in con.execute(f"SELECT {cols} FROM mb_cache WHERE status=200 AND url IN ({','.join('?' * len(chunk))})",
                             chunk):
            data = None
            if bodies and r["body_zlib"]:
                try:
                    data = json.loads(zlib.decompress(r["body_zlib"]))
                except (ValueError, zlib.error):
                    data = None
            out[r["url"]] = (r["fetched_at"], data)
    return out


def _url(base: str, mbid: str, inc: str) -> str:
    return f"{base.rstrip('/')}/work/{mbid}?inc={inc}&fmt=json"


def _composer_span(data: dict | None) -> tuple[str | None, str | None]:
    begins, ends = [], []
    for rel in (data or {}).get("relations", []):
        if rel.get("type") == "composer" and rel.get("target-type") == "artist":
            b, e = _mb_date(rel.get("begin")), _mb_date(rel.get("end"))
            if b:
                begins.append(b)
            if e or b:
                ends.append(e or b)
    return (min(begins), max(ends)) if begins else (None, None)


def _mb_date(v: Any) -> str | None:
    if not isinstance(v, str) or not v:
        return None
    p = v.split("-")
    return _ymd(p[0], p[1] if len(p) > 1 else None, p[2] if len(p) > 2 else None) if p[0].isdigit() else None


def _premiere(data: dict | None) -> tuple[str, str | None] | None:
    """(date, event url) of the earliest premiere event that took place."""
    best = None
    for rel in (data or {}).get("relations", []):
        ev = rel.get("event") or {}
        if rel.get("type") != "premiere" or rel.get("target-type") != "event" or ev.get("cancelled"):
            continue
        d = _mb_date((ev.get("life-span") or {}).get("begin")) or _mb_date(rel.get("begin"))
        if d and (best is None or d < best[0]):
            best = (d, f"https://musicbrainz.org/event/{ev['id']}" if ev.get("id") else None)
    return best


def _answer(got: dict[str, tuple[float, Any]], base: str, mbid: str) -> dict | None:
    """A work's cached MusicBrainz answers as one: the dates answer (DATES_INC) has the composer and premiere
    relationships but not the parts, which only the indexer's WORK_INC answer has (an opera's acts)."""
    rels: list[dict] = []
    found = False
    for inc in (DATES_INC, WORK_INC):
        data = (got.get(_url(base, mbid, inc)) or (0, None))[1]
        if isinstance(data, dict):
            found = True
            rels += data.get("relations") or []
    return {"relations": rels} if found else None


def _parts(data: dict | None) -> list[str]:
    return list(dict.fromkeys(
        rel["work"]["id"] for rel in (data or {}).get("relations", [])
        if rel.get("type") == "parts" and rel.get("direction") == "forward" and (rel.get("work") or {}).get("id")))


def _parts_span(con: sqlite3.Connection, base: str, data: dict | None) -> tuple[str | None, str | None]:
    """A work without dates of its own: the span of its cached parts' (an opera's acts and numbers),
    three levels down at most."""
    begins, ends, seen = [], [], set()
    level = [data]
    for _ in range(3):
        ids = [p for d in level for p in _parts(d) if p not in seen]
        seen |= set(ids)
        if not ids:
            break
        got = _cached(con, [u for p in ids for u in (_url(base, p, DATES_INC), _url(base, p, WORK_INC))], bodies=True)
        level = []
        for p in ids:
            d = _answer(got, base, p)
            b, e = _composer_span(d)
            if b:
                begins.append(b)
                ends.append(e or b)
            else:
                level.append(d)
    return (min(begins), max(ends)) if begins else (None, None)


def _work_mbids(con: sqlite3.Connection) -> dict[str, set[str]]:
    """Live work id → its MusicBrainz work ids (with those of works merged into it)."""
    out: dict[str, set[str]] = defaultdict(set)
    for r in con.execute("SELECT a.value, COALESCE(d.new_id, a.entity_id) AS wid FROM alias a "
                         "LEFT JOIN redirect d ON d.entity='work' AND d.old_id=a.entity_id "
                         "WHERE a.entity='work' AND a.kind='mbid'"):
        out[r["wid"]].add(r["value"])
    return out


def _from_mb(con: sqlite3.Connection, works: dict[str, sqlite3.Row], mbids: dict[str, set[str]],
             base: str, reread: bool = False) -> tuple[dict[str, dict], dict[str, float]]:
    """For works whose cached MusicBrainz answers changed since they were last read (all of them when
    `reread`): work id →
    {'composed': (from, to, precision, ref) | None, 'premiered': (date, precision, ref) | None}, and the
    new read marks."""
    due: dict[str, list[str]] = {}
    urls = [u for ids in mbids.values() for m in ids for u in (_url(base, m, DATES_INC), _url(base, m, WORK_INC))]
    meta = _cached(con, urls, bodies=False)
    marks: dict[str, float] = {}
    for wid, ids in mbids.items():
        w = works.get(wid)
        if w is None:
            continue
        seen = [meta[u][0] for m in ids for u in (_url(base, m, DATES_INC), _url(base, m, WORK_INC)) if u in meta]
        if seen and (reread or w["dates_mb_at"] is None or max(seen) > w["dates_mb_at"]):
            due[wid] = sorted(ids)
            marks[wid] = max(seen)
    out: dict[str, dict] = {}
    for wid, ids in due.items():
        got = _cached(con, [u for m in ids for u in (_url(base, m, DATES_INC), _url(base, m, WORK_INC))], bodies=True)
        begins, ends, prem = [], [], []
        for m in ids:
            data = _answer(got, base, m)
            b, e = _composer_span(data)
            if b is None:
                b, e = _parts_span(con, base, data)
            if b:
                begins.append(b)
                ends.append(e or b)
            if p := _premiere(data):
                prem.append(p)
        res: dict[str, Any] = {"composed": None, "premiered": None}
        if begins:
            a, z = min(begins), max(ends)
            res["composed"] = (a, None if z == a else z, _precision(a), f"https://musicbrainz.org/work/{ids[0]}")
        if prem:
            d, ref = min(prem)
            res["premiered"] = (d, _precision(d), ref or f"https://musicbrainz.org/work/{ids[0]}")
        out[wid] = res
    return out, marks


# ---- user corrections -----------------------------------------------------------------------------------


def _resolve(con: sqlite3.Connection, wid: str) -> str:
    for _ in range(10):
        r = con.execute("SELECT new_id FROM redirect WHERE entity='work' AND old_id=?", (wid,)).fetchone()
        if not r:
            break
        wid = r[0]
    return wid


def _overrides(con: sqlite3.Connection) -> dict[str, dict]:
    """Live work id → the user's dates (later corrections win per field; merged works' carry over)."""
    out: dict[str, dict] = defaultdict(dict)
    for r in con.execute("SELECT entity_id, payload_json FROM override WHERE entity='work' AND op='dates' "
                         "ORDER BY created_at, id"):
        try:
            payload = json.loads(r["payload_json"])
        except ValueError:
            continue
        out[_resolve(con, r["entity_id"])].update(payload)
    return out


def set_user_dates(con: sqlite3.Connection, wid: str, user: str | None, composed: str | None = None,
                   premiered: str | None = None, premiered_place: str | None = None) -> dict:
    """A user's correction of a work's dates. None leaves a field as it is, '' returns it to the automatic
    sources, 'unknown' hides the automatic value. Raises ValueError for a date it cannot read. The caller
    holds the write transaction; the work row is updated at once."""
    wid = _resolve(con, wid)
    if con.execute("SELECT 1 FROM work WHERE id=?", (wid,)).fetchone() is None:
        raise ValueError("unknown work")
    change: dict[str, str] = {}
    for key, value in (("composed", composed), ("premiered", premiered)):
        if value is None:
            continue
        v = str(value).strip()
        if v and v.lower() != UNKNOWN and parse_partial(v, ranges=key == "composed") is None:
            raise ValueError(f"cannot read {key} date {v!r} (try 1804, 1804–05, 1860s, c. 1805 or 1805-11-20)")
        change[key] = UNKNOWN if v.lower() == UNKNOWN else v
    if premiered_place is not None:
        change["premiered_place"] = str(premiered_place).strip()[:200]
    if not change:
        raise ValueError("nothing to change")
    now = time.time()
    con.execute("INSERT INTO override(entity, entity_id, op, payload_json, created_at, by_user) VALUES (?,?,?,?,?,?)",
                ("work", wid, "dates", json.dumps(change, sort_keys=True), now, user))
    con.execute("INSERT INTO correction_log(op, payload_json, user, at) VALUES (?,?,?,?)",
                ("work_dates", json.dumps({"work_id": wid, **change}, sort_keys=True), user, now))
    choose(con, [wid])
    return {"work_id": wid, **change}


# ---- choosing -------------------------------------------------------------------------------------------

_COLS = ("composed_start", "composed_end", "composed_precision", "composed_source", "premiered",
         "premiered_precision", "premiered_place", "premiered_source")


def choose(con: sqlite3.Connection, only: Iterable[str] | None = None) -> int:
    """Materialise the chosen dates of the live works (or of `only`) into `work`; returns how many changed."""
    works = {w["id"]: w for w in _live_works(con)}
    ids = list(works) if only is None else [i for i in only if i in works]
    rows: dict[str, dict[tuple[str, str], sqlite3.Row]] = defaultdict(dict)
    for r in con.execute("SELECT * FROM work_date"):
        rows[r["work_id"]][(r["kind"], r["source"])] = r
    users = _overrides(con)
    changed = 0
    for wid in ids:
        got = rows.get(wid, {})
        u = users.get(wid, {})
        new: dict[str, Any] = dict.fromkeys(_COLS)
        uc = u.get("composed") or ""
        if uc == UNKNOWN:
            new["composed_source"] = "user"
        elif uc and (p := parse_partial(uc)):
            new.update(composed_start=p[0], composed_end=p[1], composed_precision=p[2], composed_source="user")
        else:
            for src in PRIORITY[1:]:
                if r := got.get(("composed", src)):
                    new.update(composed_start=r["date_from"], composed_end=r["date_to"],
                               composed_precision=r["precision"], composed_source=src)
                    break
        up = u.get("premiered") or ""
        if up == UNKNOWN:
            new["premiered_source"] = "user"
        elif up and (p := parse_partial(up, ranges=False)):
            new.update(premiered=p[0], premiered_precision=p[2], premiered_source="user",
                       premiered_place=u.get("premiered_place") or None)
        else:
            for i, src in enumerate(PRIORITY[1:], 1):
                if r := got.get(("premiered", src)):
                    # a later source that agrees and knows the day (MusicBrainz says 1814, the list 23 May 1814)
                    finer = next((f for s2 in PRIORITY[i + 1:] if (f := got.get(("premiered", s2)))
                                  and len(f["date_from"]) > len(r["date_from"]) and r["precision"] != "circa"
                                  and f["date_from"].startswith(r["date_from"])), None)
                    r, src = (finer, finer["source"]) if finer else (r, src)
                    new.update(premiered=r["date_from"], premiered_precision=r["precision"],
                               premiered_place=r["place"], premiered_source=src)
                    break
            if u.get("premiered_place") and new["premiered"]:
                new["premiered_place"] = u["premiered_place"]
        w = works[wid]
        if any(w[c] != new[c] for c in _COLS):
            con.execute(f"UPDATE work SET {', '.join(f'{c}=?' for c in _COLS)} WHERE id=?",
                        (*[new[c] for c in _COLS], wid))
            changed += 1
    return changed


def _put(con: sqlite3.Connection, wid: str, kind: str, source: str, value: tuple | None,
         have: dict[tuple[str, str, str], tuple]) -> bool:
    """Make (date_from, date_to, precision, place, ref) the `source`'s answer for the work; None removes it."""
    key = (wid, kind, source)
    if value is None:
        if key in have:
            con.execute("DELETE FROM work_date WHERE work_id=? AND kind=? AND source=?", key)
            return True
        return False
    if have.get(key) == value:
        return False
    con.execute("INSERT OR REPLACE INTO work_date(work_id, kind, source, date_from, date_to, precision, place, ref, at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (*key, *value, time.time()))
    return True


def refresh(con: sqlite3.Connection, base: str = DEFAULT_BASE) -> dict[str, int]:
    """The offline dates step (writer thread): tags and the premieres list for every live work, cached
    MusicBrainz answers for the works whose answers changed, then the chosen values. Writes only changes."""
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        works = _live_works(con)
        by_id = {w["id"]: w for w in works}
        have = {(r["work_id"], r["kind"], r["source"]): (r["date_from"], r["date_to"], r["precision"], r["place"],
                                                         r["ref"]) for r in con.execute("SELECT * FROM work_date")}
        stats = Counter()
        # rows of works merged away or gone
        for key in [k for k in have if k[0] not in by_id]:
            con.execute("DELETE FROM work_date WHERE work_id=? AND kind=? AND source=?", key)
            del have[key]
            stats["dropped"] += 1
        tags = _from_tags(con)
        data = _from_dataset(works)
        for wid in by_id:
            t = tags.get(wid)
            stats["tags"] += _put(con, wid, "composed", "tags", (t[0], t[1], t[2], None, t[3]) if t else None, have)
            p = data.get(wid)
            stats["dataset"] += _put(con, wid, "premiered", "dataset",
                                     (p.date.isoformat(), None, "day", p.place or None, p.source) if p else None, have)
        r = con.execute("SELECT cursor FROM job WHERE id='dates:mb_reader'").fetchone()
        reread = r is None or r[0] != MB_READER
        mb, marks = _from_mb(con, by_id, _work_mbids(con), base, reread)
        if reread:
            con.execute("INSERT OR REPLACE INTO job(id, kind, scope, cursor, state, started_at, error) "
                        "VALUES ('dates:mb_reader', 'dates', 'all', ?, 'done', ?, NULL)", (MB_READER, time.time()))
        for wid, res in mb.items():
            c, p = res["composed"], res["premiered"]
            stats["musicbrainz"] += _put(con, wid, "composed", "musicbrainz", (*c[:3], None, c[3]) if c else None, have)
            stats["musicbrainz"] += _put(con, wid, "premiered", "musicbrainz",
                                         (p[0], None, p[1], None, p[2]) if p else None, have)
        for wid, at in marks.items():
            con.execute("UPDATE work SET dates_mb_at=? WHERE id=?", (at, wid))
        stats["changed"] = choose(con)
        if own:
            con.execute("COMMIT")
    except BaseException:
        if own:
            con.execute("ROLLBACK")
        raise
    return dict(stats)


# ---- the network step -----------------------------------------------------------------------------------


def mb_due(con: sqlite3.Connection, base: str = DEFAULT_BASE, limit: int = MB_CHUNK) -> list[str]:
    """MusicBrainz work ids whose dates answer (DATES_INC) is not cached yet, for live works that still
    want a date from it (neither date fixed by the user or the tags). Errors stay cached for a day."""
    works = {w["id"]: w for w in _live_works(con)}
    fixed = ("user", "tags")
    want = []
    for wid, ids in sorted(_work_mbids(con).items()):
        w = works.get(wid)
        if w is None or (w["composed_source"] in fixed and w["premiered_source"] == "user"):
            continue
        want += sorted(ids)
    if not want:
        return []
    now = time.time()
    have = set()
    for i in range(0, len(want), 500):
        chunk = [_url(base, m, DATES_INC) for m in want[i:i + 500]]
        have |= {r[0] for r in con.execute(f"SELECT url FROM mb_cache WHERE expires_at > ? AND url IN "
                                           f"({','.join('?' * len(chunk))})", (now, *chunk))}
    return [m for m in dict.fromkeys(want) if _url(base, m, DATES_INC) not in have][:limit]


async def fetch_mb(mb: MBClient, mbids: list[str]) -> tuple[int, str | None]:
    """Fetch the dates answers (the client caches them); (fetched, deferral reason or None). A transient
    failure stops the chunk: the rest wait for the next run."""
    n = 0
    for m in mbids:
        try:
            await mb.get(mb.url("work", m, DATES_INC))
            n += 1
        except MBTransientError as e:
            return n, str(e)
        except MBError:
            continue        # a 404/400 is cached as an error for a day
    return n, None


# ---- for the views --------------------------------------------------------------------------------------


def describe(w: dict) -> dict:
    """The work page's dates: {'composed', 'premiered', 'text', 'sources', 'hidden'} from a `work` row;
    `hidden` names the kinds a user marked unknown."""
    comp = composed_text(w.get("composed_start"), w.get("composed_end"), w.get("composed_precision"))
    prem_year = year(w.get("premiered"))
    composed = {"from": year(w.get("composed_start")), "to": year(w.get("composed_end")),
                "precision": w.get("composed_precision"), "source": w.get("composed_source"),
                "source_label": SOURCE_LABEL.get(w.get("composed_source") or ""), "text": comp} if comp else None
    premiered = None
    if prem_year is not None:
        premiered = {"date": w.get("premiered"), "year": prem_year, "precision": w.get("premiered_precision"),
                     "place": w.get("premiered_place"), "source": w.get("premiered_source"),
                     "source_label": SOURCE_LABEL.get(w.get("premiered_source") or ""),
                     "text": day_text(w.get("premiered"), w.get("premiered_precision"))}
    bits = ([f"composed {comp}"] if comp else []) + \
        ([f"premiered {prem_year if premiered['precision'] != 'circa' else premiered['text']}"] if premiered else [])
    sources = list(dict.fromkeys(SOURCE_LABEL[s] for s in (w.get("composed_source") if comp else None,
                                                           w.get("premiered_source") if premiered else None) if s))
    hidden = [k for k, col in (("composed", "composed_start"), ("premiered", "premiered"))
              if w.get(f"{k}_source") == "user" and not w.get(col)]
    return {"composed": composed, "premiered": premiered, "text": " · ".join(bits) or None, "sources": sources,
            "hidden": hidden}


def sort_year(w: dict, composer: str | None = None) -> int | None:
    """The year a list sorts and filters a work by (period, decade, date sort): its composition's first
    year. Without one, the premiere's year only when the composer is a known one who was alive within a
    year of it (a living composer counts): a posthumous premiere (Schubert's Unfinished, 1865) says nothing
    about when the work was written, so that work stays undated."""
    if (y := year(w.get("composed_start"))) is not None:
        return y
    p = year(w.get("premiered"))
    if p is None or not composer:
        return None
    kc = composers.lookup(composer) or composers.find_in_credit(composer)
    if kc is None or (kc.died is None and kc.born is None):
        return None
    return p if (kc.died is None or kc.died >= p - 1) else None
