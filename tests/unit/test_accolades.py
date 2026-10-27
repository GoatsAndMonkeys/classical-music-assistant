"""Accolades (docs/accolades.md): conservative matching, scoring, the Play pick, links, network parsers."""

import asyncio
import hashlib
import json
import time

import pytest

from classical_core import accolades as A
from classical_core import views
from classical_core.mb import MBError, MBTransientError
from tests.unit.test_index import full, run  # noqa: F401 - fixture


def entry(eid="e1", award="Grammy", year=1963, result="winner", composer="Beethoven", work="Fidelio",
          performers=("Otto Klemperer", "Philharmonia Chorus", "Philharmonia Orchestra"), category="Best Opera Recording",
          label=None, historical=False):
    return A.Entry(eid, award, category, year, result, composer, work, tuple(performers), label,
                   "https://en.wikipedia.org/wiki/x", "test", historical)


def facts(titles=("Fidelio, Op. 72",), composers=("Ludwig van Beethoven",), recorded=None, released=1962,
          credits=(), artists=(), album="", label=None, rid="r1", album_id="a1"):
    pool = A.Pool()
    for name, role in credits:
        pool.add(name, role)
    for a in artists:
        pool.add_text(a)
    pool.add_text(album, names=False)
    return A.Facts(rid, "w1", album_id, list(titles), list(composers), recorded, released, label, pool)


def match(e, f):
    wm = max((A.work_match(e.work, t) for t in f.titles), key=lambda x: {"exact": 2, "weak": 1, None: 0}[x])
    return A.grade(e, wm, A.match_performers(e.performers, f.pool), f)[0]


KLEMPERER = (("Otto Klemperer", "conductor"), ("Philharmonia Orchestra", "orchestra"),
             ("Philharmonia Chorus", "chorus"), ("Christa Ludwig", "soprano"))

# ---- matching ----------------------------------------------------------------------------------------------


def test_the_awarded_release_matches():
    assert match(entry(), facts(credits=KLEMPERER)) == "this_release"
    # performers only in a track-artist string (dad's library has many of these)
    f = facts(artists=[("Christa Ludwig/Jon Vickers/Philharmonia Chorus/Wilhelm Pitz/Philharmonia Orchestra/"
                        "Otto Klemperer")])
    assert match(entry(), f) == "this_release"


def test_a_different_conductor_or_orchestra_never_matches():
    bernstein = facts(credits=(("Leonard Bernstein", "conductor"), ("Wiener Philharmoniker", "orchestra")),
                      released=1978)
    assert match(entry(year=1979), bernstein) is None
    e = entry(work="Symphony No. 5", performers=("Herbert von Karajan", "Berlin Philharmonic"), year=1964)
    vienna = facts(titles=["Symphony No. 5 in C minor, Op. 67"], released=1963,
                   credits=(("Herbert von Karajan", "conductor"), ("Wiener Philharmoniker", "orchestra")))
    assert match(e, vienna) is None
    # same surname, other given name: Erich is not Carlos Kleiber
    e = entry(work="Symphony No. 5", performers=("Erich Kleiber",), year=1996)
    carlos = facts(titles=["Symphony No. 5"], released=1995, credits=(("Carlos Kleiber", "conductor"),))
    assert match(e, carlos) is None


def test_wrong_work_number_key_or_year_never_matches():
    e = entry(work="Symphony No. 9", performers=("Herbert von Karajan", "Berlin Philharmonic"), year=1964)
    karajan = (("Herbert von Karajan", "conductor"), ("Berliner Philharmoniker", "orchestra"))
    assert match(e, facts(titles=["Symphony No. 5 in C minor, Op. 67"], released=1963, credits=karajan)) is None
    assert A.work_match("Piano Sonata in C minor", "Piano Sonata in A major") is None
    assert A.work_match("Mass", "Missa solemnis") is None
    # recorded after the award year: another recording
    assert match(entry(), facts(credits=KLEMPERER, recorded=1975, released=1976)) is None
    # an album long before the award: another recording
    assert match(entry(year=1990), facts(credits=KLEMPERER, released=1962)) is None


def test_titles_and_ensembles_in_other_languages():
    assert A.work_match("The Flying Dutchman", "Der fliegende Holländer, WWV 63") == "exact"
    assert A.work_match("Symphony No. 9 'From the New World'", "Symfonie č. 9 e moll, op. 95") == "exact"
    assert A.work_match("Piano Concerto", "Piano Concerto No. 2 in C minor") == "weak"
    e = entry(work="Symphony No. 5", performers=("Carlos Kleiber", "Vienna Philharmonic"), year=1976,
              award="Gramophone Award", category="Orchestral")
    f = facts(titles=["Symphony No. 5 in C minor, Op. 67"], released=1975,
              credits=(("Carlos Kleiber", "conductor"), ("Wiener Philharmoniker", "orchestra")))
    assert match(e, f) == "this_release"


def test_another_release_of_the_same_recording_is_labelled_so():
    e = entry(work="Symphony No. 5", performers=("Herbert von Karajan", "Berlin Philharmonic"), year=1964,
              result="nominee", category="Best Orchestral Performance")
    reissue = facts(titles=["Symphony No. 5 in C minor, Op. 67"], recorded=1962, released=2002,
                    credits=(("Herbert von Karajan", "conductor"), ("Berliner Philharmoniker", "orchestra")))
    assert match(e, reissue) == "other_release"
    # without a recording date a later album is only a doubt
    reissue.recorded = None
    assert match(e, reissue) == "uncertain"


def test_a_concert_dated_after_the_award_is_never_the_awarded_recording():
    # Gardiner's 1991 Gramophone Missa solemnis vs a 2012 Carnegie Hall concert by the same forces
    assert A.performance_year("Beethoven: Missa solemnis in D major — Carnegie Hall, 2012-11-17") == 2012
    assert A.performance_year("Missa solemnis (live, 17 November 2012)") == 2012
    assert A.performance_year("Missa solemnis, November 17th, 2012") == 2012
    assert A.performance_year("Fidelio — Salzburg 17.8.1950") == 1950
    for undated in ("Missa solemnis (2012 remaster)", "Fidelio - Wien, '70 (1)", "Symphonies 1-9", None):
        assert A.performance_year(undated) is None
    e = entry(award="Gramophone Award", year=1991, work="Missa solemnis", category="Choral",
              performers=("John Eliot Gardiner", "Monteverdi Choir", "English Baroque Soloists"))
    gardiner = (("John Eliot Gardiner", "conductor"), ("Monteverdi Choir", "chorus"),
                ("English Baroque Soloists", "orchestra"))
    concert = facts(titles=["Missa solemnis in D major"], released=2012, credits=gardiner,
                    recorded=A.performance_year("Missa solemnis — Carnegie Hall, 2012-11-17"))
    assert match(e, concert) is None
    # the awarded session reissued later (recorded inside the window) still counts
    assert match(e, facts(titles=["Missa solemnis in D major"], released=2012, recorded=1989,
                          credits=gardiner)) == "other_release"


def test_refresh_reads_a_concert_date_from_the_album_name(awarded):
    idx, _ = awarded
    A.refresh(idx.con)
    unsure = idx.con.execute("SELECT recording_id, album_library_id FROM accolade WHERE grade='uncertain'").fetchone()
    assert unsure is not None
    # no session date in the tags: a later album of the same forces stays a doubt (it may be a reissue) ...
    idx.con.execute("UPDATE recording SET recorded_start=NULL WHERE id=?", (unsure["recording_id"],))
    assert A.refresh(idx.con)["recording:uncertain"] == 1
    # ... until the album names its concert date, after the award
    idx.con.execute("UPDATE album_info SET name = name || ' — live, 2031-05-04' WHERE album_library_id=?",
                    (unsure["album_library_id"],))
    stats = A.refresh(idx.con)
    assert "recording:uncertain" not in stats
    assert idx.con.execute("SELECT COUNT(*) FROM accolade WHERE recording_id=?", (unsure["recording_id"],)
                           ).fetchone()[0] == 0


def test_thin_evidence_goes_to_review_not_to_a_badge():
    # only the conductor's surname, in the album name
    f = facts(album="Fidelio (Klemperer, disc 1)", artists=["Various Artists"])
    assert match(entry(), f) == "uncertain"
    # nobody named
    assert match(entry(), facts(artists=["Some Choir"])) is None


def test_multi_work_entries_attach_to_the_album_only():
    e = entry(work="Symphonies Nos. 5 & 7", performers=("Carlos Kleiber", "Vienna Philharmonic"), year=1976,
              award="Gramophone Award", category="Orchestral")
    credits = (("Carlos Kleiber", "conductor"), ("Wiener Philharmoniker", "orchestra"))
    recs = {r: facts(titles=[t], released=1975, credits=credits, rid=r)
            for r, t in (("r5", "Symphony No. 5 in C minor"), ("r7", "Symphony No. 7 in A major"))}
    ms = A.match_all(recs, [e])
    assert [(m.target, m.grade, m.recording_id) for m in ms] == [("album", "this_release", None)]


def test_decisions_confirm_and_reject():
    f = facts(album="Fidelio (Klemperer, disc 1)")
    e = entry()
    assert [m.grade for m in A.match_all({"r1": f}, [e])] == ["uncertain"]
    assert [m.grade for m in A.match_all({"r1": f}, [e], {("e1", "a1", "w1"): "confirm"})] == ["confirmed"]
    assert A.match_all({"r1": f}, [e], {("e1", "a1", "w1"): "reject"}) == []
    # confirming never resurrects a contradicted match
    other = facts(credits=(("Leonard Bernstein", "conductor"), ("Wiener Philharmoniker", "orchestra")))
    assert A.match_all({"r1": other}, [e], {("e1", "a1", "w1"): "confirm"}) == []


def test_bundled_dataset_ids_are_stable_hashes_and_sources_are_cited():
    d = A.dataset()
    for row in d["entries"]:
        c = d["categories"][row[1]]
        key = json.dumps([c["award"], c.get("category"), row[2], row[3], row[4], row[5], row[6]], ensure_ascii=False)
        assert hashlib.sha1(key.encode()).hexdigest()[:10] == row[0]
        assert c["url"].startswith("https://") and d["sources"][c["source"]]["url"].startswith("https://")
    assert len(A.entries()) == len(d["entries"]) == len({e.id for e in A.entries()})


# ---- scoring and the pick -------------------------------------------------------------------------------------


def badge(text="Grammy winner 1963", weight=2.5, counts=True, category=None):
    return {"text": text, "weight": weight, "counts": counts, "category": category, "kind": "award"}


def rec(rid, complete=1, status="auto", ev=3):
    return {"id": rid, "complete": complete, "status": status, "evidence_fields": ev}


def test_acclaim_weights():
    assert A.acclaim([]) == 1.0
    assert A.acclaim([badge(counts=False)]) == 1.0                      # album-level: never counts
    assert A.acclaim([badge(weight=1.3)]) == 1.3
    assert A.acclaim([badge(weight=1.8), badge(weight=1.3)]) == pytest.approx(1.83)
    assert A.acclaim([badge(), badge(), badge()]) == 2.5                # capped
    assert A.tier_weight("award", "Grammy", "winner") == 2.5
    assert A.tier_weight("award", "Grammy", "nominee") == 1.3
    assert A.tier_weight("award", "Some prize", "winner") == 1.8          # unknown awards: tier B
    assert A.tier_weight("review", None, None, rating=5) == 1.3
    assert A.tier_weight("review", None, None, rating=3) == 1.0
    assert A.tier_weight("rating", None, None, rating=4.5, rating_count=3) == 1.0    # too few ratings
    assert A.tier_weight("rating", None, None, rating=4.5, rating_count=40) == 1.15


def test_best_pick_prefers_acclaim_after_completeness():
    recs = [rec("r1", ev=9, status="confirmed"), rec("r2"), rec("r3", complete=0)]
    accs = {"r2": [badge(category="Best Opera Recording")], "r3": [badge()]}
    assert A.best_pick(recs, accs) == (recs[1], "Grammy winner 1963 (Best Opera Recording)")
    assert A.best_pick(recs, {})[1] == "complete movement set"          # unchanged without acclaim
    assert A.best_pick([recs[0]], accs)[1] == "only recording"
    # equal acclaim: the old order decides, and the reason does not credit the award
    tie = {"r1": [badge()], "r2": [badge()]}
    assert A.best_pick(recs, tie) == (recs[0], "complete movement set")
    assert A.pick_score(recs[1], accs["r2"]) > A.pick_score(recs[0], []) > 1


# ---- the index ----------------------------------------------------------------------------------------------


SYNTH = [
    entry("kleiber", "Gramophone Award", 1976, "winner", "Beethoven", "Symphony No. 5",
          ("Carlos Kleiber", "Vienna Philharmonic"), "Orchestral"),
    entry("marriner", "Gramophone Award", 2011, "winner", "Mozart", "Symphony No. 40",
          ("Neville Marriner", "Academy of St Martin in the Fields"), "Orchestral"),
    entry("karajan", "Grammy", 1964, "nominee", "Beethoven", "Symphony No. 5",
          ("Herbert von Karajan", "Berlin Philharmonic"), "Best Orchestral Performance"),
    entry("kertesz", "Grammy", 1963, "winner", "Dvorak", "Symphony No. 9 'From the New World'",
          ("Istvan Kertesz", "Vienna Philharmonic Chorus"), "Best Orchestral Performance"),
    entry("nope", "Grammy", 1996, "winner", "Beethoven", "Symphony No. 9",
          ("Carlos Kleiber", "Vienna Philharmonic"), "Best Orchestral Performance"),
]


@pytest.fixture
def awarded(full, monkeypatch):  # noqa: F811 - imported fixture
    idx, albums = full
    monkeypatch.setattr(A, "entries", lambda: tuple(SYNTH))
    return idx, albums


def _w5(idx):
    return next(w for w in idx.works() if w["title"].startswith("Symphony No. 5"))


def test_refresh_badges_and_the_play_pick(awarded):
    idx, _ = awarded
    stats = A.refresh(idx.con)
    assert stats == {"recording:this_release": 1, "recording:other_release": 2, "recording:uncertain": 1}
    v = views.work(idx, _w5(idx)["id"])
    by_who = {r["who"].split(" /")[0]: r for r in v["recordings"]}
    k, h = by_who["Carlos Kleiber"]["accolades"], by_who["Herbert von Karajan"]["accolades"]
    assert [(a["text"], a["grade"]) for a in k] == [("Gramophone Award 1976", "other_release")]
    assert k[0]["note"] == "awarded to another release of this recording (1976)"
    assert h[0]["grade"] == "other_release" and h[0]["text"] == "Grammy nominee 1964"
    assert by_who["Carlos Kleiber"]["acclaim"] == 2.5 and by_who["Herbert von Karajan"]["acclaim"] == 1.3
    assert v["best"]["reason"] == "Gramophone Award 1976 (Orchestral)"
    assert v["best"]["recording_id"] == by_who["Carlos Kleiber"]["id"]
    assert idx.best_recording(_w5(idx)["id"])[1] == v["best"]["reason"]
    assert v["acclaim"] == {"recordings": 2, "awards": [{"award": "Grammy nominee", "n": 1},
                                                        {"award": "Gramophone Award", "n": 1}]}
    w40 = next(w for w in idx.works() if w["title"].startswith("Symphony No. 40"))
    m = {r["who"].split(" /")[0]: r["accolades"] for r in views.work(idx, w40["id"])["recordings"]}
    assert [a["grade"] for a in m["Neville Marriner"]] == ["this_release"] and m["Karl Böhm"] == []
    # the Dvořák match (the chorus named is not on the album) waits for review and shows nowhere
    items = [i for g in views.review(idx)["groups"] if g["reason"] == "accolade_match" for i in g["items"]]
    assert len(items) == 1 and [a["decision"] for a in items[0]["actions"]] == ["confirm", "reject"]
    rid = items[0]["entity_id"]
    assert views.recording(idx, rid)["awards"] == []
    # a user confirms: shown, and it survives a rebuild
    A.decide(idx.con, "kertesz", rid, "confirm", "seth")
    A.refresh(idx.con)
    assert [a["note"] for a in views.recording(idx, rid)["awards"]] == ["match confirmed by a user"]
    A.decide(idx.con, "kertesz", rid, "reject", "seth")
    A.refresh(idx.con)
    assert views.recording(idx, rid)["awards"] == [] and not [
        g for g in views.review(idx)["groups"] if g["reason"] == "accolade_match"]
    with pytest.raises(ValueError):
        A.decide(idx.con, "no-such-entry", rid, "confirm", "seth")


def test_reindex_and_removal_clear_dataset_rows(awarded):
    idx, albums = awarded
    A.refresh(idx.con)
    kleiber = next(a for a in albums if "Kleiber" in (a.artist or "") or any("Kleiber" in t.artist for t in a.tracks))
    n = idx.con.execute("SELECT COUNT(*) FROM accolade WHERE album_library_id=?", (kleiber.library_album_id,))
    assert n.fetchone()[0] == 1
    run(idx.index_album(kleiber, force=True))
    assert idx.con.execute("SELECT COUNT(*) FROM accolade WHERE album_library_id=?",
                           (kleiber.library_album_id,)).fetchone()[0] == 0
    A.refresh(idx.con)
    idx.remove_album(kleiber.library_album_id)
    assert idx.con.execute("SELECT COUNT(*) FROM accolade WHERE album_library_id=?",
                           (kleiber.library_album_id,)).fetchone()[0] == 0


def test_recording_page_reviews_ratings_links(awarded):
    idx, _ = awarded
    A.refresh(idx.con)
    v = views.work(idx, _w5(idx)["id"])
    r = next(x for x in v["recordings"] if x["who"].startswith("Carlos Kleiber"))
    aid = r["album_id"]
    now = time.time()
    res = A.NetResult(links=[("discogs", "https://www.discogs.com/release/123"),
                             ("allmusic", "https://www.allmusic.com/album/mw0000")],
                      sources={"musicbrainz", "critiquebrainz", "discogs"}, rows=[
        {"id": "cb1", "kind": "review", "publication": "CritiqueBrainz", "reviewer": "ann", "rating": 5,
         "rating_scale": 5, "url": "https://critiquebrainz.org/review/x", "source": "critiquebrainz",
         "source_ref": "x", "excerpt": "Electric.", "excerpt_license": "CC BY-SA 3.0", "fetched_at": now},
        {"id": "dg1", "kind": "rating", "publication": "Discogs", "rating": 4.6, "rating_scale": 5,
         "rating_count": 40, "url": "https://www.discogs.com/release/123", "source": "discogs", "source_ref": "123",
         "fetched_at": now, "expires_at": now + 60}])
    A.store_net(idx.con, aid, res)
    p = views.recording(idx, r["id"])
    assert [x["excerpt"] for x in p["reviews"]] == ["Electric."] and p["reviews"][0]["counts"]   # single recording
    assert p["ratings"][0]["text"] == "Discogs 4.6/5" and p["acclaim"] == 2.5
    sites = {x["site"]: x for x in p["review_links"]}
    assert sites["Discogs"]["kind"] == "page" and sites["AllMusic"]["url"] == "https://www.allmusic.com/album/mw0000"
    assert "search_query=Beethoven+Symphony+No.+5" in sites["Presto Music"]["url"]
    assert "Kleiber" in sites["Presto Music"]["url"]
    assert sites["Gramophone"]["url"].startswith("https://duckduckgo.com/?q=site%3Agramophone.co.uk+")
    # Discogs data expires (API terms): hidden after its 6 hours
    assert [x["source"] for x in A.for_recordings(idx.con, [{"id": r["id"], "album_library_id": aid}],
                                                  now=now + 120)[r["id"]] if x["kind"] == "rating"] == []
    # album with two recordings: network accolades are for the album and never count
    two = next(x for x in idx.con.execute("SELECT album_library_id, COUNT(*) n FROM recording GROUP BY 1") if x[1] > 1)
    A.store_net(idx.con, two[0], A.NetResult(sources={"wikidata"}, rows=[
        {"id": "wd1", "kind": "award", "award": "Edison Award", "year": 1970, "result": "winner",
         "url": "https://www.wikidata.org/wiki/Q1", "source": "wikidata", "source_ref": "Q1/P166/Q2",
         "fetched_at": now}]))
    rid2 = idx.con.execute("SELECT id FROM recording WHERE album_library_id=? LIMIT 1", (two[0],)).fetchone()[0]
    [b] = views.recording(idx, rid2)["awards"]
    assert b["scope"] == "album" and not b["counts"] and b["note"] == "for the album"
    out = views.accolades(idx, work_id=_w5(idx)["id"])
    assert {x["id"] for x in out["recordings"]} == {x["id"] for x in v["recordings"]} and out["best"]
    with pytest.raises(ValueError):
        views.accolades(idx)


def test_album_ids_mark_albums_due_for_enrichment(full):  # noqa: F811
    idx, albums = full
    aid = albums[0].library_album_id
    assert any(a["album_library_id"] == aid for a in A.album_for_enrichment(idx.con, limit=100))
    A.store_net(idx.con, aid, A.NetResult(sources={"musicbrainz"}))
    assert not any(a["album_library_id"] == aid for a in A.album_for_enrichment(idx.con, limit=100))
    assert A.set_album_ids(idx.con, aid, "DG", None, "0b5d1f9c-2d1e-4b44-a1a1-4f4a1d1b2c3d", None, None)
    assert not A.set_album_ids(idx.con, aid, "DG", None, "0b5d1f9c-2d1e-4b44-a1a1-4f4a1d1b2c3d", None, None)
    assert any(a["album_library_id"] == aid for a in A.album_for_enrichment(idx.con, limit=100))


# ---- network parsers and the fetch ---------------------------------------------------------------------------

RG = "11111111-2222-3333-4444-555555555555"
REL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeClient:
    def __init__(self, pages, base_url=""):
        self.pages, self.base_url, self.calls = pages, base_url, []

    async def get(self, url):
        self.calls.append(url)
        for k, v in self.pages.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        raise MBError("404")


def test_parsers():
    rels = {"relations": [
        {"target-type": "url", "type": "discogs", "url": {"resource": "https://www.discogs.com/master/9"}},
        {"target-type": "url", "type": "discogs", "url": {"resource": "https://www.discogs.com/release/77-x"}},
        {"target-type": "url", "type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q42"}},
        {"target-type": "artist", "type": "producer"}]}
    links = A.parse_url_rels(rels)
    assert len(links) == 3 and A.discogs_release_id(links) == "77" and A.wikidata_qid(links) == "Q42"
    wd = {"entities": {"Q42": {"claims": {"P166": [
        {"mainsnak": {"datavalue": {"value": {"id": "Q500"}}},
         "qualifiers": {"P585": [{"datavalue": {"value": {"time": "+1964-00-00T00:00:00Z"}}}]}},
        {"mainsnak": {"snaktype": "somevalue"}}]}}}}
    assert A.parse_wikidata_awards(wd, "Q42") == [("Q500", 1964)]
    assert A.parse_wikidata_labels({"entities": {"Q500": {"labels": {"en": {"value": "Grammy Award"}}}}}) == {
        "Q500": "Grammy Award"}
    long = "word " * 200
    cb = A.parse_critiquebrainz({"reviews": [
        {"id": "x1", "rating": 4, "text": "**Great** " + long, "license": {"full_name": "CC BY-SA 3.0"},
         "user": {"display_name": "ann"}, "published_on": "Tue, 02 Jan 2018"},
        {"id": "x2", "is_hidden": True, "text": "hidden"}]})
    assert len(cb) == 1 and len(cb[0]["text"]) <= A.EXCERPT_CHARS + 1 and cb[0]["text"].endswith("…")
    assert cb[0]["license"] == "CC BY-SA 3.0" and cb[0]["url"] == "https://critiquebrainz.org/review/x1"
    assert A.parse_discogs({"community": {"rating": {"average": 4.31, "count": 12}}}) == (4.31, 12)
    assert A.parse_discogs({}) is None


def test_fetch_album_uses_each_enabled_source():
    mb = FakeClient({f"/release/{REL}": {"release-group": {"id": RG}, "relations": []},
                     f"/release-group/{RG}": {"relations": [
                         {"target-type": "url", "type": "wikidata",
                          "url": {"resource": "https://www.wikidata.org/wiki/Q42"}},
                         {"target-type": "url", "type": "discogs",
                          "url": {"resource": "https://www.discogs.com/release/77"}}]}},
                    base_url="https://musicbrainz.org/ws/2")
    wd = FakeClient({"EntityData/Q42": {"entities": {"Q42": {"claims": {"P166": [
        {"mainsnak": {"datavalue": {"value": {"id": "Q500"}}}}]}}}},
        "wbgetentities": {"entities": {"Q500": {"labels": {"en": {"value": "Edison Award"}}}}}})
    cb = FakeClient({"critiquebrainz": {"reviews": [{"id": "x1", "rating": 5, "text": "Fine."}]}})
    dg = FakeClient({"releases/77": {"community": {"rating": {"average": 4.5, "count": 9}}}})
    album = {"album_library_id": "7", "release": REL, "mb_release": None, "mb_release_group": None,
             "discogs_release": None}
    res = asyncio.run(A.fetch_album(album, mb=mb, wikidata=wd, critiquebrainz=cb, discogs=dg))
    assert res.release_group == RG and len(res.links) == 2
    assert sorted(r["kind"] for r in res.rows) == ["award", "rating", "review"]
    award = next(r for r in res.rows if r["kind"] == "award")
    assert award["award"] == "Edison Award" and award["year"] is None
    rating = next(r for r in res.rows if r["kind"] == "rating")
    assert rating["expires_at"] - rating["fetched_at"] == A.DISCOGS_TTL
    assert res.sources == {"musicbrainz", "wikidata", "critiquebrainz", "discogs"}
    # sources off: nothing asked; MB only
    res = asyncio.run(A.fetch_album(album, mb=mb))
    assert res.rows == [] and res.sources == {"musicbrainz"}
    # a transient failure propagates (the album is retried later); a 404 is just no data
    with pytest.raises(MBTransientError):
        asyncio.run(A.fetch_album(album, mb=FakeClient({"/release/": MBTransientError("503")})))
    res = asyncio.run(A.fetch_album(album, mb=FakeClient({})))
    assert res.links == [] and res.sources == {"musicbrainz"}


def test_review_links_are_searches_without_mb_pages():
    links = A.review_links("Ludwig van Beethoven", "Fidelio, Op. 72", "Otto Klemperer")
    sites = [x["site"] for x in links]
    assert sites == ["Presto Music", "Gramophone", "MusicWeb International", "AllMusic", "Discogs"]
    assert all(x["url"].startswith("https://") and x["kind"] == "search" for x in links)
    assert "search_query=Beethoven+Fidelio+Klemperer" in links[0]["url"]


# ---- review fixes: wrong-award cases ------------------------------------------------------------------------

KARAJAN_BPO = (("Herbert von Karajan", "conductor"), ("Berliner Philharmoniker", "orchestra"))


def test_a_remake_by_the_same_artists_for_the_same_label_is_no_other_release():
    # Diapason 1985 for Karajan's digital Beethoven 5; the library holds his 1977 recording (also DG), on a 1995 CD
    e = entry(award="Diapason d'Or de l'Année", year=1985, work="Symphony No. 5",
              performers=("Herbert von Karajan", "Berlin Philharmonic"), label="Deutsche Grammophon", category=None)
    remake = facts(titles=["Symphony No. 5 in C minor, Op. 67"], recorded=1977, released=1995,
                   credits=KARAJAN_BPO, label="Deutsche Grammophon")
    assert match(e, remake) == "uncertain"
    # the awarded session itself, reissued later, still is
    remake.recorded = 1984
    assert match(e, remake) == "other_release"


def test_an_old_session_reissued_inside_the_window_is_not_the_awarded_release():
    e = entry(award="Gramophone Award", year=1985, work="Symphony No. 5",
              performers=("Herbert von Karajan", "Berlin Philharmonic"), category="Orchestral")
    reissue = facts(titles=["Symphony No. 5 in C minor, Op. 67"], recorded=1977, released=1984, credits=KARAJAN_BPO)
    assert match(e, reissue) == "uncertain"
    # a historical category is about old sessions: no cap there
    assert match(entry(award="Gramophone Award", year=1985, work="Symphony No. 5", historical=True,
                       performers=("Herbert von Karajan", "Berlin Philharmonic"), category="Historical"),
                 reissue) == "this_release"


def test_an_uncredited_other_conductor_in_an_artist_string_needs_review():
    e = entry(year=1977, composer="Brahms", work="Violin Concerto", category="Best Classical Instrumental Soloist",
              performers=("Itzhak Perlman", "Carlo Maria Giulini", "Chicago Symphony Orchestra"))
    f = facts(titles=["Violin Concerto in D major, Op. 77"], composers=["Johannes Brahms"], released=1976,
              artists=["Itzhak Perlman / Chicago Symphony Orchestra / Daniel Barenboim"])
    assert match(e, f) == "uncertain"
    f = facts(titles=["Violin Concerto in D major, Op. 77"], composers=["Johannes Brahms"], released=1976,
              artists=["Itzhak Perlman / Chicago Symphony Orchestra / Carlo Maria Giulini"])
    assert match(e, f) == "this_release"


def test_album_entries_need_every_work_they_name():
    e = entry(work="Symphonies Nos. 5 & 7", performers=("Carlos Kleiber", "Vienna Philharmonic"), year=1976,
              award="Gramophone Award", category="Orchestral")
    credits = (("Carlos Kleiber", "conductor"), ("Wiener Philharmoniker", "orchestra"))

    def album(*titles):
        return {f"r{i}": facts(titles=[t], released=1975, credits=credits, rid=f"r{i}") for i, t in enumerate(titles)}

    assert A.match_all(album("Symphony No. 4 in B-flat major"), [e]) == []
    assert A.match_all(album("Symphony No. 5 in C minor", "Symphony No. 4 in B-flat major"), [e]) == []
    [m] = A.match_all(album("Symphony No. 5 in C minor", "Symphony No. 7 in A major", "Coriolan Overture"), [e])
    assert m.target == "album" and m.why["recordings"] == ["r0", "r1"]
    assert A.match_all(album("Symphony No. 9 in D minor"), [entry(
        work="Symphonies Nos. 1 & 3", performers=("Carlos Kleiber", "Vienna Philharmonic"), year=1976,
        award="Gramophone Award", category="Orchestral")]) == []
    # an unnumbered plural ('Symphonies') needs more than one of them; a second part must be there too
    cycle = entry(work="Symphonies", performers=("Carlos Kleiber", "Vienna Philharmonic"), year=1976,
                  award="Gramophone Award", category="Orchestral")
    assert A.match_all(album("Symphony No. 5 in C minor"), [cycle]) == []
    assert len(A.match_all(album("Symphony No. 5 in C minor", "Symphony No. 7 in A major"), [cycle])) == 1
    two = entry(work="Symphony No. 5; Egmont Overture", performers=("Carlos Kleiber", "Vienna Philharmonic"),
                year=1976, award="Gramophone Award", category="Orchestral")
    assert A.match_all(album("Symphony No. 5 in C minor"), [two]) == []


def test_dataset_album_badges_show_only_on_the_matched_recordings(full):  # noqa: F811
    idx, _ = full
    rec = dict(idx.con.execute("SELECT id, album_library_id FROM recording LIMIT 1").fetchone())
    for rid_list, aid in ((["someone-else"], "x1"), ([rec["id"]], "x2")):
        idx.con.execute(
            "INSERT INTO accolade(id, target, album_library_id, kind, award, year, result, source, source_ref, grade, "
            "why_json, fetched_at) VALUES (?, 'album', ?, 'award', 'Gramophone Award', 1976, 'winner', 'dataset', "
            "'e', 'this_release', ?, 0)", (aid, rec["album_library_id"], json.dumps({"recordings": rid_list})))
    got = A.for_recordings(idx.con, [rec])
    assert [a["id"] for a in got[rec["id"]]] == ["x2"]


def test_nominations_are_not_counted_as_awards_in_the_header():
    s = [{"accolades": [{"counts": True, "kind": "award", "award": "Grammy", "result": "nominee"}]},
         {"accolades": [{"counts": True, "kind": "award", "award": "Grammy", "result": "winner"}]}]
    assert views._acclaim_summary(s)["awards"] == [{"award": "Grammy", "n": 1}, {"award": "Grammy nominee", "n": 1}]


def test_mb_page_links_are_https_and_cb_ids_are_checked():
    links = A.review_links("Beethoven", "Fidelio", "Klemperer", [
        {"kind": "allmusic", "url": "http://www.allmusic.com/album/mw0001"},
        {"kind": "discogs", "url": "javascript:alert(1)"}])
    pages = [x for x in links if x["kind"] == "page"]
    assert pages == [{"site": "AllMusic", "kind": "page", "url": "https://www.allmusic.com/album/mw0001"}]
    assert any(x["site"] == "Discogs" and x["kind"] == "search" for x in links)
    assert A.parse_critiquebrainz({"reviews": [{"id": "../../x", "text": "t"}]}) == []


def test_bundled_dataset_has_no_wikitext_debris():
    import re
    es = A.entries()
    assert len(es) > 2000
    for e in es:
        for x in (e.composer, e.work, *e.performers):
            assert not re.search(r"''|[{}*|]|==|\[\[", x), (e.id, x)
        assert not (e.category or "").startswith("|"), e.id
