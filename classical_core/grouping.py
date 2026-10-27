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
    partial: bool = False                # MB says the track performs part of the work (positive evidence)
    section: int | None = None           # opera act (0 = prologue) from the movement or title
    gap: bool = False                    # broadcast talk (intermission, announcer): transparent between parts
    continued: bool = False              # a generic number of the album's own work ('Nr. 11' opening disc 2)


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


def _evidence(s: Slot) -> bool:
    """Positive evidence that the track is one part of a longer performance: a movement identity, a
    movement number, or an MB partial-performance link. A bare work tag is not enough: two adjacent tracks
    tagged only "Symphony No. 5" may be two complete takes, so they are never joined (§5.1)."""
    return s.part_key is not None or s.movement_no is not None or s.partial or s.section is not None


def group_album(slots: list[Slot], ambiguous: bool = False) -> GroupingResult:
    runs: list[Run] = []
    notes: list[tuple[str, list[int]]] = []
    if ambiguous:
        # no order to read continuity from: nothing is joined by position. Each identified track is a
        # recording of its own, except the album's own work (the album names it, whatever the order), whose
        # numbers are one recording, in act order where the titles give one.
        notes.append(("ambiguous album positions", [i for i, s in enumerate(slots) if s.work_key]))
        album_work: dict[str, list[int]] = {}
        for i, s in enumerate(slots):
            if s.work_key is None or s.multi:
                continue
            run = album_work.get(s.work_key)
            if s.continued and run is not None and (
                    any(slots[j].occ_id in s.separate_from or s.occ_id in slots[j].separate_from for j in run)
                    or _contradicts([slots[j] for j in run], s)):
                notes.append(("split: contradicts the album's own work", [run[-1], i]))
                runs.append(Run([i], s.work_key))   # a user's 'separate', another conductor: never joined
            elif s.continued:
                if run is None:
                    run = album_work[s.work_key] = []
                    runs.append(Run(run, s.work_key))
                run.append(i)
            else:
                runs.append(Run([i], s.work_key))
        for r in runs:
            act, keyed = -1, []
            for i in r.slots:
                act = slots[i].section if slots[i].section is not None else act
                keyed.append((act, i))
            r.slots[:] = [i for _, i in sorted(keyed, key=lambda x: x[0])]
        return GroupingResult(runs, notes)
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
            if not (s.gap and cur):    # talk between two acts does not end the performance
                close()
            continue
        if cur:
            first = slots[cur[0]]
            prev = slots[cur[-1]]
            reason = None
            # one number spread over adjacent tracks, each a partial performance per MB, continues that number
            same_part = bool(s.part_key and s.partial and prev.partial and prev.part_key == s.part_key)
            if first.work_key != s.work_key:
                reason = "new work"
            elif not _evidence(s) or not _evidence(prev):
                reason = "no movement evidence"
            elif s.part_key and any(slots[j].part_key == s.part_key for j in cur) and not same_part:
                reason = "repeated movement (alternate take or second performance)"
            elif _whole(s) or any(_whole(slots[j]) for j in cur):
                reason = "complete performance of the whole work"
            elif s.section is not None and prev.section is not None and s.section < prev.section:
                reason = "acts go backwards"
            elif (s.movement_no is not None and prev.movement_no is not None and prev.parent_key == s.parent_key
                  and s.movement_no <= prev.movement_no and not same_part):
                reason = "movement numbers restart"
            elif (prev.disc != s.disc and not (s.section is not None and prev.section is not None
                                               and s.section > prev.section)
                  and not (s.continued and prev.continued)
                  and (s.movement_no is None or prev.movement_no is None or s.movement_no <= prev.movement_no)
                  and prev.parent_key == s.parent_key):
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
