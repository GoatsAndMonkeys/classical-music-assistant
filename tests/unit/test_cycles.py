"""The MB grouping boundary (§5.3): cycles and collections never become the grouped work."""

import asyncio

import pytest

from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.store import SqliteMBCache, connect, migrate
from tests.mbfake import FakeMB, uid


def index(tmp_path, fake):
    con = connect(tmp_path / "index.db")
    migrate(con)
    return Index(con, fake.client(SqliteMBCache(con)))


def mb_album(aid, recs):
    return AlbumIn(aid, aid, "", 2000, None, [
        TrackIn(str(i), 1, i + 1, title, "", 600, mbid=rid,
                mappings=[MappingIn("filesystem_local", f"{aid}/{i}", "f", None)])
        for i, (rid, title) in enumerate(recs)])


def shape(idx):
    return sorted((w["title"], sorted(r["tracks"] for r in idx.recordings(w["id"]))) for w in idx.works())


def ring(fake, typed=True, ring_cat=True):
    t = "Opera" if typed else None
    fake.work(uid(1), "Der Ring des Nibelungen", None, cats=("WWV 86",) if ring_cat else ())
    fake.work(uid(2), "Das Rheingold", t, uid(1), 1, cats=("WWV 86A",))
    fake.work(uid(3), "Die Walküre", t, uid(1), 2, cats=("WWV 86B",))
    fake.work(uid(4), "Die Walküre: Act I", None, uid(3), 1)
    fake.work(uid(7), "Die Walküre: Act II", None, uid(3), 2)
    for n, (parent, title) in enumerate([(uid(2), "Das Rheingold: Scene 1"), (uid(2), "Das Rheingold: Scene 2"),
                                         (uid(4), "Die Walküre: Act I, Scene 1"),
                                         (uid(7), "Die Walküre: Act II, Scene 1")]):
        fake.work(uid(10 + n), title, None, parent, 1 if n == 3 else n % 2 + 1)
        fake.recording(uid(100 + n), title, uid(10 + n), conductor="Georg Solti")
    return mb_album("R", [(uid(100 + n), f"t{n}") for n in range(4)])


@pytest.mark.parametrize("typed,ring_cat", [(True, True), (False, True), (False, False)])
def test_ring_operas_stay_four_works_not_one(tmp_path, typed, ring_cat):
    fake = FakeMB()
    a = ring(fake, typed, ring_cat)
    # the other two operas of the cycle, on a second album
    fake.work(uid(5), "Siegfried", "Opera" if typed else None, uid(1), 3, cats=("WWV 86C",))
    fake.work(uid(6), "Götterdämmerung", "Opera" if typed else None, uid(1), 4, cats=("WWV 86D",))
    fake.work(uid(20), "Siegfried: Act I", None, uid(5), 1)
    fake.work(uid(21), "Götterdämmerung: Prologue", None, uid(6), 1)
    fake.recording(uid(120), "s", uid(20), partial=True)
    fake.recording(uid(121), "g", uid(21), partial=True)
    b = mb_album("S", [(uid(120), "s"), (uid(121), "g")])
    idx = index(tmp_path, fake)
    asyncio.run(idx.index_album(a))
    asyncio.run(idx.index_album(b))
    # adjacent excerpts from two operas of the cycle are two works (an excerpt alone is its own work, §5.3)
    assert shape(idx) == [("Das Rheingold", [2]), ("Die Walküre", [2]), ("Götterdämmerung: Prologue", [1]),
                          ("Siegfried: Act I", [1])]
    if typed:  # an opera's parents are never fetched
        assert not any(uid(1) in c for c in fake.calls)


def wtc(fake, cat_range="BWV 846–869"):
    fake.work(uid(1), "Das wohltemperierte Klavier I", None, cats=(cat_range,) if cat_range else (),
              composer="Johann Sebastian Bach")
    recs = []
    for k, bwv in enumerate((846, 847)):
        pf = fake.work(uid(2 + k), f"Prelude and Fugue in C, BWV {bwv}", None, uid(1), k + 1, cats=(f"BWV {bwv}",),
                       composer="Johann Sebastian Bach")
        for j, part in enumerate(("Prelude", "Fugue")):
            w = fake.work(uid(10 + 2 * k + j), f"Prelude and Fugue in C, BWV {bwv}: {part}", None, pf, j + 1,
                          composer="Johann Sebastian Bach")
            recs.append((fake.recording(uid(100 + 2 * k + j), part, w), part))
    return mb_album("W", recs)


@pytest.mark.parametrize("cat_range", ["BWV 846–869", "BWV 846-869", None])
def test_wtc_prelude_and_fugue_pairs(tmp_path, cat_range):
    fake = FakeMB()
    a = wtc(fake, cat_range)
    idx = index(tmp_path, fake)
    asyncio.run(idx.index_album(a))
    assert shape(idx) == [("Prelude and Fugue in C, BWV 846", [2]), ("Prelude and Fugue in C, BWV 847", [2])]
    [w] = [w for w in idx.works() if w["catalogue_display"] == "BWV 846"]
    [rec] = idx.recordings(w["id"])
    assert rec["complete"] == 1


def test_symphony_movements_still_climb_to_the_symphony(tmp_path):
    fake = FakeMB()
    fake.work(uid(1), "Symphony No. 5 in C minor, Op. 67", "Symphony", cats=("Op. 67",), composer="Beethoven")
    recs = []
    for n in range(4):
        w = fake.work(uid(10 + n), f"Symphony No. 5: {n + 1}", None, uid(1), n + 1, composer="Beethoven")
        recs.append((fake.recording(uid(100 + n), "m", w), "m"))
    idx = index(tmp_path, fake)
    asyncio.run(idx.index_album(mb_album("B", recs)))
    assert shape(idx) == [("Symphony No. 5 in C minor, Op. 67", [4])]


def test_unfetched_parent_abstains_instead_of_merging(tmp_path):
    fake = FakeMB()
    a = wtc(fake)
    del fake.works[uid(1)]   # the collection is unavailable (404): group only up to BWV 846 / 847
    idx = index(tmp_path, fake)
    asyncio.run(idx.index_album(a))
    assert len(idx.works()) == 2
