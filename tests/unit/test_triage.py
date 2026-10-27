"""Review triage (classical_core/triage.py): decision cards, safe bulk actions, the undo log and fingerprints."""

import asyncio
import json
import time

import pytest

from classical_core import discs, triage, views
from classical_core import work_link as wl
from classical_core.index import AlbumIn, MappingIn, TrackIn
from classical_core.tags import TagInfo
from tests.mbfake import uid
from tests.unit.test_cross_disc import mahler_disc, visible
from tests.unit.test_dates import Lib
from tests.unit.test_grouping_real import new_index
from tests.unit.test_names import make_index
from tests.unit.test_work_link import SYM5, SYM10, WorksMB, _arid, job


def add(idx, r, aid, comp, work="Symphony No. 1", provider="filesystem_local", force=False):
    tracks = []
    for i, mov in enumerate(("I. Allegro", "II. Andante")):
        path = f"{aid}/{i}"
        tracks.append(TrackIn(f"{aid}-{i}", 1, i + 1, f"{work}: {mov}", "", 300,
                              mappings=[MappingIn(provider, path, "f", path)]))
        r.tags[path] = TagInfo(title=f"{work}: {mov}", composers=[comp] if comp else [], work=None)
    asyncio.run(idx.index_album(AlbumIn(aid, f"Album {aid}", "", 1990, None, tracks), force=force))


def schumann_library(tmp_path, hidden=False):
    """Robert and Clara indexed by full name; three albums credit a bare 'Schumann'."""
    idx, r = make_index(tmp_path)
    add(idx, r, "R", "Robert Schumann", "Symphony No. 3")
    add(idx, r, "C", "Clara Schumann", "Piano Trio")
    for n, aid in enumerate(("S1", "S2", "S3")):
        add(idx, r, aid, "Schumann", f"Symphony No. {n + 1}")
    if hidden:
        add(idx, r, "H", "Schumann", "Symphony No. 4", provider="filesystem--secret")
    return idx, r


def cid(idx, name):
    return next(c["id"] for c in idx.composers() if c["name"] == name)


def tx(idx, fn, *a, **kw):
    idx.con.execute("BEGIN IMMEDIATE")
    try:
        out = fn(idx, *a, **kw)
        idx.con.execute("COMMIT")
        return out
    except BaseException:
        idx.con.execute("ROLLBACK")
        raise


def apply(idx, actions, vis=None, user="seth"):
    return tx(idx, triage.apply, actions, user, vis)


def undo(idx, batch, user="seth"):
    return tx(idx, triage.undo, batch, user)


def rederive(idx, r, aids, comp="Schumann"):
    for n, aid in enumerate(aids):
        add(idx, r, aid, comp, f"Symphony No. {n + 1}", force=True)


# ---- cards ------------------------------------------------------------------------------------------------

def test_one_credit_is_one_card_with_one_decision_and_per_album_exceptions(tmp_path):
    idx, _r = schumann_library(tmp_path)
    raw = idx.review()
    assert len([v for v in raw if "Schumann" in v["detail_json"]]) >= 3
    page = triage.view(idx, can_correct=True, kind="name")
    [card] = [c for c in page["cards"] if c["key"] == "name:schumann"]
    assert card["title"] == "“Schumann”" and card["n"] >= 3
    assert {it["album_id"] for it in card["items"]} >= {"S1", "S2", "S3"}
    g = card["group_actions"][0]
    assert g["type"] == "composer" and {s["name"] for s in g["suggestions"]} == {"Robert Schumann", "Clara Schumann"}
    assert all(a["album_id"] == it["album_id"] for it in card["items"] for a in it["actions"])
    # a reader sees the card, never the controls
    ro = triage.view(idx, can_correct=False, kind="name")["cards"][0]
    assert not ro["group_actions"] and not any(it["actions"] for it in ro["items"])


def test_the_credit_mapping_applies_everywhere_but_an_album_exception_and_both_undo(tmp_path):
    idx, r = schumann_library(tmp_path)
    robert, clara = cid(idx, "Robert Schumann"), cid(idx, "Clara Schumann")
    b1 = apply(idx, [{"op": "composer_name", "name": "Schumann", "composer_id": robert}])
    assert b1["jobs"] == ["scan"] and b1["batch"]
    b2 = apply(idx, [{"op": "composer_name", "name": "Schumann", "composer_id": clara, "album_id": "S2"}])
    rederive(idx, r, ["S1", "S2", "S3"])

    def by_album():
        return {a: {c[0] for c in idx._q("SELECT c.name FROM recording rr JOIN work_composer wc ON wc.work_id=rr.work_id "
                                      "JOIN composer c ON c.id=wc.composer_id WHERE rr.album_library_id=?", (a,))}
                for a in ("S1", "S2", "S3")}

    assert {k: sorted(v) for k, v in by_album().items()} == {
        "S1": ["Robert Schumann"], "S2": ["Clara Schumann"], "S3": ["Robert Schumann"]}
    assert not triage.view(idx, kind="name")["cards"]
    # the global mapping can't be undone while the album exception on the same credit stands
    with pytest.raises(ValueError, match="later decision first"):
        undo(idx, b1["batch"])
    assert [h["can_undo"] for h in triage.history(idx, "seth")] == [True, False]
    undo(idx, b2["batch"])
    assert idx._q("SELECT signature FROM album_info WHERE album_library_id='S2'")[0][0] is None
    undo(idx, b1["batch"])
    assert not idx._q("SELECT 1 FROM override WHERE entity='name'")
    rederive(idx, r, ["S1", "S2", "S3"])
    assert "Schumann" in {c["name"] for c in idx.composers()}
    assert triage.view(idx, kind="name")["cards"][0]["key"] == "name:schumann"
    log = [x[0] for x in idx._q("SELECT op FROM correction_log ORDER BY seq")]
    assert log.count("review_undo") == 2


def test_dismissals_survive_rederive_and_undo_and_hidden_items_stay_out(tmp_path):
    idx, r = schumann_library(tmp_path, hidden=True)
    vis = ["filesystem_local"]
    everything = triage.view(idx)
    mine = triage.view(idx, vis)
    assert mine["total"] < everything["total"]
    assert "H" not in {it["album_id"] for c in mine["cards"] for it in c["items"]}
    assert "H" not in {a["id"] for a in mine["albums"]}
    hidden_ids = [it["id"] for c in everything["cards"] for it in c["items"] if it["album_id"] == "H"]
    assert hidden_ids
    # hiding a hidden album's item, or naming its album for an exception, does nothing / is not found
    assert apply(idx, [{"op": "dismiss", "items": hidden_ids}], vis)["batch"] is None
    with pytest.raises(views.NotFound):
        apply(idx, [{"op": "composer_name", "name": "Schumann", "composer_id": cid(idx, "Robert Schumann"),
                     "album_id": "H"}], vis)
    card = triage.view(idx, vis, can_correct=True, kind="name")["cards"][0]
    hide = card["group_actions"][-1]
    assert hide["hide"] and hide["label"] == f"Hide all {card['n']}"
    b = apply(idx, hide["actions"], vis)
    assert triage.view(idx, vis)["dismissed"] == card["n"]
    assert not triage.view(idx, vis, kind="name")["cards"]
    # the queue is rebuilt with new ids: the fingerprint still matches
    before = {v["id"] for v in idx.review() if v["album_library_id"] in ("S1", "S2", "S3")}
    rederive(idx, r, ["S1", "S2", "S3"])
    assert before and not before & {v["id"] for v in idx.review()}
    assert not triage.view(idx, vis, kind="name")["cards"]
    shown = triage.view(idx, vis, can_correct=True, kind="name", status="dismissed")["cards"]
    assert shown[0]["n"] == card["n"] and shown[0]["group_actions"][0]["actions"][0]["op"] == "restore"
    undo(idx, b["batch"])
    assert triage.view(idx, vis, kind="name")["cards"][0]["n"] == card["n"]


def test_fingerprints_are_stable_and_change_with_the_evidence(tmp_path):
    idx, r = schumann_library(tmp_path)
    k1 = sorted(triage._keys(idx).values())
    rederive(idx, r, ["S1", "S2", "S3"])
    assert sorted(triage._keys(idx).values()) == k1
    base = {"reason": "x", "album_library_id": "a", "entity": "album", "entity_id": "a", "detail": {"tracks": ["1"]}}
    assert triage.fingerprint(base) != triage.fingerprint({**base, "detail": {"tracks": ["1", "2"]}})
    assert triage.fingerprint({**base, "detail": {"tracks": ["1"], "earlier_recording": "r_1"}}) == \
        triage.fingerprint({**base, "detail": {"tracks": ["1"], "earlier_recording": "r_2"}})


def test_filters_pick_items_inside_cards_and_count_by_kind(tmp_path):
    idx, _r = schumann_library(tmp_path)
    page = triage.view(idx, can_correct=True)
    assert page["total"] == sum(k["items"] for k in page["kinds"])
    one = triage.view(idx, can_correct=True, album="S2")
    assert {it["album_id"] for c in one["cards"] for it in c["items"]} == {"S2"}
    name = next(c for c in one["cards"] if c["kind"] == "name")
    assert name["group_actions"][-1]["actions"] == [{"op": "dismiss", "items": [it["id"] for it in name["items"]]}]
    robert = cid(idx, "Robert Schumann")
    assert any(c["id"] == robert for c in page["composers"])
    assert triage.view(idx, composer=robert)["items"] >= 3
    assert triage.view(idx, q="zzz-nothing")["cards"] == []
    assert triage.items_for(idx, None, kind="name")
    with pytest.raises(ValueError):
        triage.view(idx, sort="random")
    with pytest.raises(ValueError):
        triage.view(idx, kind="nope")
    paged = triage.view(idx, limit=1)
    assert len(paged["cards"]) == 1 and (paged["next"] == 1) == (paged["cards_total"] > 1)


# ---- bulk safety --------------------------------------------------------------------------------------------

def test_accepts_and_credit_mappings_are_one_at_a_time(tmp_path):
    idx, _r = schumann_library(tmp_path)
    robert = cid(idx, "Robert Schumann")
    two_accepts = [{"op": "work_mb_link", "work_id": "w", "decision": "accept", "mbid": uid(1)},
                   {"op": "accolade_decide", "entry": "e", "recording_id": "r", "decision": "confirm"}]
    for bad in (two_accepts,
                [{"op": "join_discs", "first_recording_id": "a", "second_recording_id": "b"}] * 2,
                [{"op": "composer_name", "name": "Schumann", "composer_id": robert}] * 2,
                [], [{"op": "drop_tables"}], [{"nope": 1}]):
        with pytest.raises(ValueError):
            apply(idx, bad)
    assert not idx._q("SELECT 1 FROM review_batch")


def test_mb_cards_group_identical_proposals_bulk_reject_and_undo(tmp_path):
    lib = Lib(tmp_path)
    lib.add("Symphony No. 10")
    lib.add("Symphony No. 5 in C minor, Op. 67")
    _arid(lib)
    job(lib, WorksMB([SYM10, SYM5]))
    idx = lib.ix
    [item] = idx._q("SELECT entity_id, album_library_id, detail_json FROM review_item WHERE reason=?", (wl.REASON,))
    wid10 = item[0]
    # A second library work proposing the same candidate: re-point the linked Symphony No. 5 at the Sym10 proposal.
    [wid5] = [r[0] for r in idx._q("SELECT work_id FROM mb_work_match WHERE state='linked'")]
    match = idx._q("SELECT sig, detail_json FROM mb_work_match WHERE work_id=?", (wid10,))[0]
    with idx.con:
        idx.con.execute("DELETE FROM alias WHERE entity='work' AND entity_id=? AND kind='mbid'", (wid5,))
        idx.con.execute("UPDATE mb_work_match SET state='review', mbid=NULL, how=NULL, detail_json=? WHERE work_id=?",
                        (match[1], wid5))
        idx.con.execute("INSERT INTO review_item VALUES (?,?,?,?,?,?)",
                        (f"mbw:{wid5}", wl.REASON, item[1], "work", wid5, item[2]))
    page = triage.view(idx, can_correct=True, kind="mb")
    [card] = [c for c in page["cards"] if c["n"] >= 2] or [None]
    assert card is not None and card["key"] == "mb:" + uid(4)
    bulk = next(g for g in card["group_actions"] if g["type"] == "apply" and not g.get("hide"))
    assert all(a["decision"] == "reject" for a in bulk["actions"]) and len(bulk["actions"]) == card["n"]
    accepts = [a for it in card["items"] for a in it["actions"] if a.get("accept")]
    assert accepts and all(len(a["actions"]) == 1 for a in accepts)
    before = {r["work_id"]: dict(r) for r in idx._q("SELECT * FROM mb_work_match")}
    b = apply(idx, bulk["actions"])
    assert b["jobs"] == ["dates"]
    assert not triage.view(idx, kind="mb", q="Symphony No. 10")["cards"]
    undo(idx, b["batch"])
    after = {r["work_id"]: dict(r) for r in idx._q("SELECT * FROM mb_work_match")}
    assert after == before
    assert not idx._q("SELECT 1 FROM override WHERE op='mb_link'")
    assert triage.view(idx, kind="mb")["cards"][0]["key"] == card["key"]
    # an accept undone puts the alias back as it was
    one = next(a for it in card["items"] for a in it["actions"] if a.get("accept"))
    wid = one["actions"][0]["work_id"]
    b = apply(idx, one["actions"])
    assert idx._q("SELECT 1 FROM alias WHERE entity='work' AND entity_id=? AND kind='mbid'", (wid,))
    undo(idx, b["batch"])
    assert not idx._q("SELECT 1 FROM alias WHERE entity='work' AND entity_id=? AND kind='mbid'", (wid,))
    assert idx._q("SELECT state FROM mb_work_match WHERE work_id=?", (wid,))[0][0] == "review"


# ---- discs: "Keep discs separate" can be undone -----------------------------------------------------------

def test_keep_discs_separate_is_undone_from_recent_decisions(tmp_path):
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    mahler_disc(idx, r, 2)
    rid = visible(idx)[0]["id"]
    b = apply(idx, [{"op": "separate_discs", "recording_id": rid}])
    assert sorted(x["tracks"] for x in visible(idx)) == [2, 3] and b["jobs"] == ["accolades"]
    [h] = triage.history(idx, "seth")
    assert h["label"] == "Kept discs separate" and h["can_undo"]
    assert triage.history(idx, "someone-else") == []
    with pytest.raises(views.NotFound):
        undo(idx, b["batch"], user="someone-else")
    out = undo(idx, b["batch"])
    assert out["jobs"] == ["accolades"]
    assert [x["tracks"] for x in visible(idx)] == [5]
    assert not idx._q("SELECT 1 FROM override WHERE entity='disc_pair'")
    with pytest.raises(ValueError, match="already undone"):
        undo(idx, b["batch"])
    assert triage.history(idx, "seth")[0]["undone"]


def test_a_continuation_card_offers_join_alone_and_separate_in_bulk(tmp_path):
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2, performers=[], artist="Sir Georg Solti")
    for t in d2.tracks:
        t.artist = "Various Artists"
    asyncio.run(idx.index_album(d2, force=True))
    [card] = triage.view(idx, can_correct=True, kind="discs")["cards"]
    together = next(a for a in card["items"][0]["actions"] if a.get("accept"))
    b = apply(idx, together["actions"])
    assert [x["tracks"] for x in visible(idx)] == [5]
    assert not idx._q("SELECT 1 FROM review_item WHERE reason=?", (discs.CONTINUATION,))
    undo(idx, b["batch"])
    assert len(visible(idx)) == 2
    assert triage.view(idx, kind="discs")["cards"]


# ---- scale ----------------------------------------------------------------------------------------------------

def test_ten_thousand_items_build_quickly(tmp_path):
    idx, _r = schumann_library(tmp_path)
    rows = [(f"x{i}", ["no work identified on this album", "ambiguous album positions",
                       "conflicting tags on duplicate files"][i % 3], "S1", "album", "S1",
             json.dumps({"album": "Album S1", "tracks": [f"t{i}"]})) for i in range(10_000)]
    idx.con.executemany("INSERT INTO review_item VALUES (?,?,?,?,?,?)", rows)
    t = time.perf_counter()
    page = triage.view(idx, can_correct=True)
    took = time.perf_counter() - t
    assert page["total"] >= 10_000 and len(page["cards"]) <= 50
    assert took < 5, took
    info = next(c for c in page["cards"] if c["kind"] == "info")
    assert len(info["items"]) <= triage.MAX_CARD_ITEMS and info.get("more")
    ids = triage.items_for(idx, None, reason="ambiguous album positions")
    b = apply(idx, [{"op": "dismiss", "items": ids}])
    assert triage.view(idx)["dismissed"] == len(ids) and b["label"] == f"Hid {len(ids)} items"


# ---- adversarial review (2026-10-27) ------------------------------------------------------------------------

def test_a_join_without_a_decision_counts_as_an_accept(tmp_path):
    """A missing or empty decision is read as 'together' by the op, so it must count as an accept too."""
    idx, r = new_index(tmp_path)
    mahler_disc(idx, r, 1)
    d2 = mahler_disc(idx, r, 2, performers=[], artist="Sir Georg Solti")
    for t in d2.tracks:
        t.artist = "Various Artists"
    asyncio.run(idx.index_album(d2, force=True))
    [card] = triage.view(idx, can_correct=True, kind="discs")["cards"]
    one = next(a for a in card["items"][0]["actions"] if a.get("accept"))["actions"][0]
    for dec in (None, "", 0):
        sneaky = {**one, "decision": dec}
        with pytest.raises(ValueError, match="one match at a time"):
            apply(idx, [sneaky, sneaky])
        with pytest.raises(ValueError, match="one match at a time"):
            apply(idx, [sneaky, {**one, "decision": "together"}])
    rid = visible(idx)[0]["id"]
    with pytest.raises(ValueError, match="one match at a time"):
        apply(idx, [{"op": "separate_discs", "recording_id": rid}] * 2)
    assert not idx._q("SELECT 1 FROM review_batch")
    assert len(visible(idx)) == 2


def test_work_items_naming_other_works_are_distinct_and_stable(tmp_path):
    idx, r = make_index(tmp_path)
    for a in ("A", "B", "C"):
        add(idx, r, a, "Robert Schumann", "Symphony No. 3")
    k1 = sorted(triage._keys(idx).values())
    for a in ("A", "B", "C"):
        add(idx, r, a, "Robert Schumann", "Symphony No. 3", force=True)
    assert sorted(triage._keys(idx).values()) == k1
    base = {"reason": "same catalogue number, different title", "album_library_id": "a", "entity": "work",
            "entity_id": "w1", "detail": {"title": "T", "other": "w2", "why": "x"}}
    same = {"w2": "w2", "w3": "w3", "w2b": "w2"}

    def res(_e, x):
        return same.get(x, x)

    assert triage.fingerprint(base, resolve=res) != \
        triage.fingerprint({**base, "detail": {**base["detail"], "other": "w3"}}, resolve=res)
    assert triage.fingerprint(base, resolve=res) == \
        triage.fingerprint({**base, "detail": {**base["detail"], "other": "w2b"}}, resolve=res)
    dup = {**base, "reason": "possible same work on another album", "detail": {"others": ["w2"], "other": "w2"}}
    more = {**dup, "detail": {"others": ["w2", "w3"], "other": "w2"}}
    assert triage.fingerprint(dup, resolve=res) != triage.fingerprint(more, resolve=res)
    # a credit's "other" is a composer name, not a work: kept as is
    comp = {"reason": "possible same composer", "album_library_id": "a", "entity": "composer", "entity_id": "c1",
            "detail": {"name": "Schumann", "other": "Robert Schumann"}}
    assert triage.fingerprint(comp) != triage.fingerprint({**comp, "detail": {"name": "Schumann", "other": "x"}})


def test_another_users_later_decision_blocks_undo_and_says_so(tmp_path):
    idx, _r = schumann_library(tmp_path)
    card = triage.view(idx, can_correct=True, kind="name")["cards"][0]
    ids = [it["id"] for it in card["items"]]
    b1 = apply(idx, [{"op": "dismiss", "items": ids[:1]}])
    key = triage.view(idx, can_correct=True, kind="name", status="dismissed")["cards"][0]["items"][0]["key"]
    b2 = apply(idx, [{"op": "restore", "keys": [key]}], user="admin")
    with pytest.raises(ValueError, match="someone else"):
        undo(idx, b1["batch"])
    [h] = triage.history(idx, "seth")
    assert not h["can_undo"] and h["blocked_by_other"] and h["blocked_by"] == b2["batch"]
    with pytest.raises(views.NotFound):
        undo(idx, b2["batch"])                     # only your own, admin or not
    undo(idx, b2["batch"], user="admin")
    undo(idx, b1["batch"])
    assert triage.view(idx, kind="name")["cards"][0]["n"] == card["n"]


def test_schema_6_index_migrates_to_7_and_keeps_its_data(tmp_path, monkeypatch):
    from classical_core import store
    real = store._migrations()
    monkeypatch.setattr(store, "_migrations", lambda: [m for m in real if m[0] <= 6])
    con = store.connect(tmp_path / "index.db")
    assert store.migrate(con) == 6
    con.execute("INSERT INTO review_item VALUES ('v1','no work identified on this album','A','album','A','{}')")
    monkeypatch.setattr(store, "_migrations", lambda: real)
    assert store.migrate(con) == 8
    assert con.execute("SELECT COUNT(*) FROM review_item").fetchone()[0] == 1
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master")} >= {
        "review_dismissal", "review_batch", "review_item_reason", "review_batch_user"}
    assert store.migrate(con) == 8
