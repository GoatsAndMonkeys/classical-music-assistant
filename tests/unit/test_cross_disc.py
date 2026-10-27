"""One recording across the separate MA albums of a multi-disc set (classical_core/discs.py).

The fixtures copy how such sets arrive: one MA album per disc, the disc in the album name and in DISCNUMBER,
the work and movements in the tags.
"""

import asyncio

from classical_core import accolades as A
from classical_core import browse, discs, views
from classical_core.index import AlbumIn, MappingIn, TrackIn
from classical_core.tags import Person, TagInfo
from classical_core.titles import disc_marker
from tests.unit.test_accolades import entry
from tests.unit.test_grouping_real import new_index

MAHLER = ["Gustav Mahler"]
SOLTI = [Person("Georg Solti", "conductor", "person"), Person("Chicago Symphony Orchestra", "orchestra", "group"),
         Person("Isobel Buchanan", "soprano", "person"), Person("Mira Zakai", "alto", "person")]
M2 = "Symphony No. 2 in C minor (Resurrection)"
M2_MOVEMENTS = ["Allegro maestoso", "Andante moderato", "In ruhig fließender Bewegung", "Urlicht",
                "Im Tempo des Scherzos. Wild herausfahrend"]

_n = 0


def album(idx, r, name, rows, artist, year, disc, total=None, aid=None, positions=None, provider="filesystem_local"):
    """rows: (title, TagInfo kwargs). Every track carries DISCNUMBER `disc` (and TOTALDISCS `total`)."""
    global _n
    _n += 1
    aid = aid or f"X{_n}"
    tracks = []
    for i, (title, tg) in enumerate(rows):
        path = f"{aid}/{i}"
        tracks.append(TrackIn(f"{aid}-{i}", disc, positions[i] if positions else i + 1, title, artist, 600,
                              mappings=[MappingIn(provider, path, fingerprint="f", path=path)]))
        r.tags[path] = TagInfo(**{"title": title, "disc": disc, "disc_total": total, **tg})
    a = AlbumIn(aid, name, artist, year, None, tracks)
    asyncio.run(idx.index_album(a))
    return a


def mahler_disc(idx, r, n, performers=SOLTI, year=1981, name=None, artist="Sir Georg Solti", movements=None,
                aid=None, provider="filesystem_local"):
    movements = movements or ([1, 2, 3] if n == 1 else [4, 5])
    rows = [(f"Symphony No. 2: {['I', 'II', 'III', 'IV', 'V'][m - 1]}. {M2_MOVEMENTS[m - 1]}",
             {"composers": MAHLER, "work": M2, "movement_name": M2_MOVEMENTS[m - 1], "movement_no": m,
              "movement_total": 5, "performers": performers}) for m in movements]
    return album(idx, r, name or f"Mahler: Symphony No. 2 \"Resurrection\" [Disc {n}]", rows, artist, year, n, 2,
                 aid=aid, provider=provider)


def visible(idx, title="Symphony No. 2"):
    ws = [w for w in idx.works() if title.lower() in w["title"].lower()]
    return [rec for w in ws for rec in idx.recordings(w["id"])]


def test_disc_markers_in_album_names():
    assert disc_marker("Mahler: Symphony No. 2 [Disc 1 of 2]") == ("Mahler: Symphony No. 2", 1, 2)
    assert disc_marker("Wagner: Tannhäuser CD2") == ("Wagner: Tannhäuser", 2, None)
    assert disc_marker("Tosca (2)") == ("Tosca", 2, None)
    assert disc_marker("Fidelio (Klemperer, disc 3/3)") == ("Fidelio (Klemperer)", 3, 3)
    for none in ("Mahler: Symphony No. 2", "Götterdämmerung, Act 1", "Disc 2"):
        assert disc_marker(none)[1] is None
    ev = discs.album_evidence("Tosca [Disc 2]", "Karajan", [1, 1], [None, None], [1, 1], [None, None])
    assert ev["disc_no"] == 2          # a tagger's default '1' never beats the title
    assert discs.album_evidence("Tosca [Disc 2]", "Karajan", [3, 3], [], [], [])["disc_no"] is None


def test_mahler_2_on_two_discs_is_one_recording_in_disc_order(tmp_path):
    idx, r = new_index(tmp_path)
    d1 = mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2)
    recs = visible(idx)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["tracks"] == 5 and rec["complete"] == 1
    assert rec["album_library_id"] == d1.library_album_id
    page = views.recording(idx, rec["id"])
    assert [m["movement_no"] for m in page["movements"]] == [1, 2, 3, 4, 5]
    assert [m["track_id"] for m in page["movements"]] == [t.library_track_id for t in d1.tracks + d2.tracks]
    assert [d["id"] for d in page["discs"]] == [d1.library_album_id, d2.library_album_id]
    # the later disc's own recording id still opens the whole recording
    follower = idx._one("SELECT id FROM recording WHERE album_library_id=?", (d2.library_album_id,))[0]
    assert views.recording(idx, follower)["id"] == rec["id"]
    assert idx.status()["recordings"] == 1
    # re-deriving either disc gives the same join, with the same ids
    asyncio.run(idx.index_album(d2, force=True))
    asyncio.run(idx.index_album(d1, force=True))
    again = visible(idx)
    assert [x["id"] for x in again] == [rec["id"]] and again[0]["tracks"] == 5
    assert [m["ord"] for m in views.recording(idx, rec["id"])["movements"]] == [0, 1, 2, 3, 4]


def test_play_whole_work_across_discs_keeps_disc_then_track_order(tmp_path):
    idx, r = new_index(tmp_path)
    d1 = mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2)
    rid = visible(idx)[0]["id"]
    order = [t["library_track_id"] for t in idx.recording_tracks(rid)]
    assert order == [t.library_track_id for t in d1.tracks] + [t.library_track_id for t in d2.tracks]


def test_a_disc_arriving_later_joins_without_a_full_reindex(tmp_path):
    idx, r = new_index(tmp_path)
    d2 = mahler_disc(idx, r, 2)
    assert len(visible(idx)) == 1 and visible(idx)[0]["tracks"] == 2
    d1 = mahler_disc(idx, r, 1)       # only the new album is indexed
    recs = visible(idx)
    assert len(recs) == 1 and recs[0]["tracks"] == 5 and recs[0]["album_library_id"] == d1.library_album_id
    # and leaves again when that disc is removed
    idx.remove_album(d1.library_album_id)
    recs = visible(idx)
    assert len(recs) == 1 and recs[0]["album_library_id"] == d2.library_album_id and recs[0]["tracks"] == 2


def test_an_opera_on_three_discs_plays_act_by_act(tmp_path):
    idx, r = new_index(tmp_path)
    cast = [Person("Herbert von Karajan", "conductor", "person"),
            Person("Berliner Philharmoniker", "orchestra", "group"),
            Person("Katia Ricciarelli", "soprano", "person"), Person("José Carreras", "tenor", "person")]
    acts = {1: [("I", "Ah! Finalmente!"), ("I", "Dammi i colori!"), ("I", "Recondita armonia"),
                ("I", "Te Deum")],
            2: [("II", "Tosca è un buon falco!"), ("II", "Ha più forte"), ("II", "Vissi d'arte")],
            3: [("II", "E qual via scegliete?"), ("III", "Io de' sospiri"), ("III", "E lucevan le stelle"),
                ("III", "O dolci mani")]}
    albums = []
    for n in (3, 1, 2):        # ripped and added out of order
        rows = [(f"Tosca: Act {act}: {name}", {"composers": ["Giacomo Puccini"], "work": "Tosca",
                                               "movement_name": f"Act {act}: {name}", "performers": cast})
                for act, name in acts[n]]
        albums.append(album(idx, r, f"Puccini: Tosca (CD{n})", rows, "Herbert von Karajan", 1980, n, 3))
    recs = visible(idx, "Tosca")
    assert len(recs) == 1, [(x["album"], x["tracks"]) for x in recs]
    rec = recs[0]
    assert rec["tracks"] == 11 and rec["album"] == "Puccini: Tosca (CD1)"
    names = [m["name"] for m in views.recording(idx, rec["id"])["movements"]]
    assert names[0].endswith("Ah! Finalmente!") and names[4].endswith("Tosca è un buon falco!")
    assert names[-1].endswith("O dolci mani")
    assert [d["name"] for d in idx.recording_discs(rec["id"])] == [f"Puccini: Tosca (CD{n})" for n in (1, 2, 3)]


def test_a_duplicate_disc_never_joins(tmp_path):
    """Disc 1 ripped twice (two MA albums of one name), and a 'Disc 2' that holds disc 1's movements again:
    the same work and performers, but the movements repeat, so never one recording and nothing to review."""
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    mahler_disc(idx, r, 1)
    mahler_disc(idx, r, 2, movements=[1, 2, 3])
    rows = discs.members(idx, idx.album_ids())
    assert len(rows) == 3 and len({x["set_key"] for x in rows}) == 1
    one = [discs._disc(idx, x) for x in rows]
    assert len({d.last.work_id for d in one}) == 1          # it is the movement rule that keeps them apart
    by_no = {d.disc_no: d for d in one}
    assert discs.judge(by_no[1], by_no[2])[1].startswith("the same movements on both discs")
    assert sorted(x["tracks"] for x in visible(idx)) == [3, 3, 3]
    assert not idx._q("SELECT 1 FROM recording WHERE joined_to IS NOT NULL")
    assert not idx._q("SELECT 1 FROM review_item WHERE reason=?", (discs.CONTINUATION,))


def test_two_recordings_by_the_same_conductor_never_join(tmp_path):
    idx, r = new_index(tmp_path)
    nyp = [Person("Leonard Bernstein", "conductor", "person"), Person("New York Philharmonic", "orchestra", "group")]
    lso = [Person("Leonard Bernstein", "conductor", "person"), Person("London Symphony Orchestra", "orchestra", "group")]
    # the same album artist and name; another orchestra on 'disc 2'
    mahler_disc(idx, r, 1, performers=nyp, year=1963, artist="Leonard Bernstein")
    mahler_disc(idx, r, 2, performers=lso, year=1963, artist="Leonard Bernstein")
    # and the 1987 remake with the same orchestra: another year
    mahler_disc(idx, r, 2, performers=nyp, year=1987, artist="Leonard Bernstein")
    rows = {(x["disc_no"], x["year"]): discs._disc(idx, x) for x in discs.members(idx, idx.album_ids())}
    assert discs.judge(rows[(1, 1963)], rows[(2, 1963)])[1] == "different performers"
    assert discs.judge(rows[(1, 1963)], rows[(2, 1987)])[0] != "join"
    assert sorted(x["tracks"] for x in visible(idx)) == [2, 2, 3]
    assert not idx._q("SELECT 1 FROM recording WHERE joined_to IS NOT NULL")


def test_uncertain_continuation_goes_to_review_and_a_user_decides(tmp_path):
    """Disc 2 has no performer credits and no track artist: the continuation is a guess, so it is queued."""
    idx, r = new_index(tmp_path)
    d1 = mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2, performers=[], artist="Sir Georg Solti")
    for t in d2.tracks:
        t.artist = "Various Artists"
    asyncio.run(idx.index_album(d2, force=True))
    assert len(visible(idx)) == 2
    group = next(g for g in views.review(idx)["groups"] if g["reason"] == discs.CONTINUATION)
    item = group["items"][0]
    assert item["detail"]["earlier_album"] == d1.name and "performer" in item["detail"]["why"]
    act = next(a for a in item["actions"] if a["decision"] == "together")
    idx.join_discs(act["first_recording_id"], act["second_recording_id"], "seth", "together")
    recs = visible(idx)
    assert len(recs) == 1 and recs[0]["tracks"] == 5
    assert not idx._q("SELECT 1 FROM review_item WHERE reason=?", (discs.CONTINUATION,))
    # the decision survives re-deriving both discs
    asyncio.run(idx.index_album(d1, force=True))
    asyncio.run(idx.index_album(d2, force=True))
    assert [x["tracks"] for x in visible(idx)] == [5]


def test_keep_discs_separate_survives_reindex(tmp_path):
    idx, r = new_index(tmp_path)
    d1 = mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2)
    rid = visible(idx)[0]["id"]
    idx.separate_discs(rid, "seth")
    assert sorted(x["tracks"] for x in visible(idx)) == [2, 3]
    assert not idx._q("SELECT 1 FROM review_item WHERE reason=?", (discs.CONTINUATION,))
    asyncio.run(idx.index_album(d2, force=True))
    asyncio.run(idx.index_album(d1, force=True))
    assert sorted(x["tracks"] for x in visible(idx)) == [2, 3]
    # a later 'together' wins over the earlier 'separate'
    f = idx._one("SELECT id FROM recording WHERE album_library_id=?", (d2.library_album_id,))[0]
    idx.join_discs(rid, f, "seth")
    assert [x["tracks"] for x in visible(idx)] == [5]
    log = [x[0] for x in idx._q("SELECT op FROM correction_log ORDER BY seq")]
    assert log == ["discs_separate", "discs_together"]


def test_the_award_shows_once_on_the_joined_recording(tmp_path, monkeypatch):
    """Solti's Chicago Mahler 2 (Grammy 1982, as the bundled dataset has it) on two MA albums: one badge."""
    monkeypatch.setattr(A, "entries", lambda: (entry(
        "solti-m2", "Grammy", 1982, composer="Mahler", work="Symphony No. 2 in C minor",
        performers=("Georg Solti", "Chicago Symphony Orchestra"), category="Best Orchestral Performance"),))
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 2)
    A.refresh(idx.con)          # the second disc alone matches too ...
    assert idx._q("SELECT 1 FROM accolade WHERE source='dataset' AND grade<>'uncertain'")
    mahler_disc(idx, r, 1)
    A.refresh(idx.con)          # ... but once joined there is one recording to show it on
    rec = visible(idx)[0]
    awards = views.recording(idx, rec["id"])["awards"]
    assert [a["text"] for a in awards] == ["Grammy winner 1982"]
    rows = idx._q("SELECT recording_id FROM accolade WHERE source='dataset'")
    assert [x[0] for x in rows] == [rec["id"]]


def test_two_copies_of_a_set_are_never_cross_joined_by_a_guess(tmp_path):
    """Both copies of a two-disc set as four MA albums: each disc 1 could continue on either disc 2. Whatever
    the album ids, nothing is joined by the evidence; once a user joins one pair, the other pair is clear."""
    for ids in (("A1", "A2", "B1", "B2"), ("A1", "B4", "B2", "A3")):    # the second order once paired A1 with B
        idx, r = new_index(tmp_path / "".join(ids))
        a1 = mahler_disc(idx, r, 1, aid=ids[0])
        a2 = mahler_disc(idx, r, 2, aid=ids[1])
        mahler_disc(idx, r, 1, aid=ids[2])
        mahler_disc(idx, r, 2, aid=ids[3])
        assert sorted(x["tracks"] for x in visible(idx)) == [2, 2, 3, 3]
        items = idx._q("SELECT detail_json FROM review_item WHERE reason=?", (discs.CONTINUATION,))
        assert len(items) == 4 and all("more than one disc" in x[0] for x in items)
        first = idx._one("SELECT id FROM recording WHERE album_library_id=?", (a1.library_album_id,))[0]
        second = idx._one("SELECT id FROM recording WHERE album_library_id=?", (a2.library_album_id,))[0]
        idx.join_discs(first, second, "seth")
        assert sorted(x["tracks"] for x in visible(idx)) == [5, 5]
        groups = sorted(sorted(d["id"] for d in idx.recording_discs(x["id"])) for x in visible(idx))
        assert groups == [sorted(ids[:2]), sorted(ids[2:])]
        assert not idx._q("SELECT 1 FROM review_item WHERE reason=?", (discs.CONTINUATION,))


def test_discs_on_different_sources_are_queued_not_joined(tmp_path):
    """Disc 1 local, disc 2 only on a streaming service: joined, a user who sees only the local files would
    lose disc 1 too. So the evidence alone never joins them; a user can."""
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    mahler_disc(idx, r, 2, provider="qobuz--abc")
    assert sorted(x["tracks"] for x in visible(idx)) == [2, 3]
    item = idx._one("SELECT detail_json FROM review_item WHERE reason=?", (discs.CONTINUATION,))
    assert "different sources" in item[0]


def test_a_later_discs_browse_path_still_plays_the_joined_recording(tmp_path):
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2)
    rec = visible(idx)[0]
    follower = idx._one("SELECT id FROM recording WHERE album_library_id=?", (d2.library_album_id,))[0]
    assert follower != rec["id"]
    nodes = browse.browse(idx, browse.recording_item_id(rec["work_id"], follower))
    assert len(nodes) == 5
    assert idx.recording(follower)["id"] == rec["id"]


def test_an_entry_on_the_recording_and_on_a_later_discs_album_shows_once(tmp_path):
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2)
    rec = visible(idx)[0]
    for i, (target, rid, aid) in enumerate((("recording", rec["id"], rec["album_library_id"]),
                                            ("album", None, d2.library_album_id))):
        idx.con.execute(
            "INSERT INTO accolade(id, target, recording_id, album_library_id, kind, award, year, result, source, "
            "source_ref, grade, fetched_at) VALUES (?,?,?,?,'award','Grammy',1982,'winner','wikidata','Q1/P166/x',"
            "'this_release',0)", (f"a{i}", target, rid, aid))
    assert len(views.recording(idx, rec["id"])["awards"]) == 1


def test_a_reissue_year_on_one_disc_is_queued_not_rejected(tmp_path):
    """Real tags: Böhm's Fidelio disc 1 says 2005 (the reissue), disc 2 says 1969. Same artists, discs follow on:
    a person should look, never a silent miss and never a guessed join."""
    from dataclasses import replace
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    mahler_disc(idx, r, 2)
    by_no = {x["disc_no"]: discs._disc(idx, x) for x in discs.members(idx, idx.album_ids())}
    assert discs.judge(by_no[1], by_no[2])[0] == "join"
    verdict, why = discs.judge(replace(by_no[1], year=2005), by_no[2])
    assert verdict == "review" and "different album years" in why


def test_uncredited_discs_by_other_track_artists_are_not_queued(tmp_path):
    """No conductor/orchestra/soloist credits on either disc and nothing in common among the track artists:
    another performance, not a continuation to ask about."""
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1, performers=[], artist="Eugen Jochum")
    d2 = mahler_disc(idx, r, 2, performers=[], artist="Eugen Jochum")
    for t in d2.tracks:
        t.artist = "Rafael Kubelik"                  # the album artist agrees; the track artists do not
    asyncio.run(idx.index_album(d2, force=True))
    by_no = {x["disc_no"]: discs._disc(idx, x) for x in discs.members(idx, idx.album_ids())}
    assert discs.judge(by_no[1], by_no[2]) == (None, "different performers")
    assert len(visible(idx)) == 2
    assert not idx._q("SELECT 1 FROM review_item WHERE reason=?", (discs.CONTINUATION,))
