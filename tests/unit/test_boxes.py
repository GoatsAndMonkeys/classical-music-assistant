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
    assert {w["title"] for w in comp["works"]} == {"Leonore", "Fidelio, Op. 72", "Symphony No. 3 in E-flat major, Op. 55"}
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


# ---- adversarial review (2026-10-27) ----------------------------------------------------------------------

BLOMSTEDT = [Person("Herbert Blomstedt", "conductor", "person"), Person("Staatskapelle Dresden", "orchestra", "group")]


def leonore_disc(idx, r, n, movements, performers=BLOMSTEDT, total=None, tag_disc=None, tag_total=None, work="Leonore",
                 title="Complete Works"):
    rows = [(f"{work}: No. {m}", {"composers": LVB, "work": work, "movement_name": f"No. {m}", "movement_no": m,
                                  "movement_total": total, "performers": list(performers)}) for m in movements]
    return album(idx, r, f"{title} CD {n:03d} - {work} Part {n}", rows, "Various Artists", 2008, tag_disc, tag_total)


def continuation_items(idx):
    return idx._q("SELECT album_library_id, detail_json FROM review_item WHERE reason=?", (discs.CONTINUATION,))


def test_a_lone_disc_of_a_box_is_not_a_box(tmp_path):
    """'Bach Edition [CD 12 of 155]' with the other 154 discs missing: one album with one disc is no box, and
    above all not a one-album box whose disc numbers come from its tracks."""
    idx, r = new_index(tmp_path)
    album(idx, r, "Bach Edition [CD 12 of 155]",
          [row for w in ("Cantata BWV 1", "Cantata BWV 2", "Cantata BWV 3", "Cantata BWV 4")
           for row in work_rows(w, ["Johann Sebastian Bach"])], "Various Artists", 2000, None)
    assert boxes.resolve(idx.con) == []


def test_box_neighbours_follow_the_box_decision(tmp_path):
    """The continuation window opens only for a box boxes.resolve accepts: not after 'not a box set', not
    for a group whose disc numbers repeat."""
    idx, r = new_index(tmp_path)
    d61 = box_disc(idx, r, 61, "Leonore")
    d62 = box_disc(idx, r, 62, "Leonore")
    [b] = boxes.resolve(idx.con)
    assert d62.library_album_id in {m["album_library_id"] for m in discs.members(idx, [d61.library_album_id])}
    triage.apply(idx, [{"op": "box_decide", "box_id": b.id, "decision": "not_box"}], "seth")
    assert d62.library_album_id not in {m["album_library_id"] for m in discs.members(idx, [d61.library_album_id])}
    # a repeated disc number in one title + artist: no box, so no neighbours
    idx2, r2 = new_index(tmp_path / "repeat")
    a = box_disc(idx2, r2, 12, "Mass in B minor", title="Great Recordings", composers=["Johann Sebastian Bach"])
    box_disc(idx2, r2, 12, "Mass in B minor", title="Great Recordings", composers=["Johann Sebastian Bach"])
    c = box_disc(idx2, r2, 13, "Mass in B minor", title="Great Recordings", composers=["Johann Sebastian Bach"])
    assert boxes.resolve(idx2.con) == []
    assert c.library_album_id not in {m["album_library_id"] for m in discs.members(idx2, [a.library_album_id])}


def test_box_discs_never_join_other_performances(tmp_path):
    """Neighbouring box discs share album artist, label, year and title, so those rules cannot tell them apart:
    another conductor, or movements that start again, is still never joined (nor a review item)."""
    idx, r = new_index(tmp_path)
    leonore_disc(idx, r, 61, [1, 2], total=4)
    leonore_disc(idx, r, 62, [3, 4], total=4, performers=[Person("Otto Klemperer", "conductor", "person"),
                                                         Person("Philharmonia Orchestra", "orchestra", "group")])
    leonore_disc(idx, r, 63, [1, 2], total=4)
    leonore_disc(idx, r, 64, [3, 4], total=4, title="Complete Works")
    assert len(boxes.resolve(idx.con)) == 1
    joined = idx._q("SELECT id, album_library_id, joined_to FROM recording WHERE joined_to IS NOT NULL")
    # 61 -> 62: other performers; 62 -> 63: the movements start again; 63 -> 64: the one true continuation
    names = {a: n for a, n in idx._q("SELECT album_library_id, name FROM album_info")}
    assert [names[j["album_library_id"]][:20] for j in joined] == ["Complete Works CD 06"]
    assert "064" in names[joined[0]["album_library_id"]]
    assert not continuation_items(idx)


def test_box_disc_number_from_the_title_beats_a_tag_of_one(tmp_path):
    """Each box disc ripped as its own album often says DISCNUMBER 1/1: the title's 'CD 061' is its number."""
    idx, r = new_index(tmp_path)
    leonore_disc(idx, r, 61, [1, 2], total=4, tag_disc=1, tag_total=1)
    leonore_disc(idx, r, 62, [3, 4], total=4, tag_disc=1, tag_total=1)
    assert len(idx._q("SELECT 1 FROM recording WHERE joined_to IS NOT NULL")) == 1


def test_a_continuation_item_at_the_edge_of_the_window_survives(tmp_path):
    """CD 61 -> 62 is a review item (no movement numbers). Deriving CD 70 again judges 62..78: CD 62 is in that
    window, CD 61 is not, so the 61/62 item must stay."""
    idx, r = new_index(tmp_path)
    for n, parts, who in ((61, (1, 2), BLOMSTEDT), (62, (3, 4), [])):     # no performer on both discs: review
        rows = [(f"Leonore: Part {i}", {"composers": LVB, "work": "Leonore", "performers": who}) for i in parts]
        album(idx, r, f"Complete Works CD 0{n} - Leonore Part {n - 60}", rows, "Various Artists", 2008, None)
    d70 = box_disc(idx, r, 70, "Symphony No. 3")
    assert len(boxes.resolve(idx.con)) == 1
    before = continuation_items(idx)
    assert len(before) == 1
    asyncio.run(idx.index_album(d70, force=True))
    assert len(continuation_items(idx)) == 1


def test_a_box_cover_from_a_hidden_provider_is_not_shown(tmp_path):
    idx, r = new_index(tmp_path)
    box_disc(idx, r, 61, "Leonore")
    hidden = album(idx, r, "Complete Works CD 062 - Fidelio Part 1", work_rows("Fidelio, Op. 72", LVB),
                   "Various Artists", 2008, None, provider="qobuz--x")
    idx.con.execute("UPDATE album_info SET image_json=? WHERE album_library_id=?",
                    ('{"provider": "qobuz--x", "path": "secret.jpg"}', hidden.library_album_id))
    [b] = boxes.resolve(idx.con)
    assert views.box(idx, b.id, vis=["filesystem_local"])["box"]["image"] is None
    assert views.boxes(idx, vis=["filesystem_local"])["boxes"][0]["image"] is None
    assert views.box(idx, b.id)["box"]["image"]["path"] == "secret.jpg"
