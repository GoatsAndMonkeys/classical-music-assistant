"""Opera and live-broadcast albums (third review, 2026-10-26): acts as tracks, many tracks per act,
intermission and announcer tracks between acts, the radio pipeline's tagging (~/sethster tag_sandbox)."""

import asyncio

from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.store import connect, migrate
from classical_core.tags import Person, TagInfo
from classical_core.titles import parse_act

WAGNER = "Richard Wagner"
TRISTAN = "Tristan und Isolde"


class Reader:
    def __init__(self):
        self.tags: dict[str, TagInfo] = {}

    def __call__(self, path):
        return self.tags[path]


def broadcast(tmp_path, rows, artist="Yannick Nézet-Séguin", disc_of=None):
    """rows: (title, work, movement_name, movement_no) per track; work None = a talk track."""
    con = connect(tmp_path / "index.db")
    migrate(con)
    r = Reader()
    idx = Index(con, None, tag_reader=r)
    tracks = []
    for i, (title, work, mov, no) in enumerate(rows):
        path = f"A/{i}"
        disc = disc_of(i) if disc_of else 1
        tracks.append(TrackIn(str(i), disc, i + 1, title, artist, 1800, mappings=[MappingIn("filesystem_local", path,
                                                                                             "f", path)]))
        r.tags[path] = TagInfo(
            title=title, artist=artist, albumartist=artist, work=work, movement_name=mov, movement_no=no,
            composers=[WAGNER] if work else [],
            performers=[Person(artist, "conductor", "person")] if work else [Person("Debbie Price", "announcer",
                                                                                     "person")])
    asyncio.run(idx.index_album(AlbumIn("A", "Met Opera 2026-10-24", artist, 2026, None, tracks)))
    return idx


def shape(idx):
    return sorted((w["title"], sorted(r["tracks"] for r in idx.recordings(w["id"]))) for w in idx.works())


def reasons(idx):
    return {v["reason"] for v in idx.review()}


def test_parse_act():
    assert parse_act("Act I") == (1, "")
    assert parse_act("Act II: Scene 3") == (2, "Scene 3")
    assert parse_act("Akt 3, Szene 1") == (3, "Szene 1")
    assert parse_act("Prologue") == (0, "")
    assert parse_act("Erster Aufzug") == (1, "")
    assert parse_act("Overture") == (None, "Overture")
    assert parse_act("Actor's entrance") == (None, "Actor's entrance")


def test_one_track_per_act_is_one_recording(tmp_path):
    idx = broadcast(tmp_path, [(f"{TRISTAN}: Act {n}", TRISTAN, None, None) for n in ("I", "II", "III")])
    assert shape(idx) == [(TRISTAN, [3])]


def test_intermissions_between_acts_do_not_split_the_performance(tmp_path):
    rows = [("Introduction", None, None, None), (f"{TRISTAN}: Act I", TRISTAN, None, None),
            ("Intermission: Interview with Lise Davidsen", None, None, None),
            (f"{TRISTAN}: Act II", TRISTAN, None, None), ("Toll Brothers–Metropolitan Opera Quiz", None, None, None),
            (f"{TRISTAN}: Act III", TRISTAN, None, None)]
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [(TRISTAN, [3])]
    assert "non-adjacent parts of one work" not in reasons(idx)


def test_many_tracks_per_act_with_numbers_restarting_each_act(tmp_path):
    # Picard-style: movement numbers count within each act
    rows = [(f"{TRISTAN}: Act I: {m}", TRISTAN, f"Act I: {m}", n) for n, m in ((1, "Prelude"), (2, "Scene 1"))]
    rows += [("Intermission", None, None, None)]
    rows += [(f"{TRISTAN}: Act II: {m}", TRISTAN, f"Act II: {m}", n) for n, m in ((1, "Scene 1"), (2, "Scene 2"))]
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [(TRISTAN, [4])]


def test_repeated_movement_name_in_different_acts_is_not_a_retake(tmp_path):
    rows = [(f"{TRISTAN}: Act {a}: Scene 1", TRISTAN, "Scene 1", None) for a in ("I", "II")]
    # the act lives only in the title; the movement tag alone repeats
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [(TRISTAN, [2])]


def test_same_act_twice_is_still_split(tmp_path):
    rows = [(f"{TRISTAN}: Act I", TRISTAN, None, None), (f"{TRISTAN}: Act I", TRISTAN, None, None)]
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [(TRISTAN, [1, 1])]


def test_acts_out_of_order_split(tmp_path):
    rows = [(f"{TRISTAN}: Act II", TRISTAN, None, None), (f"{TRISTAN}: Act I", TRISTAN, None, None)]
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [(TRISTAN, [1, 1])]


def test_talk_track_never_becomes_a_work_even_with_a_composer_credit(tmp_path):
    # Bernstein conducts and is interviewed: his name in the credit must not make the interview a work
    rows = [("Intermission: Interview with Leonard Bernstein", None, None, None),
            ("Commentary: Bernstein on Mahler, Symphony No. 9", None, None, None)]
    idx = broadcast(tmp_path, rows, artist="Leonard Bernstein")
    assert idx.works() == []


def test_talk_bridge_needs_evidence_on_both_sides(tmp_path):
    # two whole-work takes with an announcer between them stay two recordings
    rows = [("Siegfried Idyll", "Siegfried Idyll", None, None), ("Announcer", None, None, None),
            ("Siegfried Idyll", "Siegfried Idyll", None, None)]
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [("Siegfried Idyll", [1, 1])]


def test_non_talk_gap_still_breaks_a_run(tmp_path):
    rows = [(f"{TRISTAN}: Act I", TRISTAN, None, None), ("Dancing Queen", None, None, None),
            (f"{TRISTAN}: Act II", TRISTAN, None, None)]
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [(TRISTAN, [1, 1])]


def test_whole_acts_on_separate_discs_continue(tmp_path):
    rows = [(f"{TRISTAN}: Act {n}", TRISTAN, None, None) for n in ("I", "II", "III")]
    idx = broadcast(tmp_path, rows, disc_of=lambda i: i + 1)
    assert shape(idx) == [(TRISTAN, [3])]


def test_whole_act_in_the_movement_tag(tmp_path):
    rows = [(f"{TRISTAN}: {m}", TRISTAN, m, None) for m in ("Act I", "Act II", "Act I")]
    idx = broadcast(tmp_path, rows)
    assert shape(idx) == [(TRISTAN, [1, 2])]   # the second "Act I" is a new performance


def test_mb_grouping_boundary_spans_an_intermission(tmp_path):
    from classical_core.store import SqliteMBCache
    from tests.mbfake import FakeMB, uid

    fake = FakeMB()
    fake.work(uid(1), TRISTAN, None)
    for n in (1, 2):
        fake.work(uid(1 + n), f"{TRISTAN}: Act {'I' * n}", None, uid(1), n)
        fake.recording(uid(100 + n), f"Act {'I' * n}", uid(1 + n))
    con = connect(tmp_path / "index.db")
    migrate(con)
    idx = Index(con, fake.client(SqliteMBCache(con)))
    tracks = [TrackIn(str(i), 1, i + 1, t, "", 600, mbid=m, mappings=[MappingIn("filesystem_local", f"T/{i}", "f")])
              for i, (t, m) in enumerate([("Act I", uid(101)), ("Intermission", None), ("Act II", uid(102))])]
    asyncio.run(idx.index_album(AlbumIn("T", "T", "", 2000, None, tracks)))
    assert shape(idx) == [(TRISTAN, [2])]
