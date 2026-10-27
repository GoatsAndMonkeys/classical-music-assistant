"""Recording grouping within one album (PLAN.md §5.1).

Pure function: `group_album(slots)` takes the album's occurrences in (disc, position) order, each with the
top work it was assigned, and returns runs of slot indexes that form candidate recordings, plus review
notes. Nothing here touches the database.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .text import norm_key


@dataclass(slots=True)
class Slot:
    occ_id: str
    disc: int | None
    position: int | None
    work_key: str | None                 # top work identity within this pass; None = not classical/unknown
    parent_key: str | None = None        # immediate parent of the part (act, or the work itself)
    part_key: str | None = None          # identity of the movement (MB part id, or tag movement name/number)
    movement_no: int | None = None
    conductors: set[str] = field(default_factory=set)
    orchestras: set[str] = field(default_factory=set)
    choirs: set[str] = field(default_factory=set)
    recorded: str | None = None          # ISO date (any precision)
    multi: bool = False                  # links to more than one top work: never grouped (v1)
    together_with: str | None = None     # a `together` constraint id
    separate_from: frozenset[str] = frozenset()  # occurrences a `separate` constraint keeps apart


@dataclass(slots=True)
class Run:
    slots: list[int]
    work_key: str


@dataclass(slots=True)
class GroupingResult:
    runs: list[Run]
    notes: list[tuple[str, list[int]]]   # (reason, slot indexes) for the review folder


def _days(iso: str) -> int | None:
    import datetime as dt

    parts = iso.split("-")
    try:
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 6
        d = int(parts[2][:2]) if len(parts) > 2 else 15
        return dt.date(y, m, d).toordinal()
    except (ValueError, IndexError):
        return None


def _dates_conflict(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    # year-only dates are compared by year: 1962 vs 1962-05 is no contradiction
    if len(a) == 4 or len(b) == 4:
        return a[:4] != b[:4]
    da, db = _days(a), _days(b)
    return da is not None and db is not None and abs(da - db) > 30


def _contradicts(run_slots: list[Slot], s: Slot) -> str | None:
    """Only a contradiction splits; missing or additional credits never do (§5.1)."""
    for attr, reason in (("conductors", "different conductor"), ("orchestras", "different orchestra"),
                         ("choirs", "different choir")):
        mine = getattr(s, attr)
        if not mine:
            continue
        seen: set[str] = set()
        for r in run_slots:
            seen |= getattr(r, attr)
        if seen and not (seen & mine):
            return reason
    for r in run_slots:
        if _dates_conflict(r.recorded, s.recorded):
            return "recording dates more than 30 days apart"
    return None


def _whole(s: Slot) -> bool:
    """The track performs the whole work (not one movement of it, not partially)."""
    return s.part_key is not None and s.part_key == s.work_key


def group_album(slots: list[Slot], ambiguous: bool = False) -> GroupingResult:
    runs: list[Run] = []
    notes: list[tuple[str, list[int]]] = []
    if ambiguous:
        notes.append(("ambiguous album positions", [i for i, s in enumerate(slots) if s.work_key]))
        return GroupingResult([], notes)
    cur: list[int] = []

    def close() -> None:
        if cur:
            runs.append(Run(list(cur), slots[cur[0]].work_key or ""))
            cur.clear()

    for i, s in enumerate(slots):
        if s.multi:
            close()
            notes.append(("track contains several works", [i]))
            continue
        if s.work_key is None:
            close()
            continue
        if cur:
            first = slots[cur[0]]
            prev = slots[cur[-1]]
            reason = None
            if first.work_key != s.work_key:
                reason = "new work"
            elif s.part_key and any(slots[j].part_key == s.part_key for j in cur):
                reason = "repeated movement (alternate take or second performance)"
            elif _whole(s) or any(_whole(slots[j]) for j in cur):
                reason = "complete performance of the whole work"
            elif (s.movement_no is not None and prev.movement_no is not None and prev.parent_key == s.parent_key
                  and s.movement_no <= prev.movement_no):
                reason = "movement numbers restart"
            elif prev.disc != s.disc and (s.movement_no is None or prev.movement_no is None
                                          or s.movement_no <= prev.movement_no) and prev.parent_key == s.parent_key:
                reason = "no continuity across discs"
            elif any(slots[j].occ_id in s.separate_from for j in cur):
                reason = "separate constraint"
            else:
                reason = _contradicts([slots[j] for j in cur], s)
            if reason:
                if reason != "new work":
                    notes.append((f"split: {reason}", [cur[-1], i]))
                close()
        cur.append(i)
    close()

    # the same work in non-adjacent runs stays separate (a join needs continuity), but is listed for review
    by_work: dict[str, list[int]] = {}
    for ri, r in enumerate(runs):
        by_work.setdefault(r.work_key, []).append(ri)
    for rlist in by_work.values():
        if len(rlist) > 1:
            gaps = [ri for a, b in itertools.pairwise(rlist)
                    if runs[b].slots[0] != runs[a].slots[-1] + 1 for ri in (a, b)]
            if gaps:
                notes.append(("non-adjacent parts of one work", sorted({s for ri in gaps for s in runs[ri].slots})))
    return GroupingResult(runs, notes)


def credit_key(name: str) -> str:
    return norm_key(name)
