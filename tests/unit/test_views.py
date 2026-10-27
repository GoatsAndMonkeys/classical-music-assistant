"""Companion UI views (classical_core/views.py): role buckets, cross-filters, facets, dates, visibility."""

import json
import time

import pytest

from classical_core import views
from classical_core.index import AlbumIn, MappingIn, TrackIn
from classical_core.tags import Person, TagInfo
from tests.unit.test_index import Reader, new_index, run

WORK5 = "Symphony No. 5 in C minor, Op. 67"
WORK7 = "Symphony No. 7 in A major, Op. 92"
CONCERTO = "Violin Concerto in D major, Op. 61"


def ids(rec):
    return {c["id"] for c in rec["credits"]}


def person(name, role, kind="person"):
    return Person(name, role, kind)


KARAJAN = person("Herbert von Karajan", "conductor")
KLEIBER = person("Carlos Kleiber", "conductor")
BPH = person("Berliner Philharmoniker", "orchestra", "group")
VPH = person("Wiener Philharmoniker", "orchestra", "group")
MUTTER = person("Anne-Sophie Mutter", "violin")
CHOIR = person("Wiener Singverein", "chorus", "group")
QUARTET = person("Alban Berg Quartett", "ensemble", "group")


class Lib:
    """Builds one album per recording: `add(work, performers, recorded=..., provider=...)`."""

    def __init__(self, tmp_path):
        self.r = Reader()
        self.ix = new_index(tmp_path, mb=False, reader=self.r)
        self.n = 0

    def add(self, work, performers, movements=("I. Allegro", "II. Andante"), recorded=None, released=None,
            provider="filesystem_local", year=2000, composer="Ludwig van Beethoven"):
        self.n += 1
        aid = f"A{self.n}"
        tracks = []
        for i, mov in enumerate(movements):
            path = f"{aid}/{i}"
            tracks.append(TrackIn(str(self.n * 100 + i), 1, i + 1, f"{work}: {mov}", "", 300,
                                  mappings=[MappingIn(provider, path, fingerprint="f", path=path)]))
            self.r.tags[path] = TagInfo(title=f"{work}: {mov}", work=work, movement_name=mov, movement_no=i + 1,
                                        composers=[composer], performers=list(performers), recorded=recorded,
                                        release_date=released)
        run(self.ix.index_album(AlbumIn(aid, f"Album {aid}", "", year, None, tracks)))
        return aid

    def work_id(self, title):
        [w] = [w for w in self.ix.works() if w["title"] == title]
        return w["id"]

    def pid(self, name):
        return self.ix._one("SELECT id FROM person WHERE name=?", (name,))["id"]


@pytest.fixture
def lib(tmp_path):
    lib = Lib(tmp_path)
    lib.add(WORK5, [KARAJAN, BPH], recorded="1963")
    lib.add(WORK5, [KLEIBER, VPH], recorded="1974")
    lib.add(WORK5, [KARAJAN, VPH], released="2010")
    lib.add(WORK7, [KARAJAN, BPH], recorded="1977")
    lib.add(CONCERTO, [MUTTER, KARAJAN, BPH], recorded="1980")
    lib.add("Mass in C major, Op. 86", [CHOIR, KARAJAN, BPH])
    lib.add("String Quartet No. 13 in B-flat major, Op. 130", [QUARTET])
    return lib


def test_bucket_rules():
    assert views.bucket("conductor", "person") == "conductors"
    assert views.bucket("orchestra", "orchestra") == "orchestras"
    assert views.bucket("performer", "orchestra") == "orchestras"
    assert views.bucket("chorus", "choir") == "choirs"
    assert views.bucket("ensemble", "other") == "chamber"
    assert views.bucket("violin", "person") == "soloists"
    assert views.bucket("soprano", "person") == "soloists"
    assert views.bucket("performer", "person") == "other"


def test_performers_by_bucket_keep_orchestras_chamber_and_choirs_apart(lib):
    res = views.performers(lib.ix)
    by = {p["name"]: p for p in res["items"]}
    assert by["Berliner Philharmoniker"]["roles"] == ["orchestras"]
    assert by["Alban Berg Quartett"]["roles"] == ["chamber"]
    assert by["Wiener Singverein"]["roles"] == ["choirs"]
    assert by["Anne-Sophie Mutter"]["roles"] == ["soloists"] and by["Anne-Sophie Mutter"]["instruments"] == ["violin"]
    assert by["Herbert von Karajan"]["recordings"] == 5 and by["Herbert von Karajan"]["works"] == 4
    assert res["counts"]["all"] == len(res["items"])
    assert res["counts"]["orchestras"] == 2 and res["counts"]["chamber"] == 1
    sol = views.performers(lib.ix, role="soloists")
    assert [p["name"] for p in sol["items"]] == ["Anne-Sophie Mutter"]
    with pytest.raises(ValueError):
        views.performers(lib.ix, role="kazoo")


def test_with_filter_works_both_ways(lib):
    bph, karajan = lib.pid("Berliner Philharmoniker"), lib.pid("Herbert von Karajan")
    cond = views.performers(lib.ix, role="conductors", with_id=bph)
    assert [p["name"] for p in cond["items"]] == ["Herbert von Karajan"]
    assert cond["items"][0]["recordings"] == 4, "only recordings shared with the orchestra"
    assert cond["with"]["name"] == "Berliner Philharmoniker"
    orch = views.performers(lib.ix, role="orchestras", with_id=karajan)
    assert {p["name"] for p in orch["items"]} == {"Berliner Philharmoniker", "Wiener Philharmoniker"}
    kleiber = lib.pid("Carlos Kleiber")
    assert [p["name"] for p in views.performers(lib.ix, role="orchestras", with_id=kleiber)["items"]] == [
        "Wiener Philharmoniker"]
    with pytest.raises(views.NotFound):
        views.performers(lib.ix, with_id="p_nobody")


def test_performer_page_works_and_partners(lib):
    bph = lib.pid("Berliner Philharmoniker")
    res = views.performer(lib.ix, bph)
    mine = {w["title"]: (w["mine"], w["recordings"]) for w in res["works"]}
    assert mine[WORK5] == (1, 3) and mine[WORK7] == (1, 1)
    w5 = next(w for w in res["works"] if w["title"] == WORK5)
    rec5 = [r["id"] for r in views.work(lib.ix, lib.work_id(WORK5))["recordings"] if bph in ids(r)]
    assert w5["recording_ids"] == rec5, "the performer's own recordings of each work, to play from MA's page"
    assert [p["name"] for p in res["partners"]["conductors"]] == ["Herbert von Karajan"]
    assert [p["name"] for p in res["partners"]["soloists"]] == ["Anne-Sophie Mutter"]
    both = views.performer(lib.ix, lib.pid("Herbert von Karajan"), with_id=lib.pid("Wiener Philharmoniker"))
    assert [w["title"] for w in both["works"]] == [WORK5] and both["recordings"] == 1
    with pytest.raises(views.NotFound):
        views.performer(lib.ix, "p_nobody")


def test_work_page_scope_facets_and_labels(lib):
    wid = lib.work_id(WORK5)
    karajan, bph = lib.pid("Herbert von Karajan"), lib.pid("Berliner Philharmoniker")
    res = views.work(lib.ix, wid, scope=karajan)
    assert len(res["recordings"]) == 3, "a scope never hides recordings (SHOW ALL is client-side)"
    assert res["scope"]["ids"] == [karajan]
    assert sum(karajan in ids(r) for r in res["recordings"]) == 2
    assert views.work(lib.ix, wid, scope=f"{karajan},p_nobody")["scope"]["ids"] == [karajan]
    assert views.work(lib.ix, wid, scope=lib.pid("Anne-Sophie Mutter"))["scope"] is None, "not on this work"
    f = res["facets"]
    assert {(x["name"], x["n"]) for x in f["conductors"]} == {("Herbert von Karajan", 2), ("Carlos Kleiber", 1)}
    assert {(x["name"], x["n"]) for x in f["orchestras"]} == {("Wiener Philharmoniker", 2),
                                                             ("Berliner Philharmoniker", 1)}
    assert f["decades"] == [{"decade": 1960, "n": 1}, {"decade": 1970, "n": 1}]
    kinds = {r["who"]: (r["year"], r["year_kind"]) for r in res["recordings"]}
    assert kinds["Herbert von Karajan / Berliner Philharmoniker"] == (1963, "recorded")
    assert kinds["Herbert von Karajan / Wiener Philharmoniker"] == (2010, "released")
    labels = {r["label"] for r in res["recordings"]}
    assert any("(1963)" in x for x in labels) and any("(rel. 2010)" in x for x in labels)
    rec = next(r for r in res["recordings"] if bph in ids(r))
    assert rec["play_uri"] == f"classical://folder/works/{wid}/{rec['id']}"
    assert {c["group"] for c in rec["credits"]} == {"conductors", "orchestras"}
    assert res["best"]["recording_id"] in {r["id"] for r in res["recordings"]}
    with pytest.raises(views.NotFound):
        views.work(lib.ix, "w_nothing")


def test_soloist_instrument_in_credits_and_recording_movements(lib):
    wid = lib.work_id(CONCERTO)
    res = views.work(lib.ix, wid)
    [sol] = res["facets"]["soloists"]
    assert (sol["name"], sol["instrument"]) == ("Anne-Sophie Mutter", "violin")
    rid = res["recordings"][0]["id"]
    rec = views.recording(lib.ix, rid)
    assert [m["name"] for m in rec["movements"]] == ["I. Allegro", "II. Andante"]
    assert all(m["track_uri"].startswith("library://track/") for m in rec["movements"])
    assert rec["work"]["id"] == wid and rec["album_info"]["name"].startswith("Album ")
    assert any(c.get("instrument") == "violin" for c in rec["credits"])
    with pytest.raises(views.NotFound):
        views.recording(lib.ix, "r_nothing")


def test_composers_and_works_filters(lib):
    comps = views.composers(lib.ix)
    [lvb] = comps["items"]
    assert lvb["letter"] == "B" and lvb["works"] == 5 and comps["letters"] == ["B"]
    res = views.works(lib.ix, composer_id=lvb["id"])
    assert res["total"] == 5 and res["composer"]["name"] == "Ludwig van Beethoven"
    assert {t["type"] for t in res["types"]} >= {"Symphonies"}
    assert [w["title"] for w in views.works(lib.ix, q="op 92")["items"]] == [WORK7]
    mutter = views.works(lib.ix, performer_id=lib.pid("Anne-Sophie Mutter"))
    assert [w["title"] for w in mutter["items"]] == [CONCERTO]
    with pytest.raises(views.NotFound):
        views.works(lib.ix, composer_id="c_nobody")
    with pytest.raises(ValueError):
        views.works(lib.ix, date_kind="written")


def test_date_filters_by_kind(lib):
    rec = views.works(lib.ix, date_kind="recorded", date_from=1970, date_to=1979)
    assert {w["title"] for w in rec["items"]} == {WORK5, WORK7}
    assert views.works(lib.ix, date_kind="recorded", date_from=1960, date_to=1969)["total"] == 1
    rel = views.works(lib.ix, date_kind="released", date_from=2010, date_to=2010)
    assert [w["title"] for w in rel["items"]] == [WORK5]
    comp = views.works(lib.ix, date_kind="composed", date_from=1800, date_to=1810)
    assert comp["total"] == 0, "no composition dates in the index yet"
    cov = views.works(lib.ix)["date_coverage"]
    assert cov["composed"] == 0 and cov["total"] == 5 and cov["recorded"] == 3


def test_hidden_provider_is_absent_everywhere(tmp_path):
    lib = Lib(tmp_path)
    lib.add(WORK5, [KARAJAN, BPH], recorded="1963")
    lib.add(WORK5, [KLEIBER, VPH], recorded="1974", provider="filesystem_local--hidden")
    lib.add(WORK7, [KLEIBER, VPH], provider="filesystem_local--hidden")
    vis = {"filesystem_local"}
    ix = lib.ix
    wid5, wid7 = lib.work_id(WORK5), lib.work_id(WORK7)
    kleiber, vph = lib.pid("Carlos Kleiber"), lib.pid("Wiener Philharmoniker")
    assert [w["title"] for w in views.works(ix, vis=vis)["items"]] == [WORK5]
    w = views.work(ix, wid5, vis=vis)
    assert [r["who"] for r in w["recordings"]] == ["Herbert von Karajan / Berliner Philharmoniker"]
    assert [c["name"] for c in w["facets"]["conductors"]] == ["Herbert von Karajan"]
    hidden_rid = next(r["id"] for r in views.work(ix, wid5)["recordings"] if kleiber in ids(r))
    for call in (lambda: views.work(ix, wid7, vis=vis), lambda: views.recording(ix, hidden_rid, vis=vis),
                 lambda: views.performer(ix, kleiber, vis=vis),
                 lambda: views.performers(ix, vis=vis, with_id=vph),
                 lambda: views.works(ix, vis=vis, performer_id=kleiber)):
        with pytest.raises(views.NotFound):
            call()
    names = {p["name"] for p in views.performers(ix, vis=vis)["items"]}
    assert names == {"Herbert von Karajan", "Berliner Philharmoniker"}
    assert all(p["name"] != "Carlos Kleiber" for p in views.performer(ix, lib.pid("Herbert von Karajan"),
                                                                      vis=vis)["partners"]["conductors"])
    assert views.composers(ix, vis=vis)["items"][0]["works"] == 1
    assert views.review(ix, vis=vis)["total"] == len(ix.review(vis))


def test_review_groups_and_actions(tmp_path):
    lib = Lib(tmp_path)
    # Dvořák's symphonies are not on the numbered-works list, so the same title on two albums is only a review item
    for aid, title in (("A", "Symphony No. 1 in C minor"), ("B", "Symphony No. 1 in C minor")):
        lib.n += 1
        a = AlbumIn(aid, f"Album {aid}", "Antonín Dvořák", 2000, None, [
            TrackIn(str(lib.n * 100), 1, 1, title, "Antonín Dvořák", 100,
                    mappings=[MappingIn("filesystem_local", f"{aid}/0", fingerprint="f", path=f"{aid}/0")])])
        lib.r.tags[f"{aid}/0"] = TagInfo(title=title, artist="Antonín Dvořák")
        run(lib.ix.index_album(a))
    res = views.review(lib.ix)
    assert res["total"] == sum(g["n"] for g in res["groups"]) >= 1
    item = res["groups"][0]["items"][0]
    assert item["id"].startswith("v_") and item["hint"] and item["actions"] == []
    assert item["album"].startswith("Album ")
    json.dumps(res)


def test_large_work_facets_are_small_and_fast(tmp_path):
    lib = Lib(tmp_path)
    conductors = [person(f"Conductor {i}", "conductor") for i in range(40)]
    orchestras = [person(f"Orchestra {i}", "orchestra", "group") for i in range(25)]
    soloists = [person(f"Soloist {i}", "violin") for i in range(60)]
    for i in range(400):
        lib.add(CONCERTO, [conductors[i * 7 % 40], orchestras[i * 3 % 25], soloists[i % 60]],
                movements=("I. Allegro ma non troppo", "II. Larghetto", "III. Rondo. Allegro"),
                recorded=str(1950 + i % 70))
    wid = lib.work_id(CONCERTO)
    t = time.perf_counter()
    res = views.work(lib.ix, wid)
    took = time.perf_counter() - t
    size = len(json.dumps(res))
    assert len(res["recordings"]) == 400
    assert sum(x["n"] for x in res["facets"]["conductors"]) == 400
    assert len(res["facets"]["soloists"]) == 60
    assert size < 400_000, size
    assert took < 1.0, took


def _count_queries(ix):
    n = {"q": 0}
    q, one = ix._q, ix._one

    def cq(*a, **k):
        n["q"] += 1
        return q(*a, **k)

    def co(*a, **k):
        n["q"] += 1
        return one(*a, **k)

    ix._q, ix._one = cq, co
    return n


@pytest.mark.parametrize("view", ["work", "recording"])
def test_work_and_recording_pages_do_not_query_per_recording(tmp_path, view):
    counts = []
    for size in (3, 30):
        lib = Lib(tmp_path / f"{view}{size}")
        for i in range(size):
            lib.add(CONCERTO, [person(f"Conductor {i}", "conductor"), BPH], recorded=str(1950 + i))
        wid = lib.work_id(CONCERTO)
        rid = views.work(lib.ix, wid)["recordings"][-1]["id"]
        n = _count_queries(lib.ix)
        res = views.work(lib.ix, wid) if view == "work" else views.recording(lib.ix, rid)
        counts.append(n["q"])
        if view == "recording":
            assert res["id"] == rid and res["who"].startswith("Conductor ") and len(res["movements"]) == 2
        else:
            assert len(res["recordings"]) == size
            assert all(r["label"].startswith(r["who"]) for r in res["recordings"])
    assert counts[0] == counts[1], counts


def test_review_does_not_name_works_on_hidden_sources(tmp_path):
    lib = Lib(tmp_path)
    lib.add("Fantasia for Strings", [BPH], provider="filesystem_local--hidden", composer="Ralph Vaughan Williams")
    lib.add("Fantasia for Strings", [VPH], composer="Ralph Vaughan Williams", year=2001)
    vis = {"filesystem_local"}
    everything = [it for g in views.review(lib.ix)["groups"] for it in g["items"]]
    same = [it for it in everything if it["reason"] == "possible same work on another album"]
    assert same and all("other" in it["detail"] for it in same), everything
    visible = [it for g in views.review(lib.ix, vis=vis)["groups"] for it in g["items"]
               if it["reason"] == "possible same work on another album"]
    assert visible and all("other" not in it["detail"] and not it["detail"].get("others") for it in visible)


def test_named_choirs_are_choirs_and_placeholder_artists_are_not_performers():
    # "boys' chorus" / "children's choir" credits sat with the soloists on the Mahler 8 page
    assert views.bucket("boys' chorus", "person") == "choirs"
    assert views.bucket("children's choir", None) == "choirs"
    assert views.bucket("soprano", "person") == "soloists"
    from classical_core.index import recording_label
    r = {"credits": [], "track_artist": "[unknown]", "album_artist": "Arturo Toscanini",
         "composer_names": "Ludwig van Beethoven", "album": "Toscanini Collection 54"}
    assert recording_label(r) == "Arturo Toscanini"
    assert recording_label({**r, "album_artist": "Various Artists"}) == "Toscanini Collection 54"
