"""Companion-UI view builders (PLAN.md §6, M3), MA-free.

Each function takes a read-only `Index` and the caller's visible sources (`vis`, None = all) and returns a
JSON-ready dict. Images stay as the stored `MediaItemImage` dicts under `image`; the glue turns them into
image-proxy URLs. Unknown and hidden ids raise the same `NotFound`, so ids can't be probed.

Performer data comes from one pass over the credits of visible recordings (`_credit_rows`). That is linear
in credits, which is fine for household libraries; a materialised per-performer table is the next step if a
benchmark ever says otherwise.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from . import accolades as acc
from .browse import NotFound, letter_of, play_uri
from .catalogue import WORK_TYPES
from .index import Index, _fmt_duration, recording_label
from .text import fold, norm_key

BUCKETS = ("conductors", "orchestras", "chamber", "choirs", "soloists", "other")
DATE_KINDS = ("composed", "recorded", "released")
MAX_LIMIT = 1000
_TYPE_ORDER = {t: i for i, t in enumerate(WORK_TYPES)}

__all__ = ["BUCKETS", "NotFound", "bucket", "composers", "performer", "performers", "recording", "review", "work",
           "works"]


def bucket(role: str, kind: str | None) -> str:
    """The Performers-tab filter a credit belongs to (OzGav: orchestras and chamber groups stay apart)."""
    if role == "conductor":
        return "conductors"
    if role == "orchestra" or kind == "orchestra":
        return "orchestras"
    if role == "chorus" or kind == "choir" or re.search(r"\b(?:chorus|choir|chor|singers)\b", role):
        return "choirs"
    if role == "ensemble" or kind == "other":
        return "chamber"
    if role == "performer":
        return "other"
    return "soloists"


def _sort_key(name: str | None) -> str:
    return fold(name or "").lower()


def _words(q: str | None) -> list[str]:
    return [w for w in re.split(r"\s+", norm_key(fold(q or ""))) if w]


def _matches(words: list[str], *fields: str | None) -> bool:
    hay = norm_key(fold(" ".join(f for f in fields if f)))
    return all(w in hay for w in words)


def _year(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(str(v)[:4])
    except ValueError:
        return None


def _vis_args(vis: Iterable[str] | None) -> dict:
    return Index._vis(vis)


def _visible_recordings(where: str = "") -> str:
    return f"SELECT r.id, r.work_id FROM recording r WHERE {Index._VIS_REC} {where}"


# ---- composers ------------------------------------------------------------------------------------------


def _composer(c: dict) -> dict:
    return {"id": c["id"], "name": c["name"], "sort_name": c["sort_name"], "born": c.get("born"),
            "died": c.get("died"), "works": c.get("works"), "letter": letter_of(c["sort_name"])}


def composers(ix: Index, vis=None) -> dict:
    items = sorted((_composer(c) for c in ix.composers(vis)), key=lambda c: (c["letter"] == "#", _sort_key(c["sort_name"])))
    return {"items": items, "letters": sorted({c["letter"] for c in items}, key=lambda x: (x == "#", x))}


# ---- works ----------------------------------------------------------------------------------------------


def _work_years(ix: Index, vis) -> dict[str, dict[str, list[int]]]:
    """Per work: the recording years and release years of its visible recordings."""
    out: dict[str, dict[str, list[int]]] = defaultdict(lambda: {"recorded": [], "released": []})
    rows = ix._q(f"WITH vr AS ({_visible_recordings()}) SELECT vr.work_id, r.recorded_start, "
                 "COALESCE(r.release_year, a.year) AS rel FROM vr JOIN recording r ON r.id=vr.id "
                 "LEFT JOIN album_info a ON a.album_library_id=r.album_library_id", _vis_args(vis))
    for r in rows:
        if (y := _year(r["recorded_start"])) is not None:
            out[r["work_id"]]["recorded"].append(y)
        if (y := _year(r["rel"])) is not None:
            out[r["work_id"]]["released"].append(y)
    return out


def _performer_work_ids(ix: Index, pid: str, vis) -> set[str]:
    rows = ix._q(f"WITH vr AS ({_visible_recordings()}) SELECT DISTINCT vr.work_id FROM vr "
                 "JOIN recording_slot s ON s.recording_id=vr.id JOIN track_credit tc ON tc.occurrence_id=s.occurrence_id "
                 "WHERE tc.person_id=:pid", {"pid": pid, **_vis_args(vis)})
    return {r[0] for r in rows}


def works(ix: Index, vis=None, composer_id: str | None = None, work_type: str | None = None, q: str | None = None,
          date_kind: str = "composed", date_from=None, date_to=None, performer_id: str | None = None,
          offset: int = 0, limit: int = 200) -> dict:
    """The Works tab: filters by composer, type, text, performer and a date range of one kind (derobert).

    `types` counts every filter but the type; `date_coverage` says how many of the matching works carry a
    date of each kind, so the UI can say "3 of 40 works have a composition date" instead of hiding works."""
    if date_kind not in DATE_KINDS:
        raise ValueError(f"date_kind must be one of {', '.join(DATE_KINDS)}")
    lo, hi = _year(date_from), _year(date_to)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 200), MAX_LIMIT))
    header = None
    if composer_id:
        cid = ix.resolve("composer", composer_id)
        c = next((c for c in ix.composers(vis) if c["id"] == cid), None)
        if c is None:
            raise NotFound(composer_id)
        header = _composer(c)
        rows = ix.works(cid, vis=vis)
    else:
        rows = ix.works(vis=vis)
    if performer_id:
        keep = _performer_work_ids(ix, performer_id, vis)
        if not keep:
            raise NotFound(performer_id)
        rows = [w for w in rows if w["id"] in keep]
    words = _words(q)
    if words:
        rows = [w for w in rows if _matches(words, w["title"], w["composer"], w["catalogue_display"], w["key"],
                                            w["work_type"])]
    meta = {r["id"]: dict(r) for r in ix._q("SELECT id, composed_start, composed_end FROM work")} if rows else {}
    years = _work_years(ix, vis) if rows else {}

    def dates(w: dict) -> dict[str, list[int]]:
        m = meta.get(w["id"], {})
        composed = [y for y in (_year(m.get("composed_start")), _year(m.get("composed_end"))) if y is not None]
        y = years.get(w["id"], {"recorded": [], "released": []})
        return {"composed": composed, "recorded": y["recorded"], "released": y["released"]}

    coverage = Counter()
    by_id = {}
    for w in rows:
        d = dates(w)
        by_id[w["id"]] = d
        for k in DATE_KINDS:
            coverage[k] += bool(d[k])
    if lo is not None or hi is not None:
        def in_range(w: dict) -> bool:
            return any((lo is None or y >= lo) and (hi is None or y <= hi) for y in by_id[w["id"]][date_kind])
        rows = [w for w in rows if in_range(w)]
    types = Counter(w["work_type"] for w in rows)
    if work_type:
        rows = [w for w in rows if w["work_type"] == work_type]
    items = []
    for w in rows[offset:offset + limit]:
        d = by_id[w["id"]]
        items.append({"id": w["id"], "title": w["title"], "composer": w["composer"], "work_type": w["work_type"],
                      "catalogue": w["catalogue_display"], "key": w["key"], "recordings": w["recordings"],
                      "composed": min(d["composed"]) if d["composed"] else None,
                      "recorded": [min(d["recorded"]), max(d["recorded"])] if d["recorded"] else None})
    return {
        "total": len(rows), "offset": offset, "limit": limit, "items": items, "composer": header,
        "types": [{"type": t, "n": n} for t, n in sorted(types.items(), key=lambda x: _TYPE_ORDER.get(x[0], 99))],
        "date_coverage": {"total": len(by_id), **{k: coverage[k] for k in DATE_KINDS}},
    }


# ---- work page ------------------------------------------------------------------------------------------


def _credits_by_recording(ix: Index, wid: str, vis, rid: str | None = None) -> dict[str, list[dict]]:
    """Full credits (with instrument) of a work's visible recordings, in track-and-tag order: one query."""
    rows = ix._q(
        f"WITH vr AS ({_visible_recordings('AND r.work_id=:wid AND (:rid IS NULL OR r.id=:rid)')}) "
        "SELECT s.recording_id AS rid, p.id, p.name, p.kind, tc.role, tc.instrument, MIN(tc.detail) AS detail, "
        "MIN(s.ord * 100 + tc.ord) AS o "
        "FROM vr JOIN recording_slot s ON s.recording_id=vr.id JOIN track_credit tc ON tc.occurrence_id=s.occurrence_id "
        "JOIN person p ON p.id=tc.person_id GROUP BY s.recording_id, p.id, tc.role ORDER BY s.recording_id, o",
        {"wid": wid, "rid": rid, **_vis_args(vis)})
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        # compact (a work can have hundreds of recordings): no `kind`, `instrument` only when there is one
        c = {"id": r["id"], "name": r["name"], "role": r["role"], "group": bucket(r["role"], r["kind"])}
        if r["instrument"]:
            c["instrument"] = r["instrument"]
        if r["detail"]:         # the character sung, or the context of a credit ('Pater Profundus')
            c["detail"] = r["detail"]
        out[r["rid"]].append(c)
    return out


_RECORDINGS_SQL = (
    # `Index.recordings` without its per-recording credits query (N+1): the views fetch every credit of the
    # work in one query (`_credits_by_recording`) and build the label from those
    "SELECT r.*, a.name AS album, a.artist AS album_artist, a.year AS album_year, a.image_json, "
    "(SELECT COALESCE(SUM(t.duration),0) FROM recording_slot s JOIN occurrence o ON o.id=s.occurrence_id "
    " JOIN track_info t ON t.library_track_id=o.library_track_id WHERE s.recording_id=r.id) AS duration, "
    "(SELECT COUNT(*) FROM recording_slot s WHERE s.recording_id=r.id) AS tracks, "
    "(SELECT t.artist FROM recording_slot s JOIN occurrence o ON o.id=s.occurrence_id "
    " JOIN track_info t ON t.library_track_id=o.library_track_id WHERE s.recording_id=r.id "
    " ORDER BY s.ord LIMIT 1) AS track_artist, "
    "(SELECT MIN(COALESCE(o.disc,1)*10000+COALESCE(o.position,0)) FROM recording_slot s "
    " JOIN occurrence o ON o.id=s.occurrence_id WHERE s.recording_id=r.id) AS first_pos, "
    "(SELECT GROUP_CONCAT(c.name, ';') FROM work_composer wc JOIN composer c ON c.id=wc.composer_id "
    " WHERE wc.work_id=r.work_id) AS composer_names "
    "FROM recording r LEFT JOIN album_info a ON a.album_library_id=r.album_library_id "
    f"WHERE r.work_id=:wid AND (:rid IS NULL OR r.id=:rid) AND {Index._VIS_REC} "
    "ORDER BY r.recorded_start IS NULL, r.recorded_start, r.album_library_id, first_pos")


def _recordings(ix: Index, wid: str, vis, rid: str | None = None) -> tuple[list[dict], dict[str, list[dict]]]:
    """A work's visible recordings (or just `rid`) as `Index.recordings` returns them, plus their credits:
    two queries however many recordings the work has."""
    credits = _credits_by_recording(ix, wid, vis, rid)
    out = []
    for row in ix._q(_RECORDINGS_SQL, {"wid": wid, "rid": rid, **_vis_args(vis)}):
        d = dict(row)
        d["credits"] = credits.get(d["id"], [])
        d["label"] = recording_label(d)
        image = d.pop("image_json", None)
        d["image"] = json.loads(image) if image else None
        out.append(d)
    return out, credits


def _summary(r: dict, credits: list[dict], wid: str) -> dict:
    """One recording as the work page lists it ("Karajan / Berliner Philharmoniker (1963) [32:00]")."""
    rec_year = _year(r.get("recorded_start"))
    rel_year = _year(r.get("release_year")) or _year(r.get("album_year"))
    year, kind = (rec_year, "recorded") if rec_year else ((rel_year, "released") if rel_year else (None, None))
    who = recording_label({**r, "recorded_start": None, "release_year": None, "duration": 0})
    return {
        "id": r["id"], "label": r["label"], "who": who, "year": year, "year_kind": kind,
        "recorded": r.get("recorded_start"), "release_year": rel_year,
        "decade": (year // 10 * 10) if year else None,
        "duration": int(r.get("duration") or 0), "duration_text": _fmt_duration(r.get("duration") or 0),
        "tracks": r.get("tracks") or 0, "complete": bool(r.get("complete")), "status": r.get("status"),
        "venue": r.get("venue"),
        "album": r.get("album"), "album_id": r.get("album_library_id"), "album_artist": r.get("album_artist"),
        "image": r.get("image"), "credits": credits, "play_uri": play_uri(wid, r["id"]), "accolades": [],
    }


def _best(recs: list[dict], accs: dict[str, list[dict]] | None = None) -> tuple[dict, str] | None:
    """The §6 ranking with acclaim, as `Index.best_recording` (complete, acclaim, confirmed, most evidence,
    lowest id)."""
    return acc.best_pick(recs, accs)


def _with_accolades(s: dict, accs: list[dict]) -> dict:
    s["accolades"] = accs
    s["acclaim"] = acc.acclaim(accs)
    return s


def _acclaim_summary(summaries: list[dict]) -> dict | None:
    """The work header's line: how many recordings carry counted accolades, and the distinct awards."""
    awards: dict[str, int] = {}
    n = 0
    for s in summaries:
        counted = [a for a in s["accolades"] if a["counts"]]
        n += bool(counted)
        for a in counted:
            if a["kind"] == "award":
                awards[a["award"]] = awards.get(a["award"], 0) + 1
    if not n:
        return None
    return {"recordings": n, "awards": [{"award": k, "n": v} for k, v in sorted(awards.items(), key=lambda x: (-x[1], x[0]))]}


def _facets(summaries: list[dict]) -> dict:
    groups: dict[str, Counter] = {b: Counter() for b in BUCKETS}
    names: dict[str, tuple[str, str | None]] = {}
    for s in summaries:
        seen = set()
        for c in s["credits"]:
            if (c["group"], c["id"]) in seen:
                continue
            seen.add((c["group"], c["id"]))
            groups[c["group"]][c["id"]] += 1
            names.setdefault(c["id"], (c["name"], c.get("instrument")))
    out: dict[str, list] = {}
    for b, cnt in groups.items():
        out[b] = sorted(({"id": pid, "name": names[pid][0], "instrument": names[pid][1], "n": n}
                         for pid, n in cnt.items()), key=lambda x: (-x["n"], _sort_key(x["name"])))
    dec = Counter(s["decade"] for s in summaries if s["decade"] and s["year_kind"] == "recorded")
    rel = Counter((s["release_year"] // 10 * 10) for s in summaries if s["release_year"])
    out["decades"] = [{"decade": d, "n": n} for d, n in sorted(dec.items())]
    out["release_decades"] = [{"decade": d, "n": n} for d, n in sorted(rel.items())]
    return out


def _people(ix: Index, ids: Iterable[str]) -> list[dict]:
    out = []
    for pid in ids:
        r = ix._one("SELECT id, name, kind FROM person WHERE id=?", (pid,))
        if r:
            out.append(dict(r))
    return out


def work(ix: Index, wid: str, vis=None, scope: str | list[str] | None = None) -> dict:
    """A work with every visible recording, its facets and the recording "Play" would pick.

    `scope` (a performer id, or several joined by commas) is echoed back for the UI's default filter with
    SHOW ALL (OzGav): all recordings are returned either way, each with its credits' ids."""
    w = ix.work(wid)
    if w is None:
        raise NotFound(wid)
    wid = w["id"]
    recs, credits = _recordings(ix, wid, vis)
    extra = ix.contained_tracks(wid, vis)
    if not recs and not extra:
        raise NotFound(wid)
    accs = acc.for_recordings(ix.con, recs)
    summaries = [_with_accolades(_summary(r, credits.get(r["id"], []), wid), accs.get(r["id"], [])) for r in recs]
    best = _best(recs, accs)
    ids = [s for s in (scope.split(",") if isinstance(scope, str) else (scope or [])) if s]
    on_work = {c["id"] for s in summaries for c in s["credits"]}
    people = [p for p in _people(ix, dict.fromkeys(ids)) if p["id"] in on_work]
    return {
        "work": {"id": wid, "title": w["title"], "composers": w["composers"], "work_type": w["work_type"],
                 "catalogue": w["catalogue_display"], "key": w["key"], "composed": _year(w["composed_start"]),
                 "composed_end": _year(w["composed_end"])},
        "recordings": summaries,
        "facets": _facets(summaries),
        "best": {"recording_id": best[0]["id"], "reason": best[1], "label": best[0]["label"]} if best else None,
        "acclaim": _acclaim_summary(summaries),
        "contained_tracks": [{"track_id": t["library_track_id"], "title": t["title"],
                              "track_uri": f"library://track/{t['library_track_id']}"} for t in extra],
        "scope": {"ids": [p["id"] for p in people], "people": people} if people else None,
    }


# ---- recording page -------------------------------------------------------------------------------------


def recording(ix: Index, rid: str, vis=None) -> dict:
    """One recording: summary, movements in order (with track URIs to play from any of them), full credits."""
    rid = ix.resolve("recording", rid)
    row = ix._one("SELECT work_id FROM recording WHERE id=?", (rid,))
    recs, _ = _recordings(ix, row["work_id"], vis, rid) if row else ([], {})
    if not recs:
        raise NotFound(rid)
    r = recs[0]
    w = ix.work(r["work_id"]) or {}
    s = _summary(r, r["credits"], r["work_id"])
    s["movements"] = [{
        "ord": t["ord"], "movement_no": t["movement_no"], "name": t["movement_name"] or t["title"] or "",
        "title": t["title"], "duration": int(t["duration"] or 0), "duration_text": _fmt_duration(t["duration"] or 0),
        "track_id": t["library_track_id"], "track_uri": f"library://track/{t['library_track_id']}",
    } for t in ix.recording_tracks(r["id"], vis)]
    s["album_info"] = {"id": r.get("album_library_id"), "name": r.get("album"), "artist": r.get("album_artist"),
                       "year": r.get("album_year"), "image": r.get("image")}
    s["work"] = {"id": r["work_id"], "title": w.get("title"), "composers": w.get("composers") or [],
                 "catalogue": w.get("catalogue_display"), "key": w.get("key"), "work_type": w.get("work_type")}
    s["merged"] = ix.merged_into(r["id"])
    _with_accolades(s, acc.for_recordings(ix.con, recs).get(r["id"], []))
    s["awards"] = [a for a in s["accolades"] if a["kind"] == "award"]
    s["reviews"] = [a for a in s["accolades"] if a["kind"] == "review"]
    s["ratings"] = [a for a in s["accolades"] if a["kind"] == "rating"]
    links = acc.links_for(ix.con, r.get("album_library_id"))
    s["links"] = links
    principal = next((c["name"] for c in r["credits"] if c.get("role") == "conductor"), None) \
        or next((c["name"] for c in r["credits"] if c.get("group") == "soloists"), None) \
        or r.get("album_artist")
    comp = (w.get("composers") or [{}])[0].get("name") if w.get("composers") else None
    s["review_links"] = acc.review_links(comp, w.get("title") or "", principal, links)
    return s


def accolades(ix: Index, recording_id: str | None = None, work_id: str | None = None, vis=None) -> dict:
    """`classical/accolades`: one recording's awards, reviews, ratings and review links, or every recording of
    a work with its acclaim and pick score (the curation hook, §5.9)."""
    if bool(recording_id) == bool(work_id):
        raise ValueError("give recording_id or work_id")
    if recording_id:
        r = recording(ix, recording_id, vis)
        return {"recording_id": r["id"], "accolades": r["accolades"], "acclaim": r["acclaim"],
                "links": r["links"], "review_links": r["review_links"], "dataset": acc.dataset_info()}
    w = ix.work(work_id)
    if w is None:
        raise NotFound(work_id)
    recs, _ = _recordings(ix, w["id"], vis)
    if not recs:
        raise NotFound(work_id)
    accs = acc.for_recordings(ix.con, recs)
    best = _best(recs, accs)
    return {"work_id": w["id"],
            "recordings": [{"id": r["id"], "label": r["label"], "accolades": accs.get(r["id"], []),
                            "acclaim": acc.acclaim(accs.get(r["id"], [])),
                            "pick_score": acc.pick_score(r, accs.get(r["id"], []))} for r in recs],
            "best": {"recording_id": best[0]["id"], "reason": best[1]} if best else None,
            "dataset": acc.dataset_info()}


# ---- performers -----------------------------------------------------------------------------------------


def _credit_rows(ix: Index, vis) -> list[dict]:
    return [dict(r) for r in ix._q(
        f"WITH vr AS ({_visible_recordings()}) SELECT DISTINCT vr.id AS rid, vr.work_id, p.id AS pid, p.name, "
        "p.sort_name, p.kind, tc.role, tc.instrument FROM vr JOIN recording_slot s ON s.recording_id=vr.id "
        "JOIN track_credit tc ON tc.occurrence_id=s.occurrence_id JOIN person p ON p.id=tc.person_id",
        _vis_args(vis))]


def _aggregate(rows: list[dict]) -> dict[str, dict]:
    people: dict[str, dict] = {}
    for r in rows:
        p = people.get(r["pid"])
        if p is None:
            p = people[r["pid"]] = {"id": r["pid"], "name": r["name"], "sort_name": r["sort_name"], "kind": r["kind"],
                                    "roles": set(), "instruments": set(), "recs": set(), "works": set(),
                                    "by_role": defaultdict(set)}
        b = bucket(r["role"], r["kind"])
        p["roles"].add(b)
        p["by_role"][b].add(r["rid"])
        if r["instrument"]:
            p["instruments"].add(r["instrument"])
        p["recs"].add(r["rid"])
        p["works"].add(r["work_id"])
    return people


def _person_out(p: dict, recs: set[str] | None = None, works_of: dict[str, str] | None = None) -> dict:
    recs = p["recs"] if recs is None else recs
    return {"id": p["id"], "name": p["name"], "sort_name": p["sort_name"], "kind": p["kind"],
            "roles": [b for b in BUCKETS if b in p["roles"]], "instruments": sorted(p["instruments"]),
            "recordings": len(recs),
            "works": len({works_of[r] for r in recs}) if works_of is not None else len(p["works"])}


def performers(ix: Index, vis=None, role: str = "all", with_id: str | None = None, q: str | None = None) -> dict:
    """The Performers tab: one list, filtered by role bucket; `with_id` keeps only those who share a
    recording with that performer (orchestra → its conductors and back, robnmeyer/OzGav)."""
    if role != "all" and role not in BUCKETS:
        raise ValueError(f"role must be all or one of {', '.join(BUCKETS)}")
    rows = _credit_rows(ix, vis)
    people = _aggregate(rows)
    works_of = {r["rid"]: r["work_id"] for r in rows}
    partner = None
    pool = list(people.values())
    shared: dict[str, set[str]] = {p["id"]: p["recs"] for p in pool}
    if with_id:
        partner = people.get(with_id)
        if partner is None:
            raise NotFound(with_id)
        pool = [p for p in pool if p["id"] != with_id and p["recs"] & partner["recs"]]
        shared = {p["id"]: p["recs"] & partner["recs"] for p in pool}
    words = _words(q)
    if words:
        pool = [p for p in pool if _matches(words, p["name"], " ".join(p["instruments"]))]
    counts = Counter(b for p in pool for b in p["roles"])
    total = len(pool)
    if role != "all":
        pool = [p for p in pool if role in p["roles"]]
    items = sorted((_person_out(p, shared[p["id"]], works_of) for p in pool), key=lambda x: _sort_key(x["sort_name"]))
    return {"items": items, "role": role, "counts": {"all": total, **{b: counts[b] for b in BUCKETS}},
            "with": _person_out(partner) if partner else None}


def performer(ix: Index, pid: str, vis=None, with_id: str | None = None) -> dict:
    """A performer: works performed (each with how many of the work's recordings are theirs) and partners."""
    rows = _credit_rows(ix, vis)
    people = _aggregate(rows)
    me = people.get(pid)
    if me is None:
        raise NotFound(pid)
    works_of = {r["rid"]: r["work_id"] for r in rows}
    recs = set(me["recs"])
    partner = None
    if with_id:
        partner = people.get(with_id)
        if partner is None or with_id == pid:
            raise NotFound(with_id)
        recs &= partner["recs"]
    per_work = Counter(works_of[r] for r in recs)
    catalogue = {w["id"]: w for w in ix.works(vis=vis)}
    order = {wid: i for i, wid in enumerate(catalogue)}
    works_out = [{"id": wid, "title": catalogue[wid]["title"], "composer": catalogue[wid]["composer"],
                  "work_type": catalogue[wid]["work_type"], "catalogue": catalogue[wid]["catalogue_display"],
                  "key": catalogue[wid]["key"], "recordings": catalogue[wid]["recordings"], "mine": n}
                 for wid, n in per_work.items() if wid in catalogue]
    works_out.sort(key=lambda w: (_sort_key(w["composer"]), order[w["id"]]))
    partners: dict[str, Counter] = {b: Counter() for b in BUCKETS}
    for p in people.values():
        if p["id"] == pid or (partner and p["id"] == with_id):
            continue
        for b, rs in p["by_role"].items():
            n = len(rs & recs)
            if n:
                partners[b][p["id"]] = n
    return {
        "performer": _person_out(me, works_of=works_of),
        "with": _person_out(partner) if partner else None,
        "works": works_out,
        "recordings": len(recs),
        "partners": {b: sorted(({"id": i, "name": people[i]["name"], "n": n} for i, n in cnt.items() if n),
                               key=lambda x: (-x["n"], _sort_key(x["name"]))) for b, cnt in partners.items()},
    }


# ---- review queue ---------------------------------------------------------------------------------------

_NAME_REASONS = "name"
_GROUPING_HINT = ("Grouping corrections (split, reassign, ignore) are not available yet; the item id identifies it "
                  "for a later correction.")


_ENTITY_LABEL = {"album": "Album", "occurrence": "Track", "work": "Work", "composer": "Composer", "name": "Name",
                 "recording": "Recording", "person": "Performer"}


def _review_title(it: dict, album: str | None = None) -> str:
    """The specific thing the item is about (a track, work, name or album), never the group heading: the
    review page already shows the reason above the cards."""
    d = it.get("detail") or {}
    for k in ("title", "track", "work", "name", "album"):
        if d.get(k):
            return str(d[k])
    if d.get("tracks"):
        return " · ".join(str(t) for t in d["tracks"][:3]) + (" …" if len(d["tracks"]) > 3 else "")
    if album:
        return album
    return _ENTITY_LABEL.get(it.get("entity") or "", "Item") + (f" {it['entity_id']}" if it.get("entity_id") else "")


def _review_subtitle(title: str, album: str | None, d: dict) -> str:
    """The album the item came from, unless that is already the title; then the item's own explanation."""
    if album and album != title:
        return album
    return str(d.get("why") or "")


def review(ix: Index, vis=None) -> dict:
    """Review items grouped by reason. Composer-identity items carry correction actions (the UI shows them
    only to users with the correction scope; the commands enforce it); grouping items are read-only."""
    items = ix.review(vis)
    comps = ix.composers(vis)
    by_name = {norm_key(c["name"]): c for c in comps}
    comp_ids = {c["id"]: c for c in comps}
    visible_works = {w["id"] for w in ix.works(vis=vis)}
    albums = {r[0]: r[1] for r in ix._q("SELECT album_library_id, name FROM album_info")}
    # one query for every occurrence item's recording, not one per item
    occ_rec = {r["oid"]: (r["id"], r["work_id"]) for r in ix._q(
        "SELECT s.occurrence_id AS oid, r.id, r.work_id FROM review_item v "
        "JOIN recording_slot s ON s.occurrence_id=v.entity_id JOIN recording r ON r.id=s.recording_id "
        f"WHERE v.entity='occurrence' AND {Index._VIS_REC}", _vis_args(vis))}
    visible_names = {norm_key(c["name"]) for c in comps}
    rec_work = {r["id"]: r["work_id"] for r in ix._q(
        "SELECT r.id, r.work_id FROM review_item v JOIN recording r ON r.id=v.entity_id "
        f"WHERE v.entity='recording' AND {Index._VIS_REC}", _vis_args(vis))}

    def comp_ref(name: str | None) -> dict | None:
        c = by_name.get(norm_key(name or "")) if name else None
        return {"id": c["id"], "name": c["name"]} if c else None

    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        d = it.get("detail") or {}
        link = None
        if it["entity"] == "work" and it.get("entity_id"):
            w = ix.resolve("work", it["entity_id"])
            if w in visible_works:
                link = {"kind": "work", "id": w}
        elif it["entity"] == "occurrence" and it.get("entity_id"):
            r = occ_rec.get(it["entity_id"])
            if r:
                link = {"kind": "recording", "id": r[0], "work_id": r[1]}
        elif it["entity"] == "composer" and it.get("entity_id"):
            c = comp_ids.get(ix.resolve("composer", it["entity_id"]))
            if c:
                link = {"kind": "composer", "id": c["id"]}
        elif it["entity"] == "recording" and it.get("entity_id"):
            r = rec_work.get(it["entity_id"])
            if r:
                link = {"kind": "recording", "id": it["entity_id"], "work_id": r}
        actions: list[dict] = []
        hint = None
        if it["entity"] == _NAME_REASONS and d.get("name"):
            sugg = [comp_ref(n) for n in [d.get("proposal"), d.get("known"), *(d.get("candidates") or [])]]
            actions.append({"op": "composer_name", "name": d["name"],
                            "suggestions": list({s["id"]: s for s in sugg if s}.values())})
        elif it["entity"] == "composer" and it.get("entity_id"):
            a = comp_ref(d.get("name"))
            b = comp_ids.get(ix.resolve("composer", it["entity_id"]))
            if a and b and a["id"] != b["id"]:
                b = {"id": b["id"], "name": b["name"]}
                actions += [{"op": "merge_composers", "source": a, "target": b},
                            {"op": "merge_composers", "source": b, "target": a}]
        elif it["reason"] == "accolade_match":
            if link:
                actions += [{"op": "accolade_decide", "entry": d.get("entry"), "recording_id": it["entity_id"],
                             "decision": "confirm", "label": "Same recording"},
                            {"op": "accolade_decide", "entry": d.get("entry"), "recording_id": it["entity_id"],
                             "decision": "reject", "label": "Not this recording"}]
        else:
            hint = _GROUPING_HINT
        # `other` names another composer or work, which may sit only on sources this caller can't see
        if "other" in d and not (
                (it["entity"] == "composer" and norm_key(str(d["other"] or "")) in visible_names)
                or (it["entity"] == "work" and ix.resolve("work", str(d["other"] or "")) in visible_works)):
            d = {k: v for k, v in d.items() if k != "other"}
        if it["entity"] == "work" and isinstance(d.get("others"), list):   # the same, for every match listed
            d = {**d, "others": [o for o in d["others"] if ix.resolve("work", str(o)) in visible_works]}
        album = albums.get(it.get("album_library_id"))
        title = _review_title({**it, "detail": d}, album)
        groups[it["reason"]].append({
            "id": it["id"], "reason": it["reason"], "entity": it["entity"], "entity_id": it.get("entity_id"),
            "title": title, "subtitle": _review_subtitle(title, album, d), "album": album,
            "album_id": it.get("album_library_id"), "detail": d, "link": link, "actions": actions, "hint": hint,
        })
    return {"total": len(items),
            "groups": [{"reason": k, "n": len(v), "items": v} for k, v in sorted(groups.items())]}
