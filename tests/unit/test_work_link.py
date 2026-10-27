"""Linking library works to MusicBrainz works (classical_core/work_link.py), with mocked MusicBrainz answers."""

import json
import urllib.parse

import pytest

from classical_core import views
from classical_core import work_link as wl
from classical_core.mb import MBClient
from classical_core.store import SqliteMBCache
from tests.mbfake import uid
from tests.unit.test_dates import BEETHOVEN, Lib
from tests.unit.test_index import run

ARID = uid(777)
OTHER = uid(778)


def mbw(n, title, key=None, part_of=None, arranged_from=None, composer=ARID, aliases=()):
    rels = [{"type": "composer", "direction": "backward", "target-type": "artist",
             "artist": {"id": composer, "name": "x"}}]
    if part_of:
        rels.append({"type": "parts", "direction": "backward", "target-type": "work",
                     "work": {"id": part_of, "title": "whole"}})
    if arranged_from:
        rels.append({"type": "arrangement", "direction": "backward", "target-type": "work",
                     "work": {"id": arranged_from, "title": "original"}})
    return {"id": uid(n), "title": title, "relations": rels, "aliases": [{"name": a} for a in aliases],
            "attributes": [{"type": "Key", "value": key}] if key else []}


class WorksMB:
    """A composer's MusicBrainz works, answered as browse pages, searches and lookups; every URL is counted."""

    def __init__(self, works, count=None):
        self.works = works
        self.count = count                # pretend MusicBrainz has this many works (forces search mode)
        self.calls: list[str] = []
        self.fail = False

    async def fetch(self, url, headers):
        self.calls.append(url)
        if self.fail:
            return 503, b"{}"
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith("/work") and "artist" in q:
            off = int(q["offset"][0])
            return 200, json.dumps({"work-count": self.count or len(self.works),
                                    "works": self.works[off:off + wl.PAGE]}).encode()
        if u.path.endswith("/work") and "query" in q:
            words = q["query"][0].split("work:(")[1].rstrip(")").split()
            hits = [{k: v for k, v in w.items() if k != "attributes"} for w in self.works
                    if any(x in w["title"].lower() for x in words)]
            return 200, json.dumps({"works": hits}).encode()
        mbid = u.path.rsplit("/", 1)[1]
        for w in self.works:
            if w["id"] == mbid:
                return 200, json.dumps(w).encode()
        return 404, b'{"error": "Not Found"}'

    def client(self, con):
        return MBClient("test@example.invalid", cache=SqliteMBCache(con), fetch=self.fetch, interval=0)


@pytest.fixture
def lib(tmp_path):
    return Lib(tmp_path)


def _arid(lib, composer=BEETHOVEN, arid=ARID):
    cid = lib.ix.con.execute("SELECT id FROM composer WHERE name=?", (composer,)).fetchone()[0]
    lib.ix.con.execute("INSERT OR IGNORE INTO alias(entity, entity_id, kind, value) VALUES ('composer', ?, 'mbid', ?)",
                       (cid, arid))


def job(lib, mb):
    """The glue's loop: offline refresh, fetch what is due, until nothing is."""
    con = lib.ix.con
    client = mb.client(con) if mb else None
    for _ in range(50):
        stats, due = wl.refresh(con)
        if not due or client is None:
            return stats
        _n, err = run(wl.fetch(client, due))
        if err:
            return stats
    raise AssertionError("the job never settled")


def match(lib, title):
    r = lib.ix.con.execute("SELECT * FROM mb_work_match WHERE work_id=?", (lib.wid(title),)).fetchone()
    return dict(r) | {"detail": json.loads(r["detail_json"])}


def linked(lib, title):
    r = lib.ix.con.execute("SELECT value FROM alias WHERE entity='work' AND kind='mbid' AND entity_id=?",
                           (lib.wid(title),)).fetchone()
    return r[0] if r else None


SYM5 = mbw(1, "Symphony no. 5 in C minor, op. 67", "C minor")
SYM6 = mbw(2, "Symphony no. 6 in F major, op. 68 “Pastorale”", "F major")
SYM5_I = mbw(3, "Symphony no. 5 in C minor, op. 67: I. Allegro con brio", part_of=uid(1))
SYM4 = mbw(4, "Symphony no. 4 in B-flat major, op. 60", "B-flat major")
PC4 = mbw(5, "Concerto for Piano and Orchestra no. 4 in G major, op. 58", "G major")


def test_a_matching_catalogue_number_links(lib):
    lib.add("Symphony No. 5 in C minor, Op. 67")
    _arid(lib)
    mb = WorksMB([SYM5, SYM6, SYM5_I])
    job(lib, mb)
    assert linked(lib, "Symphony No. 5 in C minor, Op. 67") == uid(1)
    m = match(lib, "Symphony No. 5 in C minor, Op. 67")
    assert m["state"] == "linked" and "Op. 67" in m["how"]
    assert len(mb.calls) == 1                           # one browse page for the composer
    assert not lib.ix.con.execute("SELECT 1 FROM review_item WHERE reason=?", (wl.REASON,)).fetchall()


def test_the_same_title_number_and_key_links(lib):
    lib.add("Symphony No. 4 in B-flat major")
    _arid(lib)
    job(lib, WorksMB([SYM4, PC4, SYM5]))
    assert linked(lib, "Symphony No. 4 in B-flat major") == uid(4)
    assert "same title, number and key" in match(lib, "Symphony No. 4 in B-flat major")["how"]


def test_an_ambiguous_match_is_a_review_item_never_a_link(lib):
    lib.add("Symphony No. 4")                           # no key: not enough to link
    lib.add("Symphony No. 7 in A major")                # MusicBrainz has it twice
    _arid(lib)
    job(lib, WorksMB([SYM4, mbw(7, "Symphony no. 7 in A major, op. 92", "A major"),
                      mbw(8, "Symphony no. 7 in A major, op. 92", "A major")]))
    assert linked(lib, "Symphony No. 4") is None and linked(lib, "Symphony No. 7 in A major") is None
    assert match(lib, "Symphony No. 4")["state"] == "review"
    assert {c["mbid"] for c in match(lib, "Symphony No. 7 in A major")["detail"]["candidates"]} == {uid(7), uid(8)}
    groups = {g["reason"]: g for g in views.review(lib.ix)["groups"]}
    items = {it["title"]: it for it in groups[wl.REASON]["items"]}
    acts = items["Symphony No. 7 in A major"]["actions"]
    assert [a["decision"] for a in acts] == ["accept", "accept", "reject"]
    assert acts[-1]["mbids"] == [uid(7), uid(8)] and acts[0]["work_id"] == lib.wid("Symphony No. 7 in A major")


def test_parts_arrangements_and_other_composers_works_are_never_candidates(lib):
    lib.add("Symphony No. 7 in A major, Op. 92")
    _arid(lib)
    job(lib, WorksMB([mbw(9, "Symphony no. 7 in A major, op. 92: II. Allegretto", part_of=uid(99)),
                      mbw(10, "Symphony no. 7 in A major, op. 92", "A major", arranged_from=uid(99)),
                      mbw(11, "Symphony no. 7 in A major, op. 92 (arr. Liszt)", "A major"),
                      mbw(12, "Symphony no. 7 in A major, op. 92", "A major", composer=OTHER)]))
    m = match(lib, "Symphony No. 7 in A major, Op. 92")
    assert m["state"] == "none" and linked(lib, "Symphony No. 7 in A major, Op. 92") is None


def test_a_library_part_or_arrangement_is_not_looked_up(lib):
    lib.add("Symphony No. 5 in C minor, Op. 67 (arr. Liszt)")
    _arid(lib)
    mb = WorksMB([SYM5])
    job(lib, mb)
    assert match(lib, "Symphony No. 5 in C minor, Op. 67 (arr. Liszt)")["state"] == "skip" and not mb.calls


def test_a_name_alone_links_only_a_work_with_that_name_alone(lib):
    lib.add("Kindertotenlieder", composer="Gustav Mahler")
    lib.add("Leonore")
    _arid(lib)
    _arid(lib, "Gustav Mahler", OTHER)
    job(lib, WorksMB([mbw(20, "Kindertotenlieder", composer=OTHER), mbw(21, "Rückert-Lieder", composer=OTHER),
                      mbw(22, "Overture “Leonore” no. 3 in C major, op. 72b", "C major", aliases=["Leonore"])]
                     ))
    assert linked(lib, "Kindertotenlieder") == uid(20)
    assert linked(lib, "Leonore") is None and match(lib, "Leonore")["state"] == "review"


def test_a_rejection_survives_and_the_candidate_is_never_proposed_again(lib):
    lib.add("Symphony No. 4")
    _arid(lib)
    mb = WorksMB([SYM4])
    job(lib, mb)
    wid = lib.wid("Symphony No. 4")
    assert match(lib, "Symphony No. 4")["state"] == "review"
    with pytest.raises(ValueError, match="not a MusicBrainz work proposed"):
        wl.set_user_link(lib.ix.con, wid, "seth", "accept", [uid(5)])
    wl.set_user_link(lib.ix.con, wid, "seth", "reject", [uid(4)])
    assert not lib.ix.con.execute("SELECT 1 FROM review_item WHERE id=?", (f"mbw:{wid}",)).fetchall()
    # forget the job's own decisions entirely (as a matcher change would): the person's still wins
    lib.ix.con.execute("DELETE FROM mb_work_match")
    lib.add("Symphony No. 4")                            # and re-index
    job(lib, mb)
    assert linked(lib, "Symphony No. 4") is None
    assert match(lib, "Symphony No. 4")["state"] == "none"
    assert not lib.ix.con.execute("SELECT 1 FROM review_item WHERE reason=?", (wl.REASON,)).fetchall()


def test_a_rejected_auto_link_is_undone_and_an_accepted_one_kept(lib):
    lib.add("Symphony No. 5 in C minor, Op. 67")
    lib.add("Symphony No. 4")
    _arid(lib)
    mb = WorksMB([SYM5, SYM4])
    job(lib, mb)
    w5, w4 = lib.wid("Symphony No. 5 in C minor, Op. 67"), lib.wid("Symphony No. 4")
    wl.set_user_link(lib.ix.con, w5, "seth", "reject", [uid(1)])
    wl.set_user_link(lib.ix.con, w4, "seth", "accept", [uid(4)])
    job(lib, mb)
    assert linked(lib, "Symphony No. 5 in C minor, Op. 67") is None
    assert linked(lib, "Symphony No. 4") == uid(4)
    # the accepted link comes back if its alias row is lost
    lib.ix.con.execute("DELETE FROM alias WHERE entity='work' AND entity_id=?", (w4,))
    job(lib, mb)
    assert linked(lib, "Symphony No. 4") == uid(4)
    ops = [json.loads(r[0]) for r in lib.ix.con.execute("SELECT payload_json FROM override WHERE op='mb_link'")]
    assert ops == [{"decision": "reject", "mbid": uid(1)}, {"decision": "accept", "mbid": uid(4)}]


def test_one_musicbrainz_work_is_never_linked_to_two_library_works(lib):
    lib.add("Symphony No. 5 in C minor, Op. 67")
    lib.add("Fidelio, Op. 72")
    _arid(lib)
    # another library work already carries the id (a file's tag, or a duplicate work)
    lib.ix.con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)",
                       (lib.wid("Fidelio, Op. 72"), uid(1)))
    job(lib, WorksMB([SYM5]))
    m = match(lib, "Symphony No. 5 in C minor, Op. 67")
    assert m["state"] == "none" and m["detail"]["candidates"][0]["taken_by"] == lib.wid("Fidelio, Op. 72")
    assert linked(lib, "Symphony No. 5 in C minor, Op. 67") is None


def test_musicbrainz_off_means_no_network(lib):
    lib.add("Symphony No. 5 in C minor, Op. 67")
    _arid(lib)
    stats = job(lib, None)
    assert linked(lib, "Symphony No. 5 in C minor, Op. 67") is None and not stats.get("linked")
    # answers already cached are still used, with no client at all
    mb = WorksMB([SYM5])
    run(mb.client(lib.ix.con).get(wl.browse_url("https://musicbrainz.org/ws/2", ARID, 0)))
    mb.calls.clear()
    job(lib, None)
    assert linked(lib, "Symphony No. 5 in C minor, Op. 67") == uid(1) and not mb.calls


def test_a_transient_failure_stops_the_chunk(lib, monkeypatch):
    async def no_wait(_s):
        return None

    monkeypatch.setattr("classical_core.mb.asyncio.sleep", no_wait)       # the client's retry backoff
    lib.add("Symphony No. 5 in C minor, Op. 67")
    _arid(lib)
    mb = WorksMB([SYM5])
    mb.fail = True
    _stats, due = wl.refresh(lib.ix.con)
    n, err = run(wl.fetch(mb.client(lib.ix.con), due + ["https://musicbrainz.org/ws/2/work/x?fmt=json"]))
    assert n == 0 and err and len(mb.calls) >= 1
    assert linked(lib, "Symphony No. 5 in C minor, Op. 67") is None


def test_a_composer_with_many_works_is_searched_work_by_work(lib):
    lib.add("Symphony No. 5 in C minor, Op. 67")
    _arid(lib)
    mb = WorksMB([SYM5, SYM6, SYM5_I], count=wl.PAGE * 40)       # 40 pages for one work: search instead
    job(lib, mb)
    assert linked(lib, "Symphony No. 5 in C minor, Op. 67") == uid(1)
    kinds = ["browse" if "artist=" in u else "search" if "query=" in u else "lookup" for u in mb.calls]
    assert kinds == ["browse", "search", "lookup"]
    assert match(lib, "Symphony No. 5 in C minor, Op. 67")["how"].startswith("search")


def test_a_hidden_work_has_no_review_item_for_the_caller(lib):
    lib.add("Symphony No. 4")
    _arid(lib)
    job(lib, WorksMB([SYM4]))
    assert views.review(lib.ix)["total"] == 1
    assert views.review(lib.ix, vis=["nobody--x"])["total"] == 0


def test_koechel_alternatives_never_join_different_numbers_of_a_set():
    a = wl.cat.primary_catalogue("Divertimento K. 439b No. 1")
    assert wl._same_cat(a, wl.cat.primary_catalogue("K. 439b No. 1"))
    assert not wl._same_cat(a, wl.cat.primary_catalogue("K. 439b No. 2"))
    assert not wl._same_cat(a, wl.cat.primary_catalogue("K. 439b"))
    assert wl._same_cat(wl.cat.primary_catalogue("Mass, K. 427"), wl.cat.primary_catalogue("K. 427/417a"))


def test_a_hyphenated_key_is_read_and_one_edition_is_not_the_work(lib):
    """MusicBrainz's Bruckner: a generic work, editions that are revisions of it, all 'in C-minor'."""
    lib.add("Symphony No. 8 in C minor, WAB 108", composer="Anton Bruckner")
    _arid(lib, "Anton Bruckner")
    generic = mbw(30, "Symphony no. 8 in C-minor, WAB 108")
    ed = mbw(31, "Symphony no. 8 in C-minor, WAB 108")
    ed["relations"].append({"type": "revision of", "direction": "backward", "target-type": "work",
                            "work": {"id": uid(30), "title": "Symphony no. 8"}})
    job(lib, WorksMB([ed, generic]))
    assert linked(lib, "Symphony No. 8 in C minor, WAB 108") == uid(30)


def test_a_title_naming_no_instrument_is_not_certainly_a_piano_sonata(lib):
    lib.add("Sonata No. 1 in C major")
    lib.add("Piano Concerto No. 4 in G major")
    _arid(lib)
    job(lib, WorksMB([mbw(32, "Piano Sonata no. 1 in C major", "C major"), PC4]))
    assert linked(lib, "Sonata No. 1 in C major") is None and match(lib, "Sonata No. 1 in C major")["state"] == "review"
    assert linked(lib, "Piano Concerto No. 4 in G major") == uid(5)       # 'and Orchestra' is no difference


def test_a_link_made_under_older_rules_is_decided_again(lib):
    lib.add("Sonata No. 1 in C major")
    _arid(lib)
    mb = WorksMB([mbw(32, "Piano Sonata no. 1 in C major", "C major")])
    job(lib, mb)
    wid = lib.wid("Sonata No. 1 in C major")
    # as the first matcher linked it
    con = lib.ix.con
    con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)", (wid, uid(32)))
    con.execute("UPDATE mb_work_match SET state='linked', mbid=?, sig='old' WHERE work_id=?", (uid(32), wid))
    job(lib, mb)
    assert linked(lib, "Sonata No. 1 in C major") is None and match(lib, "Sonata No. 1 in C major")["state"] == "review"


def test_an_unlisted_composer_found_by_alias_and_an_empty_search_asked_again(lib):
    """Glinka is not in the authority list, and MusicBrainz names him in Cyrillic."""
    lib.add("Kamarinskaya", composer="Mikhail Glinka")
    glinka = uid(790)
    found = {"on": False}
    works = WorksMB([mbw(40, "Kamarinskaya", composer=glinka)])
    base_fetch = works.fetch

    async def fetch(url, headers):
        if "/artist?" in url:
            works.calls.append(url)
            assert "alias%3A" in url
            arts = [{"id": glinka, "type": "Person", "score": 100, "name": "Михаил Иванович Глинка",
                     "life-span": {"begin": "1804", "end": "1857"}, "aliases": [{"name": "Mikhail Glinka"}]}]
            return 200, json.dumps({"artists": arts if found["on"] else []}).encode()
        return await base_fetch(url, headers)

    works.fetch = fetch
    job(lib, works)
    assert linked(lib, "Kamarinskaya") is None
    exp = lib.ix.con.execute("SELECT expires_at - fetched_at FROM mb_cache WHERE url LIKE '%/artist?%'").fetchone()[0]
    assert exp == pytest.approx(7 * 86400)               # an empty answer is kept a week, not 90 days
    found["on"] = True
    job(lib, works)
    assert linked(lib, "Kamarinskaya") is None           # still cached
    lib.ix.con.execute("UPDATE mb_cache SET expires_at=0 WHERE url LIKE '%/artist?%'")
    job(lib, works)
    assert linked(lib, "Kamarinskaya") == uid(40)


def test_unlinking_from_the_work_page(lib):
    lib.add("Symphony No. 5 in C minor, Op. 67")
    lib.add("Symphony No. 4")
    _arid(lib)
    mb = WorksMB([SYM5, SYM4])
    job(lib, mb)
    w5, w4 = lib.wid("Symphony No. 5 in C minor, Op. 67"), lib.wid("Symphony No. 4")
    assert views.work(lib.ix, w5)["mb_link"] == {"mbid": uid(1), "auto": True,
                                                  "title": "Symphony no. 5 in C minor, op. 67"}
    assert views.work(lib.ix, w4)["mb_link"] is None
    wl.set_user_link(lib.ix.con, w4, "seth", "accept", [uid(4)])
    assert views.work(lib.ix, w4)["mb_link"]["auto"] is False
    # an accepted link can be undone too, and neither comes back
    wl.set_user_link(lib.ix.con, w4, "seth", "reject", [uid(4)])
    wl.set_user_link(lib.ix.con, w5, "seth", "reject", [uid(1)])
    job(lib, mb)
    assert linked(lib, "Symphony No. 4") is None and linked(lib, "Symphony No. 5 in C minor, Op. 67") is None
    assert views.work(lib.ix, w5)["mb_link"] is None
    # a file's own tag is not the job's to offer undoing
    lib.ix.con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)", (w4, uid(9)))
    assert views.work(lib.ix, w4)["mb_link"] is None


def test_orchestra_alone_is_no_instrument_but_the_concerto_for_orchestra_is_not_a_piano_concerto():
    assert wl._agree("Piano Concerto No. 4 in G major", "Concerto for Piano and Orchestra no. 4 in G major")
    assert not wl._agree("Piano Concerto", "Concerto for Orchestra")
    assert not wl._agree("Violin Concerto", "Concerto for Violin, Piano and Orchestra")
