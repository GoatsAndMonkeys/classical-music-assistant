"""Review triage: the review queue as decision cards, bulk-safe actions and an undo log (DEVIATIONS.md
"Review triage").

The queue (`review_item`) is a derived cache that gets new ids on every derive, so everything durable here is
keyed by a fingerprint of what an item says (`fingerprint`). Cards group items that one decision settles:

- name:       every item about one credit key ("Schumann": not resolved, possible same composer, suspicious);
              one "this credit means…" for all, per-album exceptions on its items
- mb:         MusicBrainz work proposals with the same candidate set; accepts one work at a time, "none of
              these" for all
- accolade:   one award entry's doubtful matches; confirm one at a time, "not this recording" for all
- discs:      possible continuations between the same two albums; join one at a time, "different recordings"
              for all (the status quo)
- same_work:  possible duplicates of a work on other albums (read-only: hide only)
- info:       everything else, one card per reason (read-only: hide only)

Every decision runs through `apply` in the caller's write transaction and lands in `review_batch` with its
inverse steps; `undo` replays them. Hidden providers stay hidden: cards are built from `views.review`, which
filters and redacts by the caller's visible sources, and every target is checked again on apply.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from typing import Any

from . import accolades, views, work_link
from . import discs as disc_mod
from .index import Index
from .names import parse_names
from .text import fold, norm_key

KINDS = ("name", "mb", "accolade", "discs", "same_work", "info")
KIND_TITLES = {"name": "Composer credits", "mb": "MusicBrainz links", "accolade": "Awards",
               "discs": "Discs of one set", "same_work": "Same work?", "info": "Grouping notes"}
SORTS = ("impact", "acclaim", "items", "title")
MAX_ACTIONS = 5000
MAX_CARD_ITEMS = 200
_ACCEPTS = {("work_mb_link", "accept"), ("accolade_decide", "confirm"), ("join_discs", "together")}
_NAME_ENTITIES = ("name", "composer")
_SAME_WORK = "possible same work on another album"
_COUNTED_GRADES = ("this_release", "other_release", "confirmed")


# ---- fingerprints -----------------------------------------------------------------------------------------

_VOLATILE = ("earlier_recording", "other", "others")      # derived ids: the entity and album carry the identity


def fingerprint(raw: dict, rec_work: dict[str, str] | None = None, resolve=None) -> str:
    """A stable key for one review item across re-derives: reason, album, the entity in stable terms (a
    recording by its album and work, a work through redirects) and what the item says."""
    entity, eid = raw.get("entity") or "", raw.get("entity_id") or ""
    if entity == "recording" and eid:
        eid = (rec_work or {}).get(eid, eid)
    elif entity == "work" and eid and resolve:
        eid = resolve("work", eid)
    d = raw.get("detail")
    if d is None:
        d = json.loads(raw.get("detail_json") or "{}")
    d = {k: v for k, v in d.items() if k not in _VOLATILE} if isinstance(d, dict) else d
    s = "\x1f".join([raw.get("reason") or "", raw.get("album_library_id") or "", entity, eid,
                     json.dumps(d, sort_keys=True, ensure_ascii=False)])
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:24]


def _keys(ix: Index) -> dict[str, str]:
    """review_item id -> fingerprint, for the whole queue (two queries)."""
    rec_work = {r[0]: f"{r[1]}:{ix.resolve('work', r[2])}" for r in ix._q(
        "SELECT r.id, r.album_library_id, COALESCE(r.source_work_id, r.work_id) FROM review_item v "
        "JOIN recording r ON r.id=v.entity_id WHERE v.entity='recording'")}
    return {r["id"]: fingerprint(dict(r), rec_work, ix.resolve) for r in ix._q(
        "SELECT id, reason, album_library_id, entity, entity_id, detail_json FROM review_item")}


# ---- cards ------------------------------------------------------------------------------------------------

def _kind(it: dict) -> str:
    if it["entity"] in _NAME_ENTITIES and (it.get("detail") or {}).get("name"):
        return "name"
    if it["reason"] == views.MB_LINK:
        return "mb"
    if it["reason"] == "accolade_match":
        return "accolade"
    if it["reason"] == views.CONTINUATION:
        return "discs"
    if it["reason"] == _SAME_WORK and it["entity"] == "work":
        return "same_work"
    return "info"


def _card_key(kind: str, it: dict, ix: Index) -> str:
    d = it.get("detail") or {}
    if kind == "name":
        return "name:" + (it["entity_id"] if it["entity"] == "name" else norm_key(d.get("name") or ""))
    if kind == "mb":
        acts = [a for a in it.get("actions") or [] if a.get("decision") == "accept"]
        return "mb:" + ",".join(sorted(a["mbid"] for a in acts))
    if kind == "accolade":
        return "accolade:" + str(d.get("entry") or "")
    if kind == "discs":
        return "discs:" + str(d.get("earlier_album") or "") + "|" + str(it.get("album_id") or "")
    return kind + ":" + it["reason"]          # notes: nothing to decide, so one card per reason


def _apply_action(label: str, actions: list[dict], **extra) -> dict:
    return {"type": "apply", "label": label, "actions": actions, **extra}


def _name_items(card: dict, comp_by_name: dict, comp_ids: dict, ix: Index) -> None:
    """One decision for the credit: suggestions from every item, the global pin as the card action and an
    album-only pin on each item (an exception)."""
    name, sugg = None, {}
    for it in card["items"]:
        d = it.get("detail") or {}
        name = name or d.get("name")
        for a in it.pop("_actions", []):
            for s in a.get("suggestions") or []:
                sugg[s["id"]] = s
        if it["entity"] == "composer":
            c = comp_ids.get(ix.resolve("composer", it.get("entity_id") or ""))
            if c and norm_key(c["name"]) != norm_key(d.get("name") or ""):
                sugg[c["id"]] = {"id": c["id"], "name": c["name"]}
        for n in [d.get("other"), d.get("known"), *(d.get("candidates") or [])]:
            c = comp_by_name.get(norm_key(n or "")) if n else None
            if c:
                sugg[c["id"]] = {"id": c["id"], "name": c["name"], "born": c.get("born"), "died": c.get("died")}
    if not name:
        return
    suggestions = sorted(sugg.values(), key=lambda s: s["name"])
    card["title"] = f"“{name}”"
    card["group_actions"].insert(0, {"type": "composer", "label": "This credit means…", "name": name,
                                     "suggestions": suggestions})
    for it in card["items"]:
        if it.get("album_id"):
            it["actions"] = [{"type": "composer", "label": "Only on this album…", "name": name,
                              "album_id": it["album_id"], "suggestions": suggestions}]


def _item_actions(kind: str, it: dict) -> list[dict]:
    out = []
    for a in it.get("_actions", []):
        if kind == "mb":
            args = {k: a[k] for k in ("work_id", "decision", "mbid", "mbids") if k in a}
            out.append(_apply_action(a["label"], [{"op": "work_mb_link", **args}],
                                     accept=a["decision"] == "accept"))
        elif kind == "accolade":
            out.append(_apply_action(a["label"], [{"op": "accolade_decide", "entry": a["entry"],
                                                   "recording_id": a["recording_id"], "decision": a["decision"]}],
                                     accept=a["decision"] == "confirm"))
        elif kind == "discs":
            out.append(_apply_action(a["label"], [{"op": "join_discs", "first_recording_id": a["first_recording_id"],
                                                   "second_recording_id": a["second_recording_id"],
                                                   "decision": a["decision"]}],
                                     accept=a["decision"] == "together"))
    return out


def _bulk_actions(kind: str, card: dict) -> list[dict]:
    """The one safe decision for every item of a card: only rejections and the status quo, never an accept."""
    if len(card["items"]) < 2:
        return []
    acts = [x for it in card["items"] for a in it.get("actions") or [] if not a.get("accept")
            and a.get("type") == "apply" for x in a["actions"]]
    if kind == "mb" and acts:
        return [_apply_action(f"None of these, for all {len(acts)} works", acts)]
    if kind == "accolade" and acts:
        return [_apply_action(f"Not the awarded recording, all {len(acts)}", acts)]
    if kind == "discs" and acts:
        return [_apply_action(f"Different recordings, all {len(acts)}", acts)]
    return []


def _context(ix: Index) -> dict:
    """Per-album facts for impact, acclaim and the composer filter (a handful of grouped queries; the cards
    only ever look up albums and works the caller can see)."""
    return {
        "album_tracks": dict(ix._q("SELECT album_library_id, COUNT(*) FROM occurrence WHERE present=1 "
                                   "GROUP BY album_library_id")),
        "work_slots": dict(ix._q("SELECT r.work_id, COUNT(*) FROM recording r JOIN recording_slot s "
                                 "ON s.recording_id=r.id GROUP BY r.work_id")),
        "rec_slots": dict(ix._q("SELECT recording_id, COUNT(*) FROM recording_slot GROUP BY recording_id")),
        "acclaim": dict(ix._q("SELECT album_library_id, COUNT(*) FROM accolade WHERE grade IN "
                              f"({','.join('?' * len(_COUNTED_GRADES))}) GROUP BY album_library_id",
                              _COUNTED_GRADES)),
        "album_comps": _pairs(ix._q("SELECT DISTINCT r.album_library_id, wc.composer_id FROM recording r "
                                    "JOIN work_composer wc ON wc.work_id=r.work_id")),
        "work_comps": _pairs(ix._q("SELECT work_id, composer_id FROM work_composer")),
    }


def _pairs(rows) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for a, b in rows:
        out[a].add(b)
    return out


def _impact(it: dict, ctx: dict, ix: Index) -> int:
    d = it.get("detail") or {}
    if isinstance(d.get("tracks"), list) and d["tracks"]:
        return len(d["tracks"])
    e, eid = it["entity"], it.get("entity_id")
    if e == "occurrence":
        return 1
    if e == "work" and eid:
        return ctx["work_slots"].get(ix.resolve("work", eid), 1)
    if e == "recording" and eid:
        return ctx["rec_slots"].get(eid, 1)
    return ctx["album_tracks"].get(it.get("album_id"), 1)


def cards(ix: Index, vis=None) -> tuple[list[dict], dict]:
    """Every visible review item in its card (open and dismissed), unfiltered and not yet finished: `view`
    filters the items, then finishes the cards it shows."""
    rv = views.review(ix, vis)
    keys = _keys(ix)
    dismissed = {r[0] for r in ix._q("SELECT key FROM review_dismissal")}
    ctx = _context(ix)
    comps = ix.composers(vis)
    ctx["comp_ids"] = comp_ids = {c["id"]: c for c in comps}
    ctx["albums"] = {r[0]: r[1] for r in ix._q("SELECT album_library_id, name FROM album_info")}
    comp_by_name = {norm_key(c["name"]): c for c in comps}
    out: dict[tuple[str, str], dict] = {}
    for g in rv["groups"]:
        for it in g["items"]:
            key = keys.get(it["id"])
            if key is None:
                continue
            status = "dismissed" if key in dismissed else "open"
            kind = _kind(it)
            ck = _card_key(kind, it, ix)
            card = out.get((status, ck))
            if card is None:
                card = out[(status, ck)] = {"key": ck, "kind": kind, "status": status, "title": it["title"],
                                            "subtitle": "", "items": [], "group_actions": [],
                                            "hint": it.get("hint") if kind in ("same_work", "info") else None}
                if kind in ("same_work", "info"):
                    card["title"] = reason_title(it["reason"])
            row = {k: it[k] for k in ("id", "reason", "entity", "entity_id", "title", "subtitle", "album",
                                      "album_id", "detail", "link")}
            cs = set(ctx["album_comps"].get(it.get("album_id"), set()))
            link = it.get("link") or {}
            if link.get("kind") == "work":
                cs |= ctx["work_comps"].get(link["id"], set())
            elif link.get("kind") == "composer":
                cs.add(link["id"])
            row.update(key=key, _actions=it.get("actions") or [], impact=_impact(it, ctx, ix), comps=cs)
            card["items"].append(row)
    for card in out.values():
        kind = card["kind"]
        if kind == "name":
            _name_items(card, comp_by_name, comp_ids, ix)
            sugg = {s["id"] for g in card["group_actions"] for s in g.get("suggestions") or []}
            for it in card["items"]:
                it["comps"] |= sugg
        else:
            for it in card["items"]:
                it["actions"] = _item_actions(kind, it)
                if kind in ("same_work", "info") and it["key"]:
                    it["actions"].append(_apply_action("Hide", [{"op": "dismiss", "items": [it["id"]]}], hide=True))
            if kind == "mb":
                it0 = card["items"][0]
                card["title"] = it0["title"]
                card["subtitle"] = (it0.get("detail") or {}).get("composer") or ""
        for it in card["items"]:
            it.pop("_actions", None)
            it.setdefault("actions", [])
            it["comps"] = {c for c in it["comps"] if c in comp_ids}
            if card["status"] == "dismissed":
                it["actions"] = []
        card["items"].sort(key=lambda x: (-x["impact"], fold(x.get("album") or ""), x["id"]))
    return list(out.values()), ctx


def reason_title(reason: str) -> str:
    s = str(reason or "").replace("_", " ")
    return s[:1].upper() + s[1:]


def _finish(card: dict, items: list[dict], ctx: dict, can_correct: bool) -> dict:
    """A shown card: its (filtered) items, counts, impact, acclaim and the actions for exactly these items."""
    c = {**card, "items": items, "group_actions": list(card["group_actions"])}
    albums = {it["album_id"] for it in items if it.get("album_id")}
    c.update(n=len(items), impact=sum(it["impact"] for it in items),
             acclaim=sum(ctx["acclaim"].get(a, 0) for a in albums), album_ids=sorted(albums),
             composer_ids=sorted({x for it in items for x in it["comps"]}),
             reasons=sorted({it["reason"] for it in items}))
    if c["kind"] != "mb":
        c["subtitle"] = _card_subtitle(items)
    c["group_actions"] += _bulk_actions(c["kind"], c)
    ids = [it["id"] for it in items]
    if card["status"] == "open":
        c["group_actions"].append(_apply_action("Hide" if len(ids) == 1 else f"Hide all {len(ids)}",
                                                [{"op": "dismiss", "items": ids}], hide=True))
    else:
        c["group_actions"] = [_apply_action("Show again" if len(ids) == 1 else f"Show all {len(ids)} again",
                                            [{"op": "restore", "keys": [it["key"] for it in items]}])]
    c["actionable"] = c["kind"] in ("name", "mb", "accolade", "discs")
    c["items"] = [{k: v for k, v in it.items() if k != "comps"} for it in items]
    if not can_correct:
        c["group_actions"] = []
        c["items"] = [{**it, "actions": []} for it in c["items"]]
    if len(c["items"]) > MAX_CARD_ITEMS:
        c["more"] = len(c["items"]) - MAX_CARD_ITEMS
        c["items"] = c["items"][:MAX_CARD_ITEMS]
    return c


def _card_subtitle(items: list[dict]) -> str:
    albums = {it["album"] for it in items if it.get("album")}
    if len(albums) == 1:
        return next(iter(albums))
    if albums:
        return f"{len(albums)} albums"
    return ""


def _sort_key(sort: str):
    """Decisions before notes (a note has nothing to decide), then the chosen order."""
    def impact(c):
        return sum(it["impact"] for it in c["items"])

    if sort == "acclaim":
        return lambda c: (not c["actionable"], -c["acclaim"], -impact(c), fold(c["title"]), c["key"])
    if sort == "items":
        return lambda c: (not c["actionable"], -len(c["items"]), -impact(c), fold(c["title"]), c["key"])
    if sort == "title":
        return lambda c: (fold(c["title"]), c["key"])
    return lambda c: (not c["actionable"], -impact(c), -c["acclaim"], fold(c["title"]), c["key"])


def _items_matching(card: dict, f: dict, skip: str | None = None) -> list[dict]:
    """The card's items that pass the filters (`skip` one of them, for its own facet counts)."""
    if card["status"] != (f.get("status") or "open"):
        return []
    if skip != "kind" and f.get("kind") and card["kind"] != f["kind"]:
        return []
    items = card["items"]
    if skip != "kind" and f.get("reason"):
        items = [it for it in items if it["reason"] == f["reason"]]
    if skip != "composer" and f.get("composer"):
        items = [it for it in items if f["composer"] in it["comps"]]
    if skip != "album" and f.get("album"):
        items = [it for it in items if it.get("album_id") == f["album"]]
    q = fold(f.get("q") or "").split()
    if q and items and not all(w in fold(f"{card['title']} {card['subtitle']}") for w in q):
        items = [it for it in items
                 if all(w in fold(f"{card['title']} {it['title']} {it.get('album') or ''}") for w in q)]
    return items


def _filters(args: dict) -> dict:
    f = {k: (str(args[k]) if args.get(k) not in (None, "") else None)
         for k in ("kind", "reason", "composer", "album", "q", "status")}
    if f["kind"] and f["kind"] not in KINDS:
        raise ValueError("unknown card kind")
    if f["status"] and f["status"] not in ("open", "dismissed"):
        raise ValueError("status must be open or dismissed")
    return f


def view(ix: Index, vis=None, can_correct: bool = False, sort: str = "impact", offset: int = 0,
         limit: int = 50, **filters) -> dict:
    """The triage page: counts by kind and reason, the composer and album facets, and one page of cards.
    Composer, album, reason and text filters pick items inside a card; the card's actions then cover exactly
    the items shown (a credit mapping stays global: per-album exceptions are on its items)."""
    if sort not in SORTS:
        raise ValueError("unknown sort")
    f = _filters(filters)
    offset, limit = max(0, int(offset or 0)), max(1, min(200, int(limit or 50)))
    raw, ctx = cards(ix, vis)
    shown = []
    for c in raw:
        items = _items_matching(c, f)
        if items:
            shown.append({**c, "items": items, "acclaim": sum(ctx["acclaim"].get(a, 0) for a in
                                                              {it.get("album_id") for it in items} if a),
                          "actionable": c["kind"] in ("name", "mb", "accolade", "discs")})
    shown.sort(key=_sort_key(sort))
    kinds: dict[str, dict] = {k: {"kind": k, "title": KIND_TITLES[k], "cards": 0, "items": 0} for k in KINDS}
    reasons: dict[str, int] = defaultdict(int)
    comps: dict[str, int] = defaultdict(int)
    albums: dict[str, int] = defaultdict(int)
    status = {"open": 0, "dismissed": 0}
    for c in raw:
        status[c["status"]] += len(c["items"])
        items = _items_matching(c, f, skip="kind")
        if items:
            kinds[c["kind"]]["cards"] += 1
            kinds[c["kind"]]["items"] += len(items)
            for it in items:
                reasons[it["reason"]] += 1
        for it in _items_matching(c, f, skip="composer"):
            for cid in it["comps"]:
                comps[cid] += 1
        for it in _items_matching(c, f, skip="album"):
            if it.get("album_id"):
                albums[it["album_id"]] += 1
    page = [_finish(c, c["items"], ctx, can_correct) for c in shown[offset:offset + limit]]
    comp_ids, album_names = ctx["comp_ids"], ctx["albums"]
    return {
        "total": status["open"], "dismissed": status["dismissed"],
        "items": sum(len(c["items"]) for c in shown), "cards_total": len(shown),
        "kinds": [v for v in kinds.values() if v["items"]],
        "reasons": sorted(({"reason": k, "title": reason_title(k), "n": v} for k, v in reasons.items()),
                          key=lambda x: (-x["n"], x["reason"])),
        "composers": sorted(({"id": k, "name": comp_ids[k]["name"], "n": v} for k, v in comps.items()
                             if k in comp_ids), key=lambda x: (-x["n"], fold(x["name"])))[:200],
        "albums": sorted(({"id": k, "name": album_names.get(k) or k, "n": v} for k, v in albums.items()),
                         key=lambda x: (-x["n"], fold(x["name"])))[:200],
        "filters": f, "sort": sort, "offset": offset, "limit": limit,
        "cards": page, "next": offset + limit if offset + limit < len(shown) else None,
    }


def items_for(ix: Index, vis=None, **filters) -> list[str]:
    """The ids of every open item the filters show (the "hide all shown" action)."""
    f = {**_filters(filters), "status": "open"}
    raw, _ = cards(ix, vis)
    return [it["id"] for c in raw for it in _items_matching(c, f)]


# ---- decisions and the undo log -----------------------------------------------------------------------------

def _max_override(con) -> int:
    return con.execute("SELECT COALESCE(MAX(id), 0) FROM override").fetchone()[0]


def _new_overrides(con, before: int) -> list[int]:
    return [r[0] for r in con.execute("SELECT id FROM override WHERE id>? ORDER BY id", (before,))]


def _row(con, sql: str, args: tuple) -> dict | None:
    r = con.execute(sql, args).fetchone()
    return dict(r) if r is not None else None


def _check_accepts(actions: list[dict]) -> None:
    if not isinstance(actions, list) or not actions:
        raise ValueError("no actions")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"at most {MAX_ACTIONS} actions at once")
    for a in actions:
        if not isinstance(a, dict) or not isinstance(a.get("op"), str):
            raise ValueError("each action needs an op")  # noqa: TRY004 - the API answers bad input as ValueError
    accepts = sum(1 for a in actions if (a["op"], a.get("decision", "together" if a["op"] == "join_discs"
                                                                     else None)) in _ACCEPTS)
    names = sum(1 for a in actions if a["op"] == "composer_name")
    if accepts > 1 or names > 1 or (accepts and names):
        raise ValueError("accept one match at a time: only rejections and hiding apply to many at once")


class _Run:
    """One batch being applied: what to undo, what it touched and which jobs it needs."""

    def __init__(self) -> None:
        self.undo: list[dict] = []
        self.targets: set[str] = set()
        self.jobs: set[str] = set()
        self.out: list[Any] = []


def _visible_items(ix: Index, vis) -> dict[str, dict]:
    keys = _keys(ix)
    out = {}
    for g in views.review(ix, vis)["groups"]:
        for it in g["items"]:
            if it["id"] in keys:
                out[it["id"]] = {**it, "key": keys[it["id"]]}
    return out


def _do_dismiss(ix: Index, a: dict, run: _Run, user, vis, cache: dict) -> None:
    if "visible" not in cache:
        cache["visible"] = _visible_items(ix, vis)
    vis_items = cache["visible"]
    ids = a.get("items")
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise ValueError("dismiss needs the item ids")
    now, done = time.time(), []
    for i in ids:
        it = vis_items.get(i)
        if it is None:
            continue                        # gone since the page loaded, or not this caller's to see
        cur = ix.con.execute("INSERT OR IGNORE INTO review_dismissal(key, reason, album_library_id, by_user, at) "
                             "VALUES (?,?,?,?,?)", (it["key"], it["reason"], it.get("album_id"), user, now))
        if cur.rowcount:
            done.append(it["key"])
    run.undo.append({"t": "undismiss", "keys": done})
    run.targets |= {"dismiss:" + k for k in done}
    run.out.append({"op": "dismiss", "n": len(done)})


def _do_restore(ix: Index, a: dict, run: _Run, user, vis, cache: dict) -> None:
    if "visible" not in cache:
        cache["visible"] = _visible_items(ix, vis)
    allowed = {it["key"] for it in cache["visible"].values()}
    keys = a.get("keys")
    if not isinstance(keys, list):
        raise ValueError("restore needs the item keys")  # noqa: TRY004
    rows = []
    for k in keys:
        if k not in allowed:
            continue
        r = _row(ix.con, "SELECT * FROM review_dismissal WHERE key=?", (k,))
        if r:
            ix.con.execute("DELETE FROM review_dismissal WHERE key=?", (k,))
            rows.append(r)
    run.undo.append({"t": "redismiss", "rows": rows})
    run.targets |= {"dismiss:" + r["key"] for r in rows}
    run.out.append({"op": "restore", "n": len(rows)})


def _do_composer_name(ix: Index, a: dict, run: _Run, user, vis, cache: dict) -> None:
    name, cid, album = a.get("name"), a.get("composer_id"), a.get("album_id")
    if not isinstance(name, str) or not isinstance(cid, str):
        raise ValueError("composer_name needs a name and a composer")  # noqa: TRY004
    if album is not None:
        if "visible" not in cache:
            cache["visible"] = _visible_items(ix, vis)
        if not any(it.get("album_id") == album for it in cache["visible"].values()):
            raise views.NotFound(album)
    before = _max_override(ix.con)
    albums = ix.set_name_identity(name, cid, user, album=album)
    run.undo.append({"t": "overrides", "ids": _new_overrides(ix.con, before)})
    run.undo.append({"t": "resign", "album": album})
    run.targets |= {"name:" + c.key for c in parse_names(name)} or {"name:" + norm_key(name)}
    run.jobs.add("scan")
    run.out.append({"op": "composer_name", "albums": len(albums)})


def _do_mb(ix: Index, a: dict, run: _Run, user, vis, cache: dict) -> None:
    con = ix.con
    wid = views.work(ix, str(a.get("work_id") or ""), vis)["work"]["id"]      # NotFound when hidden
    ids = list(a.get("mbids") or []) + ([a["mbid"]] if a.get("mbid") else [])
    aliases = {r[0] for r in con.execute("SELECT value FROM alias WHERE entity='work' AND kind='mbid' "
                                         "AND entity_id=?", (wid,))}
    match = _row(con, "SELECT * FROM mb_work_match WHERE work_id=?", (wid,))
    item = _row(con, "SELECT * FROM review_item WHERE id=?", (f"mbw:{wid}",))
    before = _max_override(con)
    out = work_link.set_user_link(con, wid, user, str(a.get("decision") or ""), ids)
    after = {r[0] for r in con.execute("SELECT value FROM alias WHERE entity='work' AND kind='mbid' "
                                       "AND entity_id=?", (wid,))}
    run.undo += [{"t": "overrides", "ids": _new_overrides(con, before)},
                 {"t": "aliases", "work": wid, "add": sorted(aliases - after), "drop": sorted(after - aliases)},
                 {"t": "mb_match", "work": wid, "row": match},
                 {"t": "review_row", "id": f"mbw:{wid}", "row": item}]
    run.targets.add("work:" + wid)
    run.jobs.add("dates")
    run.out.append(out)


def _do_accolade(ix: Index, a: dict, run: _Run, user, vis, cache: dict) -> None:
    con = ix.con
    rid = views.recording(ix, str(a.get("recording_id") or ""), vis)["id"]      # NotFound when hidden
    entry = str(a.get("entry") or a.get("entry_ref") or "")
    r = con.execute("SELECT album_library_id, COALESCE(source_work_id, work_id) FROM recording WHERE id=?",
                    (rid,)).fetchone()
    if r is None:
        raise ValueError("unknown recording")
    key = [entry, str(r[0]), str(r[1])]
    prev = _row(con, "SELECT * FROM accolade_decision WHERE entry_ref=? AND album_library_id=? AND work_id=?",
                tuple(key))
    out = accolades.decide(con, entry, rid, str(a.get("decision") or ""), user)
    run.undo.append({"t": "accolade", "key": key, "row": prev})
    run.targets.add("accolade:" + ":".join(key))
    run.jobs.add("accolades")
    run.out.append(out)


def _disc_targets(ix: Index, ids: list[int], run: _Run) -> None:
    albums: list[str] = []
    for oid in ids:
        r = ix.con.execute("SELECT entity_id, payload_json FROM override WHERE id=?", (oid,)).fetchone()
        run.targets.add("discs:" + r[0])
        p = json.loads(r[1])
        albums += [x for x in (p.get("first"), p.get("second")) if x and x not in albums]
    # undone in reverse: the decisions go first, then the sets are recomputed
    run.undo += [{"t": "join_sets", "albums": albums}, {"t": "overrides", "ids": ids}]
    run.jobs.add("accolades")


def _do_join(ix: Index, a: dict, run: _Run, user, vis, cache: dict) -> None:
    first, second = str(a.get("first_recording_id") or ""), str(a.get("second_recording_id") or "")
    for rid in (first, second):
        views.recording(ix, rid, vis)      # NotFound when hidden
    before = _max_override(ix.con)
    leads = ix.join_discs(first, second, user, str(a.get("decision") or "together"))
    _disc_targets(ix, _new_overrides(ix.con, before), run)
    run.out.append({"op": "join_discs", "recordings": leads})


def _do_separate(ix: Index, a: dict, run: _Run, user, vis, cache: dict) -> None:
    rid = str(a.get("recording_id") or "")
    views.recording(ix, rid, vis)          # NotFound when hidden
    before = _max_override(ix.con)
    rids = ix.separate_discs(rid, user)
    _disc_targets(ix, _new_overrides(ix.con, before), run)
    run.out.append({"op": "separate_discs", "recordings": rids})


_OPS = {"dismiss": _do_dismiss, "restore": _do_restore, "composer_name": _do_composer_name,
        "work_mb_link": _do_mb, "accolade_decide": _do_accolade, "join_discs": _do_join,
        "separate_discs": _do_separate}


def _label(actions: list[dict], run: _Run) -> str:
    ops = {a["op"] for a in actions}
    if ops == {"dismiss"}:
        n = sum(o.get("n", 0) for o in run.out)
        return f"Hid {n} item{'s' if n != 1 else ''}"
    if ops == {"restore"}:
        return "Showed hidden items again"
    a = actions[0]
    if a["op"] == "composer_name":
        return f"Credit “{a.get('name')}”" + (" on one album" if a.get("album_id") else "")
    if a["op"] == "work_mb_link":
        return "MusicBrainz link" if a.get("decision") == "accept" else \
            f"Rejected MusicBrainz proposals ({len(actions)})"
    if a["op"] == "accolade_decide":
        return "Award confirmed" if a.get("decision") == "confirm" else f"Award rejected ({len(actions)})"
    if a["op"] == "join_discs":
        return "Joined discs" if a.get("decision", "together") == "together" else f"Kept discs apart ({len(actions)})"
    if a["op"] == "separate_discs":
        return "Kept discs separate"
    return "Decision"


def apply(ix: Index, actions: list[dict], user: str | None, vis=None, label: str | None = None) -> dict:
    """Apply one request's decisions as one undoable batch. The caller holds the write transaction (a failure
    anywhere rolls back the whole batch). At most one accept (an MB link, an award, a disc join) or one
    credit mapping per batch: only rejections and hiding are bulk."""
    _check_accepts(actions)
    run, cache = _Run(), {}
    for a in actions:
        fn = _OPS.get(a["op"])
        if fn is None:
            raise ValueError(f"unknown action {a['op']}")
        fn(ix, a, run, user, vis, cache)
    if not run.targets and all(a["op"] in ("dismiss", "restore") for a in actions):
        return {"batch": None, "label": "Nothing changed", "results": run.out, "jobs": []}
    label = (str(label)[:200] if label else "") or _label(actions, run)
    cur = ix.con.execute(
        "INSERT INTO review_batch(op, label, by_user, at, actions, undo_json, targets_json) VALUES (?,?,?,?,?,?,?)",
        (actions[0]["op"] if len({a["op"] for a in actions}) == 1 else "mixed", label, user, time.time(),
         len(actions), json.dumps(run.undo), json.dumps(sorted(run.targets))))
    seq = cur.lastrowid
    if any(a["op"] == "dismiss" for a in actions):
        keys = [k for u in run.undo if u["t"] == "undismiss" for k in u["keys"]]
        ix.con.executemany("UPDATE review_dismissal SET batch=? WHERE key=?", [(seq, k) for k in keys])
    return {"batch": seq, "label": label, "results": run.out, "jobs": sorted(run.jobs)}


def _conflict(con, seq: int, targets: set[str]) -> dict | None:
    for r in con.execute("SELECT seq, label, targets_json FROM review_batch WHERE seq>? AND undone_at IS NULL "
                         "ORDER BY seq", (seq,)):
        if targets & set(json.loads(r["targets_json"])):
            return {"batch": r["seq"], "label": r["label"]}
    return None


def undo(ix: Index, seq: int, user: str | None) -> dict:
    """Reverse one batch (the caller's own; inside the caller's write transaction). Refused while a later,
    not undone batch touches the same thing: undo that one first."""
    con = ix.con
    b = _row(con, "SELECT * FROM review_batch WHERE seq=?", (int(seq),))
    if b is None or b["by_user"] != user:
        raise views.NotFound(str(seq))
    if b["undone_at"] is not None:
        raise ValueError("already undone")
    later = _conflict(con, b["seq"], set(json.loads(b["targets_json"])))
    if later:
        raise ValueError(f"undo the later decision first: {later['label']}")
    jobs: set[str] = set()
    for step in reversed(json.loads(b["undo_json"])):
        t = step["t"]
        if t == "undismiss":
            con.executemany("DELETE FROM review_dismissal WHERE key=?", [(k,) for k in step["keys"]])
        elif t == "redismiss":
            con.executemany("INSERT OR REPLACE INTO review_dismissal(key, reason, album_library_id, by_user, at, "
                            "batch) VALUES (:key, :reason, :album_library_id, :by_user, :at, :batch)", step["rows"])
        elif t == "overrides":
            con.executemany("DELETE FROM override WHERE id=?", [(i,) for i in step["ids"]])
        elif t == "resign":
            if step["album"]:
                con.execute("UPDATE album_info SET signature=NULL WHERE album_library_id=?", (step["album"],))
            else:
                con.execute("UPDATE album_info SET signature=NULL")
            jobs.add("scan")
        elif t == "aliases":
            con.executemany("DELETE FROM alias WHERE entity='work' AND kind='mbid' AND entity_id=? AND value=?",
                            [(step["work"], m) for m in step["drop"]])
            con.executemany("INSERT OR IGNORE INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)",
                            [(step["work"], m) for m in step["add"]])
            jobs.add("dates")
        elif t == "mb_match":
            con.execute("DELETE FROM mb_work_match WHERE work_id=?", (step["work"],))
            if step["row"]:
                _insert(con, "mb_work_match", step["row"])
        elif t == "review_row":
            con.execute("DELETE FROM review_item WHERE id=?", (step["id"],))
            if step["row"]:
                _insert(con, "review_item", step["row"])
        elif t == "accolade":
            con.execute("DELETE FROM accolade_decision WHERE entry_ref=? AND album_library_id=? AND work_id=?",
                        tuple(step["key"]))
            if step["row"]:
                _insert(con, "accolade_decision", step["row"])
            jobs.add("accolades")
        elif t == "join_sets":
            disc_mod.join_sets(ix, [a for a in step["albums"]
                                    if con.execute("SELECT 1 FROM album_info WHERE album_library_id=?",
                                                   (a,)).fetchone()])
            jobs.add("accolades")
    now = time.time()
    con.execute("UPDATE review_batch SET undone_at=? WHERE seq=?", (now, b["seq"]))
    con.execute("INSERT INTO correction_log(op, payload_json, user, at) VALUES (?,?,?,?)",
                ("review_undo", json.dumps({"batch": b["seq"], "op": b["op"], "label": b["label"]}), user, now))
    return {"batch": b["seq"], "label": b["label"], "jobs": sorted(jobs)}


def _insert(con, table: str, row: dict) -> None:
    cols = list(row)
    con.execute(f"INSERT OR REPLACE INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [row[c] for c in cols])


def history(ix: Index, user: str | None, limit: int = 30) -> list[dict]:
    """The caller's recent decisions, newest first, and whether each can be undone now."""
    rows = [dict(r) for r in ix._q("SELECT seq, op, label, at, undone_at, actions, targets_json FROM review_batch "
                                   "WHERE by_user IS ? ORDER BY seq DESC LIMIT ?", (user, max(1, min(100, limit))))]
    out = []
    for r in rows:
        blocked = None if r["undone_at"] else _conflict(ix.con, r["seq"], set(json.loads(r["targets_json"])))
        out.append({"batch": r["seq"], "op": r["op"], "label": r["label"], "at": r["at"], "actions": r["actions"],
                    "undone": r["undone_at"] is not None, "can_undo": r["undone_at"] is None and blocked is None,
                    "blocked_by": blocked["batch"] if blocked else None})
    return out
