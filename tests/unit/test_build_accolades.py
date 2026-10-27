"""scripts/build_accolades.py: the BBC Music Magazine Award page parser (docs/accolades.md), and the loader's
per-entry url column."""

import importlib.util

import pytest

from classical_core import accolades as A
from tests.helpers import ROOT
from tests.unit.test_accolades import facts, match


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load("build_accolades")


def test_html_lines_keep_heading_bold_and_italic_styles():
    html = ("<nav>Home</nav><h2>Orchestral Award</h2><p><strong>Jane Doe</strong></p><p><em>A Label</em></p>"
            "<p>Brahms: Symphony No. 4</p><p>plain <b>mixed</b></p><!-- a comment -->")
    assert B.html_lines(html) == [("Orchestral Award", "head"), ("Jane Doe", "head"), ("A Label", "em"),
                                  ("Brahms: Symphony No. 4", ""), ("plain mixed", "")]


def test_bbc_category_headings():
    assert B.bbc_category("Orchestral & Disc of the Year") == ["Orchestral", "Recording of the Year"]
    assert B.bbc_category("BBC Music Magazine Awards 2024 - Concerto") == ["Concerto"]
    assert B.bbc_category("DVD Award") == []                      # an award that is not kept
    assert B.bbc_category("Personality of the Year") == []
    assert B.bbc_category("Some news about the awards") is None   # not a heading at all


def test_bbc_disc_layouts():
    assert B.bbc_disc([("Shostakovich: Symphony No. 7", ""), ("Example Symphony Orchestra/Jane Doe", "")],
                      "Example EX 1234") == {"composers": ["Shostakovich"], "work": "Symphony No. 7",
                                             "performers": "Example Symphony Orchestra/Jane Doe",
                                             "label": "Example EX 1234"}
    # the artist heading first, then 'Composer Work' with no colon: split at a known composer
    d = B.bbc_disc([("Jane Doe", "head"), ("Smetana Má vlast", ""), ("Example Philharmonic/Jane Doe", "")],
                   None, "Jane Doe", frozenset({"Smetana"}))
    assert (d["composers"], d["work"], d["performers"]) == (["Smetana"], "Má vlast", "Example Philharmonic/Jane Doe")
    # a label and catalogue number run on at the end of the performer line
    d = B.bbc_disc([("Delius: Orchestral Works", ""), ("Example Sinfonia/Jane Doe Chandos CHSA 0001", "")], None)
    assert (d["performers"], d["label"]) == ("Example Sinfonia/Jane Doe", "Chandos CHSA 0001")
    # a multi-composer disc keeps every composer (album-level scope)
    d = B.bbc_disc([("Fauré • Ravel String Quartets", ""), ("Example Quartet", "")], "Label")
    assert (d["composers"], d["work"]) == (["Fauré", "Ravel"], "String Quartets")


def test_bbc_disc_drops_what_it_cannot_take_apart():
    # an all-capitals heading naming the artist is not 'composer + work'; the work line is cut short
    lines = [("PAVEL HAAS STRING QUARTET", "head"), ("No. 2, from the Monkey Mountains; Janácek - String Quartet", ""),
             ("Pavel Haas String Quartet.", "")]
    assert B.bbc_disc(lines, None, None, frozenset({"Pavel Haas", "Janáček"})) is None


def test_bbc_people_normalises_performers():
    assert B.bbc_people("Jane Doe, John Roe; Example SO, Cologne/Max Mustermann") == [
        "Jane Doe", "John Roe", "Max Mustermann", "Example Symphony Orchestra, Cologne"]
    assert B.bbc_people("Jane Doe (violin), Sir John Roe (conductor); Example Ensemble/Max Mustermann") == [
        "Jane Doe", "John Roe", "Max Mustermann", "Example Ensemble"]
    assert B.bbc_people("Raphaë l Doe, Example Consort") == ["Raphaël Doe", "Example Consort"]
    assert B.bbc_people("Jane Doe (piano, direction); Example Chamber Orch") == ["Jane Doe",
                                                                                "Example Chamber Orchestra"]


def test_recitals_and_anthologies_are_dropped():
    known = {"faure", "ravel", "schubert"}

    def disc(comp, work):
        return {"composers": [comp], "work": work, "performers": "Jane Doe"}

    assert B.is_compilation(disc("Jane Doe", "Songs by Fauré and Ravel"), known)
    assert B.is_compilation(disc("Jane Doe", "Arias"), known)
    assert not B.is_compilation(disc("Schubert", "Winterreise"), known)
    assert not B.is_compilation(disc("Unknown Name", "Symphony No. 2"), known)   # a composer new to the list


@pytest.fixture
def fake_dataset(monkeypatch):
    d = {"categories": [{"award": "Example Award", "category": "Opera", "source": 0,
                         "url": "https://example.org/list"}],
         "sources": [{"title": "Example", "url": "https://example.org/"}],
         "entries": [["e1", 0, 2020, "winner", "Verdi", "Aida", ["Jane Doe"]],
                     ["e2", 0, 2020, "winner", "Verdi", "Otello", ["Jane Doe"], "Example EX 1"],
                     ["e3", 0, 2021, "winner", "Verdi", "Falstaff", ["Jane Doe"], None, "https://example.org/2021"]]}
    monkeypatch.setattr(A, "dataset", lambda: d)
    A.entries.cache_clear()
    yield d
    A.entries.cache_clear()


def test_loader_reads_the_label_and_the_per_entry_url(fake_dataset):
    e1, e2, e3 = A.entries()
    assert (e1.label, e1.url) == (None, "https://example.org/list")
    assert (e2.label, e2.url) == ("Example EX 1", "https://example.org/list")
    assert (e3.label, e3.url) == (None, "https://example.org/2021")      # a null label, the winner's own page


def test_bbc_award_years_exclude_sessions_after_the_awarded_disc():
    assert A.year_window("BBC Music Magazine Award", 2015) == (2013, 2014)
    e = A.Entry("e1", "BBC Music Magazine Award", "Opera", 2015, "winner", "Beethoven", "Fidelio",
                ("Otto Klemperer", "Philharmonia Orchestra"), None, None, "test")
    credits = (("Otto Klemperer", "conductor"), ("Philharmonia Orchestra", "orchestra"))
    assert match(e, facts(credits=credits, released=2014)) == "this_release"
    assert match(e, facts(credits=credits, released=2015, recorded=2015)) is None   # a later concert
