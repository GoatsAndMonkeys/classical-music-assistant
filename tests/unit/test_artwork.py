"""Album art from the Cover Art Archive (classical_core/artwork.py)."""

import asyncio
import itertools
import json

import pytest

from classical_core import artwork
from classical_core import release as mbrel
from classical_core.mb import MBClient, MBTransientError
from classical_core.store import SqliteMBCache, connect, migrate
from tests.mbfake import uid

REL, REL2, RG, RG2 = uid(1), uid(2), uid(11), uid(12)
MB = "https://musicbrainz.org/ws/2"


class FakeCAA:
    """HEAD answers by URL; anything unknown is a 404."""

    def __init__(self, found=(), status=None):
        self.found, self.status, self.calls = set(found), status or {}, []

    async def fetch(self, url, headers):
        self.calls.append((url, headers["User-Agent"]))
        if url in self.status:
            return self.status[url], {"Retry-After": "5"}
        if url in self.found:
            return 307, {"Location": "https://archive.org/download/x/x_thumb500.jpg"}
        return 404, {}

    def client(self, cache=None):
        return artwork.CAAClient("me@example.invalid", cache=cache, fetch=self.fetch, interval=0)


class FakeMB:
    def __init__(self, releases=None):
        self.releases, self.calls = releases or {}, []

    async def fetch(self, url, headers):
        self.calls.append(url)
        for mbid, body in self.releases.items():
            if f"/release/{mbid}?inc=release-groups" in url:
                return 200, json.dumps(body).encode()
        return 404, b""

    def client(self, cache=None):
        return MBClient("me@example.invalid", cache=cache, fetch=self.fetch, interval=0)


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "index.db")
    migrate(c)
    return c


def front(kind, mbid):
    return artwork.front_url(kind, mbid)


def test_front_url_and_image():
    assert front("release", REL) == f"https://coverartarchive.org/release/{REL}/front-500"
    assert front("release-group", RG).endswith(f"/release-group/{RG}/front-500")
    with pytest.raises(ValueError):
        artwork.front_url("artist", REL)
    assert artwork.image("u") == {"type": "thumb", "path": "u", "provider": "coverartarchive",
                                  "remotely_accessible": True}


def _search(*releases):
    return {"releases": [{"id": i, "score": s, "title": t, "release-group": {"id": g},
                          "artist-credit": [{"name": "Georg Solti", "artist": {"name": "Georg Solti"}}]}
                         for i, s, t, g in releases]}


def test_group_from_search_needs_one_group_and_a_strong_best_fit():
    name, artist = "Beethoven: Fidelio", "Georg Solti"
    assert artwork.group_from_search(_search((REL, 100, "Fidelio", RG), (REL2, 92, "Fidelio", RG)), name, artist) == RG
    # two pressings in two groups: no answer
    assert artwork.group_from_search(_search((REL, 100, "Fidelio", RG), (REL2, 95, "Fidelio", RG2)), name, artist) is None
    # a weaker other-title release does not count; a weak best fit is no answer
    assert artwork.group_from_search(_search((REL, 100, "Fidelio", RG), (REL2, 99, "Egmont", RG2)), name, artist) == RG
    assert artwork.group_from_search(_search((REL, 93, "Fidelio", RG)), name, artist) is None
    # the artist must be credited
    assert artwork.group_from_search(_search((REL, 100, "Fidelio", RG)), name, "Karajan") is None
    assert artwork.group_from_search({}, name, artist) is None


def test_caa_client_caches_answers_and_defers_on_errors(con):
    caa = FakeCAA(found={front("release", REL)}, status={front("release", REL2): 503})
    client = caa.client(SqliteMBCache(con, table="web_cache"))
    assert asyncio.run(client.exists(front("release", REL))) is True
    assert asyncio.run(client.exists(front("release-group", RG))) is False
    assert asyncio.run(client.exists(front("release", REL))) is True        # cached: no second request
    assert asyncio.run(client.exists(front("release-group", RG))) is False
    assert [u for u, _ in caa.calls] == [front("release", REL), front("release-group", RG)]
    assert all("me@example.invalid" in ua for _, ua in caa.calls)
    with pytest.raises(MBTransientError):
        asyncio.run(client.exists(front("release", REL2)))
    assert con.execute("SELECT COUNT(*) FROM web_cache WHERE url=?", (front("release", REL2),)).fetchone()[0] == 0
    rows = dict(con.execute("SELECT url, expires_at - fetched_at FROM web_cache").fetchall())
    assert round(rows[front("release", REL)]) == artwork.FOUND_TTL
    assert round(rows[front("release-group", RG)]) == artwork.MISSING_TTL


def test_caa_client_network_error_is_transient():
    async def boom(url, headers):
        raise OSError("down")

    with pytest.raises(MBTransientError):
        asyncio.run(artwork.CAAClient("me", fetch=boom, interval=0).exists(front("release", REL)))


def test_caa_client_spaces_requests():
    times = []

    async def fetch(url, headers):
        times.append(asyncio.get_running_loop().time())
        return 404, {}

    async def run():
        c = artwork.CAAClient("me", fetch=fetch, interval=0.2)
        await asyncio.gather(*(c.exists(front("release", uid(i))) for i in range(3)))

    asyncio.run(run())
    assert all(b - a >= 0.19 for a, b in itertools.pairwise(times))


def _album(**kw):
    return {"album_library_id": "1", "name": "Beethoven: Fidelio", "artist": "Georg Solti", "release_mbid": None,
            "mb_release": None, "mb_release_group": None, **kw}


def test_find_prefers_the_release_cover():
    caa = FakeCAA(found={front("release", REL), front("release-group", RG)})
    art = asyncio.run(artwork.find(_album(release_mbid=REL), caa.client(), FakeMB().client()))
    assert art == artwork.Art(front("release", REL), REL, None, True)
    assert len(caa.calls) == 1


def test_find_falls_back_to_the_release_group_of_the_release():
    caa, mb = FakeCAA(found={front("release-group", RG)}), FakeMB({REL: {"id": REL, "release-group": {"id": RG}}})
    art = asyncio.run(artwork.find(_album(mb_release=REL), caa.client(), mb.client()))
    assert art == artwork.Art(front("release-group", RG), REL, RG, True)
    assert mb.calls == [artwork.release_group_url(MB, REL)]
    # MA's own release group is used without asking MusicBrainz
    mb2 = FakeMB()
    art = asyncio.run(artwork.find(_album(mb_release=REL, mb_release_group=RG), caa.client(), mb2.client()))
    assert art.release_group == RG and mb2.calls == []


def test_find_without_any_cover_or_ids():
    caa = FakeCAA()
    assert asyncio.run(artwork.find(_album(release_mbid=REL), caa.client(), FakeMB().client())) is None
    assert asyncio.run(artwork.find(_album(name="Unknown", artist="Various Artists"), caa.client(), None)) is None
    assert asyncio.run(artwork.find(_album(release_mbid="not-an-mbid"), caa.client(), None)) is None


def test_find_reads_the_release_search_from_the_cache_only(con):
    cache = SqliteMBCache(con)
    mb = FakeMB().client(cache)
    caa = FakeCAA(found={front("release-group", RG)})
    album = _album()
    # not searched yet: no request at all
    assert asyncio.run(artwork.find(album, caa.client(), mb)) is None
    assert caa.calls == []
    url = mbrel.search_url(MB, album["name"], album["artist"])
    asyncio.run(cache.put(url, 200, json.dumps(_search((REL, 100, "Fidelio", RG))).encode(), 3600))
    art = asyncio.run(artwork.find(album, caa.client(), mb))
    assert art == artwork.Art(front("release-group", RG), None, RG, False)   # picture only: never MA's id


def test_due_lists_albums_without_an_image_in_id_order(con):
    for aid, img in (("2", None), ("10", None), ("3", '{"path": "x"}'), ("4", None)):
        con.execute("INSERT INTO album_info(album_library_id, name, artist, image_json) VALUES (?,?,?,?)",
                    (aid, "A", "B", img))
    assert [r["album_library_id"] for r in artwork.due(con, None, 10)] == ["10", "2", "4"]
    assert [r["album_library_id"] for r in artwork.due(con, "10", 1)] == ["2"]
    artwork.store_image(con, "2", "https://caa/x")
    artwork.store_image(con, "3", "https://caa/y")                             # an image MA gave is kept
    assert json.loads(con.execute("SELECT image_json FROM album_info WHERE album_library_id='2'").fetchone()[0]) == \
        artwork.image("https://caa/x")
    assert con.execute("SELECT image_json FROM album_info WHERE album_library_id='3'").fetchone()[0] == '{"path": "x"}'
