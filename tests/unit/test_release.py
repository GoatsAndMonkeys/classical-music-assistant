"""MusicBrainz release matching (PLAN.md §5.11 fallback): accepted only when unambiguous and confirmed."""

import asyncio
import json
import re

from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.release import pick_release, split_disc, title_keys, track_recordings, wanted
from classical_core.store import connect, migrate
from classical_core.tags import TagInfo
from tests.mbfake import FakeMB, uid

DORATI = [{"name": "Antal Doráti", "artist": {"name": "Antal Doráti", "sort-name": "Doráti, Antal"}}]


def rel(mbid, title="Der fliegende Holländer", score=100, media=(16, 16), credit=DORATI):
    return {"id": mbid, "score": score, "title": title, "artist-credit": credit,
            "media": [{"track-count": n} for n in media]}


def test_disc_markers_and_composer_prefixes():
    assert split_disc("Richard Wagner: Der Fliegende Holländer Disc 2") == ("Richard Wagner: Der Fliegende Holländer", 2)
    assert split_disc("Leonore - Fidelio (CD2)") == ("Leonore - Fidelio", 2)
    assert split_disc("CD 2") == ("CD 2", None)
    assert title_keys("Richard Wagner: Der Fliegende Holländer Disc 2") == {
        "richard wagner der fliegende hollander", "der fliegende hollander"}
    assert not wanted("Unknown Album (2/11/2009 10:42:23 PM)", "Various Artists")
    assert not wanted("Fidelio", "Various Artists")
    assert wanted("Fidelio", "Karl Böhm")


def test_one_disc_of_a_set_matches_its_medium():
    s = {"releases": [rel(uid(1))]}
    p = pick_release(s, "Richard Wagner: Der Fliegende Holländer Disc 2", "Antal Dorati", {1: 16})
    assert (p.mbid, p.why) == (uid(1), "ok")


def test_anything_short_of_certain_abstains():
    name, artist = "Wagner: Der fliegende Holländer", "Antal Dorati"
    whole = {1: 16, 2: 16}
    assert pick_release({"releases": [rel(uid(1)), rel(uid(2), score=93)]}, name, artist, whole).why == "ambiguous"
    assert pick_release({"releases": [rel(uid(1), score=92)]}, name, artist, whole).why == "low score"
    assert pick_release({"releases": [rel(uid(1), media=(16, 15))]}, name, artist, whole).why == "none"
    assert pick_release({"releases": [rel(uid(1), title="Tannhäuser")]}, name, artist, whole).why == "none"
    other = [{"name": "Georg Solti", "artist": {"name": "Georg Solti", "sort-name": "Solti, Georg"}}]
    assert pick_release({"releases": [rel(uid(1), credit=other)]}, name, artist, whole).why == "none"
    assert pick_release({"releases": [rel(uid(1)), rel(uid(2), score=60)]}, name, artist, whole).mbid == uid(1)


def test_track_lengths_must_confirm_the_release():
    media = [{"tracks": [{"position": i, "length": 200_000 + i * 1000, "recording": {"id": uid(100 + i)}}
                         for i in (1, 2, 3)]}]
    ours = [(f"t{i}", 1, i, 200 + i) for i in (1, 2, 3)]
    got, why = track_recordings({"media": media}, ours, None)
    assert why == "ok" and got == {f"t{i}": uid(100 + i) for i in (1, 2, 3)}
    wrong = [(f"t{i}", 1, i, 400) for i in (1, 2, 3)]
    assert track_recordings({"media": media}, wrong, None) == ({}, "track lengths do not match")
    assert track_recordings({"media": media}, [(f"t{i}", 1, i, 0) for i in (1, 2, 3)], None)[0] == {}


class ReleaseMB(FakeMB):
    def __init__(self):
        super().__init__()
        self.search: dict = {"releases": []}
        self.releases: dict[str, dict] = {}

    async def fetch(self, url, headers):
        if "/release?" in url:
            self.calls.append(url)
            return 200, json.dumps(self.search).encode()
        m = re.search(r"/release/([0-9a-f-]{36})\?", url)
        if m:
            self.calls.append(url)
            return (200, json.dumps(self.releases[m.group(1)]).encode()) if m.group(1) in self.releases else (
                404, b"{}")
        return await super().fetch(url, headers)


def index_album(tmp_path, mb, name, artist, titles, durations):
    con = connect(tmp_path / "index.db")
    migrate(con)
    tags = {f"p{i}": TagInfo(title=t) for i, t in enumerate(titles)}
    idx = Index(con, mb.client(), tag_reader=lambda p: tags[p])
    tracks = [TrackIn(f"T{i}", None, i + 1, t, artist, d, mappings=[MappingIn("filesystem_local", f"p{i}",
                                                                           fingerprint="f", path=f"p{i}")])
              for i, (t, d) in enumerate(zip(titles, durations, strict=True))]
    asyncio.run(idx.index_album(AlbumIn("A1", name, artist, None, None, tracks)))
    return idx


def test_a_confirmed_release_links_recordings_to_works(tmp_path):
    """Dorati Holländer disc 2 with no tags beyond titles: the release's recordings name the opera."""
    mb = ReleaseMB()
    opera = mb.work(uid(10), "Der fliegende Holländer", type="Opera")
    titles = ["Acht, wo weilt sie", "Senta! Willst du mich verdeben?", "Steuermann, lass die Wacht!"]
    for i in (1, 2, 3):
        mb.work(uid(20 + i), f"Der fliegende Holländer: No. {i}", parent=opera, order=i)
        mb.recording(uid(100 + i), titles[i - 1], uid(20 + i), conductor="Antal Doráti")
    mb.search = {"releases": [rel(uid(1), media=(3, 3))]}
    mb.releases[uid(1)] = {"id": uid(1), "media": [
        {"tracks": [{"position": i, "length": 1000 * (200 + i), "recording": {"id": uid(900 + i)}} for i in (1, 2, 3)]},
        {"tracks": [{"position": i, "length": 1000 * (300 + i), "recording": {"id": uid(100 + i)}} for i in (1, 2, 3)]}]}
    idx = index_album(tmp_path, mb, "Richard Wagner: Der Fliegende Holländer Disc 2", "Antal Dorati", titles,
                      [301, 302, 303])
    works = idx.works()
    assert [w["title"] for w in works] == ["Der fliegende Holländer"], works
    assert [x["tracks"] for x in idx.recordings(works[0]["id"])] == [3]
    search = next(u for u in mb.calls if "/release?" in u)
    assert "Der%20Fliegende%20Holl%C3%A4nder" in search and "Wagner" not in search


def test_an_ambiguous_release_search_is_listed_not_used(tmp_path):
    mb = ReleaseMB()
    mb.search = {"releases": [rel(uid(1), media=(2,)), rel(uid(2), media=(2,), score=98)]}
    idx = index_album(tmp_path, mb, "Der fliegende Holländer", "Antal Dorati", ["Ouvertüre", "Senta!"], [600, 300])
    assert idx.works() == []
    assert not any("/release/" in u for u in mb.calls)
    items = [v for v in idx.review() if v["reason"] == "MusicBrainz release match uncertain"]
    assert len(items) == 1 and items[0]["detail"]["releases"] == [uid(1), uid(2)]


def test_albums_with_mbids_or_no_artist_are_not_searched(tmp_path):
    mb = ReleaseMB()
    index_album(tmp_path, mb, "Tanglewood 7", "Various Artists", ["01"], [300])
    assert not any("/release" in u for u in mb.calls)


def test_only_tracks_whose_lengths_agree_are_linked():
    media = [{"tracks": [{"position": i, "length": 200_000, "recording": {"id": uid(100 + i)}} for i in range(1, 7)]}]
    ours = [(f"t{i}", 1, i, 200) for i in range(1, 6)] + [("t6", 1, 6, 420)]   # a bonus take in slot 6
    got, why = track_recordings({"media": media}, ours, None)
    assert why == "ok" and "t6" not in got and len(got) == 5


def test_discs_that_are_not_one_to_n_do_not_fit():
    s = {"releases": [rel(uid(1), media=(16, 16))]}
    assert pick_release(s, "Der fliegende Holländer", "Antal Dorati", {2: 16, 3: 16}).why == "none"


def _two_disc_mb():
    mb = ReleaseMB()
    opera = mb.work(uid(10), "Der fliegende Holländer", type="Opera")
    for i in (1, 2, 3, 4):
        mb.work(uid(20 + i), f"Der fliegende Holländer: No. {i}", parent=opera, order=i)
        mb.recording(uid(100 + i), f"No. {i}", uid(20 + i))
    mb.search = {"releases": [rel(uid(1), media=(2, 2))]}
    mb.releases[uid(1)] = {"id": uid(1), "media": [
        {"tracks": [{"position": i, "length": 1000 * (200 + i), "recording": {"id": uid(100 + i)}} for i in (1, 2)]},
        {"tracks": [{"position": i, "length": 1000 * (300 + i), "recording": {"id": uid(102 + i)}} for i in (1, 2)]}]}
    return mb


def _album(aid, name, base):
    return AlbumIn(aid, name, "Antal Dorati", None, None, [
        TrackIn(f"{aid}-{i}", None, i, f"Track {i}", "Antal Dorati", base + i,
                mappings=[MappingIn("filesystem_local", f"{aid}/{i}", fingerprint="f", path=f"{aid}/{i}")])
        for i in (1, 2)])


def _release_links(idx, aid):
    return {r[0] for r in idx.con.execute("SELECT entity_id FROM evidence WHERE entity='release_track' "
                                          "AND retracted_at IS NULL AND entity_id LIKE ?", (f"{aid}-%",))}


def test_two_discs_making_the_same_search_keep_their_own_matches(tmp_path):
    """'Disc 1' and 'Disc 2' search for the same release; indexing disc 2 must not retract disc 1's links."""
    mb = _two_disc_mb()
    con = connect(tmp_path / "index.db")
    migrate(con)
    idx = Index(con, mb.client(), tag_reader=lambda p: TagInfo(title=None))
    asyncio.run(idx.index_album(_album("A1", "Der fliegende Holländer Disc 1", 200)))
    asyncio.run(idx.index_album(_album("A2", "Der fliegende Holländer Disc 2", 300)))
    assert _release_links(idx, "A1") == {"A1-1", "A1-2"} and _release_links(idx, "A2") == {"A2-1", "A2-2"}
    # renamed to something MB does not know: the old match is retracted, not kept under the old search
    mb.search = {"releases": []}
    asyncio.run(idx.index_album(_album("A1", "Fliegende Holländer (my rip) Disc 1", 200), force=True))
    assert _release_links(idx, "A1") == set() and _release_links(idx, "A2") == {"A2-1", "A2-2"}
