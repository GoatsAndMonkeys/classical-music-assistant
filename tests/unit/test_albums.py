"""Albums by composer (MA discussion #5481): a compilation is listed under each composer it holds, on the
composer page, in the Albums view and in Browse; arrangers credited in COMPOSER are not composers."""

import pytest

from classical_core import browse as tree
from classical_core import views
from classical_core.index import AlbumIn, MappingIn, TrackIn
from classical_core.names import parse_names
from classical_core.tags import TagInfo
from tests.unit.test_index import Reader, new_index, run

MOZART, RIMSKY, BACH, HAYDN = ("Wolfgang Amadeus Mozart", "Nikolai Rimsky-Korsakov", "Johann Sebastian Bach",
                               "Joseph Haydn")


class Lib:
    def __init__(self, tmp_path):
        self.r = Reader()
        self.ix = new_index(tmp_path, mb=False, reader=self.r)

    def album(self, aid, name, year, items, provider="filesystem_local"):
        """`items`: (work title, COMPOSER tag values) per track."""
        tracks = []
        for i, (work, comps) in enumerate(items):
            path = f"{aid}/{i}"
            tracks.append(TrackIn(f"{aid}{i}", 1, i + 1, work, "", 300,
                                  mappings=[MappingIn(provider, path, fingerprint="f", path=path)]))
            self.r.tags[path] = TagInfo(title=work, work=work, composers=list(comps))
        run(self.ix.index_album(AlbumIn(aid, name, "Various", year, None, tracks)))

    def cid(self, name):
        return self.ix._one("SELECT id FROM composer WHERE name=?", (name,))["id"]


@pytest.fixture
def lib(tmp_path):
    lib = Lib(tmp_path)
    # a compilation: one composer per track, written the ways real tags write them
    lib.album("1", "Encores", 1999, [
        ("Serenade No. 13 in G major, K. 525", ["Mozart, W.A."]),
        ("The Tale of Tsar Saltan: Flight of the Bumblebee", ["Rimsky-Korsakov, Nikolai"]),
        ("Partita No. 2 in D minor, BWV 1004: Chaconne", ["Bach, J.S.; arr. Busoni"]),
    ])
    lib.album("2", "Mozart Symphonies", 1985, [("Symphony No. 40 in G minor, K. 550", ["Mozart, W.A."])])
    # a multi-value COMPOSER tag on one track: the album belongs to both
    lib.album("3", "A Haydn and Mozart Collection", 2010,
              [("Symphony No. 94 in G major, Hob. I:94", ["Haydn, Joseph / Mozart, W.A."])])
    return lib


def test_arrangers_in_a_composer_tag_are_not_composers():
    names = lambda raw: [c.name for c in parse_names(raw)]
    assert names("Rimsky-Korsakov") == ["Rimsky-Korsakov"]
    assert names("Mozart, W.A.") == ["W.A. Mozart"]
    assert names("Bach, J.S.; arr. Busoni") == ["J.S. Bach"]
    assert names("Bach / Arranged by Stokowski") == ["Bach"]
    assert names("Fauré / Busoni (arr.)") == ["Fauré"]
    assert names("Bach & Busoni (arranger)") == ["Bach"]
    assert names("Bach (arr. Busoni)") == ["Bach"]
    assert names("Bach, arr. Busoni") == ["Bach"]
    assert names("Mozart, W.A.; Haydn, Joseph") == ["W.A. Mozart", "Joseph Haydn"]


def test_compilation_composers(lib):
    names = [c["name"] for c in lib.ix.composers()]
    assert sorted(names) == sorted([BACH, HAYDN, MOZART, RIMSKY])     # no 'arr. Busoni'


def test_composer_albums_list_each_album_under_every_composer(lib):
    entries = lib.ix.composer_albums()
    assert [(e["composer"], e["name"]) for e in entries] == [
        (BACH, "Encores"), (HAYDN, "A Haydn and Mozart Collection"),
        (MOZART, "A Haydn and Mozart Collection"), (MOZART, "Encores"), (MOZART, "Mozart Symphonies"),
        (RIMSKY, "Encores")]
    enc = next(e for e in entries if e["composer"] == MOZART and e["name"] == "Encores")
    assert [c["name"] for c in enc["composers"]] == [BACH, MOZART, RIMSKY]
    assert enc["works"] == 1 and len(enc["recordings"]) == 1
    assert [e["name"] for e in lib.ix.composer_albums(lib.cid(MOZART))] == [
        "A Haydn and Mozart Collection", "Encores", "Mozart Symphonies"]
    assert lib.ix.composer_albums(lib.cid(RIMSKY), vis=["other_provider"]) == []


def test_albums_view_sorts(lib):
    by_c = views.albums(lib.ix)
    assert [(i["composer"]["name"], i["name"]) for i in by_c["items"]][:3] == [
        (BACH, "Encores"), (HAYDN, "A Haydn and Mozart Collection"), (MOZART, "A Haydn and Mozart Collection")]
    assert by_c["total"] == 6
    by_title = views.albums(lib.ix, sort="title")
    assert [i["name"] for i in by_title["items"]] == ["A Haydn and Mozart Collection", "Encores", "Mozart Symphonies"]
    assert by_title["items"][1]["composer"] is None and len(by_title["items"][1]["recordings"]) == 3
    assert [i["year"] for i in views.albums(lib.ix, sort="year")["items"]] == [1985, 1999, 2010]
    page = views.albums(lib.ix, composer_id=lib.cid(MOZART), sort="year")
    assert page["composer"]["name"] == MOZART
    assert [i["name"] for i in page["items"]] == ["Mozart Symphonies", "Encores", "A Haydn and Mozart Collection"]
    enc = page["items"][1]
    assert [r["title"] for r in enc["recordings"]] == ["Serenade No. 13 in G major, K. 525"]
    assert enc["recordings"][0]["play_uri"].startswith("classical://folder/works/")
    with pytest.raises(ValueError):
        views.albums(lib.ix, sort="nope")
    with pytest.raises(views.NotFound):
        views.albums(lib.ix, composer_id="c_missing")


def test_browse_albums_under_each_composer(lib):
    assert [n.name for n in tree.browse(lib.ix, "")] == ["Composers", "Works", "Albums", "Review"]
    for name in (BACH, MOZART, RIMSKY):
        cpath = f"composers/{lib.cid(name)}"
        folders = tree.browse(lib.ix, cpath)
        assert folders[-1].name == "Albums"
        albums = tree.browse(lib.ix, folders[-1].path)
        assert "Encores" in [a.name for a in albums]
        enc = next(a for a in albums if a.name == "Encores")
        assert enc.path.rsplit("/", 1)[0] == folders[-1].path
        [rec] = tree.browse(lib.ix, enc.path)
        assert rec.playable and rec.item_id.startswith("works/w_") and rec.path.startswith(enc.path + "/")
        assert tree.browse(lib.ix, rec.path)                 # the movements
    # the Albums root: sorted by composer, the compilation under each of them
    root = tree.browse(lib.ix, "albums")
    assert [(n.name, n.version.split(" · ")[0]) for n in root][:2] == [("Encores", BACH),
                                                                         ("A Haydn and Mozart Collection", HAYDN)]
    assert [n.name for n in root].count("Encores") == 3
    for bad in ("albums/x", f"composers/{lib.cid(RIMSKY)}/albums/a-2", f"composers/{lib.cid(RIMSKY)}/albums/2"):
        with pytest.raises(tree.NotFound):
            tree.browse(lib.ix, bad)
