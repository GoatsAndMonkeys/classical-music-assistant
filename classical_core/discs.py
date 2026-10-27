"""One recording across the separate MA albums of a multi-disc set ('[Disc 1]' / '[Disc 2]', 'CD1', '(2)').

Every album keeps deriving its own recordings. After an album is derived, the join pass looks at the other
albums of its set (same name without the disc marker and same album artist, or the same MusicBrainz release;
for a disc of a box set, the box's discs either side: boxes.py) and joins a recording that ends one disc to the one that starts the next disc when the evidence agrees:

* the same work, and disc numbers that follow on (title marker, DISCNUMBER/TOTALDISCS tags, MA's disc number);
* the same album artist, label, year and release where both albums have one;
* the same principal performers (a conductor or orchestra in common, or a soloist when there is neither,
  else the track artists) and no conductor or orchestra on one disc that the other lacks entirely;
* movements that continue: no movement identity on both discs, movement numbers that go on from where the
  earlier disc stopped, acts that do not go backwards, no more parts than the work has, and an earlier disc
  whose recording is not already complete.

Anything that breaks a rule is never joined. A pair that breaks none but lacks disc numbers, performer
evidence, movement identities or an unbroken movement sequence is queued for review instead. A user decision
on a pair of albums ('together' or 'separate', the `disc_pair` override) wins over the evidence and is read
on every pass, so it survives re-derivation.

A join moves the later recording's slots onto the earlier (lead) recording in disc order; the later recording
keeps its row with `joined_to` set (hidden everywhere, and a redirect sends its id to the lead). Each slot
keeps its home recording and position, so a join is undone exactly before either album is derived again.
"""

from __future__ import annotations

import itertools
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .boxes import WINDOW
from .release import PLACEHOLDER_ARTISTS
from .text import norm_key
from .titles import disc_marker

if TYPE_CHECKING:
    from .index import Index

CONTINUATION = "possible continuation on another disc"
MAX_SET = 30                   # a 'set' bigger than this is a naming collision, not a box set: left alone
MEANINGFUL = ("mb:", "tagpart:", "tagtail:", "titlepart:")     # seq:/generic: keys are positions, not identities
_NOT_PERFORMING = {"conductor", "lyricist", "arranger", "composer", "orchestra", "ensemble", "chorus", "choir"}


# ---- per-album disc evidence (computed when the album is derived) ---------------------------------------


def album_evidence(name: str, artist: str, tag_discs: list[int | None], tag_totals: list[int | None],
                   ma_discs: list[int | None], releases: list[str | None]) -> dict[str, Any]:
    """The album's set key, disc number, disc total and MB release. A number counts only when every track
    agrees; a title marker beats a tag that says 1 (a tagger's default), any other disagreement is unknown."""
    base, title_n, title_total = disc_marker(name)

    def one(values: list) -> Any:
        vals = {v for v in values if v}
        return vals.pop() if len(vals) == 1 and None not in [v or None for v in values] else None

    tag_n = one(tag_discs) or one(ma_discs)
    if title_n is not None:
        disc = title_n if tag_n in (None, 1, title_n) else None
    else:
        disc = tag_n
    tag_total = one(tag_totals)
    total = title_total or tag_total
    if title_total and tag_total and title_total != tag_total:
        total = None
    if disc is not None and total is not None and disc > total:
        disc = total = None
    key = norm_key(base)
    return {"set_key": f"{key}|{norm_key(artist)}" if key else None, "disc_no": disc, "disc_total": total,
            "release_mbid": one(releases)}


# ---- facts the pair rules read ---------------------------------------------------------------------------


@dataclass
class Rec:
    id: str
    aid: str
    work_id: str
    complete: bool
    total: int | None
    years: tuple[int, int] | None
    slots: int
    keys: list[str] = field(default_factory=list)
    nums: list[int] = field(default_factory=list)
    sections: list[int] = field(default_factory=list)
    conductors: set[str] = field(default_factory=set)
    orchestras: set[str] = field(default_factory=set)
    soloists: set[str] = field(default_factory=set)
    artists: set[str] = field(default_factory=set)


@dataclass
class Disc:
    aid: str
    name: str
    artist: str | None
    year: int | None
    label: str | None
    disc_no: int | None
    disc_total: int | None
    release: str | None
    first: Rec | None = None       # the recording that starts the album's placed tracks
    last: Rec | None = None        # the recording that ends them
    sources: frozenset[str] = frozenset()   # provider instances that play every placed track of the album


def _year(v: Any) -> int | None:
    try:
        return int(str(v)[:4]) if v else None
    except ValueError:
        return None


def _rec(ix: Index, rid: str) -> Rec | None:
    r = ix._one("SELECT id, work_id, album_library_id, complete, parts_total, recorded_start, recorded_end, "
                "own_json FROM recording WHERE id=?", (rid,))
    if r is None:
        return None
    own = json.loads(r["own_json"]) if r["own_json"] else {}
    lo, hi = _year(own.get("recorded_start", r["recorded_start"])), _year(own.get("recorded_end", r["recorded_end"]))
    rec = Rec(r["id"], r["album_library_id"], ix.resolve("work", r["work_id"]),
              bool(own.get("complete", r["complete"])), r["parts_total"],
              (lo, hi or lo) if lo else None, 0)
    for s in ix._q("SELECT s.part_key, s.section, s.movement_no, t.artist FROM recording_slot s "
                   "JOIN occurrence o ON o.id=s.occurrence_id LEFT JOIN track_info t "
                   "ON t.library_track_id=o.library_track_id WHERE s.home_id=? ORDER BY s.home_ord", (rid,)):
        rec.slots += 1
        if s["part_key"] and s["part_key"].startswith(MEANINGFUL) and "#" not in s["part_key"]:
            rec.keys.append(s["part_key"])
        if s["movement_no"] is not None:
            rec.nums.append(s["movement_no"])
        if s["section"] is not None:
            rec.sections.append(s["section"])
        a = norm_key(s["artist"] or "")
        if a and a not in PLACEHOLDER_ARTISTS:
            rec.artists.add(a)
    for c in ix._q("SELECT DISTINCT tc.role, p.kind, p.name FROM recording_slot s JOIN track_credit tc "
                   "ON tc.occurrence_id=s.occurrence_id JOIN person p ON p.id=tc.person_id WHERE s.home_id=?", (rid,)):
        name, role = norm_key(c["name"]), (c["role"] or "").lower()
        if not name:
            continue
        if role == "conductor":
            rec.conductors.add(name)
        elif role in ("chorus", "choir"):
            continue
        elif c["kind"] == "group" or role in ("orchestra", "ensemble"):
            rec.orchestras.add(name)
        elif role not in _NOT_PERFORMING:
            rec.soloists.add(name)
    return rec


def _disc(ix: Index, row: Any) -> Disc:
    # a box disc's number from its title ('CD 061') when the tags give none
    no = row["disc_no"] if row["disc_no"] is not None else row["box_disc"]
    d = Disc(row["album_library_id"], row["name"] or "", row["artist"], row["year"], row["label"], no,
             row["disc_total"], row["release_mbid"] or row["mb_release"])
    placed = ix._q("SELECT s.home_id FROM recording_slot s JOIN occurrence o ON o.id=s.occurrence_id "
                   "WHERE o.album_library_id=? AND o.present=1 "
                   "ORDER BY COALESCE(o.disc,1), COALESCE(o.position,0), s.home_ord", (d.aid,))
    if placed:
        d.first = _rec(ix, placed[0][0])
        d.last = d.first if placed[-1][0] == placed[0][0] else _rec(ix, placed[-1][0])
        per_track: dict[str, set[str]] = {}
        for m in ix._q("SELECT o.id, m.provider_instance FROM recording_slot s JOIN occurrence o "
                       "ON o.id=s.occurrence_id LEFT JOIN mapping_state m ON m.library_track_id=o.library_track_id "
                       "AND m.status IN ('ok','read_error') WHERE o.album_library_id=? AND o.present=1", (d.aid,)):
            per_track.setdefault(m[0], set()).update([m[1]] if m[1] else [])
        d.sources = frozenset(set.intersection(*per_track.values())) if per_track else frozenset()
    return d


# ---- the pair rules ---------------------------------------------------------------------------------------


def _differ(a: Any, b: Any, key=norm_key) -> bool:
    return bool(a) and bool(b) and key(str(a)) != key(str(b))


def judge(a: Disc, b: Disc, forced: bool = False) -> tuple[str | None, str]:
    """('join' | 'review' | None, why) for 'b continues a'. None: never (no review either)."""
    ra, rb = a.last, b.first
    if ra is None or rb is None or ra.id == rb.id:
        return None, "nothing placed"
    if ra.work_id != rb.work_id:
        return None, "different works"
    if forced:
        return "join", "joined by a user"
    if a.disc_no is not None and b.disc_no is not None and b.disc_no != a.disc_no + 1:
        return None, "discs do not follow on"
    if _differ(a.disc_total, b.disc_total, str):
        return None, "different disc totals"
    if any(d.disc_total and d.disc_no and d.disc_no > d.disc_total for d in (a, b)):
        return None, "disc number above the total"
    if _differ(a.label, b.label) or _differ(a.release, b.release, str.lower) \
            or (_differ(a.artist, b.artist) and norm_key(a.artist or "") not in PLACEHOLDER_ARTISTS):
        return None, "different album artist, label or release"
    if ra.complete or rb.complete:
        return None, "a disc already holds the whole work"
    shared = set(ra.keys) & set(rb.keys)
    split_number = bool(shared) and shared == {ra.keys[-1]} and rb.keys[:1] == [ra.keys[-1]]
    if shared and not split_number:
        return None, "the same movements on both discs (a duplicate or another performance)"
    if ra.nums and rb.nums and min(rb.nums) <= max(ra.nums):
        return None, "movement numbers restart"
    if ra.sections and rb.sections and min(rb.sections) < max(ra.sections):
        return None, "acts go backwards"
    total = ra.total or rb.total
    if total and ra.slots + rb.slots > total and not split_number:
        return None, "more parts than the work has"
    for x, y in ((ra.conductors, rb.conductors), (ra.orchestras, rb.orchestras)):
        if x and y and not x & y:
            return None, "different performers"
    no_lead = not (ra.conductors and rb.conductors) and not (ra.orchestras and rb.orchestras)
    if no_lead and ra.soloists and rb.soloists and not ra.soloists & rb.soloists:
        return None, "different performers"
    uncredited = not (ra.conductors or ra.orchestras or ra.soloists or rb.conductors or rb.orchestras or rb.soloists)
    if uncredited and ra.artists and rb.artists and not ra.artists & rb.artists:
        return None, "different performers"            # no credits at all, and other track artists
    if ra.years and rb.years and (rb.years[0] - ra.years[1] > 1 or ra.years[0] - rb.years[1] > 1):
        return None, "recorded in different years"
    # nothing contradicts: join only on positive evidence of every kind
    doubts = []
    if _differ(a.year, b.year, str):
        doubts.append("different album years")         # a reissue year on one disc's tags is common
    if a.disc_no is None or b.disc_no is None:
        doubts.append("no disc number on both albums")
    agree = (ra.conductors & rb.conductors or ra.orchestras & rb.orchestras
             or (no_lead and ra.soloists & rb.soloists) or (uncredited and ra.artists & rb.artists))
    if not agree:
        doubts.append("no performer known on both discs")
    if not ((ra.keys and rb.keys) or (ra.nums and rb.nums)):
        doubts.append("no movement identities to compare")
    elif ra.nums and rb.nums and min(rb.nums) != max(ra.nums) + 1:
        doubts.append("a movement seems to be missing between the discs")
    if split_number:
        doubts.append("one movement continues across the discs")
    if a.sources != b.sources:
        # a joined recording is shown only to a user who can play all of it: never hide a disc by a guess
        doubts.append("the discs play from different sources")
    return ("review", "; ".join(doubts)) if doubts else ("join", "the discs continue")


# ---- sets -------------------------------------------------------------------------------------------------


def _overrides(ix: Index) -> dict[str, tuple[str, dict]]:
    """The latest user decision per album pair ('a|b', sorted): (op, payload)."""
    out: dict[str, tuple[str, dict]] = {}
    for r in ix._q("SELECT entity_id, op, payload_json FROM override WHERE entity='disc_pair' ORDER BY id"):
        out[r["entity_id"]] = (r["op"], json.loads(r["payload_json"] or "{}"))
    return out


def pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


_ALBUM_COLS = ("SELECT album_library_id, name, artist, year, label, disc_no, disc_total, release_mbid, mb_release, "
               "set_key, box_id, box_disc FROM album_info ")


def members(ix: Index, aids: list[str], overrides: dict[str, tuple[str, dict]] | None = None) -> list[Any]:
    """The albums of the sets these albums belong to (same set key, same MB release, or joined by a user), the
    discs of a box set either side of them, and every album already joined with any of those."""
    overrides = _overrides(ix) if overrides is None else overrides
    linked: dict[str, set[str]] = {}
    for k, (op, _) in overrides.items():
        if op == "together":
            x, _, y = k.partition("|")
            linked.setdefault(x, set()).add(y)
            linked.setdefault(y, set()).add(x)
    seen: dict[str, Any] = {}
    todo = list(aids)
    while todo and len(seen) <= MAX_SET:
        aid = todo.pop()
        if aid in seen:
            continue
        row = ix._one(_ALBUM_COLS + "WHERE album_library_id=?", (aid,))
        if row is None:
            continue
        seen[aid] = row
        rel = row["release_mbid"] or row["mb_release"]
        for r in ix._q(_ALBUM_COLS + "WHERE (set_key IS NOT NULL AND set_key=?) OR (release_mbid IS NOT NULL AND "
                       "release_mbid=?) OR (mb_release IS NOT NULL AND mb_release=?) LIMIT ?",
                       (row["set_key"], rel, rel, MAX_SET + 2)):
            if r["album_library_id"] not in seen:
                todo.append(r["album_library_id"])
        todo += [x for x in linked.get(aid, ()) if x not in seen]
    if len(seen) > MAX_SET:
        # a naming collision, or a box whose discs share one release: only the box neighbours below
        seen = {a: r for a, r in seen.items() if a in aids}
    # a disc of a box set ('Complete Works CD 061'): the box's discs either side (boxes.py), whatever their names
    for aid in aids:
        row = seen.get(aid)
        if row is not None and row["box_id"] and row["box_disc"] is not None:
            for r in ix._q(_ALBUM_COLS + "WHERE box_id=? AND box_disc BETWEEN ? AND ?",
                           (row["box_id"], row["box_disc"] - WINDOW, row["box_disc"] + WINDOW)):
                seen.setdefault(r["album_library_id"], r)
    # every album already joined with one of these: recomputing the set undoes its joins, so it is redone too
    for r in ix._q("SELECT DISTINCT r2.album_library_id FROM recording r JOIN recording r2 "
                   "ON COALESCE(r2.joined_to, r2.id)=COALESCE(r.joined_to, r.id) "
                   "WHERE r.album_library_id IN (SELECT value FROM json_each(?)) "
                   "AND (r.joined_to IS NOT NULL OR r.own_json IS NOT NULL)", (json.dumps(list(seen)),)):
        if r[0] not in seen:
            row = ix._one(_ALBUM_COLS + "WHERE album_library_id=?", (r[0],))
            if row is not None:
                seen[r[0]] = row
    return list(seen.values())


# ---- undo ---------------------------------------------------------------------------------------------------


_OWN = ("recorded_start", "recorded_end", "recorded_precision", "complete", "evidence_fields")


def unjoin(ix: Index, lead: str) -> None:
    """Every slot of this lead's group goes back to its home recording and position."""
    con = ix.con
    followers = [r[0] for r in ix._q("SELECT id FROM recording WHERE joined_to=?", (lead,))]
    con.execute("UPDATE recording_slot SET recording_id=home_id, ord=home_ord WHERE recording_id=?", (lead,))
    con.execute("UPDATE recording SET joined_to=NULL WHERE joined_to=?", (lead,))
    con.executemany("DELETE FROM redirect WHERE entity='recording' AND old_id=? AND reason='cross-disc'",
                    [(f,) for f in followers])
    r = ix._one("SELECT own_json FROM recording WHERE id=?", (lead,))
    if r and r[0]:
        own = json.loads(r[0])
        con.execute("UPDATE recording SET " + ", ".join(f"{k}=?" for k in _OWN) + ", own_json=NULL WHERE id=?",
                    (*[own.get(k) for k in _OWN], lead))


def unjoin_albums(ix: Index, aids: list[str]) -> None:
    """Undo every join that touches one of these albums (as the lead or as a later disc)."""
    if not aids:
        return
    leads = {r[0] for r in ix._q(
        "SELECT COALESCE(r.joined_to, r.id) FROM recording r WHERE r.album_library_id IN "
        "(SELECT value FROM json_each(?)) AND (r.joined_to IS NOT NULL OR r.own_json IS NOT NULL "
        "OR EXISTS (SELECT 1 FROM recording f WHERE f.joined_to=r.id))", (json.dumps(aids),))}
    for lead in sorted(leads):
        unjoin(ix, lead)


# ---- the pass -------------------------------------------------------------------------------------------------


def _candidates(discs: list[Disc]) -> list[tuple[Disc, Disc]]:
    """Ordered (earlier, later) album pairs worth judging."""
    out = []
    for a in discs:
        for b in discs:
            if a is b:
                continue
            if a.disc_no is not None and b.disc_no is not None:
                if b.disc_no == a.disc_no + 1:
                    out.append((a, b))
            elif (a.disc_no is not None or b.disc_no is not None) and (b.disc_no is None or b.disc_no > 1):
                out.append((a, b))                         # an unnumbered album may come before or after
    return out


def _chain_ok(chain: list[Rec]) -> bool:
    keys = [k for r in chain for k in dict.fromkeys(r.keys)]
    total = next((r.total for r in chain if r.total), None)
    n = sum(r.slots for r in chain)
    return len(keys) - len(set(keys)) <= len(chain) - 1 and not (total and n > total + len(chain) - 1)


def join_sets(ix: Index, aids: list[str]) -> list[str]:
    """Recompute the joins of every set these albums belong to. Inside the caller's transaction. Returns the
    lead recordings of the joins made."""
    overrides = _overrides(ix)
    rows = members(ix, aids, overrides)
    if len(rows) > MAX_SET:
        return []
    ids = [r["album_library_id"] for r in rows]
    unjoin_albums(ix, ids)
    ix.con.execute("DELETE FROM review_item WHERE reason=? AND album_library_id IN (SELECT value FROM json_each(?))",
                   (CONTINUATION, json.dumps(ids)))
    if len(rows) < 2:
        return []
    discs = {r["album_library_id"]: _disc(ix, r) for r in rows}
    edges: list[tuple[Disc, Disc, bool]] = []
    reviews: list[tuple[Disc, Disc, str]] = []
    pairs = {(a.aid, b.aid): (a, b) for a, b in _candidates(list(discs.values()))}
    for k, (op, p) in overrides.items():
        x, _, y = k.partition("|")
        if op == "together" and x in discs and y in discs:
            first, second = (p.get("first"), p.get("second")) if {p.get("first"), p.get("second")} == {x, y} \
                else (x, y)
            pairs.pop((second, first), None)
            pairs[(first, second)] = (discs[first], discs[second])
    for (x, y), (a, b) in sorted(pairs.items(), key=lambda kv: (kv[1][0].disc_no or 0, kv[0])):
        op = overrides.get(pair_key(x, y), (None, {}))[0]
        if op == "separate":
            continue
        verdict, why = judge(a, b, forced=op == "together")
        if verdict == "join":
            edges.append((a, b, op == "together"))
        elif verdict == "review":
            reviews.append((a, b, why))
    # chains: each recording follows at most one and is followed by at most one. Where the evidence offers
    # two (a duplicate disc, two copies of a set) no guess is joined: a user's decision first, else a review.
    nxt: dict[str, Rec] = {}
    prev: dict[str, Rec] = {}
    recs: dict[str, Rec] = {}
    forced_from: set[str] = set()

    def link(ra: Rec, rb: Rec) -> None:
        nxt[ra.id], prev[rb.id] = rb, ra
        recs[ra.id], recs[rb.id] = ra, rb

    for a, b, forced in edges:
        if forced and a.last and b.first and a.last.id not in nxt and b.first.id not in prev:
            link(a.last, b.first)
            forced_from.add(a.last.id)
    guesses = [(a, b) for a, b, forced in edges if not forced and a.last and b.first
               and a.last.id not in nxt and b.first.id not in prev]       # a user's decision settles the rest
    outs, ins = Counter(a.last.id for a, _ in guesses), Counter(b.first.id for _, b in guesses)
    for a, b in guesses:
        ra, rb = a.last, b.first
        assert ra is not None and rb is not None
        if outs[ra.id] > 1 or ins[rb.id] > 1:
            reviews.append((a, b, "more than one disc could continue here"))
            continue
        link(ra, rb)
    leads: list[str] = []
    joined: set[str] = set()
    for rid, r in recs.items():
        if rid in prev:
            continue
        chain = [r]
        while chain[-1].id in nxt and len(chain) <= MAX_SET:
            chain.append(nxt[chain[-1].id])
        if not all(x.id in forced_from for x in chain[:-1]) and not _chain_ok(chain):
            reviews.append((discs[chain[0].aid], discs[chain[-1].aid], "the discs together hold more than the work"))
            continue
        _apply(ix, chain)
        leads.append(r.id)
        joined |= {x.id for x in chain}
    queued: set[str] = set()
    for a, b, why in reviews:
        if b.first is None or a.last is None or b.first.id in joined or pair_key(a.aid, b.aid) in queued:
            continue                                   # one item per album pair, whichever way round
        queued.add(pair_key(a.aid, b.aid))
        ix.con.execute(
            "INSERT INTO review_item(id, reason, album_library_id, entity, entity_id, detail_json) VALUES (?,?,?,?,?,?)",
            (_new_id(), CONTINUATION, b.aid, "recording", b.first.id,
             json.dumps({"album": b.name, "earlier_album": a.name, "earlier_recording": a.last.id,
                         "why": why}, ensure_ascii=False)))
    return leads


def _new_id() -> str:
    from .ids import new_id

    return new_id("v")


def _apply(ix: Index, chain: list[Rec]) -> None:
    con = ix.con
    lead = chain[0]
    rows = {r["id"]: dict(r) for r in ix._q(
        "SELECT id, recorded_start, recorded_end, recorded_precision, complete, evidence_fields, parts_total "
        "FROM recording WHERE id IN (SELECT value FROM json_each(?))", (json.dumps([c.id for c in chain]),))}
    own = {k: rows[lead.id][k] for k in _OWN}
    con.execute("UPDATE recording SET own_json=? WHERE id=?", (json.dumps(own), lead.id))
    offset = ix._one("SELECT COALESCE(MAX(home_ord), -1) + 1 FROM recording_slot WHERE home_id=?", (lead.id,))[0]
    now = time.time()
    for r in chain[1:]:
        con.execute("UPDATE recording_slot SET recording_id=?, ord=home_ord+? WHERE home_id=?", (lead.id, offset, r.id))
        offset += ix._one("SELECT COALESCE(MAX(home_ord), -1) + 1 FROM recording_slot WHERE home_id=?", (r.id,))[0]
        con.execute("UPDATE recording SET joined_to=? WHERE id=?", (lead.id, r.id))
        con.execute("INSERT OR REPLACE INTO redirect(entity, old_id, new_id, reason, at) "
                    "VALUES ('recording', ?, ?, 'cross-disc', ?)", (r.id, lead.id, now))
    starts = sorted((rows[c.id]["recorded_start"], rows[c.id]["recorded_precision"]) for c in chain
                    if rows[c.id]["recorded_start"])
    ends = sorted(rows[c.id]["recorded_end"] for c in chain if rows[c.id]["recorded_end"])
    total = next((c.total for c in chain if c.total), None)
    nums = [n for c in chain for n in c.nums]
    n = sum(c.slots for c in chain)
    complete = bool(total and n == total and (not nums or sorted(set(nums)) == list(range(1, total + 1))))
    con.execute("UPDATE recording SET recorded_start=?, recorded_end=?, recorded_precision=?, complete=?, "
                "evidence_fields=? WHERE id=?",
                (starts[0][0] if starts else None, ends[-1] if ends else None, starts[0][1] if starts else None,
                 int(complete), max(rows[c.id]["evidence_fields"] or 0 for c in chain), lead.id))


def group(ix: Index, rid: str) -> list[dict]:
    """The albums a recording spans, in play order (one entry when it is not joined)."""
    rid = ix.resolve("recording", rid)
    return [dict(r) for r in ix._q(
        "SELECT r.id AS recording_id, a.album_library_id AS id, a.name, a.disc_no FROM recording r "
        "JOIN album_info a ON a.album_library_id=r.album_library_id WHERE r.id=? OR r.joined_to=? "
        "ORDER BY (SELECT MIN(s.ord) FROM recording_slot s WHERE s.home_id=r.id)", (rid, rid))]


# ---- user decisions -----------------------------------------------------------------------------------------


def decide(ix: Index, first: str, second: str, op: str, user: str | None) -> list[str]:
    """A user decision on two recordings of a set: 'together' (the second continues the first on the next disc)
    or 'separate' (never join them). Recorded per album pair; the pass reruns now. Returns the joined leads."""
    if op not in ("together", "separate"):
        raise ValueError("decision must be together or separate")
    ra, rb = ix._one("SELECT id, album_library_id, work_id FROM recording WHERE id=?", (first,)), \
        ix._one("SELECT id, album_library_id, work_id FROM recording WHERE id=?", (second,))
    if ra is None or rb is None:
        raise ValueError("unknown recording")
    if ra["album_library_id"] == rb["album_library_id"]:
        raise ValueError("both recordings are on the same album")
    if op == "together":
        if ix.resolve("work", ra["work_id"]) != ix.resolve("work", rb["work_id"]):
            raise ValueError("the recordings are of different works")
        da, db = (ix._one("SELECT COALESCE(disc_no, box_disc) FROM album_info WHERE album_library_id=?", (r["album_library_id"],))
                  for r in (ra, rb))
        if da and db and da[0] and db[0] and db[0] < da[0]:
            ra, rb = rb, ra
        ends = _disc(ix, ix._one(_ALBUM_COLS + "WHERE album_library_id=?", (ra["album_library_id"],)))
        starts = _disc(ix, ix._one(_ALBUM_COLS + "WHERE album_library_id=?", (rb["album_library_id"],)))
        if not (ends.last and ends.last.id == ra["id"] and starts.first and starts.first.id == rb["id"]):
            raise ValueError("only the recording that ends one disc and the one that starts the next can be joined")
    a, b = ra["album_library_id"], rb["album_library_id"]
    now = time.time()
    payload = {"first": a, "second": b, "work": ix.resolve("work", ra["work_id"])}
    ix.con.execute("INSERT INTO override(entity, entity_id, op, payload_json, created_at, by_user) "
                   "VALUES ('disc_pair', ?, ?, ?, ?, ?)", (pair_key(a, b), op, json.dumps(payload), now, user))
    ix.con.execute("INSERT INTO correction_log(op, payload_json, user, at) VALUES (?,?,?,?)",
                   (f"discs_{op}", json.dumps({**payload, "recordings": [ra["id"], rb["id"]]}), user, now))
    return join_sets(ix, [a, b])


def separate(ix: Index, rid: str, user: str | None) -> list[str]:
    """'Keep these discs apart': a 'separate' decision on every joined pair of this recording's group."""
    albums = group(ix, rid)
    if len(albums) < 2:
        raise ValueError("this recording does not span several discs")
    now = time.time()
    for x, y in itertools.pairwise(albums):
        ix.con.execute("INSERT INTO override(entity, entity_id, op, payload_json, created_at, by_user) "
                       "VALUES ('disc_pair', ?, 'separate', ?, ?, ?)",
                       (pair_key(x["id"], y["id"]), json.dumps({"first": x["id"], "second": y["id"]}), now, user))
    ix.con.execute("INSERT INTO correction_log(op, payload_json, user, at) VALUES (?,?,?,?)",
                   ("discs_separate", json.dumps({"recording": rid, "albums": [a["id"] for a in albums]}), user, now))
    join_sets(ix, [a["id"] for a in albums])
    return [a["recording_id"] for a in albums]
