from classical_core.grouping import Slot, group_album


def s(i, work="W", part=None, no=None, parent=None, disc=1, cond=(), orch=(), rec=None, multi=False):
    return Slot(f"o{i}", disc, i, work, parent or work, part or (f"p{no}" if no else None), no,
                set(cond), set(orch), set(), rec, multi)


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
    res = group_album([s(0, no=1), s(1, no=2)], ambiguous=True)
    assert res.runs == [] and res.notes[0][0] == "ambiguous album positions"
