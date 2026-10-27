"""The Classical browse tree (PLAN.md §6), MA-free.

`browse(index, sub_path, vis)` turns a path below `classical://` into a list of `Node`s; the plugin glue
converts nodes into MA `BrowseFolder`/`Track` objects. Paths nest, so MA's ".." entry (the path minus its
last segment) always leads back up:

    composers[/letter-x]/<cid>/<type-slug>[/pN]/<wid>/<rid>
    works/<type-slug>[/pN]/<wid>/<rid>
    works/<wid>/<rid>                 the short form a recording folder's URI resolves to (play URI)
    review/<reason-slug>[/pN]

Segments are recognised by shape: internal ids by their prefix (`c_`, `w_`, `r_`), `letter-x` and `pN`
markers, type and reason slugs. Unknown ids raise `NotFound`, the same as hidden ones, so ids can't be
probed. Every non-playable list is capped at `CAP` items; larger ones split into page folders. Recording
folders are always complete.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .catalogue import WORK_TYPES
from .ids import kind_of
from .index import Index
from .text import fold, slugify

CAP = 500
SCHEME = "classical://"
TYPE_BY_SLUG = {slugify(t): t for t in WORK_TYPES}
_PAGE = re.compile(r"^p(\d+)$")


class NotFound(LookupError):
    pass


@dataclass(slots=True)
class Node:
    kind: str                       # "folder" | "track"
    path: str                       # sub path below classical:// (folders); "" for tracks
    name: str
    version: str = ""
    item_id: str | None = None      # folder item id when it must differ from the path (recordings)
    playable: bool = False
    image: dict | None = None
    track_id: str | None = None     # library track id (tracks)
    sort_name: str | None = None
    translation_key: str | None = None


def recording_item_id(wid: str, rid: str) -> str:
    """Folder item id of a recording: its URI is classical://folder/works/<wid>/<rid>."""
    return f"works/{wid}/{rid}"


def play_uri(wid: str, rid: str) -> str:
    return f"{SCHEME}folder/{recording_item_id(wid, rid)}"


def letter_of(name: str) -> str:
    c = fold(name or "")[:1].upper()
    return c if "A" <= c <= "Z" else "#"


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def _paged(items: list, base: str, page: int | None, make) -> list[Node]:
    """Page folders when a list exceeds CAP; `make(item)` builds one node."""
    if page is None and len(items) > CAP:
        out = []
        for i in range(0, len(items), CAP):
            n = i // CAP + 1
            out.append(Node("folder", _join(base, f"p{n}"), f"{i + 1}–{min(i + CAP, len(items))}"))
        return out
    if page is not None:
        if page < 1 or (page - 1) * CAP >= max(len(items), 1):
            raise NotFound(base)
        items = items[(page - 1) * CAP: page * CAP]
    return [make(x) for x in items]


def browse(index: Index, sub_path: str, vis=None) -> list[Node]:
    segs = [s for s in sub_path.strip("/").split("/") if s]
    if not segs:
        return root(index, vis)
    rid = next((s for s in segs if kind_of(s) == "r"), None)
    wid = next((s for s in segs if kind_of(s) == "w"), None)
    page = next((int(m.group(1)) for s in segs if (m := _PAGE.match(s))), None)
    base = "/".join(segs)
    if rid:
        return recording_nodes(index, rid, vis)
    if wid:
        return work_nodes(index, wid, base, vis, page)
    head = segs[0]
    if head == "composers":
        return composer_branch(index, segs[1:], base, vis, page)
    if head == "works":
        if len(segs) == 1:
            return type_folders(index, None, base, vis)
        wt = TYPE_BY_SLUG.get(segs[1])
        if wt is None:
            raise NotFound(sub_path)
        if len([s for s in segs[2:] if not _PAGE.match(s)]):
            raise NotFound(sub_path)
        return works_nodes(index, None, wt, _join("works", segs[1]), vis, page, with_composer=True)
    if head == "review":
        return review_nodes(index, segs[1:], vis, page)
    raise NotFound(sub_path)


def root(index: Index, vis) -> list[Node]:
    n_review = len(index.review(vis))
    return [
        Node("folder", "composers", "Composers"),
        Node("folder", "works", "Works"),
        Node("folder", "review", "Review", version=f"{n_review} items" if n_review else "nothing to review"),
    ]


def _composer_node(c: dict, base: str) -> Node:
    life = "–".join(x for x in (c.get("born") or "", c.get("died") or "") if x) if c.get("born") else ""
    works = f"{c['works']} work{'s' if c['works'] != 1 else ''}"
    return Node("folder", _join(base, c["id"]), c["name"], version=" · ".join(x for x in (life, works) if x),
                sort_name=c["sort_name"])


def composer_branch(index: Index, rest: list[str], base: str, vis, page: int | None) -> list[Node]:
    plain = [s for s in rest if not _PAGE.match(s)]      # the path without its page marker
    cid = next((s for s in plain if kind_of(s) == "c"), None)
    if cid is None:
        letter = plain[0][len("letter-"):].upper() if plain and plain[0].startswith("letter-") else None
        if len(plain) > (1 if letter else 0):
            raise NotFound(base)
        lbase = _join("composers", plain[0] if letter else "")
        comps = index.composers(vis)
        if letter is None and page is None and len(comps) > CAP:
            counts = Counter(letter_of(c["sort_name"]) for c in comps)
            return [Node("folder", _join(lbase, f"letter-{x.lower()}"), x, version=f"{n} composers")
                    for x, n in sorted(counts.items(), key=lambda kv: (kv[0] == "#", kv[0]))]
        if letter is not None:
            comps = [c for c in comps if letter_of(c["sort_name"]) == letter]
            if not comps:
                raise NotFound(base)
        nbase = _join(lbase, f"p{page}" if page else "")
        return _paged(comps, lbase, page, lambda c: _composer_node(c, nbase))
    i = plain.index(cid)
    cbase = _join("composers", *plain[: i + 1])
    after = plain[i + 1:]
    if index.composer(cid) is None:
        raise NotFound(cid)
    if not after:
        nodes = type_folders(index, cid, cbase, vis)
        if not nodes:
            raise NotFound(cid)
        return nodes
    wt = TYPE_BY_SLUG.get(after[0])
    if wt is None or len(after) > 1:
        raise NotFound(base)
    return works_nodes(index, cid, wt, _join(cbase, after[0]), vis, page, with_composer=False)


def type_folders(index: Index, cid: str | None, base: str, vis) -> list[Node]:
    if cid is not None:
        counts = index.composer_types(cid, vis)
    else:
        c = Counter(w["work_type"] for w in index.works(vis=vis))
        order = {t: i for i, t in enumerate(WORK_TYPES)}
        counts = sorted(c.items(), key=lambda x: order.get(x[0], 99))
    return [Node("folder", _join(base, slugify(t)), t, version=f"{n} work{'s' if n != 1 else ''}")
            for t, n in counts]


def _work_version(w: dict, with_composer: bool) -> str:
    n = w["recordings"]
    parts = [w["composer"] if with_composer else None, w.get("catalogue_display"),
             f"{n} recording{'s' if n != 1 else ''}"]
    return " · ".join(p for p in parts if p)


def works_nodes(index: Index, cid: str | None, wt: str, base: str, vis, page: int | None,
                with_composer: bool) -> list[Node]:
    works = index.works(cid, wt, vis)
    if not works:
        raise NotFound(base)
    return _paged(works, base, page, lambda w: Node("folder", _join(base, f"p{page}" if page else "", w["id"]),
                                                    w["title"], version=_work_version(w, with_composer)))


def work_nodes(index: Index, wid: str, base: str, vis, page: int | None) -> list[Node]:
    w = index.work(wid)
    if w is None:
        raise NotFound(wid)
    wid = w["id"]
    recs = index.recordings(wid, vis)
    extra = index.contained_tracks(wid, vis)
    if not recs and not extra:
        raise NotFound(wid)
    nodes = [Node("folder", _join(base, r["id"]), r["label"], version=r.get("album") or "",
                  item_id=recording_item_id(wid, r["id"]), playable=True, image=r.get("image"))
             for r in recs]
    nodes += [Node("track", "", t["title"] or "", version=f"contains {w['title']}", track_id=t["library_track_id"])
              for t in extra]
    return nodes  # recordings of one work are listed whole (page folders by decade/conductor come in M2)


def recording_nodes(index: Index, rid: str, vis) -> list[Node]:
    r = index.recording(rid, vis)
    if r is None:
        raise NotFound(rid)
    w = index.work(r["work_id"])
    title = w["title"] if w else ""
    return [Node("track", "", t["movement_name"] or t["title"] or "", version=title, track_id=t["library_track_id"])
            for t in index.recording_tracks(rid, vis)]


def review_nodes(index: Index, rest: list[str], vis, page: int | None) -> list[Node]:
    items = index.review(vis)
    by_reason: dict[str, list[dict]] = {}
    for it in items:
        by_reason.setdefault(slugify(it["reason"]), []).append(it)
    if not rest:
        return [Node("folder", _join("review", slug), its[0]["reason"],
                     version=f"{len(its)} item{'s' if len(its) != 1 else ''}") for slug, its in sorted(by_reason.items())]
    its = by_reason.get(rest[0])
    if its is None or (len(rest) > 1 and page is None):
        raise NotFound(rest[0])

    def node(it: dict) -> Node:
        detail = it["detail"] or {}
        label = detail.get("title") or detail.get("track") or detail.get("work") or detail.get("album") or ""
        if it["entity"] == "occurrence" and it.get("entity_id"):
            tid = index.occurrence_track(it["entity_id"], vis)
            if tid:
                return Node("track", "", label or it["reason"], version=f"{it['id']} · {it['reason']}", track_id=tid)
        if it["entity"] == "work" and it.get("entity_id"):
            return Node("folder", _join("works", index.resolve("work", it["entity_id"])), label or it["reason"],
                        version=it["id"])
        return Node("folder", _join("review", rest[0]), label or it["reason"], version=it["id"])

    return _paged(its, _join("review", rest[0]), page, node)
