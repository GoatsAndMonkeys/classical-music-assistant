from dataclasses import replace

from classical_core.grouping import Slot, group_album


def s(i, work="W", part=None, no=None, parent=None, disc=1, cond=(), orch=(), rec=None, multi=False, partial=False):
    return Slot(f"o{i}", disc, i, work, parent or work, part or (f"p{no}" if no else None), no,
                set(cond), set(orch), set(), rec, multi, partial=partial)


def runs(slots, **kw):
    return [r.slots for r in group_album(slots, **kw).runs]


def test_simple_and_new_work():
    assert runs([s(0, no=1), s(1, no=2), s(2, "X", no=1), s(3, "X", no=2)]) == [[0, 1], [2, 3]]


def test_alternate_take_splits():
    sl = [s(0, no=1), s(1, no=2), s(2, no=3), s(3, part="p3", no=3)]
    res = group_album(sl)
    assert [r.slots for r in res.runs] == [[0, 1, 2], [3]]
    assert res.notes and "repeated movement" in res.notes[0][0]


def test_act_restart_is_normal():
    # movement numbers restart at Act II (a different parent) -> one recording
    sl = [s(0, "Op", "a1", 1, "Act1"), s(1, "Op", "a2", 2, "Act1"), s(2, "Op", "b1", 1, "Act2"),
          s(3, "Op", "b2", 2, "Act2")]
    assert runs(sl) == [[0, 1, 2, 3]]


def test_restart_within_same_parent_splits():
    assert runs([s(0, no=1), s(1, no=2), s(2, part="x1", no=1), s(3, part="x2", no=2)]) == [[0, 1], [2, 3]]


def test_changing_soloists_never_split_conductor_does():
    sl = [s(0, no=1, cond={"k"}), s(1, no=2), s(2, no=3, cond={"k"}, orch={"bpo"})]
    assert runs(sl) == [[0, 1, 2]]
    sl = [s(0, no=1, cond={"k"}), s(1, no=2, cond={"b"})]
    assert runs(sl) == [[0], [1]]


def test_two_performances_same_ensemble_on_one_box():
    sl = [s(i, no=n, cond={"k"}) for i, n in enumerate([1, 2, 3, 1, 2, 3])]
    assert runs(sl) == [[0, 1, 2], [3, 4, 5]]


def test_dates_more_than_30_days_apart_split():
    assert runs([s(0, no=1, rec="1962-01-05"), s(1, no=2, rec="1962-01-20")]) == [[0, 1]]
    assert runs([s(0, no=1, rec="1962-01-05"), s(1, no=2, rec="1962-05-20")]) == [[0], [1]]
    assert runs([s(0, no=1, rec="1962"), s(1, no=2, rec="1962-05-20")]) == [[0, 1]]


def test_multi_disc_continuity():
    assert runs([s(0, no=1), s(1, no=2), s(2, no=3, disc=2), s(3, no=4, disc=2)]) == [[0, 1, 2, 3]]
    # numbering restarts on disc 2 under the same parent: a second performance, not a continuation
    assert runs([s(0, no=1), s(1, no=2), s(2, part="z1", no=1, disc=2)]) == [[0, 1], [2]]


def test_multi_work_track_is_never_grouped():
    res = group_album([s(0, no=1), s(1, no=2, multi=True), s(2, no=3)])
    assert [r.slots for r in res.runs] == [[0], [2]]
    assert any(n[0] == "track contains several works" for n in res.notes)


def test_non_adjacent_parts_listed_for_review():
    res = group_album([s(0, no=1), s(1, "X"), s(2, no=2)])
    assert [r.slots for r in res.runs] == [[0], [1], [2]]
    assert any(n[0] == "non-adjacent parts of one work" for n in res.notes)


def test_ambiguous_album_not_grouped():
    """No order, no joins: each identified track stands alone (still listed for review)."""
    res = group_album([s(0, no=1), s(1, no=2), s(2, None)], ambiguous=True)
    assert [r.slots for r in res.runs] == [[0], [1]] and res.notes[0][0] == "ambiguous album positions"


def test_ambiguous_album_keeps_the_album_work_together_in_act_order():
    """FIDELIO / FURTWANGLER: two CUE discs, both disc 1, so positions repeat. The album's own work is one
    recording, ordered by act; another work on the album stays a recording of its own."""
    def c(i, section=None):
        return replace(s(i), continued=True, section=section)

    a = [c(0), c(1, 2), c(2, 1), s(3, "X"), c(4, 1)]
    res = group_album(a, ambiguous=True)
    assert [r.slots for r in res.runs] == [[0, 2, 4, 1], [3]]


def test_complete_performances_of_whole_work_split():
    # part_key == work_key marks a track that performs the whole work (not partial)
    assert runs([s(0, part="W"), s(1, part="W")]) == [[0], [1]]
    assert runs([s(0, part="W"), s(1, no=1), s(2, no=2)]) == [[0], [1, 2]]
    assert runs([s(0, partial=True), s(1, partial=True)]) == [[0, 1]]   # MB partial links still join


def test_no_movement_evidence_never_joins():
    # adjacent tracks tagged only with the work (no movement name or number): maybe two complete takes
    res = group_album([s(0), s(1)])
    assert [r.slots for r in res.runs] == [[0], [1]]
    assert any(n[0] == "split: no movement evidence" for n in res.notes)
    assert runs([s(0, no=1), s(1)]) == [[0], [1]]
    assert runs([s(0), s(1, no=1), s(2, no=2)]) == [[0], [1, 2]]


def test_ambiguous_album_work_respects_separate_constraints_and_contradictions():
    """No order is no licence to join what a user kept apart or another conductor performed."""
    def c(i, **kw):
        return replace(s(i, **kw), continued=True)

    a = [c(0, cond=("Furtwängler",)), replace(c(1), separate_from=frozenset({"o0"})), c(2, cond=("Böhm",)),
         c(3)]
    res = group_album(a, ambiguous=True)
    assert [r.slots for r in res.runs] == [[0, 3], [1], [2]]
    assert sum(n[0] == "split: contradicts the album's own work" for n in res.notes) == 2


def test_one_number_split_over_adjacent_partial_tracks_is_one_recording():
    # MB: 'Nr. 6 Finale' performed partially by three consecutive tracks (Dorati's Holländer, disc 2)
    sl = [s(0, part="n5", partial=True), s(1, part="n6", partial=True), s(2, part="n6", partial=True),
          s(3, part="n6", partial=True), s(4, part="n7", partial=True)]
    assert runs(sl) == [[0, 1, 2, 3, 4]]
    # a repeat that is not partial, or not adjacent, is still a second performance
    assert runs([s(0, part="n6"), s(1, part="n6")]) == [[0], [1]]
    assert runs([s(0, part="n6", partial=True), s(1, part="n7", partial=True),
                 s(2, part="n6", partial=True)]) == [[0, 1], [2]]
