import asyncio
import json

import pytest

from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.store import SqliteMBCache, connect, migrate
from classical_core.tags import Person, TagInfo
from tests.helpers import fake_library, offline_mb


def run(coro):
    return asyncio.run(coro)


def new_index(tmp_path, mb=True, reader=None):
    con = connect(tmp_path / "index.db")
    migrate(con)
    kw = {"tag_reader": reader} if reader else {}
    return Index(con, offline_mb(cache=SqliteMBCache(con)) if mb else None, **kw)


@pytest.fixture
def full(tmp_path, library):
    idx = new_index(tmp_path)
    albums = fake_library(library)
    for a in albums:
        run(idx.index_album(a))
    return idx, albums


def by_title(idx, part):
    ws = [w for w in idx.works() if part in w["title"]]
    assert ws, part
    return ws


def test_library_end_to_end(full):
    idx, _ = full
    st = idx.status()
    assert st["albums"] == 10 and st["occurrences"] == 42 and st["mappings"] == {"ok": 42}
    names = [c["name"] for c in idx.composers()]
    assert "Ludwig van Beethoven" in names and "Antonín Dvořák" in names and "Johannes Brahms" in names
    assert not [w for w in idx.works() if "Dancing" in w["title"]]                 # pop track ignored


def test_tag_merge_rule_two_and_labels(full):
    idx, _ = full
    [w40] = by_title(idx, "Symphony No. 40")
    recs = idx.recordings(w40["id"])
    assert [r["label"] for r in recs] == ["Karl Böhm / Berliner Philharmoniker (1976) [12:00]",
                                          "Neville Marriner (rel. 2010) [12:00]"]
    [w5] = by_title(idx, "Symphony No. 5")
    assert {r["label"].split(" (")[0] for r in idx.recordings(w5["id"])} == {
        "Herbert von Karajan / Berliner Philharmoniker", "Carlos Kleiber / Wiener Philharmoniker"}


def test_movements_in_order_and_play_ranking(full):
    idx, _ = full
    [w5] = by_title(idx, "Symphony No. 5")
    rec, reason = idx.best_recording(w5["id"])
    tracks = idx.recording_tracks(rec["id"])
    assert [t["movement_no"] for t in tracks] == [1, 2, 3, 4] and rec["complete"] == 1
    assert reason in ("complete movement set", "most evidence")


def test_mb_only_album_grouped_by_parent_walk(full):
    idx, _ = full
    [w9] = [w for w in idx.works() if w["catalogue_display"] == "Op. 95"]
    assert w9["identity_tier"] == "mb" and w9["work_type"] == "Symphonies"
    [rec] = idx.recordings(w9["id"])
    assert rec["label"].startswith("István Kertész / Wiener Philharmoniker (1961)")
    assert [t["movement_name"] for t in idx.recording_tracks(rec["id"])] == [
        "Adagio – Allegro molto", "Largo", "Scherzo – Molto vivace", "Allegro con fuoco"]


def test_opera_two_discs_one_recording(full):
    idx, _ = full
    [fig] = by_title(idx, "Figaro")
    assert fig["work_type"] == "Opera & Stage"
    [rec] = idx.recordings(fig["id"])
    assert rec["tracks"] == 6 and rec["label"].endswith("(rel. 1968) [18:00]")


def test_alternate_take_split_and_reviewed(full):
    idx, _ = full
    [w] = by_title(idx, "Trumpet Concerto")
    assert sorted(r["tracks"] for r in idx.recordings(w["id"])) == [1, 3]
    assert any(v["reason"].startswith("split:") for v in idx.review())


def test_ids_stable_across_reindex(full):
    idx, albums = full
    before = {(w["id"], r["id"]) for w in idx.works() for r in idx.recordings(w["id"])}
    for a in albums:
        run(idx.index_album(a, force=True))
    after = {(w["id"], r["id"]) for w in idx.works() for r in idx.recordings(w["id"])}
    assert before == after


# ---- scenarios with a fake tag reader ----------------------------------------------------------------


class Reader:
    def __init__(self):
        self.tags: dict[str, TagInfo] = {}
        self.fail: set[str] = set()

    def __call__(self, path):
        if path in self.fail:
            raise OSError("unreadable")
        return self.tags[path]


def album(aid, titles, provider="filesystem_local", base=0, artist=""):
    return AlbumIn(aid, f"Album {aid}", artist, 2000, None, [
        TrackIn(str(base + i), 1, i + 1, t, artist, 100,
                mappings=[MappingIn(provider, f"{aid}/{i}", fingerprint="f1", path=f"{aid}/{i}")])
        for i, t in enumerate(titles)])


def tag(work=None, mov=None, no=None, composer="Frédéric Chopin", conductor=None, title=None, **kw):
    perf = [Person(conductor, "conductor", "person")] if conductor else []
    return TagInfo(title=title or (f"{work}: {mov}" if work else mov), work=work, movement_name=mov,
                   movement_no=no, composers=[composer] if composer else [], performers=perf, **kw)


def test_tier3_never_merges_across_albums(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    titles = ["Symphony No. 1 in C minor, Op. 68: I. Allegro", "Symphony No. 1 in C minor, Op. 68: II. Andante"]
    for aid, base in (("A", 0), ("B", 10)):
        a = album(aid, titles, base=base, artist="Johannes Brahms")
        for t in a.tracks:
            r.tags[t.mappings[0].path] = TagInfo(title=t.title, artist="Johannes Brahms")
        run(idx.index_album(a))
    ws = by_title(idx, "Symphony No. 1")
    assert len(ws) == 2 and all(w["identity_tier"] == "title" for w in ws)
    assert any(v["reason"] == "possible same work on another album" for v in idx.review())


def test_collection_level_opus_goes_to_review_not_merge(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    a = album("A", ["x"], base=0)
    r.tags["A/0"] = tag("24 Preludes, Op. 28", "No. 4 in E minor", 4)
    b = album("B", ["y"], base=10)
    r.tags["B/0"] = tag("Prelude in E minor, Op. 28 No. 4", None, None)
    c = album("C", ["z"], base=20)
    r.tags["C/0"] = tag("Prelude in B minor, Op. 28 No. 6", None, None)
    for x in (a, b, c):
        run(idx.index_album(x))
    assert len(idx.works()) == 3
    assert any(v["reason"] == "collection-level catalogue match" for v in idx.review())


def test_same_full_catalogue_merges_conflicting_key_does_not(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    for aid, base, work in (("A", 0, "Ballade No. 1 in G minor, Op. 23"), ("B", 10, "Ballade No. 1, Op. 23"),
                            ("C", 20, "Ballade in F major, Op. 23")):
        x = album(aid, ["t"], base=base)
        r.tags[f"{aid}/0"] = tag(work, None, None)
        run(idx.index_album(x))
    ws = idx.works()
    assert sorted(w["recordings"] for w in ws) == [1, 2]
    assert any(v["reason"] == "merge candidate with a conflict" for v in idx.review())


def test_conflicting_mbids_never_merge(tmp_path):
    """Same tag title and composer, but the work-MBID tags resolve to different MB works: two works."""
    r = Reader()
    idx = new_index(tmp_path, reader=r)
    for aid, base, wid in (("A", 0, "aacb1ab0-c740-436a-a782-ed60026cf82b"),
                           ("B", 10, "be255a2d-85b1-4258-b78c-280e8739df95"),
                           ("C", 20, "aacb1ab0-c740-436a-a782-ed60026cf82b")):
        x = album(aid, ["t"], base=base)
        r.tags[f"{aid}/0"] = tag("Symphony No. 9 in E minor", composer="Antonín Dvořák", work_mbid=wid)
        run(idx.index_album(x))
    assert sorted(w["recordings"] for w in idx.works()) == [1, 2]          # A and C share rule (1)


def test_mb_identified_work_blocks_tag_rule(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    x = album("A", ["t"])
    r.tags["A/0"] = tag("Ballade No. 1 in G minor, Op. 23")
    run(idx.index_album(x))
    idx.con.execute("INSERT INTO alias(entity, entity_id, kind, value) SELECT 'work', id, 'mbid', "
                    "'33333333-3333-4333-8333-333333333333' FROM work")
    x = album("C", ["t"], base=20)
    r.tags["C/0"] = tag("Ballade No. 1 in G minor, Op. 23")
    run(idx.index_album(x))
    assert len(idx.works()) == 2
    assert any(v["reason"] == "merge candidate with a conflict" for v in idx.review())


def test_removed_tag_is_retracted_read_error_retracts_nothing(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    a = album("A", ["m1", "m2"])
    for i, t in enumerate(a.tracks):
        r.tags[t.mappings[0].path] = tag("Symphony No. 2, Op. 36", f"M{i + 1}", i + 1, composer="Ludwig van Beethoven",
                                         conductor="Old Conductor")
    run(idx.index_album(a))
    [w] = idx.works()
    assert idx.recordings(w["id"])[0]["label"].startswith("Old Conductor")
    # read error: nothing retracted
    for t in a.tracks:
        t.mappings[0].fingerprint = "f2"
        r.fail.add(t.mappings[0].path)
    run(idx.index_album(a))
    assert idx.recordings(w["id"])[0]["label"].startswith("Old Conductor")
    assert idx.status()["mappings"] == {"read_error": 2}
    # MA still plays the file, so the old grouping stays visible; then a good read without the conductor tag
    r.fail.clear()
    for i, t in enumerate(a.tracks):
        t.mappings[0].fingerprint = "f3"
        r.tags[t.mappings[0].path] = tag("Symphony No. 2, Op. 36", f"M{i + 1}", i + 1, composer="Ludwig van Beethoven")
    run(idx.index_album(a))
    lbl = idx.recordings(w["id"])[0]["label"]
    assert "Old Conductor" not in lbl
    retracted = idx.con.execute("SELECT COUNT(*) FROM evidence WHERE retracted_at IS NOT NULL").fetchone()[0]
    assert retracted >= 2


def test_gone_mapping_retracts(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    a = album("A", ["m1"])
    r.tags["A/0"] = tag("Ballade No. 1, Op. 23")
    run(idx.index_album(a))
    idx.mark_gone("filesystem_local", "A/0")
    assert idx.con.execute("SELECT COUNT(*) FROM evidence WHERE retracted_at IS NULL").fetchone()[0] == 0


def test_duplicate_files_resolve_independently_of_order(tmp_path):
    results = []
    for order in ((("zz", "B"), ("aa", "A")), (("aa", "A"), ("zz", "B"))):
        r = Reader()
        idx = new_index(tmp_path / f"o{len(results)}", mb=False, reader=r)
        t = TrackIn("1", 1, 1, "t", "", 100, mappings=[MappingIn(p, f"x{p}", "f", path=p) for p, _ in order])
        for p, c in order:
            r.tags[p] = tag(f"Nocturne {c}, Op. 9 No. 1")
        run(idx.index_album(AlbumIn("A", "A", "", 2000, None, [t])))
        results.append([w["title"] for w in idx.works()])
        assert any(v["reason"] == "conflicting tags on duplicate files" for v in idx.review())
    assert results[0] == results[1] == ["Nocturne A, Op. 9 No. 1"]


def test_visibility_filters_everything(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    a = album("A", ["m1"], provider="filesystem_local")
    b = album("B", ["m1"], provider="filesystem_local--hidden", base=10)
    r.tags["A/0"] = tag("Ballade No. 1 in G minor, Op. 23")
    r.tags["B/0"] = tag("Nocturne in E-flat major, Op. 9 No. 2")
    run(idx.index_album(a))
    run(idx.index_album(b))
    assert len(idx.works()) == 2
    vis = {"filesystem_local"}
    assert [w["title"] for w in idx.works(vis=vis)] == ["Ballade No. 1 in G minor, Op. 23"]
    [c] = idx.composers(vis=vis)
    assert c["works"] == 1
    assert idx.search("nocturne", vis=vis) == []
    hidden = next(w for w in idx.works() if "Nocturne" in w["title"])
    assert idx.recordings(hidden["id"], vis=vis) == []
    # a provider whose removal is pending is hidden for everyone at once
    idx.con.execute("INSERT INTO provider_seen VALUES ('filesystem_local--hidden', 'removal_pending', 0)")
    assert len(idx.works()) == 1


def test_track_on_two_albums_two_occurrences(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    m = MappingIn("filesystem_local", "same", "f", path="same")
    r.tags["same"] = tag("Ballade No. 1 in G minor, Op. 23")
    for aid in ("A", "B"):
        run(idx.index_album(AlbumIn(aid, aid, "", 2000, None, [TrackIn("7", 1, 1, "t", "", 100, mappings=[m])])))
    assert idx.status()["occurrences"] == 2
    [w] = idx.works()
    assert w["recordings"] == 2


def test_mb_failure_keeps_previous_evidence(tmp_path, library):
    idx = new_index(tmp_path)
    [kertesz] = [a for a in fake_library(library) if "Dvořák" in a.name]
    run(idx.index_album(kertesz))
    n = idx.con.execute("SELECT COUNT(*) FROM evidence WHERE source='mb' AND retracted_at IS NULL").fetchone()[0]
    assert n > 0
    idx.con.execute("DELETE FROM mb_cache")

    async def broken(url, headers):
        raise OSError("network down")
    idx.mb._fetch = broken
    idx.mb.interval = 0
    import classical_core.mb as mbmod
    orig = mbmod.asyncio.sleep

    async def nosleep(_):
        await orig(0)
    mbmod.asyncio.sleep = nosleep
    try:
        run(idx.index_album(kertesz, force=True))
    finally:
        mbmod.asyncio.sleep = orig
    n2 = idx.con.execute("SELECT COUNT(*) FROM evidence WHERE source='mb' AND retracted_at IS NULL").fetchone()[0]
    assert n2 == n and len(idx.works()) == 1


def test_multi_work_track_contained_not_grouped(tmp_path, library):
    """A recording linked to two different top works stays ungrouped and is shown under both."""
    idx = new_index(tmp_path)
    from classical_core.index import Index as _I  # noqa: F401
    # build a fake MB recording linking to Sym 9 IV and Vltava by injecting evidence
    largo = "e531bd3c-8584-4fab-b031-ff1231324f7b"
    vlt = "e40b8e05-d10a-4cb9-9219-818ee6b9d329"
    from classical_core.index import AlbumStats
    st = AlbumStats()
    run(idx.refresh_mb_recording(largo, st))
    run(idx.refresh_mb_recording(vlt, st))
    links = idx.con.execute("SELECT value, edge FROM evidence WHERE entity='mb_recording' AND entity_id=? "
                            "AND field='work_link'", (vlt,)).fetchall()
    for v, e in links:
        d = json.loads(v)
        d["ord"] = 1
        idx.con.execute("INSERT INTO evidence(entity, entity_id, field, value, source, source_ref, edge, observed_at) "
                        "VALUES ('mb_recording', ?, 'work_link', ?, 'mb', 'test', ?, 0)", (largo, json.dumps(d), e))
    a = AlbumIn("M", "Medley", "", 2000, None, [TrackIn("1", 1, 1, "Medley", "", 100, mbid=largo,
                                                       mappings=[MappingIn("filesystem_local", "m", "f", None)])])
    idx.mb = None  # no refetch: use the injected evidence
    run(idx.index_album(a))
    assert idx.status()["recordings"] == 0
    ws = {w["id"] for w in idx.con.execute("SELECT work_id AS id FROM work_link").fetchall()}
    assert len(ws) == 2
    for wid in ws:
        assert len(idx.contained_tracks(wid)) == 1
    assert any(v["reason"] == "track contains several works" for v in idx.review())


# ---- regressions from the 2026-10 code review ----

VLTAVA_WORK = "009b2276-f259-3f13-9112-7f1df8e6962a"
VLTAVA_REC = "e40b8e05-d10a-4cb9-9219-818ee6b9d329"


def test_adjacent_complete_performances_of_a_partless_work_do_not_merge(tmp_path):
    """Two adjacent tracks, each a full (non-partial) performance of Vltava, are two recordings."""
    idx = new_index(tmp_path)
    a = AlbumIn("V", "Vltava x2", "", 2000, None, [
        TrackIn(str(i), 1, i + 1, "Vltava", "", 700, mbid=VLTAVA_REC,
                mappings=[MappingIn("filesystem_local", f"v{i}", "f", None)]) for i in range(2)])
    run(idx.index_album(a))
    [w] = [w for w in idx.works() if "Vltava" in w["title"]]
    assert sorted(r["tracks"] for r in idx.recordings(w["id"])) == [1, 1]


def test_adjacent_whole_work_tags_with_work_mbid_do_not_merge(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, reader=r)
    a = album("V", ["Vltava", "Vltava"])
    for t in a.tracks:
        r.tags[t.mappings[0].path] = tag("Vltava", composer="Bedřich Smetana", work_mbid=VLTAVA_WORK)
    run(idx.index_album(a))
    [w] = idx.works()
    assert sorted(x["tracks"] for x in idx.recordings(w["id"])) == [1, 1]


def test_mapping_dropped_by_ma_is_marked_gone(tmp_path):
    """A provider mapping MA no longer lists on the track must stop granting visibility and tags."""
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    m1 = MappingIn("filesystem_local", "a", "f", path="a")
    m2 = MappingIn("filesystem_local--other", "b", "f", path="b")
    r.tags["a"] = r.tags["b"] = tag("Ballade No. 1 in G minor, Op. 23")
    t = TrackIn("1", 1, 1, "t", "", 100, mappings=[m1, m2])
    run(idx.index_album(AlbumIn("A", "A", "", 2000, None, [t])))
    assert len(idx.works(vis={"filesystem_local--other"})) == 1
    t.mappings = [m1]
    run(idx.index_album(AlbumIn("A", "A", "", 2000, None, [t])))
    assert idx.works(vis={"filesystem_local--other"}) == []
    assert len(idx.works(vis={"filesystem_local"})) == 1
    st = idx.con.execute("SELECT status FROM mapping_state WHERE item_id='b'").fetchone()[0]
    assert st == "gone"


def test_returning_gone_mapping_gets_its_evidence_back(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    a = album("A", ["m1"])
    r.tags["A/0"] = tag("Ballade No. 1 in G minor, Op. 23")
    run(idx.index_album(a))
    idx.retract_provider("filesystem_local")
    idx.set_provider_state("filesystem_local", "configured")       # the provider is added back
    run(idx.index_album(a))                                         # same file, same tags
    live = idx.con.execute("SELECT COUNT(*) FROM evidence WHERE source='tags' AND retracted_at IS NULL").fetchone()[0]
    assert live > 0 and len(idx.works()) == 1


def test_cue_track_mapping_is_not_read_as_a_file(tmp_path):
    r = Reader()
    idx = new_index(tmp_path, mb=False, reader=r)
    m = MappingIn("filesystem_local", "disc.cue::track03", "f", path="/music/disc.cue::track03")
    run(idx.index_album(AlbumIn("A", "A", "", 2000, None, [TrackIn("1", 1, 1, "t", "", 100, mappings=[m])])))
    assert idx.status()["mappings"] == {"ok": 1}
    assert idx.album_signature("A") is not None and not idx._retry_due(
        AlbumIn("A", "A", "", 2000, None, [TrackIn("1", 1, 1, "t", "", 100, mappings=[m])]))


@pytest.mark.parametrize("failure", ["client_error", "malformed_json"])
def test_unexpected_mb_failures_do_not_fail_the_album(tmp_path, failure):
    idx = new_index(tmp_path)
    idx.mb.interval = 0

    class ServerDisconnectedError(Exception):   # aiohttp.ClientError is not an OSError
        pass

    async def fetch(url, headers):
        if failure == "client_error":
            raise ServerDisconnectedError("server disconnected")
        return 200, b'[]'                           # valid JSON, wrong shape

    idx.mb._fetch = fetch
    import classical_core.mb as mbmod
    orig = mbmod.asyncio.sleep

    async def nosleep(_):
        await orig(0)
    mbmod.asyncio.sleep = nosleep
    try:
        a = AlbumIn("V", "V", "", 2000, None, [TrackIn("1", 1, 1, "Vltava", "", 700, mbid=VLTAVA_REC,
                                                       mappings=[MappingIn("filesystem_local", "v", "f", None)])])
        assert run(idx.index_album(a)) is not None
    finally:
        mbmod.asyncio.sleep = orig
