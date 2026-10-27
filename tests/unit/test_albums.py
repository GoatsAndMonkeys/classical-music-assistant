"""Albums by composer (MA discussion #5481): a compilation is listed under each composer it holds, on the
composer page, in the Albums view and in Browse; arrangers credited in COMPOSER are not composers."""

import pytest

from classical_core import browse as tree
from classical_core import views
from classical_core.index import AlbumIn, Index, MappingIn, TrackIn, sort_key
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
        """`items`: (work title, COMPOSER tag values[, provider]) per track."""
        tracks = []
        for i, (work, comps, *prov) in enumerate(items):
            path = f"{aid}/{i}"
            tracks.append(TrackIn(f"{aid}{i}", 1, i + 1, work, "", 300,
                                  mappings=[MappingIn(prov[0] if prov else provider, path, fingerprint="f", path=path)]))
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


def test_a_composer_tag_of_arranger_credits_only_keeps_the_composer():
    """Nothing but an arranger credit in COMPOSER: the person stays the composer (as before arrangers were told
    apart), the credit still recorded as the arranger role; beside a composer the arranger is dropped."""
    def got(raw):
        return [(c.name, c.role_tag) for c in parse_names(raw)]
    assert got("Liszt (transcr.)") == [("Liszt", "arranger")]
    assert got("Ravel, Maurice (orchestrator)") == [("Maurice Ravel", "arranger")]
    assert got("arr. Busoni") == [("Busoni", "arranger")]
    assert got("J.S. Bach; arr. Busoni") == [("J.S. Bach", None)]
    assert got("Bach/Busoni") == [("Bach", None), ("Busoni", None)]
    assert got("Stokowski [Arranger]") == []        # an explicit role tag is not a composer, as before


def test_arranger_only_tags_still_index_a_composer(tmp_path):
    lib = Lib(tmp_path)
    lib.album("1", "Transcriptions", 2001, [("Schwanengesang, S. 560: Ständchen", ["Liszt (transcr.)"]),
                                            ("Partita No. 2 in D minor, BWV 1004: Chaconne", ["J.S. Bach; arr. Busoni"])])
    names = [c["name"] for c in lib.ix.composers()]
    assert len(names) == 2 and any("Liszt" in n for n in names) and any("Bach" in n for n in names)
    assert not any("Busoni" in n for n in names)


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


def test_albums_sort_by_composer_folds_accents(tmp_path):
    """Composer order is the composer list's (accents folded), not SQL byte order: 'Dvořák' before 'Dvorsky'."""
    lib = Lib(tmp_path)
    lib.album("1", "Ödön Encores", 2000, [("Humoresque", ["Dvorsky, Michal"])])
    lib.album("2", "Slavonic Dances", 2001, [("Slavonic Dance", ["Dvořák, Antonín"])])
    lib.album("3", "Overtures", 2002, [("Overture", ["Dvořák, Antonín"])])
    lib.album("4", "Zebra", 2003, [("Te Deum", ["Dvořák, Antonín"])])
    entries = lib.ix.composer_albums()
    assert [(e["composer"].split()[-1], e["name"]) for e in entries] == [
        ("Dvořák", "Overtures"), ("Dvořák", "Slavonic Dances"), ("Dvořák", "Zebra"), ("Dvorsky", "Ödön Encores")]
    # titles fold too: 'Ödön' with the Os
    assert [i["name"] for i in views.albums(lib.ix, sort="title")["items"]] == [
        "Ödön Encores", "Overtures", "Slavonic Dances", "Zebra"]


def test_albums_by_title_and_year_leave_out_a_hidden_provider(tmp_path):
    lib = Lib(tmp_path)
    # one compilation spread over two providers: a viewer of one sees only its recordings and composers
    lib.album("1", "Encores", 1999, [("Serenade No. 13 in G major, K. 525", ["Mozart, W.A."], "fs_a"),
                                     ("The Tale of Tsar Saltan: Flight of the Bumblebee", ["Rimsky-Korsakov, Nikolai"],
                                      "fs_b")])
    lib.album("2", "Mozart Symphonies", 1985, [("Symphony No. 40 in G minor, K. 550", ["Mozart, W.A."])], "fs_b")
    lib.album("3", "Abendmusik", None, [("Symphony No. 94 in G major, Hob. I:94", ["Haydn, Joseph"])], "fs_a")
    for sort in ("title", "year"):
        everything = views.albums(lib.ix, sort=sort)
        assert everything["total"] == 3
        mine = views.albums(lib.ix, vis=["fs_a"], sort=sort)
        assert mine["total"] == 2
        assert [i["name"] for i in mine["items"]] == (["Abendmusik", "Encores"] if sort == "title" else
                                                      ["Encores", "Abendmusik"])       # undated last
        enc = next(i for i in mine["items"] if i["name"] == "Encores")
        assert [c["name"] for c in enc["composers"]] == [MOZART]
        assert [r["title"] for r in enc["recordings"]] == ["Serenade No. 13 in G major, K. 525"]
        assert enc["works"] == 1 and enc["composer"] is None
    assert views.albums(lib.ix, vis=["fs_c"], sort="title") == {
        "total": 0, "offset": 0, "limit": 200, "sort": "title", "composer": None, "items": []}


def test_browse_albums_root_pages(lib, monkeypatch):
    monkeypatch.setattr(tree, "CAP", 2)
    pages = tree.browse(lib.ix, "albums")
    assert [(n.path, n.name) for n in pages] == [("albums/p1", "1–2"), ("albums/p2", "3–4"), ("albums/p3", "5–6")]
    seen = [n for p in pages for n in tree.browse(lib.ix, p.path)]
    assert [(n.version.split(" · ")[0], n.name) for n in seen] == [
        (BACH, "Encores"), (HAYDN, "A Haydn and Mozart Collection"), (MOZART, "A Haydn and Mozart Collection"),
        (MOZART, "Encores"), (MOZART, "Mozart Symphonies"), (RIMSKY, "Encores")]
    # each entry opens under its composer, the recordings of that composer's works only
    [rec] = tree.browse(lib.ix, seen[3].path)
    assert seen[3].path.startswith(f"composers/{lib.cid(MOZART)}/albums/a-")
    assert rec.name == "Serenade No. 13 in G major, K. 525"
    for bad in ("albums/p0", "albums/p4", "albums/p1/x"):
        with pytest.raises(tree.NotFound):
            tree.browse(lib.ix, bad)
    # a hidden provider's albums leave the pages
    assert tree.browse(lib.ix, "albums", vis=["other_provider"]) == []


def test_whole_album_list_is_cached_until_the_index_changes(lib):
    first = lib.ix.composer_albums()
    assert lib.ix.composer_albums() == first
    lib.album("9", "Late Addition", 2020, [("Symphony No. 41 in C major, K. 551", ["Mozart, W.A."])])
    assert "Late Addition" in [e["name"] for e in lib.ix.composer_albums()]
    assert lib.ix.composer_albums(vis=["other_provider"]) == []      # the visibility is part of the key


def test_synthetic_library_albums(tmp_path):
    """The bulk helper used to time the Albums lists builds a library the reads understand."""
    from classical_core.store import connect, migrate
    from tests.helpers import synthetic_albums
    con = connect(tmp_path / "big.db")
    migrate(con)
    synthetic_albums(con, n_albums=50, n_composers=12)
    ix = Index(con, None)
    entries = ix.composer_albums()
    assert len({e["id"] for e in entries}) == 50
    keys = [sort_key(e["composer_sort"]) for e in entries]
    assert keys == sorted(keys)
    assert views.albums(ix, limit=10)["total"] == len(entries)
