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


def add(idx, r, name, rows, artist="Various Artists", year=None, disc=None, paths=None, positions=None):
    """rows: (title, TagInfo | dict | None). A dict is TagInfo kwargs; title defaults to the track title.
    paths: the files' item ids (a CUE track's '::trackNN' id is never read for tags); positions: (disc, n)."""
    global _n
    _n += 1
    aid = f"A{_n}"
    tracks = []
    for i, (title, tg) in enumerate(rows):
        path = paths[i] if paths else f"{aid}/{i}"
        d, pos = positions[i] if positions else (disc, i + 1)
        tracks.append(TrackIn(f"{aid}-{i}", d, pos, title, artist, 300,
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


def test_another_opera_named_on_a_stage_album_is_not_pulled_into_the_album_work(tmp_path):
    idx, r = new_index(tmp_path)
    w = {"composers": ["Richard Wagner"]}
    add(idx, r, "Wagner: Tannhäuser", [("Tannhäuser: Overture", w), ("Nr. 2", w), ("Nr. 3", w),
                                       ("Lohengrin: Prelude to Act 1", w), ("Lohengrin: Prelude to Act 3", w)])
    assert recs(idx, "Tannh")[1] == [3], titles(idx)


def test_another_composers_aria_on_a_stage_album_stays_out(tmp_path):
    """No composer tag, but the title names Gounod: not a number of the album's Mozart opera."""
    idx, r = new_index(tmp_path)
    m = {"composers": ["Wolfgang Amadeus Mozart"]}
    add(idx, r, "Mozart: Le nozze di Figaro", [("Nr. 1", m), ("Nr. 2", m), ("Charles Gounod: Le veau d'or", None)])
    assert recs(idx, "Figaro")[1] == [2], titles(idx)


def test_album_named_after_its_performer_invents_no_work(tmp_path):
    idx, r = new_index(tmp_path)
    m = {"composers": ["Wolfgang Amadeus Mozart"]}
    add(idx, r, "Bryn Terfel", [("Aria", m), ("Recitativo", m), ("Nr. 3", m)], artist="Bryn Terfel")
    assert "Bryn Terfel" not in titles(idx)


# --- albums that derived nothing on the 2026-10 test library (FIDELIO / FURTWANGLER, Dorati Holländer
# disc 2, BSO broadcast logs): every one with a composer and a title now yields works or a review item


def reasons(idx):
    return [v["reason"] for v in idx.review()]


def test_tracks_titled_by_their_text_join_the_opera_the_album_names(tmp_path):
    """Dorati Holländer disc 2: composer tags, no work tag, no 'Nr.' title — only the text sung."""
    idx, r = new_index(tmp_path)
    w = {"composers": ["Richard Wagner"], "performers": [Person("Antal Dorati", "conductor", "person")]}
    rows = [(t, w) for t in ("Acht, wo weilt sie", "Senta! Willst du mich verdeben?",
                             "Bleib', Senta! Bleib' nue einen Augenblick!", "Auf hohem Felsen lag ich träumend",
                             "Mein Kind, du siehst mich auf der Schwelle...", "Steuermann, lass die Wacht!")]
    add(idx, r, "Richard Wagner: Der Fliegende Holländer Disc 2", rows, artist="Antal Dorati")
    assert recs(idx, "Holländer")[1] == [6], titles(idx)
    assert titles(idx) == ["Der Fliegende Holländer"]


def test_text_titles_are_not_pulled_into_a_form_the_album_names(tmp_path):
    """Only an album named after a work by its name ('Fidelio') takes in text-titled tracks: songs on
    'Beethoven: Violin Concerto' are not movements of the concerto, and are listed instead."""
    idx, r = new_index(tmp_path)
    rows = [("Adelaide", {"composers": BEETHOVEN}), ("Ich liebe dich", {"composers": BEETHOVEN})]
    add(idx, r, "Beethoven: Violin Concerto", rows)
    assert titles(idx) == []
    assert "no work identified on this album" in reasons(idx)


def test_untagged_cue_album_takes_its_composer_from_the_folder(tmp_path):
    """FIDELIO / FURTWANGLER: CUE tracks (no tags), both CDs numbered disc 1, the composer only in the
    folder 'Beethoven - Fidelio - Furtwangler - EMI 2CDs'."""
    idx, r = new_index(tmp_path)
    folder = "Beethoven - Fidelio - Furtwangler - EMI 2CDs"
    rows = [("Obertura", None), ("Acto I - Leb wohl, du wannes Sonnenlicht", None),
            ("Acto I - Jetzt,Schätzten, jetzt sind wir allein", None),
            ("Acto II - Gott! Welch' dunkel hier! - In des Lebens Frühlingstagen", None),
            ("Acto I - Der arme Jaquino dauert mich beinahe", None)]
    paths = [f"{folder}/CD1/CD1.ape.cue::track01", f"{folder}/CD2/CD2.ape.cue::track01",
             f"{folder}/CD1/CD1.ape.cue::track02", f"{folder}/CD2/CD2.ape.cue::track02",
             f"{folder}/CD1/CD1.ape.cue::track03"]
    add(idx, r, "FIDELIO", rows, artist="FURTWANGLER", paths=paths,
        positions=[(1, 1), (1, 1), (1, 2), (1, 2), (1, 3)])
    ws, sizes = recs(idx, "Fidelio")
    assert len(ws) == 1 and sizes == [5], titles(idx)
    assert [c["name"] for c in idx.composers()] == ["Ludwig van Beethoven"]
    assert "ambiguous album positions" in reasons(idx)


def test_composer_named_in_an_album_name_segment(tmp_path):
    """'Opera - Beethoven : Fidelio (Bernstein)': no composer tag; the composer is the middle segment."""
    idx, r = new_index(tmp_path)
    rows = [(t, {}) for t in ("Ouvertüre", "Act 1: Duett - Jetzt, Schätzchen, jetzt sind wir allein",
                              "Act 1: Armer Jaquino, ich war ihm sonst recht gut",
                              "Act 1: Arie - O wär ich schon mit dir vereint")]
    add(idx, r, "Opera - Beethoven : Fidelio (Bernstein)", rows, artist="Wiener Philharmoniker")
    assert recs(idx, "Fidelio")[1] == [4], titles(idx)
    assert [c["name"] for c in idx.composers()] == ["Ludwig van Beethoven"]


def test_composer_and_work_in_the_artist_field(tmp_path):
    """Salzburg 1950 Fidelio: album named by its cast, artist 'Beethoven: Fidelio'."""
    idx, r = new_index(tmp_path)
    rows = [(t, {}) for t in ("Act 1: Leb wohl, du warmes Sonnenlicht", "Act 2: Gott ! Welch Dunkel hier!",
                              "Nur hurtig fort", "Euch werde Lohn")]
    add(idx, r, "Flagstad; Patzak; Schoeffler; Greindl; Schwarkopf", rows, artist="Beethoven: Fidelio", disc=2)
    assert recs(idx, "Fidelio")[1] == [4], titles(idx)


def test_ambiguous_positions_still_yield_the_album_work(tmp_path):
    """'Leonore - Fidelio (CD2)': no disc numbers and positions repeated by a second disc's files."""
    idx, r = new_index(tmp_path)
    rows = [(t, {}) for t in ("No. 12 Finale: 'O welche Lust, in frier Luft'",
                              "(Recitativ:) 'Entfernt euch jetzt! Nun, könnt ihr eilen?'",
                              "(Duett:) 'Wir müssen gleich zu werke schreiten'",
                              "(Arie:) 'Auf euch nur will ich bauen'")]
    add(idx, r, "Leonore - Fidelio (CD2)", rows, artist="Ludwig van Beethoven",
        positions=[(None, 1), (None, 2), (None, 1), (None, 2)])
    ws, sizes = recs(idx, "Leonore")
    assert len(ws) == 1 and sizes == [4], titles(idx)


def test_broadcast_log_shorthand_titles(tmp_path):
    """BSO broadcast logs: 'Rands--Symphonic Fantasy', 'Britten VC', 'DSCH 13', 'Beethoven 9-1'; no tags
    name a composer, and the log's label ('230506 BSO Babi Yar') is not a work."""
    idx, r = new_index(tmp_path)
    rows = [(t, {}) for t in ("Intro", "Rands--Symphonic Fantasy", "A&C", "Britten VC", "Encore", "DSCH 13",
                              "Beethoven 9-1", "Beethoven 9-2", "Applause")]
    add(idx, r, "230506 BSO Babi Yar", rows)
    assert set(titles(idx)) == {"Symphonic Fantasy", "Violin Concerto", "Symphony No. 13", "Symphony No. 9"}
    assert recs(idx, "Symphony No. 9")[1] == [2]
    assert {c["name"] for c in idx.composers()} >= {"Benjamin Britten", "Dmitri Shostakovich",
                                                    "Ludwig van Beethoven"}


def test_concert_log_label_without_composer_tags_is_not_a_work(tmp_path):
    """'220814 Ma Tanglewood': generic letters between named works; 'Ma Tanglewood' is the players."""
    idx, r = new_index(tmp_path)
    rows = [(t, {}) for t in ("Intro", "Clyne--Masquerade", "A", "B", "C")]
    add(idx, r, "220814 Ma Tanglewood", rows)
    assert titles(idx) == ["Masquerade"]


def test_concert_log_label_with_composer_tags_names_the_opera(tmp_path):
    """'1960 Fidelio MET': every track tagged with the composer, the log's label minus the house is the work."""
    idx, r = new_index(tmp_path)
    add(idx, r, "1960 Fidelio MET", [("Act 1", {"composers": BEETHOVEN}), ("Act 2", {"composers": BEETHOVEN})])
    assert titles(idx) == ["Fidelio"] and recs(idx, "Fidelio")[1] == [2], titles(idx)


def test_album_with_nothing_to_go_on_is_listed_for_review(tmp_path):
    idx, r = new_index(tmp_path)
    add(idx, r, "Unknown Album (2/11/2009 10:42:23 PM)", [(f"Track {i}", {}) for i in range(1, 4)])
    assert titles(idx) == []
    assert "no work identified on this album" in reasons(idx)


def test_underscored_act_titles_are_numbers_not_works(tmp_path):
    """Karajan EMI box: '010 Act 1_ Terzett: Gut, Söhnchen' ('_' for ':') is not a work 'Act 1_ Terzett'."""
    idx, r = new_index(tmp_path)
    rows = [(t, {}) for t in ("010 Act 1_ Terzett: Gut, Sohnchen, gut", "015 Act 1_ Duett: Jetzt, Alter")]
    add(idx, r, "Various: Karajan The Complete EMI Recordings 1946-1984", rows, artist="Beethoven, Ludwig Van:")
    assert not any("Act 1" in t for t in titles(idx)), titles(idx)


def test_composer_tagged_tracks_left_over_are_listed(tmp_path):
    """Tanglewood logs: a named work, then '01'-style tracks tagged by several composers."""
    idx, r = new_index(tmp_path)
    add(idx, r, "Tanglewood 7", [("Symphony No. 5 in C minor, Op. 67", {"composers": BEETHOVEN}),
                                 ("Adagio", {"composers": ["Mahler"]}), ("Lied", {"composers": ["Barber"]})])
    assert titles(idx) == ["Symphony No. 5 in C minor, Op. 67"]
    assert "tracks with a composer but no identifiable work" in reasons(idx)
