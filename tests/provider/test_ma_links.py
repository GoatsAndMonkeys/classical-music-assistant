"""MA-frontend links (provider/classical/ml.py): artist matching, attaching, descriptions, commands."""

import asyncio
import json
import types
from unittest.mock import MagicMock

import pytest

from classical_core import views
from tests.unit.test_views import BPH, KARAJAN, WORK5, Lib

from .test_glue_web import mods  # noqa: F401 - fixture


@pytest.fixture
def ml(mods):  # noqa: F811
    return mods.ui.ma_links


def artist(item_id, name, image=None, mbid=None):
    img = types.SimpleNamespace(to_dict=lambda: image) if image else None
    ext = {("musicbrainz_artistid", mbid)} if mbid else set()
    return types.SimpleNamespace(item_id=str(item_id), name=name, image=img, uri=f"library://artist/{item_id}",
                                 favorite=False, external_ids=ext)


def refs(ml, *artists):
    out = []
    for a in artists:
        r = ml.artist_ref(a)
        r["keys"] = [ml.ident_key(a.name)]
        r["mbids"] = ml._mb_artist_ids(a)
        out.append(r)
    return out


@pytest.fixture
def lib(tmp_path):
    lib = Lib(tmp_path)
    lib.add(WORK5, [KARAJAN, BPH], recorded="1963")
    return lib


def test_match_by_name_key_prefers_an_image_then_the_exact_name(ml, lib):
    karajan = lib.pid("Herbert von Karajan")
    links = ml.match_artists(lib.ix, refs(ml, artist(9, "Herbert von Karajan"),
                                                artist(12, "HERBERT VON KARAJAN", image={"path": "k.jpg"}),
                                                artist(3, "Karajan")))
    assert links[karajan]["item_id"] == "12" and links[karajan]["image"] == {"path": "k.jpg"}
    assert "keys" not in links[karajan]
    links = ml.match_artists(lib.ix, refs(ml, artist(9, "Herbert von Karajan"), artist(4, "Herbert  von Karajan")))
    assert links[karajan]["item_id"] == "4", "same key and no image: lowest MA id"


def test_composer_matches_by_musicbrainz_id_first(ml, lib):
    cid = lib.ix._one("SELECT id FROM composer")[0]
    lib.ix.con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('composer', ?, 'mbid', ?)",
                       (cid, "1f9df192-a621-4f54-8850-2c5373b7eac9"))
    links = ml.match_artists(lib.ix, refs(ml, artist(5, "Ludwig van Beethoven"),
                                                artist(7, "Beethoven", mbid="1f9df192-a621-4f54-8850-2c5373b7eac9")))
    assert links[cid]["item_id"] == "7"


def test_attach_marks_composers_and_people_only(ml):
    res = {"work": {"id": "w_1", "name": "x", "composers": [{"id": "c_1", "name": "B"}]},
           "recordings": [{"id": "r_1", "credits": [{"id": "p_1", "name": "K"}, {"id": "p_2", "name": "M"}]}]}
    ml.attach(res, {"c_1": {"item_id": "5"}, "p_1": {"item_id": "9"}, "w_1": {"item_id": "0"}})
    assert res["work"]["composers"][0]["ma_artist"] == {"item_id": "5"}
    assert res["recordings"][0]["credits"][0]["ma_artist"] == {"item_id": "9"}
    assert "ma_artist" not in res["recordings"][0]["credits"][1] and "ma_artist" not in res["work"]


def test_artist_links_refresh_in_the_background_and_keep_the_last_map_on_error(ml, lib):
    now = [0.0]
    karajan = lib.pid("Herbert von Karajan")
    mass = MagicMock()
    listing = [[artist(9, "Herbert von Karajan")]]

    async def items():
        for a in listing[0]:
            yield a

    mass.music.artists.iter_library_items = items

    async def read(fn):
        return fn(lib.ix)

    links = ml.ArtistLinks(mass, read, MagicMock(), clock=lambda: now[0])

    async def go():
        first = await links.get()
        assert first[karajan]["item_id"] == "9"
        listing[0] = None                      # iterating fails: the old map stays
        now[0] += ml.TTL + 1
        await links.get()
        await links._task
        assert (await links.get())[karajan]["item_id"] == "9"
        await links.close()

    asyncio.run(go())


class FakeClient:
    def __init__(self, base, pages):
        self.base_url, self.pages, self.seen = base, pages, []

    def url(self, entity, mbid, inc):
        return f"{self.base_url}/{entity}/{mbid}?inc={inc}&fmt=json"

    async def get(self, url):
        self.seen.append(url)
        return self.pages[url]


MBID = "7f8c5e2a-0000-4000-8000-000000000067"


def clients(extract="The Symphony No. 5 in C minor, Op. 67, is a symphony.", kind="standard"):
    mb = FakeClient("https://musicbrainz.org/ws/2", {
        f"https://musicbrainz.org/ws/2/work/{MBID}?inc=url-rels&fmt=json": {"relations": [
            {"target-type": "url", "type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q155571"}}]}})
    wd = FakeClient("https://www.wikidata.org", {
        "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q155571&props=sitelinks&sitefilter=enwiki"
        "&format=json": {"entities": {"Q155571": {"sitelinks": {"enwiki": {"title": "Symphony No. 5 (Beethoven)"}}}}}})
    wp = FakeClient("https://en.wikipedia.org", {
        "https://en.wikipedia.org/api/rest_v1/page/summary/Symphony_No._5_%28Beethoven%29": {
            "type": kind, "title": "Symphony No. 5 (Beethoven)", "extract": extract,
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Symphony_No._5_(Beethoven)"}}}})
    return mb, wd, wp


def test_work_description_follows_curated_links_and_carries_the_licence(ml):
    out = asyncio.run(ml.work_description(MBID, *clients()))
    assert out["text"].startswith("The Symphony No. 5") and out["license"] == "CC BY-SA 4.0"
    assert out["url"] == "https://en.wikipedia.org/wiki/Symphony_No._5_(Beethoven)"
    assert out["wikidata"] == "https://www.wikidata.org/wiki/Q155571"
    assert asyncio.run(ml.work_description(MBID, *clients(kind="disambiguation"))) is None
    assert asyncio.run(ml.work_description("not-an-mbid", *clients())) is None
    long = "A sentence. " * 200
    assert len(asyncio.run(ml.work_description(MBID, *clients(extract=long)))["text"]) <= ml.MAX_EXTRACT


def _provider(mods, lib):  # noqa: F811
    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass, p.logger = MagicMock(), MagicMock()
    p.mass.metadata.compute_image_id = lambda prov, path: "ID"
    p._vis = lambda: None

    async def read(fn, *a):
        return fn(lib.ix, *a)

    p._read = read
    return p


def test_work_description_command(mods, lib):  # noqa: F811
    p = _provider(mods, lib)
    wid = lib.work_id(WORK5)
    assert asyncio.run(p.cmd_work_description(wid)) == {"work_id": wid, "description": None}, "no MB link"
    lib.ix.con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)", (wid, MBID))
    assert asyncio.run(p.cmd_work_description(wid))["description"] is None, "no MusicBrainz contact"
    mb, wd, wp = clients()
    p._mb, p._wiki = mb, {"wikidata": wd, "wikipedia": wp}
    assert asyncio.run(p.cmd_work_description(wid))["description"]["source"] == "Wikipedia"
    p._wiki["wikipedia"].pages.clear()          # a source failing: no description, no error
    assert asyncio.run(p.cmd_work_description(wid))["description"] is None
    with pytest.raises(mods.ui.InvalidDataError):
        asyncio.run(p.cmd_work_description("w_nothing"))


def test_performer_tracks_and_views_carry_ma_artists(mods, lib):  # noqa: F811
    p = _provider(mods, lib)
    karajan = lib.pid("Herbert von Karajan")
    in_rec = str(lib.ix._one("SELECT library_track_id FROM occurrence")[0])
    other = types.SimpleNamespace(item_id="999", provider_mappings=[], to_dict=lambda: {"item_id": "999"})
    used = types.SimpleNamespace(item_id=in_rec, provider_mappings=[], to_dict=lambda: {"item_id": in_rec})

    async def tracks(item_id):
        assert item_id == "9"
        return [used, other]

    p.mass.music.artists.get_library_artist_tracks = tracks
    p._artists = types.SimpleNamespace(get=lambda: _done({karajan: {"item_id": "9", "name": "Karajan"}}))
    res = asyncio.run(p.cmd_performer_tracks(karajan))
    assert res == {"artist": {"item_id": "9", "name": "Karajan"}, "tracks": [{"item_id": "999"}]}
    bph = lib.pid("Berliner Philharmoniker")
    assert asyncio.run(p.cmd_performer_tracks(bph)) == {"artist": None, "tracks": []}
    work = asyncio.run(p.cmd_work(lib.work_id(WORK5)))
    credits = {c["id"]: c for c in work["recordings"][0]["credits"]}
    assert credits[karajan]["ma_artist"]["item_id"] == "9" and "ma_artist" not in credits[bph]
    json.dumps(work)
    with pytest.raises(mods.ui.InvalidDataError):
        asyncio.run(p.cmd_performer_tracks("p_nobody"))


async def _done(value):
    return value


def test_views_still_build_without_links(lib):
    assert views.performer(lib.ix, lib.pid("Herbert von Karajan"))["works"][0]["recording_ids"]
