"""Today's shelves (classical_core/curation.py): the church calendar, anniversaries, seasons, weekdays,
weighting, diversity and determinism, all on fixed dates."""

import datetime as dt
import re

import pytest

from classical_core import composers, curation
from classical_core.browse import play_uri
from classical_core.tags import Person
from tests.unit.test_views import Lib

D = dt.date


def cand(title, composer, work_type="Symphonies", cat="", accs=(), complete=1, wid=None):
    wid = wid or "w_" + re.sub(r"\W+", "_", f"{composer}_{title}").lower()
    w = {"id": wid, "title": title, "composer": composer, "work_type": work_type, "catalogue_display": cat}
    rec = {"id": "r" + wid[1:], "complete": complete, "status": "auto", "evidence_fields": 3, "label": "Rec",
           "image": None}
    return curation._cand(w, rec, list(accs))


GRAMOPHONE = {"counts": True, "weight": 2.0, "text": "Gramophone Award 1990"}

LIBRARY = [
    cand("Messiah, HWV 56", "George Frideric Handel", "Choral & Sacred"),
    cand("The Nutcracker, Op. 71", "Pyotr Ilyich Tchaikovsky", "Opera & Stage"),
    cand("Weihnachtsoratorium, BWV 248", "Johann Sebastian Bach", "Choral & Sacred"),
    cand("Mass in B minor, BWV 232", "Johann Sebastian Bach", "Choral & Sacred"),
    cand("Cantata 'Ach Gott, wie manches Herzeleid', BWV 58", "Johann Sebastian Bach", "Choral & Sacred"),
    cand("Ich glaube, lieber Herr, hilf meinem Unglauben, BWV 109", "Johann Sebastian Bach", "Choral & Sacred"),
    cand("St Matthew Passion, BWV 244", "Johann Sebastian Bach", "Choral & Sacred"),
    cand("Easter Oratorio, BWV 249", "Johann Sebastian Bach", "Choral & Sacred"),
    cand("Symphony No. 9 in D minor, Op. 125", "Ludwig van Beethoven", accs=[GRAMOPHONE]),
    cand("Symphony No. 5 in C minor, Op. 67", "Ludwig van Beethoven"),
    cand("Piano Sonata No. 8 'Pathétique', Op. 13", "Ludwig van Beethoven", "Keyboard"),
    cand("Symphony No. 2 'Resurrection'", "Gustav Mahler"),
    cand("Symphony No. 40 in G minor, K. 550", "Wolfgang Amadeus Mozart"),
    cand("Piano Concerto No. 20 in D minor, K. 466", "Wolfgang Amadeus Mozart", "Concertos"),
    cand("Der fliegende Holländer, WWV 63", "Richard Wagner", "Opera & Stage"),
    cand("Danse macabre, Op. 40", "Camille Saint-Saëns", "Orchestral"),
    cand("Le quattro stagioni: L'inverno, RV 297", "Antonio Vivaldi", "Concertos"),
    cand("Le quattro stagioni: L'estate, RV 315", "Antonio Vivaldi", "Concertos"),
    cand("Requiem in D minor, K. 626", "Wolfgang Amadeus Mozart", "Choral & Sacred"),
    cand("Cello Concerto in E minor, Op. 85", "Edward Elgar", "Concertos"),
    cand("String Quartet No. 8, Op. 110", "Dmitri Shostakovich", "Chamber"),
    cand("Ballade No. 1, Op. 23", "Frédéric Chopin", "Keyboard"),
    cand("Violin Concerto in D major, Op. 77", "Johannes Brahms", "Concertos", accs=[GRAMOPHONE]),
]


def shelf(shelves, sid):
    return next((s for s in shelves if s["id"] == sid), None)


def reasons(s):
    return [p["reason"] for p in s["picks"]]


@pytest.mark.parametrize("year,expected", [(2027, D(2027, 3, 28)), (2026, D(2026, 4, 5)), (2024, D(2024, 3, 31)),
                                           (2025, D(2025, 4, 20)), (2000, D(2000, 4, 23)), (2038, D(2038, 4, 25)),
                                           (1818, D(1818, 3, 22)), (2285, D(2285, 3, 22))])
def test_easter(year, expected):
    assert curation.easter(year) == expected


@pytest.mark.parametrize("day,name,first", [
    (D(2026, 10, 25), "21st Sunday after Trinity", "109"),
    (D(2026, 11, 29), "1st Sunday of Advent", "61"),
    (D(2026, 12, 27), "Sunday after Christmas", "152"),
    (D(2027, 1, 3), "Sunday after New Year", "153"),
    (D(2027, 1, 17), "2nd Sunday after Epiphany", "155"),
    (D(2027, 2, 7), "Estomihi", "22"),
    (D(2027, 3, 21), "Palm Sunday", "182"),
    (D(2027, 3, 28), "Easter Sunday", "4"),
    (D(2027, 5, 16), "Pentecost", "172"),
    (D(2027, 5, 23), "Trinity Sunday", "165"),
    (D(2027, 5, 30), "1st Sunday after Trinity", "75"),
])
def test_liturgical_sundays(day, name, first):
    lit = curation.liturgical_sunday(day)
    assert lit.name == name and lit.cantatas[0] == first


def test_weekdays_have_no_sunday_name_and_feasts_are_found():
    assert curation.liturgical_sunday(D(2026, 10, 26)) is None
    assert curation.feast(D(2027, 3, 26)).name == "Good Friday"          # Easter 2027 - 2
    assert curation.feast(D(2027, 5, 6)).name == "Ascension"              # Easter 2027 + 39
    assert curation.feast(D(2026, 10, 31)).cantatas[0] == "80"
    assert curation.feast(D(2026, 10, 27)) is None


def test_christmas_day():
    day = D(2026, 12, 25)
    shelves = curation.today(LIBRARY, day)
    otd = shelf(shelves, "on_this_day")
    # the Christmas Oratorio's first part was first heard on Christmas Day 1734
    assert any(p["title"].startswith("Weihnachtsoratorium") and p["reason"].startswith("Premiered on this day in 1734")
               for p in otd["picks"])
    season = shelf(shelves, "season")
    assert season["subtitle"].startswith("Christmas")
    titles = {p["title"] for s in shelves for p in s["picks"]}
    assert {"Messiah, HWV 56", "The Nutcracker, Op. 71", "Weihnachtsoratorium, BWV 248"} <= titles
    assert all(r.startswith(("Christmas", "Beethoven month", "Winter")) for r in reasons(season))
    assert reasons(season)[0].startswith("Christmas")


def test_easter_2027_is_computed_and_themed():
    day = curation.easter(2027)
    assert day == D(2027, 3, 28)
    assert [t.id for t in curation.themes(day)] == ["easter", "composer_month", "spring"]
    shelves = curation.today(LIBRARY, day)
    season = shelf(shelves, "season")
    assert season["subtitle"].startswith("Easter")
    titles = [p["title"] for p in season["picks"]]
    assert "Symphony No. 2 'Resurrection'" in titles or "Easter Oratorio, BWV 249" in titles
    # Easter Sunday is also a Sunday: Bach's Easter cantatas, then the oratorio, lead the weekday shelf if unused
    sunday = shelf(shelves, "weekday")
    assert sunday["subtitle"].startswith("Easter Sunday")
    # Good Friday two days before: the Passion
    gf = curation.today(LIBRARY, D(2027, 3, 26))
    assert any("St Matthew Passion" in p["title"] for p in shelf(gf, "season")["picks"])
    assert [t.id for t in curation.themes(D(2027, 3, 26))][:2] == ["feast", "holy_week"]


def test_beethoven_baptism_on_17_december():
    kc = composers.lookup("Ludwig van Beethoven")
    assert (kc.born_md, kc.baptised, kc.died_md) == ("12-17", True, "03-26")
    shelves = curation.today(LIBRARY, D(2026, 12, 17))
    otd = shelf(shelves, "on_this_day")
    assert otd["subtitle"] == "17 December"
    beet = [p for p in otd["picks"] if p["composer"] == "Ludwig van Beethoven"]
    assert len(beet) == 1
    assert beet[0]["reason"] == "Beethoven: Baptised on this day in 1770 (birth date unrecorded) (256 years ago)"


def test_round_anniversaries_say_so_and_weigh_more():
    # 26 March 2027: 200 years since Beethoven died
    otd = shelf(curation.today(LIBRARY, D(2027, 3, 26)), "on_this_day")
    [p] = [p for p in otd["picks"] if p["composer"] == "Ludwig van Beethoven"]
    assert p["reason"] == "Beethoven: Died on this day in 1827 (200th anniversary)"


def test_sunday_brings_the_cantata_for_the_day():
    day = D(2026, 10, 25)
    sunday = shelf(curation.today(LIBRARY, day), "weekday")
    assert sunday["title"] == "For Sunday"
    assert sunday["subtitle"] == "21st Sunday after Trinity · Sunday morning: sacred music"
    first = sunday["picks"][0]
    assert "BWV 109" in first["title"] and first["reason"] == "Bach's cantata for the 21st Sunday after Trinity"
    # sacred music first; a small library is topped up to three picks with anything else
    assert [p["work_type"] for p in sunday["picks"][:2]] == ["Choral & Sacred"] * 2
    assert all(p["reason"] == "A Sunday pick" for p in sunday["picks"] if p["work_type"] != "Choral & Sacred")


def test_sunday_without_its_cantata_falls_back_to_bach_sacred():
    lib = [c for c in LIBRARY if "BWV 109" not in c.title]
    sunday = shelf(curation.today(lib, D(2026, 10, 25)), "weekday")
    assert sunday["picks"][0]["composer"] == "Johann Sebastian Bach"
    assert sunday["picks"][0]["reason"] == "Bach on a Sunday"


def test_monday_is_mozart_and_december_mondays_messiah():
    mon = shelf(curation.today(LIBRARY, D(2026, 10, 26)), "weekday")
    assert mon["title"] == "For Monday" and mon["subtitle"].startswith("Mozart Monday")
    assert mon["picks"][0]["composer"] == "Wolfgang Amadeus Mozart" and mon["picks"][0]["reason"] == "Mozart Monday"
    assert {p["work_type"] for p in mon["picks"][1:]} <= {"Keyboard", "Chamber"}


def test_messiah_monday_when_the_season_leaves_it():
    lib = [c for c in LIBRARY if c.title != "Le quattro stagioni: L'inverno, RV 297"]
    day = D(2026, 12, 7)
    used = set()
    s = curation.for_weekday(lib, day, used)
    assert s["picks"][0]["reason"] == "Messiah Monday" and s["subtitle"].startswith("Messiah Monday")


def test_halloween_and_october_crescendo():
    oct27 = curation.themes(D(2026, 10, 27))
    assert oct27[0].id == "optoberfest"
    hw = curation.themes(D(2026, 10, 31))
    assert [t.id for t in hw][:2] == ["feast", "halloween"]      # Reformation Day too
    rules = {t.id: t for t in hw}["halloween"].rules
    assert all(r[2] == 1.0 for r in rules) and not any("requiem" in r[1] for r in rules)   # 'yes' works only
    early, late = curation.themes(D(2026, 10, 1))[0], curation.themes(D(2026, 10, 30))[0]
    assert late.weight > early.weight
    danse = curation._match(early.rules, LIBRARY[15]), curation._match(late.rules, LIBRARY[15])
    req = curation._match(early.rules, LIBRARY[18]), curation._match(late.rules, LIBRARY[18])
    assert danse[1][0] > danse[0][0] and req[1][0] < req[0][0]


def test_southern_hemisphere_flips_the_natural_season():
    july = D(2026, 7, 15)
    assert curation.natural_season(july) == "summer" and curation.natural_season(july, "south") == "winter"
    assert curation.natural_season(D(2026, 12, 1)) == "winter" and curation.natural_season(D(2026, 3, 1)) == "spring"
    north = shelf(curation.today(LIBRARY, july), "season")
    south = shelf(curation.today(LIBRARY, july, "south"), "season")
    assert any("RV 315" in p["title"] for p in north["picks"]) and north["subtitle"] == "Summer"
    assert any("RV 297" in p["title"] for p in south["picks"]) and south["subtitle"] == "Winter"


def test_acclaimed_shelf_uses_the_accolade_text():
    acc = shelf(curation.today(LIBRARY, D(2026, 8, 4)), "acclaimed")
    assert acc and {p["reason"] for p in acc["picks"]} == {"Gramophone Award 1990"}
    plain = [c for c in LIBRARY if c.acclaim == 1.0]
    assert shelf(curation.today(plain, D(2026, 8, 4)), "acclaimed") is None


def test_acclaim_and_evidence_weight_the_draw():
    loud = cand("Symphony No. 1", "Johannes Brahms", accs=[{"counts": True, "weight": 3.0, "text": "x"}])
    quiet = cand("Symphony No. 2", "Johannes Brahms", complete=0)
    assert loud.score > quiet.score
    wins = sum(curation.choose([(0, loud.score, loud, ""), (0, quiet.score, quiet, "")], D(2026, 1, 1) +
                               dt.timedelta(days=i), "t", set(), 1)[0]["title"] == "Symphony No. 1"
               for i in range(200))
    assert wins > 120


def test_deterministic_per_day_and_varies_across_days():
    day = D(2026, 10, 27)
    assert curation.today(LIBRARY, day) == curation.today(list(reversed(LIBRARY)), day)
    picks = {tuple(p["work_id"] for s in curation.today(LIBRARY, day + dt.timedelta(weeks=w)) for p in s["picks"])
             for w in range(6)}
    assert len(picks) > 1


@pytest.mark.parametrize("day", [D(2026, 12, 25), D(2027, 3, 28), D(2026, 10, 25), D(2026, 10, 26), D(2026, 7, 6),
                                 D(2026, 12, 17), D(2026, 10, 31)])
def test_one_composer_per_shelf_and_one_shelf_per_work(day):
    shelves = curation.today(LIBRARY, day)
    seen = []
    for s in shelves:
        comps = [p["composer"] for p in s["picks"]]
        assert len(comps) == len(set(comps)), s
        assert 0 < len(s["picks"]) <= curation.SIZE
        seen += [p["work_id"] for p in s["picks"]]
    assert len(seen) == len(set(seen))


def test_same_work_indexed_twice_is_not_shown_twice():
    a = cand("Symphony No. 4", "Johannes Brahms", wid="w_a")
    b = cand("Symphony No. 4", "Johannes Brahms", wid="w_b")
    assert a.ident == b.ident
    shelves = curation.today([a, b, cand("Ballade No. 1", "Frédéric Chopin", "Keyboard")], D(2026, 10, 25))
    assert sum(p["title"] == "Symphony No. 4" for s in shelves for p in s["picks"]) == 1


def test_widened_window_states_the_real_date():
    otd = shelf(curation.today([LIBRARY[19]], D(2026, 10, 29)), "on_this_day")
    assert otd["subtitle"] == "Around 29 October"
    assert otd["picks"][0]["reason"] == "Premiered 27 October 1919, Queen's Hall, London (107 years ago)"


def test_leap_day_anniversaries_fall_on_28_february():
    assert curation._on(2027, "02-29") == D(2027, 2, 28) and curation._on(2028, "02-29") == D(2028, 2, 29)
    assert curation._gap(D(2027, 1, 1), "12-31") == 1 and curation._gap(D(2026, 12, 31), "01-01") == -1


def test_premiere_dataset_is_sourced_and_parses():
    ps = curation.premieres()
    assert len(ps) >= 100
    for p in ps:
        assert p.source.startswith("https://en.wikipedia.org/wiki/") and p.place and p.date.year > 1600
    # every composer either resolves in the authority list or is matched by surname
    assert sum(composers.lookup(p.composer) is not None for p in ps) > len(ps) * 0.8


def test_titles_and_header():
    assert [t for _, t in curation.titles(D(2026, 10, 27))] == ["On this day", "For the season", "For Tuesday",
                                                                 "Acclaimed recordings"]
    h = curation.header(D(2026, 10, 25), "south")
    assert h == {"date": "2026-10-25", "weekday": "Sunday", "label": "25 October 2026",
                 "liturgical": "21st Sunday after Trinity", "season": "spring", "hemisphere": "south"}
    with pytest.raises(ValueError):
        curation.header(D(2026, 10, 25), "east")


def test_candidates_from_a_real_index(tmp_path):
    lib = Lib(tmp_path)
    karajan, bph = Person("Herbert von Karajan", "conductor", "person"), Person("Berliner Philharmoniker",
                                                                                 "orchestra", "group")
    lib.add("Symphony No. 9 in D minor, Op. 125 'Choral'", [karajan, bph], recorded="1962")
    lib.add("Symphony No. 9 in D minor, Op. 125 'Choral'", [karajan, bph], recorded="1977",
            movements=("I. Allegro",))
    lib.add("Danse macabre, Op. 40", [bph], composer="Camille Saint-Saëns")
    cands = curation.candidates(lib.ix)
    assert len(cands) == 2
    ninth = next(c for c in cands if "125" in c.title)
    assert ninth.kc.name == "Ludwig van Beethoven" and ninth.ckey == "Ludwig van Beethoven"
    assert ninth.rec["tracks"] == 2                     # the fuller of the two recordings is the Play pick
    shelves = curation.shelves(lib.ix, D(2027, 5, 7))
    [p] = shelf(shelves, "on_this_day")["picks"]
    assert p["reason"] == "Premiered on this day in 1824, Kärntnertortheater, Vienna (203 years ago)"
    assert p["play_uri"] == play_uri(p["work_id"], p["recording_id"]) == \
        f"classical://folder/works/{ninth.work_id}/{ninth.rec['id']}"
    assert curation.candidates(lib.ix, vis=["somewhere_else"]) == []
