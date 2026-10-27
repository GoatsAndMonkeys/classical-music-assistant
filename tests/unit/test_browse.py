import pytest

from classical_core import browse as tree
from classical_core.ids import kind_of
from tests.helpers import fake_library
from tests.unit.test_index import new_index, run


@pytest.fixture
def idx(tmp_path, library):
    i = new_index(tmp_path)
    for a in fake_library(library):
        run(i.index_album(a))
    return i


def names(nodes):
    return [n.name for n in nodes]


def test_root_and_composer_walk(idx):
    root = tree.browse(idx, "")
    assert names(root) == ["Composers", "Works", "Review"]
    comps = tree.browse(idx, "composers")
    beet = next(n for n in comps if n.name == "Ludwig van Beethoven")
    assert beet.path.startswith("composers/c_") and "2 works" in beet.version
    types = tree.browse(idx, beet.path)
    assert names(types) == ["Symphonies"]
    works = tree.browse(idx, types[0].path)
    assert [w.name for w in works] == ["Symphony No. 5 in C minor, Op. 67", "Symphony No. 7 in A major, Op. 92"]
    recs = tree.browse(idx, works[0].path)
    assert all(r.playable and r.item_id.startswith("works/w_") for r in recs)
    assert {r.name.split(" (")[0] for r in recs} == {"Herbert von Karajan / Berliner Philharmoniker",
                                                     "Carlos Kleiber / Wiener Philharmoniker"}
    # nested path: MA's ".." (path minus last segment) leads back to the work
    karajan = next(r for r in recs if "Karajan" in r.name)
    assert karajan.path.rsplit("/", 1)[0] == works[0].path
    movs = tree.browse(idx, karajan.path)
    assert [m.kind for m in movs] == ["track"] * 4
    assert movs[0].name.startswith("I.") or "Allegro" in movs[0].name
    assert movs[0].version == "Symphony No. 5 in C minor, Op. 67"
    # the play URI's short form resolves to the same movements
    assert [m.track_id for m in tree.browse(idx, karajan.item_id)] == [m.track_id for m in movs]
    wid, rid = karajan.item_id.split("/")[1:]
    assert tree.play_uri(wid, rid) == f"classical://folder/works/{wid}/{rid}"


def test_works_by_type_and_review(idx):
    types = tree.browse(idx, "works")
    assert "Symphonies" in names(types) and "Opera & Stage" in names(types)
    opera = tree.browse(idx, "works/opera-stage")
    assert any("Figaro" in n.name for n in opera) and "Mozart" in opera[0].version
    reasons = tree.browse(idx, "review")
    assert reasons and all(r.path.startswith("review/") for r in reasons)
    items = tree.browse(idx, reasons[0].path)
    assert items


def test_unknown_and_hidden_ids_are_not_found(idx):
    for p in ("nope", "composers/c_doesnotexist", "works/w_missing", "works/w_missing/r_missing",
              "works/not-a-type", "composers/letter-q/extra", "review/no-such-reason"):
        with pytest.raises(tree.NotFound):
            tree.browse(idx, p)
    w = idx.works()[0]
    with pytest.raises(tree.NotFound):
        tree.browse(idx, f"works/{w['id']}", vis=["some_other_provider"])
    assert tree.browse(idx, "composers", vis=["some_other_provider"]) == []


def test_bounded_folders_page_and_letter(monkeypatch, idx):
    monkeypatch.setattr(tree, "CAP", 2)
    comps = tree.browse(idx, "composers")
    assert all(n.path.startswith("composers/letter-") for n in comps)
    b = tree.browse(idx, "composers/letter-b")
    assert all(kind_of(n.path.rsplit("/", 1)[1]) == "c" for n in b) or all("/p" in n.path for n in b)
    syms = tree.browse(idx, "works/symphonies")
    assert [n.name for n in syms][:2] == ["1–2", "3–4"]
    page1 = tree.browse(idx, syms[0].path)
    assert len(page1) == 2 and all(n.path.startswith("works/symphonies/p1/w_") for n in page1)
    # a work reached through a page still browses (and ".." returns to the page)
    assert tree.browse(idx, page1[0].path)
    with pytest.raises(tree.NotFound):
        tree.browse(idx, "works/symphonies/p99")


def test_provider_presence_states(idx):
    n_works = len(idx.works())
    assert idx.provider_instances() == ["filesystem_local"]
    assert idx.set_provider_state("filesystem_local", "unavailable")
    assert not idx.set_provider_state("filesystem_local", "unavailable")
    assert idx.works() == [] and tree.browse(idx, "composers") == []        # hidden, evidence kept
    live = idx.status()["evidence_live"]
    assert live > 0
    idx.set_provider_state("filesystem_local", "configured")
    assert len(idx.works()) == n_works                                      # back on reload
    albums = idx.retract_provider("filesystem_local")
    assert len(albums) == 10 and idx.provider_state("filesystem_local") == "removed"
    st = idx.status()
    assert st["mappings"] == {"gone": 42}
    assert all(idx.album_signature(a) is None for a in albums)              # next pass re-derives them


def test_review_hides_occurrences_from_invisible_providers(tmp_path):
    """An album with one visible and one hidden track must not expose the hidden track via Review."""
    from classical_core.index import AlbumIn, MappingIn, TrackIn
    from tests.unit.test_index import Reader, tag

    r = Reader()
    i = new_index(tmp_path, mb=False, reader=r)
    r.tags["a"] = tag("Ballade No. 1 in G minor, Op. 23")
    r.tags["b"] = tag("Secret Nocturne, Op. 9 No. 2")
    run(i.index_album(AlbumIn("A", "A", "", 2000, None, [
        TrackIn("1", 1, 1, "t1", "", 100, mappings=[MappingIn("filesystem_local", "a", "f", path="a")]),
        TrackIn("2", 1, 2, "t2", "", 100, mappings=[MappingIn("hidden_share", "b", "f", path="b")])])))
    hidden_occ = i.con.execute("SELECT id FROM occurrence WHERE library_track_id='2'").fetchone()[0]
    i.con.execute("INSERT INTO review_item VALUES ('v_x', 'test reason', 'A', 'occurrence', ?, ?)",
                  (hidden_occ, '{"title": "Secret Nocturne"}'))
    vis = ["filesystem_local"]
    assert not [v for v in i.review(vis) if v["id"] == "v_x"]
    assert any(v["id"] == "v_x" for v in i.review())
    assert i.occurrence_track(hidden_occ, vis) is None and i.occurrence_track(hidden_occ) == "2"
    for n in tree.browse(i, "review", vis=vis):
        assert all("Secret" not in x.name for x in tree.browse(i, n.path, vis=vis))
