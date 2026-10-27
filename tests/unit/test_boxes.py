"""Box sets (classical_core/boxes.py): one MA album holding many discs and works, or many MA albums titled
'Complete Works CD 061 - Leonore Part 1' (Brilliant's Beethoven box on the M1, ripped one album per disc).

A box is decided at read time from the evidence each album stores when it is derived, so a removal, an import
in progress and a user's 'not a box set' all apply without re-indexing the other discs."""

import asyncio

import pytest

from classical_core import accolades as A
from classical_core import boxes, discs, triage, views
from classical_core.index import AlbumIn, MappingIn, TrackIn
from classical_core.tags import Person, TagInfo
from tests.unit.test_accolades import entry
from tests.unit.test_cross_disc import album, mahler_disc
from tests.unit.test_grouping_real import new_index

LVB = ["Ludwig van Beethoven"]
KLEMPERER = [Person("Otto Klemperer", "conductor", "person"), Person("Philharmonia Orchestra", "orchestra", "group"),
             Person("Christa Ludwig", "soprano", "person")]


def work_rows(work, composers=LVB, n=2, performers=(), recorded=None):
    return [(f"{work}: {i + 1}. Part {i + 1}", {"composers": composers, "work": work, "movement_name": f"Part {i + 1}",
                                                "movement_no": i + 1, "movement_total": n,
                                                "performers": list(performers), "recorded": recorded})
            for i in range(n)]


def box_disc(idx, r, n, work, title="Complete Works", composers=LVB, artist="Various Artists", year=2008,
             track_artist=None, **kw):
    name, rows = f"{title} CD {n:03d} - {work} Part 1", work_rows(work, composers, **kw)
    if track_artist is None:
        return album(idx, r, name, rows, artist, year, None)
    a = album(idx, r, name, rows, track_artist, year, None)       # the tracks' own artist, then the box's
    a = AlbumIn(a.library_album_id, name, artist, year, None, a.tracks)
    asyncio.run(idx.index_album(a, force=True))
    return a


def one_album_box(idx, r, works, aid="BIG", name="Bach Edition", artist="Various Artists"):
    """One MA album, one work (two movements) per disc."""
    tracks = []
    for d, w in enumerate(works, 1):
        for i, (title, tg) in enumerate(work_rows(w, ["Johann Sebastian Bach"])):
            path = f"{aid}/{d}/{i}"
            tracks.append(TrackIn(f"{aid}-{d}-{i}", d, i + 1, title, artist, 600,
                                  mappings=[MappingIn("filesystem_local", path, fingerprint="f", path=path)]))
            r.tags[path] = TagInfo(**{"title": title, "disc": d, "disc_total": len(works), **tg})
    a = AlbumIn(aid, name, artist, 2010, None, tracks)
    asyncio.run(idx.index_album(a))
    return a


def test_box_titles_parse():
    assert boxes.parse_title("Complete Works CD 061 - Leonore Part 1 (Blomstedt)") == \
        ("Complete Works", 61, None, "Leonore Part 1 (Blomstedt)")
    assert boxes.parse_title("Bach Edition [CD 12 of 155]") == ("Bach Edition", 12, 155, None)
    for none in ("CD 5", "Toscanini Collection, Volume 54", "Fidelio (2)",
                 "Various: Karajan The Complete EMI Recordings 1946-1984; Karajan/PO/VPO"):
        assert boxes.parse_title(none) is None, none


def test_albums_of_one_box_are_one_box_with_their_own_works(tmp_path):
    idx, r = new_index(tmp_path)
    d61 = box_disc(idx, r, 61, "Leonore")
    d63 = box_disc(idx, r, 63, "Fidelio, Op. 72")
    d12 = box_disc(idx, r, 12, "Symphony No. 3", performers=KLEMPERER)
    [b] = boxes.resolve(idx.con)
    assert b.title == "Ludwig van Beethoven: Complete Works"      # generic title, one composer
    assert not b.single and [m["box_disc"] for m in b.members] == [12, 61, 63]
    page = views.box(idx, b.id)
    assert [d["disc"] for d in page["discs"]] == [12, 61, 63]
    assert [d["subtitle"] for d in page["discs"]][1] == "Leonore Part 1"
    assert page["box"]["works"] == 3 and page["box"]["composers"] == ["Ludwig van Beethoven"]
    [comp] = page["composers"]
    assert {w["title"] for w in comp["works"]} == {"Leonore", "Fidelio, Op. 72", "Symphony No. 3"}
    # each disc keeps its own credits: Klemperer is on CD 12 only, nothing smeared
    who = {d["disc"]: d["recordings"][0]["who"] for d in page["discs"]}
    assert who[12].startswith("Otto Klemperer") and "Klemperer" not in who[61] + who[63]
    rec = page["discs"][0]["recordings"][0]
    assert rec["play_uri"] and rec["disc"] == 12
    # the recording and work pages say where it sits
    assert views.recording(idx, rec["id"])["box"] == {"id": b.id, "title": b.title, "disc": 12, "single": False}
    assert views.work(idx, rec["work_id"])["recordings"][0]["box"]["disc"] == 12
    assert [x["id"] for x in views.boxes(idx)["boxes"]] == [b.id]
    # a disc removed: still a box of two; another removed: one album is no box
    idx.remove_album(d61.library_album_id)
    assert len(boxes.resolve(idx.con)[0].members) == 2
    idx.remove_album(d63.library_album_id)
    assert boxes.resolve(idx.con) == []
    assert "box" not in views.recording(idx, rec["id"])
    assert d12


def test_ordinary_sets_are_not_boxes(tmp_path):
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    mahler_disc(idx, r, 2)
    # a four-disc opera in one album: many discs, one work
    tracks = []
    for d in range(1, 5):
        for i in range(3):
            path = f"OP/{d}/{i}"
            title = f"Parsifal: Act {d} scene {i + 1}"
            tracks.append(TrackIn(f"op-{d}-{i}", d, i + 1, title, "Knappertsbusch", 900,
                                  mappings=[MappingIn("filesystem_local", path, fingerprint="f", path=path)]))
            r.tags[path] = TagInfo(title=title, composers=["Richard Wagner"], work="Parsifal", disc=d, disc_total=4)
    asyncio.run(idx.index_album(AlbumIn("OP", "Parsifal (Bayreuth 1956)", "Knappertsbusch", 1956, None, tracks)))
    assert boxes.resolve(idx.con) == []
    with pytest.raises(views.NotFound):
        views.box(idx, "x000000000000")
    assert views.boxes(idx) == {"boxes": []}


def test_one_album_holding_a_whole_box(tmp_path):
    idx, r = new_index(tmp_path)
    a = one_album_box(idx, r, ["Mass in B minor", "St Matthew Passion", "Christmas Oratorio", "Magnificat"])
    [b] = boxes.resolve(idx.con)
    assert b.single and b.title == "Bach Edition"
    page = views.box(idx, b.id)
    assert [d["disc"] for d in page["discs"]] == [1, 2, 3, 4]
    assert page["discs"][2]["recordings"][0]["title"] == "Christmas Oratorio"
    rec = page["discs"][2]["recordings"][0]
    assert views.recording(idx, rec["id"])["box"]["disc"] == 3
    # three works are a set, not a box
    idx2, r2 = new_index(tmp_path / "three")
    one_album_box(idx2, r2, ["Mass in B minor", "St Matthew Passion", "Christmas Oratorio"])
    assert boxes.resolve(idx2.con) == []
    assert a


def test_same_disc_number_twice_splits_by_composer(tmp_path):
    """Two boxes with the same title and album artist (a label's 'Great Recordings' series): the disc numbers
    collide, so each composer's discs are their own box; whatever still collides is no box at all."""
    idx, r = new_index(tmp_path)
    box_disc(idx, r, 12, "Mass in B minor", title="Great Recordings", composers=["Johann Sebastian Bach"])
    box_disc(idx, r, 13, "Magnificat", title="Great Recordings", composers=["Johann Sebastian Bach"])
    box_disc(idx, r, 12, "Requiem", title="Great Recordings", composers=["Wolfgang Amadeus Mozart"])
    [b] = boxes.resolve(idx.con)
    assert [m["box_disc"] for m in b.members] == [12, 13] and b.composers == ["Johann Sebastian Bach"]
    box_disc(idx, r, 13, "Magnificat", title="Great Recordings", composers=["Johann Sebastian Bach"])
    assert boxes.resolve(idx.con) == []


def test_not_a_box_set_is_a_correction_that_can_be_undone(tmp_path):
    idx, r = new_index(tmp_path)
    box_disc(idx, r, 61, "Leonore")
    box_disc(idx, r, 63, "Fidelio, Op. 72")
    [b] = boxes.resolve(idx.con)
    with pytest.raises(ValueError):
        triage.apply(idx, [{"op": "box_decide", "box_id": b.id, "decision": "is_box"}], "seth")
    out = triage.apply(idx, [{"op": "box_decide", "box_id": b.id, "decision": "not_box"}], "seth")
    assert out["label"] == "Not a box set" and out["jobs"] == ["accolades"]
    assert boxes.resolve(idx.con) == []
    with pytest.raises(views.NotFound):
        views.box(idx, b.id)
    # survives re-indexing: the override is on the box, not on an album
    box_disc(idx, r, 62, "Leonore")
    assert boxes.resolve(idx.con) == []
    assert triage.undo(idx, out["batch"], "seth")["jobs"] == ["accolades"]
    assert [len(x.members) for x in boxes.resolve(idx.con)] == [3]
    log = [x[0] for x in idx._q("SELECT op FROM correction_log ORDER BY seq")]
    assert log == ["box_not_box", "review_undo"]


def test_hidden_providers_hide_the_box(tmp_path):
    idx, r = new_index(tmp_path)
    box_disc(idx, r, 61, "Leonore")
    box_disc(idx, r, 63, "Fidelio, Op. 72")
    [b] = boxes.resolve(idx.con)
    assert views.box(idx, b.id, vis=["filesystem_local"])["box"]["recordings"] == 2
    with pytest.raises(views.NotFound):
        views.box(idx, b.id, vis=["qobuz--x"])
    assert views.boxes(idx, vis=["qobuz--x"]) == {"boxes": []}
    with pytest.raises(views.NotFound):
        triage.apply(idx, [{"op": "box_decide", "box_id": b.id, "decision": "not_box"}], "seth", vis=["qobuz--x"])


def test_neighbouring_discs_of_a_box_are_continuation_candidates(tmp_path):
    """Leonore on CD 61 and 62 of the Beethoven box: no set key ties the two albums (their names differ), but
    the box puts them side by side, so the second disc is weighed as the first one's continuation."""
    idx, r = new_index(tmp_path)
    d61 = box_disc(idx, r, 61, "Leonore")
    d62 = box_disc(idx, r, 62, "Leonore")
    far = box_disc(idx, r, 99, "Fidelio, Op. 72")
    near = {m["album_library_id"] for m in discs.members(idx, [d61.library_album_id])}
    assert d62.library_album_id in near and far.library_album_id not in near


def test_box_reissue_matches_its_award_without_smearing_the_album_artist(tmp_path, monkeypatch):
    """Klemperer's 1962 Fidelio (Grammy 1963) on a 2010 box disc whose album artist is another conductor: the
    disc's own credits match as another release; the album artist is never added to a credited recording."""
    monkeypatch.setattr(A, "entries", lambda: (entry(),))
    idx, r = new_index(tmp_path)
    box_disc(idx, r, 41, "Fidelio, Op. 72", title="EMI Opera Edition", artist="Herbert von Karajan", year=2010,
             track_artist="Otto Klemperer", performers=KLEMPERER, recorded="1962-02-10")
    box_disc(idx, r, 42, "Tosca", title="EMI Opera Edition", artist="Herbert von Karajan", year=2010,
             track_artist="Victor de Sabata", composers=["Giacomo Puccini"], performers=[Person("Victor de Sabata", "conductor", "person")])
    assert len(boxes.resolve(idx.con)) == 1
    facts = A._facts(idx.con)
    assert all(p.sur != "karajan" for f in facts.values() for p, _ in f.pool.persons)
    assert all("karajan" not in f.pool.loose for f in facts.values())
    A.refresh(idx.con)
    grades = [x[0] for x in idx._q("SELECT grade FROM accolade WHERE source='dataset'")]
    assert grades == ["other_release"]
