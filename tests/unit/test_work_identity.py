"""Cross-album work identity, movement names, nested credits and title-word composer names (2026-10).

Titles, album names, file names and credits are copied from the test library's index (M1 copy, 2026-10-27).
"""

import asyncio

from classical_core import views
from classical_core import work_identity as wi
from classical_core.catalogue import parse_catalogues
from classical_core.composers import lookup
from classical_core.tags import Person, _person, normalize_person

from .test_grouping_real import add, new_index

BEETHOVEN = ["Ludwig van Beethoven"]
WAGNER = ["Richard Wagner"]


def works_like(idx, part):
    return [w for w in idx.works() if part.lower() in w["title"].lower()]


def n_recs(idx, wid):
    return len(idx.recordings(wid))


# ---- pure rules ------------------------------------------------------------------------------------------


def test_known_titles_from_real_tags():
    fid = ("fidelio", "Opera & Stage")
    for t in ("FIDELIO", "Fidelio", "Fidelio, Op. 72", "Fidelio, opera, Op. 72", "Fidelio, op. 72"):
        assert wi.known_work("Ludwig van Beethoven", t) == fid, t
    for t in ("Der fliegende Holländer (The Flying Dutchman), opera, WWV 63", "Der Fliegende Holländer",
              "Der fliegende Hollander", "Der fliegende Holländer, WWV 63"):
        assert wi.known_work("Richard Wagner", t) is not None, t
    # other works, other composers, generic titles
    assert wi.known_work("Ludwig van Beethoven", "Leonore") != fid
    assert wi.known_work("Ludwig van Beethoven", "Leonore Overture #3, Op. 72A") != fid
    assert wi.known_work("Ludwig van Beethoven", "Overture “Leonore” no. 3 in C major, op. 72b") != fid
    assert wi.known_work("Richard Wagner", "Fidelio") is None
    assert wi.known_work("Ludwig van Beethoven", "Symphony No. 7") is None
    assert wi.known_work("Ludwig van Beethoven", "Violin Concerto") is None


def test_every_known_composer_is_in_the_composer_list():
    for name in wi._KNOWN:
        k = lookup(name)
        assert k is not None and k.name == name, name


def _keys(title, composer="Ludwig van Beethoven"):
    return {v for _, v in wi.cross_keys(composer, title, parse_catalogues(title))}


def test_catalogue_keys_need_the_full_number():
    assert _keys("Leonore Overture No. 3, Op.72a") == _keys("Leonoren Overture Nr 3, op. 72a")
    assert not _keys("Leonore Overture No. 3, Op.72a") & _keys("Fidelio, Op. 72")
    assert not _keys("Overture “Leonore” no. 3 in C major, op. 72b") & _keys("Leonore Overture No. 3, Op.72a")
    assert not _keys("String Quartet No. 7 in F major, Op. 59 No. 1") & _keys("String Quartet No. 8, Op. 59 No. 2")


def test_titles_must_agree_beyond_the_catalogue():
    assert wi.titles_agree("Leonore Overture #3, Op. 72A", "Leonoren Overture Nr 3, op. 72a") is None
    assert wi.titles_agree("Leonore Overture No. 3, Op.72a", "Leonore Overture #3, Op. 72A") is None
    assert wi.titles_agree("Leonore Overture No. 2, Op. 72", "Fidelio, Op. 72") is not None
    assert wi.titles_agree("Leonore Overture No. 3, Op. 72", "Leonore Overture No. 2, Op. 72") is not None


def test_suites_excerpts_and_arrangements_are_not_the_stage_work():
    """Brackets that name another piece keep it apart; 'orch.' marks an arrangement even before a name."""
    assert wi.known_work("Igor Stravinsky", "The Firebird (1919 Suite)") is None
    assert wi.known_work("Igor Stravinsky", "The Firebird [Suite]") is None
    assert wi.known_work("Georges Bizet", "Carmen (Suite)") is None
    assert wi.known_work("Ludwig van Beethoven", "Fidelio [Excerpts]") is None
    assert wi.known_work("Richard Wagner", "Der fliegende Holländer (The Flying Dutchman)") is not None
    for t in ("Pictures at an Exhibition (orch. Ravel)", "Wesendonck Lieder, WWV 91 (orch. Mottl)",
              "Boris Godunov (orch Rimsky-Korsakov)", "Symphony No. 5 (arr. Liszt)", "Orchestration by Ravel"):
        assert wi.is_arrangement(t), t
        assert wi.cross_keys("Modest Mussorgsky", t, parse_catalogues(t)) == [], t
    for t in ("Pictures at an Exhibition", "Concerto for Orchestra", "Afternoon of a Faun", "Arrival"):
        assert not wi.is_arrangement(t), t


def test_extra_name_words_on_one_side_are_another_piece():
    assert wi.titles_agree("Tristan und Isolde: Prelude, WWV 90",
                           "Tristan und Isolde: Prelude and Liebestod, WWV 90") == "different names"
    assert wi.titles_agree("Tannhäuser: Overture, WWV 70",
                           "Tannhäuser: Overture and Venusberg Music, WWV 70") == "different names"
    assert wi.titles_agree("Die Zauberflöte, K. 620", "Die Zauberflöte, K. 620: Der Hölle Rache") is not None
    # a translation in brackets or a missing nickname still agrees
    assert wi.titles_agree("Der fliegende Holländer (The Flying Dutchman), WWV 63",
                           "Der fliegende Holländer, WWV 63") is None
    assert wi.titles_agree("Symphony No. 9 'Choral', Op. 125", "Symphony No. 9 in D minor, Op. 125") is None


# ---- cross-album merging in the index --------------------------------------------------------------------


def _fidelio_albums(idx, r):
    add(idx, r, "Beethoven: Fidelio Disc 1", [("Fidelio, opera, Op. 72", {"composers": BEETHOVEN})] * 6)
    add(idx, r, "FIDELIO", [(t, {"composers": BEETHOVEN}) for t in (
        "Obertura", "Acto I - Leb wohl, du wannes Sonnenlicht", "Acto I - Jetzt,Schätzten, jetzt sind wir allein")])
    add(idx, r, "Opera - Beethoven : Fidelio (Bernstein)",
        [(t, {"composers": BEETHOVEN}) for t in ("Ouvertüre", "Marsch", "Abscheulicher!")])


def test_fidelio_across_albums_is_one_work(tmp_path):
    idx, r = new_index(tmp_path)
    _fidelio_albums(idx, r)
    ws = works_like(idx, "Fidelio")
    assert len(ws) == 1, [w["title"] for w in ws]
    assert n_recs(idx, ws[0]["id"]) == 3
    assert ws[0]["work_type"] == "Opera & Stage"
    assert "Op. 72" in (ws[0]["catalogue_display"] or ws[0]["title"])


def test_merged_work_keeps_a_stable_id(tmp_path):
    idx, r = new_index(tmp_path)
    add(idx, r, "Beethoven: Fidelio Disc 1", [("Fidelio, opera, Op. 72", {"composers": BEETHOVEN})] * 4)
    first = works_like(idx, "Fidelio")[0]["id"]
    add(idx, r, "FIDELIO", [(t, {"composers": BEETHOVEN}) for t in ("Obertura", "Acto I - Leb wohl, du wannes Sonnenlicht")])
    ws = works_like(idx, "Fidelio")
    assert [w["id"] for w in ws] == [first]
    src = idx._one("SELECT source_work_id FROM recording WHERE work_id=? AND source_work_id<>?", (first, first))[0]
    assert idx.resolve("work", src) == first        # the merged album work's id still leads to Fidelio


def test_leonore_overtures_and_the_1805_leonore_stay_apart_from_fidelio(tmp_path):
    idx, r = new_index(tmp_path)
    _fidelio_albums(idx, r)
    add(idx, r, "Beethoven: Fidelio", [("Leonore Overture No. 3, Op.72a", {"composers": BEETHOVEN})])
    add(idx, r, "Beethoven: Fidelio [Disc 2]", [("Leonore Overture #3, Op. 72A", {"composers": BEETHOVEN})])
    add(idx, r, "Beethoven: Fidelio Disc 2",
        [("Overture “Leonore” no. 3 in C major, op. 72b", {"composers": BEETHOVEN})])
    add(idx, r, "Beethoven: Leonore - Gardiner",
        [(t, {"composers": BEETHOVEN}) for t in ("Ouvertüre: Leonore II", "Marsch")])
    add(idx, r, "Beethoven: Overtures", [("Leonore Overture No. 2, Op. 72", {"composers": BEETHOVEN})])
    fid = works_like(idx, "Fidelio")
    assert len(fid) == 1 and n_recs(idx, fid[0]["id"]) == 3
    leo = sorted(w["title"] for w in works_like(idx, "Leonore"))
    assert len(leo) == 4, leo     # 72a (two albums, one work), 72b, the 1805 Leonore, Leonore No. 2
    a72 = [w for w in works_like(idx, "Leonore") if "72a" in w["title"].lower()]
    assert len(a72) == 1 and n_recs(idx, a72[0]["id"]) == 2


def test_generic_titles_never_merge(tmp_path):
    idx, r = new_index(tmp_path)
    for name in ("Violin Concertos", "Great Concertos"):
        add(idx, r, name, [(f"Violin Concerto: {m}", {"composers": BEETHOVEN})
                           for m in ("I. Allegro ma non troppo", "II. Larghetto", "III. Rondo")])
    assert len(works_like(idx, "Violin Concerto")) == 2


def test_hollander_spellings_merge_across_albums(tmp_path):
    idx, r = new_index(tmp_path)
    add(idx, r, "Wagner: Der fliegende Holländer Disc 1",
        [("Der fliegende Holländer (The Flying Dutchman), opera, WWV 63", {"composers": WAGNER})] * 3)
    add(idx, r, "Der fliegende Hollander1",
        [(t, {"composers": WAGNER}) for t in ("Ouverture", "Steuermann!")])
    add(idx, r, "Opera - Wagner : Der Fliegende Holländer (Böhm)",
        [(t, {"composers": WAGNER}) for t in ("Act 1: Overtüre", "Act 1: Südwind! Südwind!")])
    ws = works_like(idx, "Holl")
    assert len(ws) == 1, [w["title"] for w in ws]
    assert n_recs(idx, ws[0]["id"]) == 3


def test_keep_separate_undoes_the_merge_and_sticks(tmp_path):
    idx, r = new_index(tmp_path)
    add(idx, r, "Beethoven: Fidelio Disc 1", [("Fidelio, opera, Op. 72", {"composers": BEETHOVEN})] * 4)
    b = add(idx, r, "FIDELIO", [(t, {"composers": BEETHOVEN}) for t in ("Obertura", "Acto I - Leb wohl, du wannes Sonnenlicht")])
    (w,) = works_like(idx, "Fidelio")
    rec = next(x for x in idx.recordings(w["id"]) if x["album_library_id"] == b.library_album_id)
    assert idx.merged_into(rec["id"])
    assert views.recording(idx, rec["id"])["merged"] is True
    albums = idx.separate_work(rec["id"], user="seth")
    assert albums == [b.library_album_id]
    asyncio.run(idx.index_album(b))
    ws = works_like(idx, "Fidelio")
    assert len(ws) == 2 and all(n_recs(idx, x["id"]) == 1 for x in ws)
    asyncio.run(idx.index_album(b, force=True))
    assert len(works_like(idx, "Fidelio")) == 2       # later derivations keep them apart
    assert idx._one("SELECT COUNT(*) FROM review_item WHERE reason LIKE 'possible same work%' "
                    "AND entity_id IN (?, ?)", tuple(x["id"] for x in ws))[0] == 0


def test_an_arranger_credit_keeps_the_album_work_apart(tmp_path):
    idx, r = new_index(tmp_path)
    mus = ["Modest Mussorgsky"]
    add(idx, r, "Mussorgsky: Pictures (piano)", [("Pictures at an Exhibition: Promenade", {"composers": mus})] * 2)
    add(idx, r, "Mussorgsky/Ravel: Pictures",
        [("Pictures at an Exhibition: Promenade", {"composers": mus, "arrangers": ["Maurice Ravel"]})] * 2)
    add(idx, r, "Pictures (orch.)", [("Pictures at an Exhibition (orch. Ravel): Gnomus", {"composers": mus})] * 2)
    assert len(works_like(idx, "Pictures")) == 3


# ---- movement names ----------------------------------------------------------------------------------------


def test_identical_titles_take_movement_names_from_the_file_names(tmp_path):
    """Levine Holländer disc 1: every track titled with the work; the file names carry the numbers."""
    idx, r = new_index(tmp_path)
    folder = "Der fliegende Holländer (Wagner) - Morris, Voigt, Heppner; Levine/Dutchman 1/"
    files = ["01-Overture.mp3", "02-Hojoje! Hojoje! Hollojo! Ho!.mp3", "03-Lied Mit Gewitter und Sturm.mp3",
             "04-Die Frist ist um.mp3"]
    add(idx, r, "Wagner: Der fliegende Holländer Disc 1",
        [("Der fliegende Holländer (The Flying Dutchman), opera, WWV 63", {"composers": WAGNER})] * 4,
        paths=[folder + f for f in files])
    (w,) = works_like(idx, "Holl")
    (rec,) = idx.recordings(w["id"])
    names = [t["movement_name"] for t in idx.recording_tracks(rec["id"])]
    assert names == ["Overture", "Hojoje! Hojoje! Hollojo! Ho!", "Lied Mit Gewitter und Sturm", "Die Frist ist um"]


def test_movement_name_strips_the_work_prefix():
    from classical_core.index import movement_from_title
    assert movement_from_title("Beethoven: Fidelio, Op. 72 - Act 2: Gott! Welch' Dunkel Hier!",
                               "Beethoven: Fidelio, Op. 72") == "Act 2: Gott! Welch' Dunkel Hier!"
    assert movement_from_title("Fidelio, opera, Op. 72", "Fidelio, opera, Op. 72") is None


# ---- nested credits ----------------------------------------------------------------------------------------


def test_nested_performer_roles():
    p = _person("Ain Anger (bass (Pater Profundus))", "performer")
    assert (p.name, p.role, p.detail) == ("Ain Anger", "bass", "Pater Profundus")
    p = _person("Antonio Pappano (conductor (Brahms concert))", "performer")
    assert (p.name, p.role, p.detail) == ("Antonio Pappano", "conductor", "Brahms concert")
    p = _person("Tanglewood Festival Chorus (chorus (Symphony of Psalms))", "performer")
    assert (p.name, p.role, p.detail, p.kind) == ("Tanglewood Festival Chorus", "chorus", "Symphony of Psalms",
                                                  "group")
    # stored before nested parsing: one person, not one per character
    old = normalize_person(Person("Ain Anger (bass (Pater Profundus))", "performer", "person"))
    assert (old.name, old.role, old.detail) == ("Ain Anger", "bass", "Pater Profundus")


def test_one_person_per_singer_whatever_the_character(tmp_path):
    idx, r = new_index(tmp_path)
    add(idx, r, "Mahler: Symphony No. 8",
        [(f"Symphony No. 8: Part II, {s}", {"composers": ["Gustav Mahler"], "performers": [
            _person("Ain Anger (bass (Pater Profundus))", "performer")]}) for s in ("first section", "close")])
    people = [p for p in idx._q("SELECT name FROM person")]
    assert [p[0] for p in people if "Anger" in p[0]] == ["Ain Anger"]
    (w,) = works_like(idx, "Symphony No. 8")
    (rec,) = idx.recordings(w["id"])
    c = next(c for c in views.recording(idx, rec["id"])["credits"] if c["name"] == "Ain Anger")
    assert c["detail"] == "Pater Profundus"


# ---- composer names copied from the work title -------------------------------------------------------------


def test_alexander_prokofiev_on_alexander_nevsky_is_sergei(tmp_path):
    idx, r = new_index(tmp_path)
    add(idx, r, "Carnegie Hall Live — Carnegie Hall, 2015-10-22",
        [(f"Alexander Nevsky, Op. 78: {m}", {"composers": ["Alexander Prokofiev"]})
         for m in ("Russia under the Mongolian Yoke", "Song about Alexander Nevsky", "The Crusaders in Pskov")])
    names = [c["name"] for c in idx.composers()]
    assert names == ["Sergei Prokofiev"]
    assert any(v["reason"] == "composer name corrected from the work title" for v in idx.review())


def test_given_name_not_in_a_title_stays_for_review(tmp_path):
    idx, r = new_index(tmp_path)
    add(idx, r, "Prokofiev: Piano Sonatas",
        [(f"Piano Sonata No. 7: {m}", {"composers": ["Alexander Prokofiev"]}) for m in ("I. Allegro", "II. Andante")])
    assert [c["name"] for c in idx.composers()] == ["Alexander Prokofiev"]
    assert any(v["reason"] == "suspicious composer name" for v in idx.review())


# ---- review cards ------------------------------------------------------------------------------------------


def test_review_cards_name_the_item_not_the_group():
    it = {"reason": "MusicBrainz release match uncertain", "entity": "album", "entity_id": "a1",
          "detail": {"album": "Beethoven: Fidelio", "why": "several releases match"}}
    assert views._review_title(it, "Beethoven: Fidelio") == "Beethoven: Fidelio"
    assert views._review_subtitle("Beethoven: Fidelio", "Beethoven: Fidelio", it["detail"]) == "several releases match"
    bare = {"reason": "some reason", "entity": "album", "entity_id": "a1", "detail": {}}
    assert views._review_title(bare, "Wagner: Der fliegende Holländer Disc 1") == \
        "Wagner: Der fliegende Holländer Disc 1"
    assert views._review_title(bare) != "some reason"


def test_review_view_titles(tmp_path):
    idx, r = new_index(tmp_path)
    _fidelio_albums(idx, r)
    for g in views.review(idx)["groups"]:
        for it in g["items"]:
            assert it["title"] != g["reason"]
            assert it["subtitle"] != it["title"]



def test_title_word_correction_needs_a_listed_work_and_a_unique_surname(tmp_path):
    """Siegfried Wagner on a Siegfried album, Leopold Mozart, or the full name written in a title stay as
    they are: only 'Alexander' from 'Alexander Nevsky' (a listed Prokofiev work) is a copied title word."""
    idx, r = new_index(tmp_path)
    add(idx, r, "Wagner Family: Siegfried and more",
        [("Siegfried Idyll", {"composers": ["Siegfried Wagner"], "work": "Siegfried Idyll"}),
         ("Siegfried: Waldweben", {"composers": WAGNER, "work": "Siegfried, WWV 86C"})])
    add(idx, r, "Leopold: Toy Symphony",
        [("Cassation in G 'Leopold'", {"composers": ["Leopold Mozart"], "work": "Cassation in G 'Leopold'"})])
    add(idx, r, "Alexander Nevsky / Alexander Prokofiev plays",
        [("Alexander Nevsky, Op. 78: Russia under the Mongolian Yoke", {"composers": ["Alexander Prokofiev"]})])
    names = {c["name"] for c in idx.composers()}
    assert {"Siegfried Wagner", "Leopold Mozart", "Alexander Prokofiev"} <= names, names
    assert "Sergei Prokofiev" not in names
