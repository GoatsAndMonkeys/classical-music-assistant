"""Composition and premiere dates of works (classical_core/dates.py): parsing, sources, priority, the job."""

import datetime as dt
import json

import pytest

from classical_core import curation, views
from classical_core import dates as wd
from classical_core.index import AlbumIn, MappingIn, TrackIn
from classical_core.mb import WORK_INC, MBClient
from classical_core.store import SqliteMBCache
from classical_core.tags import Person, TagInfo
from tests.mbfake import uid
from tests.unit.test_index import Reader, new_index, run

BEETHOVEN = "Ludwig van Beethoven"
FIDELIO = "Fidelio, Op. 72"
WORK5 = "Symphony No. 5 in C minor, Op. 67"
CONCERTO = "Violin Concerto in D major, Op. 61"
KARAJAN = Person("Herbert von Karajan", "conductor", "person")


class Lib:
    def __init__(self, tmp_path):
        self.r = Reader()
        self.ix = new_index(tmp_path, mb=False, reader=self.r)
        self.n = 0

    def add(self, work, composition_date=None, composer=BEETHOVEN, recorded=None):
        self.n += 1
        aid = f"A{self.n}"
        tracks = []
        for i, mov in enumerate(("I. Allegro", "II. Andante")):
            path = f"{aid}/{i}"
            tracks.append(TrackIn(str(self.n * 100 + i), 1, i + 1, f"{work}: {mov}", "", 300,
                                  mappings=[MappingIn("filesystem_local", path, fingerprint="f", path=path)]))
            self.r.tags[path] = TagInfo(title=f"{work}: {mov}", work=work, movement_name=mov, movement_no=i + 1,
                                        composers=[composer], performers=[KARAJAN], recorded=recorded,
                                        composition_date=composition_date)
        run(self.ix.index_album(AlbumIn(aid, f"Album {aid}", "", 2000, None, tracks)))
        return aid

    def wid(self, title):
        [w] = [w for w in self.ix.works() if w["title"] == title]
        return w["id"]

    def row(self, title):
        return dict(self.ix.con.execute("SELECT * FROM work WHERE id=?", (self.wid(title),)).fetchone())

    def refresh(self):
        return wd.refresh(self.ix.con)


@pytest.fixture
def lib(tmp_path):
    return Lib(tmp_path)


# ---- parsing and formatting ------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "want"), [
    ("1805", ("1805", None, "year")),
    ("1804–05", ("1804", "1805", "year")),
    ("1804/1805", ("1804", "1805", "year")),
    ("1898-1902", ("1898", "1902", "year")),
    ("1804 to 1806", ("1804", "1806", "year")),
    ("1804-05", ("1804", "1805", "year")),        # an ASCII hyphen before a later two-digit year: a range
    ("1805-11", ("1805", "1811", "year")),
    ("c. 1804-05", ("1804", "1805", "circa")),
    ("1898-03", ("1898-03", None, "month")),     # not later than '98: a month
    ("1900-01", ("1900", "1901", "year")),
    ("1805-11-20", ("1805-11-20", None, "day")),
    ("1860s", ("1860", "1869", "decade")),
    ("186x", ("1860", "1869", "decade")),
    ("c. 1805", ("1805", None, "circa")),
])
def test_parse_partial(text, want):
    assert wd.parse_partial(text) == want


@pytest.mark.parametrize("text", ["", "soon", "123", "3000", "1700–1850", "1806–1804", "Op. 67", "1850-20"])
def test_parse_partial_rejects(text):
    assert wd.parse_partial(text) is None


def test_a_premiere_reads_a_hyphenated_month_as_a_month():
    assert wd.parse_partial("1805-11", ranges=False) == ("1805-11", None, "month")
    assert wd.parse_partial("1804-05", ranges=False) == ("1804-05", None, "month")


def test_list_year_uses_a_premiere_only_in_the_composers_lifetime():
    assert wd.sort_year({"composed_start": "1822", "premiered": "1865-12-17"}, "Franz Schubert") == 1822
    assert wd.sort_year({"premiered": "1865-12-17"}, "Franz Schubert") is None          # Unfinished: posthumous
    assert wd.sort_year({"premiered": "1814-05-23"}, BEETHOVEN) == 1814
    assert wd.sort_year({"premiered": "1912-06-26"}, "Gustav Mahler") == 1912           # died 1911: within a year
    assert wd.sort_year({"premiered": "1814-05-23"}, "Nobody Known") is None
    assert wd.sort_year({"premiered": "1814-05-23"}, None) is None


def test_formatting_and_periods():
    assert wd.year_range(1804, 1805) == "1804–05"
    assert wd.year_range(1898, 1902) == "1898–1902"
    assert wd.year_range(1805, None) == "1805"
    assert wd.composed_text("1805", None, "circa") == "c. 1805"
    assert wd.composed_text("1860", "1869", "decade") == "1860s"
    assert wd.composed_text(None, None, None) is None
    assert [wd.period_of(y) for y in (1550, 1600, 1749, 1750, 1819, 1820, 1899, 1900, 1974, 1975, None)] == [
        "early", "baroque", "baroque", "classical", "classical", "romantic", "romantic", "20th", "20th",
        "contemporary", None]
    assert wd.describe({"composed_start": "1804", "composed_end": "1805", "composed_precision": "year",
                        "composed_source": "musicbrainz", "premiered": "1805-11-20", "premiered_precision": "day",
                        "premiered_source": "dataset"})["text"] == "composed 1804–05 · premiered 1805"


# ---- sources and priority --------------------------------------------------------------------------------

def test_tags_give_composition_date_and_dataset_the_premiere(lib):
    lib.add(WORK5, composition_date="1804-1808")
    lib.add(CONCERTO)
    st = lib.refresh()
    assert st["tags"] == 1 and st["dataset"] == 2
    w5 = lib.row(WORK5)
    assert (w5["composed_start"], w5["composed_end"], w5["composed_source"]) == ("1804", "1808", "tags")
    assert (w5["premiered"], w5["premiered_source"]) == ("1808-12-22", "dataset")
    vc = lib.row(CONCERTO)
    assert vc["composed_start"] is None and vc["premiered"] == "1806-12-23"
    # incremental: nothing changes the second time
    assert lib.refresh().get("changed", 0) == 0


def test_recording_and_release_years_are_never_dates(lib):
    lib.add("Romance No. 1 in G major, Op. 40", recorded="1961-05-02")
    lib.refresh()
    w = lib.row("Romance No. 1 in G major, Op. 40")
    assert w["composed_start"] is None and w["premiered"] is None


def test_dataset_guards_skip_derived_works_and_other_types(lib):
    lib.add("Cantata on the Death of Emperor Joseph II, WoO 87")
    lib.add("Symphony No. 5 in C minor, Op. 67 (arr. Liszt for piano)")
    lib.refresh()
    assert lib.row("Cantata on the Death of Emperor Joseph II, WoO 87")["premiered"] is None
    assert lib.row("Symphony No. 5 in C minor, Op. 67 (arr. Liszt for piano)")["premiered"] is None


def test_dataset_skips_joined_titles_and_pieces_of_a_stage_work(lib):
    debussy, wagner, ravel = "Claude Debussy", "Richard Wagner", "Maurice Ravel"
    joined = "Blanc et noir; Prélude à l'après‑midi d'un faune"
    lib.add(joined, composer=debussy)
    lib.add("Prélude à l'après-midi d'un faune", composer=debussy)
    lib.add("Parsifal – Prelude to Act\u202f1 and Good Friday music", composer=wagner)
    lib.add("Parsifal: Act III", composer=wagner)
    lib.add("Parsifal, WWV 111", composer=wagner)
    lib.add("Daphnis et Chloé (complete ballet; possible)", composer=ravel)
    lib.add("Cavalleria rusticana / Parsifal", composer=wagner)
    lib.refresh()
    assert lib.row(joined)["premiered"] is None
    assert lib.row("Prélude à l'après-midi d'un faune")["premiered"] == "1894-12-22"
    assert lib.row("Parsifal – Prelude to Act\u202f1 and Good Friday music")["premiered"] is None
    assert lib.row("Parsifal: Act III")["premiered"] is None
    assert lib.row("Cavalleria rusticana / Parsifal")["premiered"] is None
    assert lib.row("Parsifal, WWV 111")["premiered"] == "1882-07-26"
    assert lib.row("Daphnis et Chloé (complete ballet; possible)")["premiered"] is not None   # ';' in brackets
    # "On this day" falls back to the list for works the dates job left unsettled: the same guards apply
    got = curation.events(dt.date(2026, 7, 26), curation.candidates(lib.ix))
    assert [c.title for e, c in got if e.kind == "premiere"] == ["Parsifal, WWV 111"]
    got = curation.events(dt.date(2026, 12, 22), curation.candidates(lib.ix))
    assert [c.title for e, c in got if e.kind == "premiere"] == ["Prélude à l'après-midi d'un faune"]


def test_user_premiere_reads_a_hyphenated_month(lib):
    lib.add(WORK5)
    lib.refresh()
    wd.set_user_dates(lib.ix.con, lib.wid(WORK5), "u", composed="1804-08", premiered="1808-12")
    w = lib.row(WORK5)
    assert (w["composed_start"], w["composed_end"]) == ("1804", "1808")
    assert (w["premiered"], w["premiered_precision"]) == ("1808-12", "month")


def test_user_correction_wins_and_survives_reindex(lib):
    lib.add(WORK5, composition_date="1804-1808")
    lib.refresh()
    con = lib.ix.con
    wid = lib.wid(WORK5)
    with pytest.raises(ValueError):
        wd.set_user_dates(con, wid, "u", composed="whenever")
    with pytest.raises(ValueError):
        wd.set_user_dates(con, wid, "u")
    wd.set_user_dates(con, wid, "u", composed="1807–08", premiered="unknown")
    w = lib.row(WORK5)
    assert (w["composed_start"], w["composed_end"], w["composed_source"]) == ("1807", "1808", "user")
    assert w["premiered"] is None and w["premiered_source"] == "user"
    d = wd.describe(w)
    assert d["text"] == "composed 1807–08" and d["sources"] == ["your correction"] and d["hidden"] == ["premiered"]
    log = con.execute("SELECT op, payload_json FROM correction_log").fetchall()
    assert [r["op"] for r in log] == ["work_dates"]
    # re-index the same album: the correction stays
    lib.n -= 1
    lib.add(WORK5, composition_date="1804-1808")
    lib.refresh()
    w = lib.row(WORK5)
    assert (w["composed_start"], w["composed_source"], w["premiered"]) == ("1807", "user", None)
    # '' returns both to the automatic sources
    wd.set_user_dates(con, wid, "u", composed="", premiered="")
    w = lib.row(WORK5)
    assert (w["composed_start"], w["composed_source"]) == ("1804", "tags")
    assert (w["premiered"], w["premiered_source"]) == ("1808-12-22", "dataset")


# ---- MusicBrainz -----------------------------------------------------------------------------------------

def _mb_work(mbid, title, begin=None, end=None, premiere=None, parts=()):
    rels = [{"target-type": "artist", "type": "composer", "direction": "backward", "begin": begin, "end": end,
             "artist": {"id": uid(999), "name": BEETHOVEN}}]
    if premiere:
        rels.append({"target-type": "event", "type": "premiere", "direction": "backward",
                     "event": {"id": uid(777), "name": "premiere", "cancelled": False,
                               "life-span": {"begin": premiere}}})
    rels += [{"target-type": "work", "type": "parts", "direction": "forward", "work": {"id": p, "title": "act"}}
             for p in parts]
    return {"id": mbid, "title": title, "relations": rels}


class DatesMB:
    def __init__(self, works):
        self.works = {w["id"]: w for w in works}
        self.calls: list[str] = []
        self.fail = False

    async def fetch(self, url, headers):
        self.calls.append(url)
        if self.fail:
            return 503, b"{}"
        m = url.split("/work/")[1].split("?")[0]
        if m in self.works:
            return 200, json.dumps(self.works[m]).encode()
        return 404, b'{"error": "Not Found"}'

    def client(self, con):
        return MBClient("test@example.invalid", cache=SqliteMBCache(con), fetch=self.fetch, interval=0)


def _link(con, wid, mbid):
    con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)", (wid, mbid))
    con.commit()


def test_musicbrainz_composer_dates_parts_and_premiere(lib):
    lib.add(FIDELIO)
    lib.add(WORK5)
    con = lib.ix.con
    fid, act1, act2, w5 = uid(1), uid(2), uid(3), uid(4)
    _link(con, lib.wid(FIDELIO), fid)
    _link(con, lib.wid(WORK5), w5)
    fake = DatesMB([_mb_work(fid, "Fidelio", parts=(act1, act2), premiere="1814-05-23"),
                    _mb_work(act1, "Act I", "1805", "1806"), _mb_work(act2, "Act II", "1814", "1814"),
                    _mb_work(w5, "Symphony No. 5", "1804", "1808", premiere="1808-12-22")])
    mb = fake.client(con)
    lib.refresh()
    due = wd.mb_due(con, limit=50)
    assert set(due) == {fid, w5}
    assert run(wd.fetch_mb(mb, due)) == (2, None)
    assert wd.mb_due(con, limit=50) == []
    # the acts are read from the cache the indexer's hierarchy walk fills; the dates job fetches no parts
    for p in (act1, act2):
        run(mb.get(mb.url("work", p, WORK_INC)))
    st = lib.refresh()
    assert st["musicbrainz"] >= 3
    f = lib.row(FIDELIO)
    assert (f["composed_start"], f["composed_end"], f["composed_source"]) == ("1805", "1814", "musicbrainz")
    assert (f["premiered"], f["premiered_source"]) == ("1814-05-23", "musicbrainz")
    s = lib.row(WORK5)
    assert (s["composed_start"], s["composed_end"]) == ("1804", "1808")
    # a second refresh reads nothing new from the cache
    assert lib.refresh().get("musicbrainz", 0) == 0
    calls = len(fake.calls)
    assert run(wd.fetch_mb(mb, wd.mb_due(con, limit=50))) == (0, None) and len(fake.calls) == calls


def test_musicbrainz_transient_failure_defers_and_mb_off_uses_cache_only(lib):
    lib.add(WORK5)
    con = lib.ix.con
    _link(con, lib.wid(WORK5), uid(4))
    fake = DatesMB([_mb_work(uid(4), "Symphony No. 5", "1804", "1808")])
    fake.fail = True
    mb = fake.client(con)
    lib.refresh()
    n, err = run(wd.fetch_mb(mb, wd.mb_due(con)))
    assert n == 0 and err
    # MusicBrainz off: the offline step never touches the network and still works
    lib.refresh()
    assert lib.row(WORK5)["composed_source"] is None
    fake.fail = False
    run(wd.fetch_mb(fake.client(con), wd.mb_due(con)))
    calls = len(fake.calls)
    lib.refresh()
    assert len(fake.calls) == calls
    assert lib.row(WORK5)["composed_source"] == "musicbrainz"


def test_tags_beat_musicbrainz_and_mb_due_skips_settled_works(lib):
    lib.add(WORK5, composition_date="1807")
    con = lib.ix.con
    _link(con, lib.wid(WORK5), uid(4))
    lib.refresh()
    wd.set_user_dates(con, lib.wid(WORK5), "u", premiered="1808-12-22")
    assert wd.mb_due(con) == []


# ---- views and curation ----------------------------------------------------------------------------------

def test_views_filter_sort_and_count_by_period_and_decade(lib):
    lib.add(WORK5, composition_date="1804-1808")
    lib.add(CONCERTO)                                   # premiered 1806 (dataset)
    lib.add("Grosse Fuge, Op. 133", composition_date="1825")
    lib.add("Bagatelle in A minor, WoO 59")             # undated
    lib.refresh()
    res = views.works(lib.ix, sort="date")
    titles = [w["title"] for w in res["items"]]
    assert titles == [WORK5, CONCERTO, "Grosse Fuge, Op. 133", "Bagatelle in A minor, WoO 59"]
    assert res["items"][0]["composed_text"] == "1804–08" and res["items"][1]["premiered"] == 1806
    assert {p["period"]: p["n"] for p in res["periods"]} == {"classical": 2, "romantic": 1}
    assert {d["decade"]: d["n"] for d in res["decades"]} == {1800: 2, 1820: 1}
    assert res["undated"] == 1
    desc = [w["title"] for w in views.works(lib.ix, sort="date_desc")["items"]]
    assert desc[:3] == ["Grosse Fuge, Op. 133", CONCERTO, WORK5] and desc[-1] == "Bagatelle in A minor, WoO 59"
    assert [w["title"] for w in views.works(lib.ix, period="romantic")["items"]] == ["Grosse Fuge, Op. 133"]
    assert {w["title"] for w in views.works(lib.ix, decade=1800)["items"]} == {WORK5, CONCERTO}
    # a composition range matches a year range it overlaps
    assert [w["title"] for w in views.works(lib.ix, date_from=1806, date_to=1806)["items"]] == [WORK5]
    assert [w["title"] for w in views.works(lib.ix, date_kind="premiered", date_from=1806,
                                            date_to=1806)["items"]] == [CONCERTO]
    for bad in ({"period": "medieval"}, {"sort": "random"}, {"date_kind": "written"}):
        with pytest.raises(ValueError):
            views.works(lib.ix, **bad)
    page = views.work(lib.ix, lib.wid(WORK5))
    assert page["work"]["dates"]["text"] == "composed 1804–08 · premiered 1808"
    assert page["work"]["dates"]["sources"] == ["file tags", "premieres list"]


def test_on_this_day_uses_the_stored_premiere(lib):
    lib.add(WORK5)
    lib.refresh()
    con = lib.ix.con
    wd.set_user_dates(con, lib.wid(WORK5), "u", premiered="1808-12-23", premiered_place="somewhere")
    cands = curation.candidates(lib.ix)
    got = curation.events(dt.date(2026, 12, 23), cands)
    assert [(e.kind, e.year, c.title) for e, c in got if e.kind == "premiere"] == [("premiere", 1808, WORK5)]
    # the bundled list's 22 December no longer fires for this work
    assert not [e for e, _ in curation.events(dt.date(2026, 12, 22), cands) if e.kind == "premiere"]
