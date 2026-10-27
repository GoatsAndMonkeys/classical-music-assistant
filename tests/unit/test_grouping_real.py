"""Grouping regressions built from real tag shapes seen in a test library (2026-10).

Each album reproduces the titles and tags MA handed us, trimmed to the tracks that matter.
"""

import asyncio

from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.store import connect, migrate
from classical_core.tags import Person, TagInfo
from classical_core.titles import is_generic_title, is_talk_tail


class Reader:
    def __init__(self):
        self.tags: dict[str, TagInfo] = {}

    def __call__(self, path):
        return self.tags[path]


def new_index(tmp_path):
    con = connect(tmp_path / "index.db")
    migrate(con)
    r = Reader()
    return Index(con, None, tag_reader=r), r


_n = 0


def add(idx, r, name, rows, artist="Various Artists", year=None, disc=None):
    """rows: (title, TagInfo | dict | None). A dict is TagInfo kwargs; title defaults to the track title."""
    global _n
    _n += 1
    aid = f"A{_n}"
    tracks = []
    for i, (title, tg) in enumerate(rows):
        path = f"{aid}/{i}"
        tracks.append(TrackIn(f"{aid}-{i}", disc, i + 1, title, artist, 300,
                              mappings=[MappingIn("filesystem_local", path, fingerprint="f", path=path)]))
        if isinstance(tg, dict):
            tg = TagInfo(**{"title": title, **tg})
        r.tags[path] = tg if tg is not None else TagInfo(title=title)
    a = AlbumIn(aid, name, artist, year, None, tracks)
    asyncio.run(idx.index_album(a))
    return a


def recs(idx, part):
    ws = [w for w in idx.works() if part.lower() in w["title"].lower()]
    return ws, sorted((x["tracks"] for w in ws for x in idx.recordings(w["id"])), reverse=True)


def titles(idx):
    return sorted(w["title"] for w in idx.works())


BEETHOVEN = ["Ludwig van Beethoven"]
HAITINK = [Person("Bernard Haitink", "conductor", "person")]


def test_identical_work_titles_with_one_odd_track_are_one_recording(tmp_path):
    """Haitink Fidelio: every track titled 'Fidelio, opera, Op. 72', one stray title at 16, two discs."""
    idx, r = new_index(tmp_path)
    same = {"title": "Fidelio, opera, Op. 72", "composers": BEETHOVEN, "performers": HAITINK}
    d1 = [("Fidelio, opera, Op. 72", same)] * 15 + [("abscheulicher Norman Haitink", {**same, "title": None})] \
        + [("Fidelio, opera, Op. 72", same)] * 4
    add(idx, r, "Beethoven: Fidelio Disc 1", d1, artist="Jessye Norman", year=1990)
    add(idx, r, "Beethoven: Fidelio Disc 2", [("Fidelio, opera, Op. 72", same)] * 10, artist="Jessye Norman", year=1990)
    ws, sizes = recs(idx, "Fidelio")
    assert len(ws) == 1, titles(idx)
    assert sizes == [20, 10]


def test_track_n_titles_inherit_the_album_work_shared_across_discs(tmp_path):
    """Levine Holländer disc 2: 'Track N' titles without tags, then tracks titled with the work."""
    idx, r = new_index(tmp_path)
    work = "Der fliegende Holländer (The Flying Dutchman), opera, WWV 63"
    tg = {"composers": ["Richard Wagner"], "performers": [Person("James Levine", "conductor", "person")]}
    add(idx, r, "Wagner: Der fliegende Holländer Disc 1", [(work, tg)] * 11, artist="Richard Wagner", year=1997)
    add(idx, r, "Wagner: Der fliegende Holländer Disc 2",
        [(f"Track {i}", None) for i in range(1, 12)] + [(work, tg)] * 4, artist="Richard Wagner", year=1997)
    ws, sizes = recs(idx, "Holländer")
    assert len(ws) == 1, titles(idx)
    assert sizes == [15, 11]
    assert not any(is_generic_title(t) for t in titles(idx))


def test_number_titles_never_become_works(tmp_path):
    """Furtwängler 'Beethoven : Fidelio1': Duett/Arie/Quartett/Rezitativ titles are movements of Fidelio."""
    idx, r = new_index(tmp_path)
    comp = {"composers": ["beethoven"]}
    rows = [(t, comp) for t in ("Overture", "Duett Jaquino - Marzelline", "Arie - Marzelline",
                                "Quartett Marzelline - Leonore - Rocco - Jaquino", "Arie - Rocco",
                                "Terzett Rocco - Leonore - Marzelline", "Marsch Orchester",
                                "Rezitativ & Arie Leonore", "Finale Gefangenen - Chor",
                                '"Nun sprecht, wie ging\'s" Leonore - Rocco')]
    add(idx, r, "Beethoven : Fidelio1", rows, artist="Wilhelm Furtwängler", year=1953, disc=1)
    ws, sizes = recs(idx, "Fidelio")
    assert len(idx.works()) == 1 and len(ws) == 1, titles(idx)
    assert sizes == [10]


def test_aria_titles_without_any_work_join_the_album_opera(tmp_path):
    """Klemperer: 'Ouverture', aria incipits and 'Marsch', composer tag only."""
    idx, r = new_index(tmp_path)
    comp = {"composers": ["Beethoven, Ludwig van (1770-1827)"]}
    rows = [(t, comp) for t in ("Ouverture", "Jetzt, Schatzchen, Jetzt Sind Wir Allein",
                                "Der Arme Jaquino Dauert Mich Beinahe", "O War' Ich Schon Mit Dir Vereint",
                                "Mir Ist So Wunderbar", "Marsch", "Ha! Welch Ein Augenblick!")]
    add(idx, r, "Fidelio (Klemperer, disc 1)", rows, year=1962)
    ws, sizes = recs(idx, "Fidelio")
    assert len(idx.works()) == 1 and len(ws) == 1, titles(idx)
    assert sizes == [7]


def test_act_suffix_in_title_is_a_section_not_a_new_work(tmp_path):
    """Böhm: 'Beethoven: Fidelio, Op. 72 - Overture' then '... - Act 1: <incipit>'."""
    idx, r = new_index(tmp_path)
    comp = {"composers": BEETHOVEN}
    rows = [("Beethoven: Fidelio, Op. 72 - Overture", comp)] + [
        (f"Beethoven: Fidelio, Op. 72 - Act 1: {t}", comp)
        for t in ("Jetzt, Schätzchen, Jetzt Sind Wir Allein", "Der Arme Jaquino! Ich War Ihm Sonst Recht Gut",
                  "O Wär Ich Schon Mit Dir Vereint", "Mir Ist So Wunderbar")]
    add(idx, r, "Beethoven: Fidelio [Disc 1]", rows, year=2005)
    ws, sizes = recs(idx, "Fidelio")
    assert len(ws) == 1, titles(idx)
    assert sizes == [5]


def test_numbered_track_prefixes_and_glued_act_numbers(tmp_path):
    idx, r = new_index(tmp_path)
    comp = {"composers": ["Richard Wagner"]}
    rows = [(t, comp) for t in ("001 Overture", "002 Act1 Hojohe! Hallojo!", "003 Act1 Die Frist ist um",
                                "004 Act2 Summ und brumm")]
    add(idx, r, "Der fliegende Hollander1", rows)
    ws, sizes = recs(idx, "Holl")
    assert len(idx.works()) == 1 and len(ws) == 1, titles(idx)
    assert sizes == [4]


MAHLER6 = "Symphony No. 6 in A minor, 'Tragic'"


def test_broadcast_talk_tracks_are_excluded_and_movements_stay_one_recording(tmp_path):
    idx, r = new_index(tmp_path)
    tg = {"work": MAHLER6, "composers": ["Gustav Mahler"]}

    def mv(no, name):
        return (f"{MAHLER6}: Mvt {no}: {name}", {**tg, "movement_name": name, "movement_no": no})

    rows = [(f"{MAHLER6}: Announcer introduction and Nelsons interview excerpt; Nelsons then comes on stage", tg),
            mv(1, "Allegro energico, ma non troppo"), mv(2, "Scherzo: Wuchtig"),
            (f"{MAHLER6}: Mid-symphony host commentary after the first two movements", tg),
            mv(3, "Andante moderato"), mv(4, "Finale"),
            (f"{MAHLER6}: Applause, then host wrap-up: tour of Japan, Eichler review, players standing, credits", tg)]
    add(idx, r, f"Mahler: {MAHLER6} — Symphony Hall, 2022-10-22", rows, year=2022)
    ws, sizes = recs(idx, "Symphony No. 6")
    assert len(ws) == 1 and sizes == [4]


def test_continuous_segments_and_vague_continuations_are_one_recording(tmp_path):
    idx, r = new_index(tmp_path)
    wr = {"work": "War Requiem", "composers": ["Benjamin Britten"]}
    rows = [("Broadcast opening: host introduction and continuity", {"composers": []})]
    rows += [(f"War Requiem: Continuous segment {i} of 6", wr) for i in range(1, 6)]
    rows += [("War Requiem: Continuous segment 6 of 6. Ends with the close of the Libera me", wr),
             ("Applause and closing broadcast continuity", {"composers": []})]
    add(idx, r, "Britten: War Requiem — Symphony Hall, 2022-04-02", rows, year=2022)
    nv = {"work": "Alexander Nevsky, Op. 78", "composers": ["Sergei Prokofiev"]}
    add(idx, r, "Carnegie Hall Live — Carnegie Hall, 2015-10-22", [
        ("Alexander Nevsky, Op. 78: Part of Prokofiev's Alexander Nevsky", nv),
        ("Alexander Nevsky, Op. 78: Continuation of Alexander Nevsky", nv),
        ("Alexander Nevsky, Op. 78: Continuation of Alexander Nevsky", nv),
        ("Alexander Nevsky, Op. 78: Conclusion of Alexander Nevsky", nv)], year=2015)
    assert recs(idx, "War Requiem")[1] == [6]
    assert recs(idx, "Nevsky")[1] == [4]


def test_repeated_vague_movement_tails_are_one_recording(tmp_path):
    idx, r = new_index(tmp_path)
    tg = {"work": "Symphony No. 8 in G major, Op. 88", "composers": ["Antonín Dvořák"]}
    rows = [("Symphony No. 8 in G major, Op. 88: Probably a movement", tg)] * 3 \
        + [("Symphony No. 8 in G major, Op. 88: Finale", tg)]
    add(idx, r, "Dvořák — Symphony Hall, 2019-01-01", rows, year=2019)
    assert recs(idx, "Symphony No. 8")[1] == [4]


def test_mixed_composer_album_does_not_get_an_album_template(tmp_path):
    """Tanglewood 6: bare '01'..'06' titles with a different composer each; no invented work."""
    idx, r = new_index(tmp_path)
    rows = [(f"0{i}", {"composers": [c]}) for i, c in enumerate(("Mozart", "Britten", "Berg", "Brahms"), 1)]
    add(idx, r, "Tanglewood 6", rows, year=2015)
    assert idx.works() == []


def test_two_complete_takes_of_a_symphony_still_split(tmp_path):
    idx, r = new_index(tmp_path)
    tg = {"work": "Symphony No. 5 in C minor, Op. 67", "composers": BEETHOVEN}
    mvs = ("Allegro con brio", "Andante con moto", "Scherzo. Allegro", "Allegro")
    rows = [(f"Symphony No. 5 in C minor, Op. 67: {m}", {**tg, "movement_name": m, "movement_no": i})
            for _ in range(2) for i, m in enumerate(mvs, 1)]
    add(idx, r, "Beethoven 5 twice", rows, year=2000)
    assert recs(idx, "Symphony No. 5")[1] == [4, 4]


def test_title_helpers():
    for t in ("Nr.1", "Act 1", "Rezitativ", "Duett", "Arie", "Erster Aufzug", "Track 7", "01", "Overture",
              "Ouvertüre", "Duett Jaquino - Marzelline"):
        assert is_generic_title(t), t
    for t in ("Fidelio, Op. 72", "Mir Ist So Wunderbar", "Symphony No. 5", "Overture, Op. 124"):
        assert not is_generic_title(t), t
    assert is_talk_tail("Announcer introduction and Nelsons interview excerpt; Nelsons then comes on stage")
    assert is_talk_tail("Mid-symphony host commentary after the first two movements")
    for t in ("Probable, based on duration and Track 09 commentary", "Part II, close and applause",
              "Continuous segment 6 of 6. Ends with the close of the Libera me", "Post-concert encore / applause"):
        assert not is_talk_tail(t), t


def test_librettist_listed_first_neither_crashes_nor_hides_the_composer(tmp_path):
    """TCOM 'Sonnleithner, Joseph von [Author]/Ludwig van Beethoven' as two values, librettist first."""
    idx, r = new_index(tmp_path)
    comp = {"composers": ["Sonnleithner, Joseph von [Author]", "Ludwig van Beethoven"]}
    add(idx, r, "Beethoven: Fidelio", [(t, comp) for t in ("Nr. 1", "Nr. 2", "Nr. 3")])
    ws, sizes = recs(idx, "Fidelio")
    assert len(ws) == 1 and sizes == [3], titles(idx)
    assert [c["name"] for c in idx.composers()] == ["Ludwig van Beethoven"]


def test_scored_or_keyed_form_titles_are_works_not_numbers(tmp_path):
    """'Romanze für Violine und Orchester Nr. 2 F-Dur' after a tagged concerto is a work of its own, not part
    of the album's concerto recording."""
    for t in ("Romanze für Violine und Orchester Nr. 2 F-Dur", "Kanon in D", "Duo concertant: I. Cantilène",
              "Romance for violin and orchestra"):
        assert not is_generic_title(t), t
    idx, r = new_index(tmp_path)
    tg = {"work": "Violin Concerto in D major, Op. 61", "composers": BEETHOVEN}
    rows = [(f"Violin Concerto in D major, Op. 61: {m}", {**tg, "movement_name": m, "movement_no": i})
            for i, m in enumerate(("Allegro ma non troppo", "Larghetto", "Rondo"), 1)]
    rows += [("Romanze für Violine und Orchester Nr. 1 G-Dur", {"composers": BEETHOVEN}),
             ("Romanze für Violine und Orchester Nr. 2 F-Dur", {"composers": BEETHOVEN})]
    add(idx, r, "Beethoven: Violin Concerto; Romances", rows)
    assert recs(idx, "Violin Concerto")[1] == [3], titles(idx)
