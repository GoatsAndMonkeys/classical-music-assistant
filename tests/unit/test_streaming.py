"""Streaming albums in the library (PLAN.md §5.10): service credits, barcode/ISRC matching, provider badges."""

import asyncio
import json
import re

from classical_core import views
from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.store import connect, migrate
from classical_core.streaming import match_search, provider_facts, search_query, source
from classical_core.tags import TagInfo
from tests.mbfake import FakeMB, uid

MVTS = ["I. Allegro con brio", "II. Andante con moto", "III. Allegro", "IV. Allegro"]
WORK = "Symphony No. 5 in C Minor, Op. 67"


def run(coro):
    return asyncio.run(coro)


# ---- what each service's `performers` says ----

def test_qobuz_credits_give_composer_and_performers():
    info = provider_facts("qobuz", [
        "Ludwig van Beethoven, Composer", "Carlos Kleiber, Conductor", "Wiener Philharmoniker, Orchestra",
        "Hans Weber, Recording Engineer", "Werner Mayer, Producer"],
        ["Carlos Kleiber", "Wiener Philharmoniker"])
    assert info.composers == ["Ludwig van Beethoven"]
    assert {(p.name, p.role, p.kind) for p in info.performers} == {
        ("Carlos Kleiber", "conductor", "person"), ("Wiener Philharmoniker", "orchestra", "group")}


def test_qobuz_hyphen_damage_keeps_only_confirmed_names():
    # MA splits 'Jean-Efflam Bavouzet, Piano' and 'Camille Saint-Saëns, Composer' on '-'
    info = provider_facts("qobuz", ["Jean", "Efflam Bavouzet, Piano", "Camille Saint", "Saëns, Composer",
                                    "Somebody Else, Piano"], ["Jean-Efflam Bavouzet"])
    assert [p.name for p in info.performers] == ["Jean-Efflam Bavouzet"]
    assert info.composers == ["Camille Saint-Saëns"]


def test_apple_composer_field_and_spotify_artists():
    apple = provider_facts("apple_music", ["Wolfgang Amadeus Mozart", "Lorenzo Da Ponte"], ["Karl Böhm"])
    assert apple.composers == ["Wolfgang Amadeus Mozart"] and [p.name for p in apple.performers] == ["Karl Böhm"]
    assert provider_facts("apple_music", ["A. Nobody", "B. Nobody"], []) is None
    spot = provider_facts("spotify", [], ["Ludwig van Beethoven", "Carlos Kleiber", "Wiener Philharmoniker"])
    assert spot.composers == [] and {p.name for p in spot.performers} == {"Carlos Kleiber", "Wiener Philharmoniker"}
    assert provider_facts("spotify", [], []) is None


def test_source_badges():
    assert source("qobuz--x1") == {"instance": "qobuz--x1", "domain": "qobuz", "name": "Qobuz", "streaming": True}
    assert source("filesystem_smb--nas")["streaming"] is False


# ---- indexing a streaming album next to a local one ----

class StreamMB(FakeMB):
    def __init__(self):
        super().__init__()
        self.barcode: dict = {"releases": []}
        self.releases: dict[str, dict] = {}
        self.isrcs: dict[str, list[dict]] = {}

    async def fetch(self, url, headers):
        if "/release?" in url:
            self.calls.append(url)
            return 200, json.dumps(self.barcode if "barcode" in url else {"releases": []}).encode()
        m = re.search(r"/release/([0-9a-f-]{36})\?", url)
        if m:
            self.calls.append(url)
            r = self.releases.get(m.group(1))
            return (200, json.dumps(r).encode()) if r else (404, b"{}")
        m = re.search(r"/isrc/([A-Z0-9]+)\?", url)
        if m:
            self.calls.append(url)
            recs = self.isrcs.get(m.group(1))
            return (200, json.dumps({"recordings": recs}).encode()) if recs is not None else (404, b"{}")
        return await super().fetch(url, headers)


def new_index(tmp_path, mb=None, tags=None):
    con = connect(tmp_path / "index.db")
    migrate(con)
    tags = {} if tags is None else tags
    return Index(con, mb.client() if mb else None, tag_reader=lambda p: tags[p])


def local_album(tags):
    tracks = []
    for i, m in enumerate(MVTS):
        p = f"kleiber/{i + 1}.flac"
        tags[p] = TagInfo(title=f"{WORK}: {m}", composers=["Ludwig van Beethoven"],
                          performers=[], album="Beethoven 5 & 7")
        tracks.append(TrackIn(f"L{i}", 1, i + 1, f"{WORK}: {m}", "Carlos Kleiber", 400 + i,
                              mappings=[MappingIn("filesystem_local", p, fingerprint="f", path=p)]))
    return AlbumIn("A-local", "Beethoven 5 & 7", "Carlos Kleiber", 1975, None, tracks)


def qobuz_album(barcode=None, isrcs=None, aid="A-qobuz"):
    tracks = [TrackIn(f"Q{i}", 1, i + 1, f"Beethoven: {WORK}: {m}", "Paavo Järvi", 300 + i,
                      mappings=[MappingIn("qobuz--abc", f"q{i}", path=None, domain="qobuz")],
                      isrcs=[isrcs[i]] if isrcs else [],
                      performers=["Ludwig van Beethoven, Composer", "Paavo Järvi, Conductor",
                                  "Die Deutsche Kammerphilharmonie Bremen, Orchestra"],
                      artists=["Paavo Järvi", "Die Deutsche Kammerphilharmonie Bremen"])
              for i, m in enumerate(MVTS)]
    return AlbumIn(aid, "Beethoven: Symphonies Nos. 5 & 7", "Paavo Järvi", 2009, None, tracks,
                   barcodes=[barcode] if barcode else [])


def test_streaming_and_local_recordings_share_one_work(tmp_path):
    tags = {}
    ix = new_index(tmp_path, tags=tags)
    run(ix.index_album(local_album(tags)))
    run(ix.index_album(qobuz_album()))
    [w] = [w for w in ix.works() if "Symphony No. 5" in w["title"]]
    recs = views.work(ix, w["id"])["recordings"]
    assert len(recs) == 2
    by = {tuple(s["name"] for s in r["sources"]): r for r in recs}
    assert set(by) == {("Local files",), ("Qobuz",)}
    assert "Järvi" in by[("Qobuz",)]["who"] and "Kleiber" in by[("Local files",)]["who"]

    # a user who may not see the Qobuz account sees only the local recording
    mine = views.work(ix, w["id"], vis={"filesystem_local"})["recordings"]
    # ... with no badges, as a library with only local files always has
    assert [("Kleiber" in r["who"], r.get("sources")) for r in mine] == [(True, None)]


def test_a_streaming_track_without_a_composer_is_not_guessed(tmp_path):
    ix = new_index(tmp_path)
    t = TrackIn("S1", 1, 1, "Symphony No. 5 in C Minor, Op. 67: I. Allegro con brio", "Somebody", 400,
                mappings=[MappingIn("spotify--s", "s1", path=None, domain="spotify")],
                performers=[], artists=["Somebody"])
    run(ix.index_album(AlbumIn("A-s", "Symphonies", "Somebody", None, None, [t])))
    assert not [w for w in ix.works() if "Symphony No. 5" in w["title"]]


def _beethoven_mb():
    mb = StreamMB()
    sym = mb.work(uid(10), WORK, type="Symphony", composer="Ludwig van Beethoven")
    for i in range(4):
        mb.work(uid(20 + i), f"{WORK}: {MVTS[i]}", parent=sym, order=i + 1, composer="Ludwig van Beethoven")
        mb.recording(uid(100 + i), MVTS[i], uid(20 + i), conductor="Paavo Järvi")
    return mb


def _release(mbid, rec_base, barcode="0886975965221"):
    return {"id": mbid, "barcode": barcode, "media": [{"tracks": [
        {"position": i + 1, "length": 1000 * (300 + i), "recording": {"id": uid(rec_base + i)}} for i in range(4)]}]}


def test_barcode_links_a_streaming_album_to_its_release(tmp_path):
    mb = _beethoven_mb()
    mb.barcode = {"releases": [{"id": uid(1), "barcode": "0886975965221", "media": [{"track-count": 4}]},
                               {"id": uid(2), "barcode": "999", "media": [{"track-count": 4}]}]}
    mb.releases[uid(1)] = _release(uid(1), 100)
    ix = new_index(tmp_path, mb)
    run(ix.index_album(qobuz_album(barcode="886975965221")))       # UPC-A for the EAN on MB
    [w] = [w for w in ix.works() if w["title"] == WORK]
    assert w["identity_tier"] == "mb"
    assert any("barcode" in u for u in mb.calls) and not any(f"/release/{uid(2)}" in u for u in mb.calls)


def test_releases_sharing_a_barcode_that_disagree_go_to_review(tmp_path):
    mb = _beethoven_mb()
    for i in (100, 200):
        for j in range(4):
            mb.recs.setdefault(uid(i + j), mb.recs.get(uid(100 + j)))
    mb.barcode = {"releases": [{"id": uid(n), "barcode": "0886975965221", "media": [{"track-count": 4}]}
                               for n in (1, 2)]}
    mb.releases[uid(1)] = _release(uid(1), 100)
    mb.releases[uid(2)] = _release(uid(2), 200)
    ix = new_index(tmp_path, mb)
    run(ix.index_album(qobuz_album(barcode="0886975965221")))
    items = [v for v in ix.review() if v["reason"] == "MusicBrainz release match uncertain"]
    assert items and items[0]["detail"]["why"] == "releases with this barcode disagree"


def test_isrc_gives_the_recording_and_an_ambiguous_isrc_is_reviewed(tmp_path):
    mb = _beethoven_mb()
    codes = [f"DEX12090000{i}" for i in range(4)]
    for i, c in enumerate(codes):
        mb.isrcs[c] = [{"id": uid(100 + i), "length": 1000 * (300 + i)}]
    mb.recording(uid(300), "other", uid(10))                      # another work's recording, same length
    mb.isrcs[codes[3]].append({"id": uid(300), "length": 303_000})
    ix = new_index(tmp_path, mb)
    run(ix.index_album(qobuz_album(isrcs=codes)))
    assert any("/isrc/" in u for u in mb.calls)
    # three tracks have one recording each; the fourth's ISRC names two, so it keeps only its title
    # (its title still names the listed Symphony No. 5, so it joins the work, but not the recording)
    [w] = [w for w in ix.works() if w["title"] == WORK and w["identity_tier"] == "mb"]
    movements = sorted([t["movement_no"] for t in ix.recording_tracks(r["id"])] for r in ix.recordings(w["id"]))
    assert movements == [[1, 2, 3], [4]]
    items = [v for v in ix.review() if v["reason"] == "MusicBrainz ISRC match uncertain"]
    assert len(items) == 1 and items[0]["detail"]["recordings"] == sorted([uid(103), uid(300)])


def test_local_albums_never_query_barcodes_or_isrcs(tmp_path):
    mb = _beethoven_mb()
    tags = {}
    ix = new_index(tmp_path, mb, tags)
    a = local_album(tags)
    a.barcodes = ["0886975965221"]
    for t in a.tracks:
        t.isrcs = ["DEX120900001"]
    run(ix.index_album(a))
    assert not any("barcode" in u or "/isrc/" in u for u in mb.calls)


# ---- "Other recordings on Qobuz" ----

def _hit(item, title, album_uri, album, artists, year=2009):
    return {"provider_instance": "qobuz--abc", "item_id": item, "uri": f"qobuz://track/{item}", "title": title,
            "artists": artists, "duration": 400,
            "album": {"uri": album_uri, "name": album, "artists": artists, "year": year, "image": None}}


def test_search_results_are_grouped_by_album_and_filtered():
    assert search_query(WORK, "Ludwig van Beethoven") == f"Beethoven {WORK}"
    tracks = [
        _hit("1", f"Symphony No. 5 in C minor, Op. 67: {MVTS[0]}", "qobuz://album/a", "Beethoven 5", ["Ludwig van Beethoven", "Karajan"]),
        _hit("2", f"Symphony No. 5 in C minor, Op. 67: {MVTS[1]}", "qobuz://album/a", "Beethoven 5", ["Ludwig van Beethoven", "Karajan"]),
        _hit("3", "Symphony No. 5 in E minor, Op. 64: I. Andante", "qobuz://album/b", "Tchaikovsky 5", ["Tchaikovsky"]),
        _hit("4", "Symphony No. 5, Op. 67: I. Allegro con brio", "qobuz://album/c", "Beethoven: 5", ["Abbado"]),
        _hit("5", "Symphony No. 5, Op. 67: I. Allegro con brio", "qobuz://album/d", "Owned", ["Beethoven"]),
        _hit("9", "Fifth in a pop mix", "qobuz://album/e", "Pop", ["Beethoven"]),
    ]
    got = match_search(WORK, "Ludwig van Beethoven", tracks, {("qobuz--abc", "5")})
    # b is another composer's Op. 64, d is owned, e names no work; c names Beethoven only on its album
    assert [a["uri"] for a in got] == ["qobuz://album/a", "qobuz://album/c"]
    assert got[0]["tracks"] == 2 and got[0]["performers"] == ["Karajan"]


# ---- the composer prefix is dropped only when it names the track's own composer ----

def _run_album(ix, aid, titles, composer, domain="qobuz", tags=None, artist="Somebody"):
    tracks = []
    for i, t in enumerate(titles):
        if domain == "filesystem_local":
            p = f"{aid}/{i}.flac"
            tags[p] = TagInfo(title=t, composers=[composer], performers=[], album=aid)
            maps = [MappingIn("filesystem_local", p, fingerprint="f", path=p)]
            perf = []
        else:
            maps = [MappingIn(f"{domain}--x", f"{aid}{i}", path=None, domain=domain)]
            perf = [f"{composer}, Composer", f"{artist}, Conductor"]
        tracks.append(TrackIn(f"{aid}-{i}", 1, i + 1, t, artist, 300 + i, mappings=maps, performers=perf,
                              artists=[artist]))
    run(ix.index_album(AlbumIn(aid, aid, artist, None, None, tracks)))


def test_an_ambiguous_surname_prefix_is_dropped_when_it_is_the_tracks_composer(tmp_path):
    # 'Mozart' alone is ambiguous (Leopold, W. A.), but the track's own composer says which one is meant
    tags = {}
    ix = new_index(tmp_path, tags=tags)
    req = "Requiem in D Minor, K. 626"
    mv = ["I. Introitus: Requiem aeternam", "II. Kyrie", "III. Sequentia: Dies irae"]
    _run_album(ix, "loc", [f"{req}: {m}" for m in mv], "Wolfgang Amadeus Mozart", "filesystem_local", tags)
    _run_album(ix, "qob", [f"Mozart: {req}: {m}" for m in mv], "Wolfgang Amadeus Mozart")
    works = [w for w in ix.works() if "Requiem" in w["title"]]
    assert [w["title"] for w in works] == [req]
    assert len(ix.recordings(works[0]["id"])) == 2


def test_a_prefix_naming_another_composer_or_a_character_is_kept(tmp_path):
    ix = new_index(tmp_path)
    # Liszt's transcription: 'Beethoven' is not this track's composer, so it stays part of the title
    _run_album(ix, "liszt", [f"Beethoven: {WORK}: {m}" for m in MVTS[:3]], "Franz Liszt")
    # an opera's title character is not a composer
    _run_album(ix, "dg", ["Don Giovanni: Act 1: Overture", "Don Giovanni: Act 1: Notte e giorno faticar",
                          "Don Giovanni: Act 1: Madamina, il catalogo è questo"], "Wolfgang Amadeus Mozart")
    titles = {w["title"] for w in ix.works()}
    assert WORK not in titles and any(t.startswith("Beethoven: Symphony No. 5") for t in titles)
    assert any(t.startswith("Don Giovanni") for t in titles)


def test_a_performing_composer_is_not_made_a_performer_of_his_own_work():
    # Spotify: no roles, the composer is one of the track artists
    mahler = provider_facts("spotify", [], ["Gustav Mahler", "Claudio Abbado", "Berliner Philharmoniker"])
    assert {p.name for p in mahler.performers} == {"Claudio Abbado", "Berliner Philharmoniker"}
    lenny = provider_facts("spotify", [], ["Ludwig van Beethoven", "Leonard Bernstein", "Wiener Philharmoniker"])
    assert {p.name for p in lenny.performers} == {"Leonard Bernstein", "Wiener Philharmoniker"}
    # and a performer is never promoted to composer by the service facts themselves
    assert lenny.composers == [] and mahler.composers == []


def test_a_refused_barcode_query_falls_back_to_the_title_search(tmp_path):
    class Refusing(StreamMB):
        async def fetch(self, url, headers):
            if "/release?" in url and "barcode" in url:
                self.calls.append(url)
                return 400, b"{}"
            return await super().fetch(url, headers)

    mb = Refusing()
    ix = new_index(tmp_path, mb)
    run(ix.index_album(qobuz_album(barcode="0886975965221")))
    assert any("barcode" in u for u in mb.calls)
    assert any("/release?" in u and "barcode" not in u for u in mb.calls)
