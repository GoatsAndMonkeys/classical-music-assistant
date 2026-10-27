"""Box sets: a label's reissue of many recordings as one release ('Beethoven: Complete Works', 'Karajan: The
Complete EMI Recordings'), whether MA lists it as one album with many discs or as one album per disc.

Each album's box evidence is computed when the album is derived (`album_evidence`, stored on album_info):
the box it may belong to (`box_id`, a hash of its key), the box title, its disc number and the disc total.
The key is, in order:

* the MusicBrainz release, when the album is one disc of it ('r:');
* the album name before a disc marker with a disc number up to three digits ('Complete Works CD 061 - Leonore
  Part 1', 'Bach Edition [CD 12 of 155]': 't:' box title + album artist);
* the album's set key (its name without the disc marker, discs.py) when its tags give a disc number ('s:');
* the album itself when its own tracks span several discs ('a:').

Whether those albums ARE a box is decided when it is read (`resolve`), from what the index says now, so it
follows every user correction and every album added or removed during a long import without a recompute:

* one album: at least 4 discs and at least 4 different works of more than one track each (a 2-3 disc opera or
  symphony stays an album; a badly tagged opera whose arias each became a 'work' is not a box either);
* several albums: at least 2 of them, no disc number twice, and either a disc number or total of 10 or more
  ('CD 061') or at least 4 discs holding at least 4 such works. A 4-6 disc opera ripped one album per disc
  is a set (discs.py joins it), not a box. Two boxes that share a generic title and artist ('Complete Works',
  'Various Artists') and so a disc number are told apart by composer; when that does not settle it, neither is
  a box.
* a user's 'not a box set' (the `box` override) wins.

Nothing here changes a recording: a box only adds context ('from Complete Works, CD 61') and a page that
lists its discs and works.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .release import PLACEHOLDER_ARTISTS
from .text import norm_key
from .titles import strip_disc_marks

SINGLE_MIN_DISCS = 4            # one MA album: this many discs ...
SINGLE_MIN_WORKS = 4            # ... and this many works
MULTI_BIG_DISC = 10             # several albums: a disc number or total this high says box on its own ...
MULTI_MIN_DISCS = 4             # ... else this many discs ...
MULTI_MIN_WORKS = 4             # ... holding this many works
WINDOW = 8                      # discs.py looks this many box discs either side for a continuation

# 'Complete Works CD 061 - Leonore Part 1', 'Bach Edition [CD 12 of 155]', 'Karajan, Disc 3/5: Brahms'
_BOX_DISC = re.compile(
    r"^(?P<box>.*?[^\W_].*?)[\s,;:–—-]*[\[(]?\s*\b(?:cd|disc|disk)\s*(?P<n>\d{1,3})"
    r"(?:\s*(?:of|/)\s*(?P<t>\d{1,3}))?\b\s*[\])]?(?:\s*[-–—:.,]\s*(?P<sub>.*\S))?\s*$", re.IGNORECASE)
_GENERIC = re.compile(r"^(?:the )?(?:complete(?: works| recordings| edition)?|collection|edition|box(?: set)?|"
                      r"works|recordings|anniversary edition|the collection)$")


def parse_title(name: str | None) -> tuple[str, int, int | None, str | None] | None:
    """'Complete Works CD 061 - Leonore Part 1' -> ('Complete Works', 61, None, 'Leonore Part 1'); None when the
    name has no disc marker after a box title."""
    m = _BOX_DISC.match((name or "").strip())
    if not m:
        return None
    n, t = int(m.group("n")), int(m.group("t")) if m.group("t") else None
    box = m.group("box").strip(" ,;:-–—([")
    if n < 1 or (t is not None and n > t) or len(norm_key(box)) < 3:
        return None
    return box, n, t, (m.group("sub") or "").strip() or None


def box_id(key: str) -> str:
    return "x" + hashlib.sha1(key.encode()).hexdigest()[:12]


def album_evidence(aid: str, name: str | None, artist: str | None, set_key: str | None, disc_no: int | None,
                   disc_total: int | None, release: str | None, track_discs: list[int | None]) -> dict[str, Any]:
    """The box this album may be a disc of (or be, on its own), as album_info stores it."""
    none = {"box_id": None, "box_title": None, "box_disc": None, "box_total": None}
    parsed = parse_title(name)
    discs = {d for d in track_discs if d}
    if release and disc_no and len(discs) <= 1:
        key = "r:" + release.lower()
        title = parsed[0] if parsed else strip_disc_marks(name)
        disc, total = disc_no, disc_total
    elif parsed and len(discs) <= 1:
        key = f"t:{norm_key(parsed[0])}|{norm_key(artist)}"
        title, disc, total = parsed[0], parsed[1], parsed[2] or disc_total
    elif disc_no and set_key and len(discs) <= 1:
        key = "s:" + set_key
        title, disc, total = strip_disc_marks(name), disc_no, disc_total
    elif len(discs) >= SINGLE_MIN_DISCS:
        key = "a:" + aid
        title, disc, total = name, None, len(discs)
    else:
        return none
    return {"box_id": box_id(key), "box_title": title or name, "box_disc": disc, "box_total": total}


def subtitle(name: str | None) -> str | None:
    """What an album name says about its own disc of a box ('Leonore Part 1'); None when it says nothing more."""
    p = parse_title(name)
    return p[3] if p else None


# ---- resolution -----------------------------------------------------------------------------------------------


@dataclass
class Box:
    id: str
    title: str
    artist: str | None
    single: bool                                            # one MA album holding the whole box
    total: int | None                                       # discs in the box, as far as anything says
    members: list[dict] = field(default_factory=list)       # album rows, in disc order
    composers: list[str] = field(default_factory=list)      # the composers of its works, most works first

    def disc_of(self, aid: str) -> int | None:
        return next((m["box_disc"] for m in self.members if m["album_library_id"] == aid), None)


def _not_boxes(con) -> set[str]:
    out: dict[str, str] = {}
    for r in con.execute("SELECT entity_id, op FROM override WHERE entity='box' ORDER BY id"):
        out[r[0]] = r[1]
    return {k for k, op in out.items() if op == "not_box"}


_COLS = ("SELECT album_library_id, name, artist, year, image_json, box_id, box_title, box_disc, box_total "
         "FROM album_info ")


def resolve(con, ids: list[str] | None = None, aids: list[str] | None = None) -> list[Box]:
    """The boxes among these box ids (or the boxes of these albums; all when neither is given)."""
    if aids is not None:
        ids = [r[0] for r in con.execute("SELECT DISTINCT box_id FROM album_info WHERE box_id IS NOT NULL AND "
                                         "album_library_id IN (SELECT value FROM json_each(?))", (json.dumps(aids),))]
    if ids is not None:
        base = sorted({i.split(".")[0] for i in ids})
        if not base:
            return []
        rows = con.execute(_COLS + "WHERE box_id IN (SELECT value FROM json_each(?))", (json.dumps(base),)).fetchall()
    else:
        rows = con.execute(_COLS + "WHERE box_id IS NOT NULL").fetchall()
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["box_id"]].append(dict(r))
    # only groups that could be a box need their works
    maybe = {k: g for k, g in groups.items()
             if len(g) >= 2 or (g[0]["box_total"] or 0) >= SINGLE_MIN_DISCS}
    album_ids = [m["album_library_id"] for g in maybe.values() for m in g]
    works: dict[str, set[str]] = defaultdict(set)       # works of more than one track: a work, not a stray
    composers: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, str] = {}
    for r in con.execute(
            "SELECT r.album_library_id AS aid, r.work_id, c.id AS cid, c.name, "
            "(SELECT COUNT(*) FROM recording_slot s WHERE s.home_id=r.id) AS n FROM recording r "
            "LEFT JOIN work_composer wc ON wc.work_id=r.work_id AND wc.ord=0 LEFT JOIN composer c ON c.id=wc.composer_id "
            "WHERE r.album_library_id IN (SELECT value FROM json_each(?))", (json.dumps(album_ids),)):
        if r["n"] >= 2:
            works[r["aid"]].add(r["work_id"])
        if r["cid"]:
            composers[r["aid"]][r["cid"]] += 1
            names[r["cid"]] = r["name"]
    vetoed = _not_boxes(con)
    out: list[Box] = []
    for key, g in sorted(maybe.items()):
        for sub_id, members in _split(key, g, composers):
            if sub_id in vetoed or key in vetoed:
                continue
            box = _judge(sub_id, members, works, composers, names)
            if box is not None:
                out.append(box)
    if ids is not None:
        wanted = set(ids)
        out = [b for b in out if b.id in wanted or b.id.split(".")[0] in wanted]
    return out


def _split(key: str, members: list[dict], composers: dict[str, Counter]) -> list[tuple[str, list[dict]]]:
    """One group, or one per composer when a disc number repeats (two 'Complete Works' boxes under 'Various
    Artists'); a group that still repeats a disc number is dropped later."""
    nums = [m["box_disc"] for m in members if m["box_disc"] is not None]
    if len(nums) == len(set(nums)):
        return [(key, members)]
    by: dict[str, list[dict]] = defaultdict(list)
    for m in members:
        top = composers[m["album_library_id"]].most_common(1)
        by[top[0][0] if top else ""].append(m)
    return [(f"{key}.{hashlib.sha1(c.encode()).hexdigest()[:6]}", ms) for c, ms in sorted(by.items()) if c]


def _judge(bid: str, members: list[dict], works: dict[str, set[str]], composers: dict[str, Counter],
           names: dict[str, str]) -> Box | None:
    nums = [m["box_disc"] for m in members if m["box_disc"] is not None]
    if len(nums) != len(set(nums)):
        return None
    all_works = set().union(*(works[m["album_library_id"]] for m in members))
    single = len(members) == 1
    if single:
        total = members[0]["box_total"] or 0
        if total < SINGLE_MIN_DISCS or len(all_works) < SINGLE_MIN_WORKS:
            return None
    else:
        big = max([*nums, *(m["box_total"] or 0 for m in members)], default=0)
        if not (big >= MULTI_BIG_DISC or (len(members) >= MULTI_MIN_DISCS and len(all_works) >= MULTI_MIN_WORKS)):
            return None
        total = max((m["box_total"] or 0 for m in members), default=0) or None
    members = sorted(members, key=lambda m: (m["box_disc"] is None, m["box_disc"] or 0, m["album_library_id"]))
    count: Counter = Counter()
    for m in members:
        count.update(composers[m["album_library_id"]])
    comp = [names[c] for c, _ in count.most_common()]
    title = Counter(m["box_title"] for m in members if m["box_title"]).most_common(1)
    title_s = title[0][0] if title else members[0]["name"] or "Box set"
    if _GENERIC.match(norm_key(title_s)) and len(comp) == 1:
        title_s = f"{comp[0]}: {title_s}"           # 'Complete Works' alone says little
    artists = Counter(m["artist"] for m in members if m["artist"]
                      and norm_key(m["artist"]) not in PLACEHOLDER_ARTISTS).most_common(1)
    return Box(bid, title_s, artists[0][0] if artists else None, single, total or None, members, comp)


def of_albums(con, aids: list[str]) -> dict[str, tuple[Box, int | None]]:
    """album id -> (its box, its disc in the box) for the albums that are in a box."""
    out: dict[str, tuple[Box, int | None]] = {}
    want = set(aids)
    for b in resolve(con, aids=list(want)):
        for m in b.members:
            if m["album_library_id"] in want:
                out[m["album_library_id"]] = (b, m["box_disc"])
    return out


def context(box: Box, disc: int | None) -> dict:
    """What a recording on this box shows: 'from Complete Works, CD 61'."""
    return {"id": box.id, "title": box.title, "disc": disc, "single": box.single}


# ---- user decision --------------------------------------------------------------------------------------------


def decide(con, bid: str, op: str, user: str | None) -> None:
    """'not_box': these albums are not a box set, whatever the evidence says (survives re-index)."""
    if op != "not_box":
        raise ValueError("decision must be not_box")
    now = time.time()
    con.execute("INSERT INTO override(entity, entity_id, op, payload_json, created_at, by_user) "
                "VALUES ('box', ?, ?, '{}', ?, ?)", (bid, op, now, user))
    con.execute("INSERT INTO correction_log(op, payload_json, user, at) VALUES (?,?,?,?)",
                ("box_" + op, json.dumps({"box": bid}), user, now))
