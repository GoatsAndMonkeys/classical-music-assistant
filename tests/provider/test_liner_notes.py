"""Liner-note commands (provider/classical/ui.py) and the artist bio chain (ma_links.artist_bio)."""

import asyncio
import json

import pytest

from classical_core.mb import MBError, MBTransientError
from tests.unit.test_views import BPH, KARAJAN, WORK5, Lib

from .test_glue_web import mods  # noqa: F401 - fixture
from .test_ma_links import MBID, _provider, clients

KARAJAN_MB = "d2ced2f1-6b58-47cf-ae87-5943e2ab6d99"
REC_MB = "0b5e6b2c-0000-4000-8000-00000000000a"
GUIDE = " ".join(["Listen for the four-note motto as it returns in every movement of the symphony."] * 4)
KARAJAN_URL = f"https://musicbrainz.org/ws/2/artist/{KARAJAN_MB}?inc=url-rels&fmt=json"
THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/k.jpg/320px-k.jpg"


def with_karajan(mb, wd, wp, thumb=THUMB):
    mb.pages[KARAJAN_URL] = {"name": "Herbert von Karajan", "life-span": {"begin": "1908-04-05", "end": "1989-07-16"},
                             "relations": [{"target-type": "url", "type": "wikidata",
                                            "url": {"resource": "https://www.wikidata.org/wiki/Q57140"}}]}
    wd.pages["https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q57140&props=sitelinks&sitefilter=enwiki"
             "&format=json"] = {"entities": {"Q57140": {"sitelinks": {"enwiki": {"title": "Herbert von Karajan"}}}}}
    wp.pages["https://en.wikipedia.org/api/rest_v1/page/summary/Herbert_von_Karajan"] = {
        "type": "standard", "title": "Herbert von Karajan", "description": "Austrian conductor (1908–1989)",
        "extract": "Herbert von Karajan was an Austrian conductor.", "thumbnail": {"source": thumb}}


@pytest.fixture
def lib(tmp_path):
    lib = Lib(tmp_path)
    lib.add(WORK5, [KARAJAN, BPH], recorded="1963")
    wid = lib.work_id(WORK5)
    lib.ix.con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)", (wid, MBID))
    lib.ix.con.execute("UPDATE mapping_state SET mbid=?", (REC_MB,))
    lib.ix.con.execute("INSERT INTO evidence(entity, entity_id, field, value, source, source_ref, edge, observed_at) "
                       "VALUES ('mb_recording', ?, 'credit', ?, 'mb', 'u', 'e', 1)",
                       (REC_MB, json.dumps({"name": "Herbert von Karajan", "mbid": KARAJAN_MB, "role": "conductor",
                                            "kind": "person", "ord": 0, "edge": "e"})))
    return lib


def test_artist_bio_follows_url_rels_to_wikipedia_with_life_dates_and_thumbnail(mods):  # noqa: F811
    ml = mods.ui.ma_links
    mb, wd, wp = clients()
    with_karajan(mb, wd, wp)
    bio = asyncio.run(ml.artist_bio(KARAJAN_MB, mb, wd, wp))
    assert bio["text"] == "Herbert von Karajan was an Austrian conductor."
    assert (bio["born"], bio["died"], bio["thumbnail"]) == (1908, 1989, THUMB)
    assert bio["description"] == "Austrian conductor (1908–1989)" and bio["license"] == "CC BY-SA 4.0"
    assert bio["wikidata"] == "https://www.wikidata.org/wiki/Q57140"
    assert bio["url"] == "https://en.wikipedia.org/wiki/Herbert_von_Karajan"
    assert mb.seen == [KARAJAN_URL], "no search: only the artist's own url-relations"
    with_karajan(mb, wd, wp, thumb="https://evil.example/k.jpg")
    assert asyncio.run(ml.artist_bio(KARAJAN_MB, mb, wd, wp))["thumbnail"] is None, "Wikimedia's host only"
    mb.pages[KARAJAN_URL]["relations"] = []
    assert asyncio.run(ml.artist_bio(KARAJAN_MB, mb, wd, wp)) is None, "no Wikidata link, no bio"
    assert asyncio.run(ml.artist_bio("not-an-mbid", mb, wd, wp)) is None


def provider(mods, lib, guide=None):  # noqa: F811
    p = _provider(mods, lib)
    p.index = lib.ix
    mb, wd, wp = clients()
    with_karajan(mb, wd, wp)
    p._mb, p._wiki, p._guide_llm = mb, {"wikidata": wd, "wikipedia": wp}, guide
    return p


def test_liner_notes_command_shows_summary_movements_and_bios_and_caches_them(mods, lib):  # noqa: F811
    p = provider(mods, lib)
    wid = lib.work_id(WORK5)
    res = asyncio.run(p.cmd_liner_notes(wid))
    n = res["notes"]
    assert n["summary"]["text"].startswith("The Symphony No. 5") and n["key"] == "C minor"
    assert n["movements"] == ["I. Allegro", "II. Andante"]
    assert n["musicbrainz"] == f"https://musicbrainz.org/work/{MBID}"
    assert [(b["name"], b["group"], b["bio"]["born"]) for b in res["bios"]] == [
        ("Herbert von Karajan", "conductors", 1908)], "no MusicBrainz id for the orchestra or composer: no bio"
    assert res["guide"] is None and res["guide_due"] is False, "the guide option is off"
    seen = len(p._mb.seen)
    assert asyncio.run(p.cmd_liner_notes(wid)) == res
    assert len(p._mb.seen) == seen, "served from the liner_note cache"
    with pytest.raises(mods.ui.InvalidDataError):
        asyncio.run(p.cmd_liner_notes("w_nope"))


def test_liner_notes_cache_rules_for_failures(mods, lib, monkeypatch):  # noqa: F811
    p = provider(mods, lib)
    calls = []

    async def flaky(*a):
        calls.append(a)
        raise MBTransientError("503")

    monkeypatch.setattr(mods.ui.ma_links, "artist_bio", flaky)
    assert asyncio.run(p._artist_bio(KARAJAN_MB)) is None
    assert asyncio.run(p._artist_bio(KARAJAN_MB)) is None and len(calls) == 2, "a transient failure is not cached"

    async def gone(*a):
        calls.append(a)
        raise MBError("404")

    monkeypatch.setattr(mods.ui.ma_links, "artist_bio", gone)
    asyncio.run(p._artist_bio(KARAJAN_MB))
    asyncio.run(p._artist_bio(KARAJAN_MB))
    assert len(calls) == 3, "a definitive failure is cached as none"


def test_performer_bio_command(mods, lib):  # noqa: F811
    p = provider(mods, lib)
    res = asyncio.run(p.cmd_performer_bio(lib.pid("Herbert von Karajan")))
    assert res["bio"]["title"] == "Herbert von Karajan" and res["bio"]["died"] == 1989
    assert asyncio.run(p.cmd_performer_bio(lib.pid("Berliner Philharmoniker")))["bio"] is None
    with pytest.raises(mods.ui.InvalidDataError):
        asyncio.run(p.cmd_performer_bio("p_nope"))


class FakeLLM:
    model = "test-model"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, system, user, **kw):
        self.calls.append((system, json.loads(user), kw))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_no_guide_and_no_model_call_while_the_option_is_off(mods, lib):  # noqa: F811
    p = provider(mods, lib, guide=None)
    wid = lib.work_id(WORK5)
    assert asyncio.run(p.cmd_listening_guide(wid)) == {"work_id": wid, "guide": None}
    assert lib.ix._one("SELECT COUNT(*) FROM listening_guide")[0] == 0


def test_the_guide_is_written_once_from_the_facts_and_kept_with_its_model(mods, lib):  # noqa: F811
    llm = FakeLLM(GUIDE)
    p = provider(mods, lib, guide=llm)
    wid = lib.work_id(WORK5)
    assert asyncio.run(p.cmd_liner_notes(wid))["guide_due"] is True
    res = asyncio.run(p.cmd_listening_guide(wid))
    assert res["guide"] == {"text": GUIDE, "model": "test-model"}
    [(_, facts, kw)] = llm.calls
    assert facts["movements"] == ["I. Allegro", "II. Andante"] and "Karajan" not in json.dumps(facts)
    assert kw == {"max_tokens": 4000}
    assert asyncio.run(p.cmd_listening_guide(wid))["guide"]["text"] == GUIDE and len(llm.calls) == 1
    notes = asyncio.run(p.cmd_liner_notes(wid))
    assert notes["guide"]["model"] == "test-model" and notes["guide_due"] is False


def test_a_refused_guide_is_never_asked_again_and_an_error_waits_a_day(mods, lib, monkeypatch):  # noqa: F811
    from classical_core import liner_notes as ln
    from classical_core.names import LLMRefused

    wid = lib.work_id(WORK5)
    llm = FakeLLM(LLMRefused("stop_reason refusal"))
    p = provider(mods, lib, guide=llm)
    assert asyncio.run(p.cmd_listening_guide(wid))["guide"] is None
    assert asyncio.run(p.cmd_listening_guide(wid))["guide"] is None and len(llm.calls) == 1
    p.logger.warning.assert_called_once()
    assert lib.ix._one("SELECT status FROM listening_guide")[0] == "failed"

    lib.ix.con.execute("DELETE FROM listening_guide")
    llm = FakeLLM(RuntimeError("HTTP 529"), GUIDE)
    p = provider(mods, lib, guide=llm)
    now = [1000.0]
    monkeypatch.setattr(mods.ui.time, "time", lambda: now[0])
    assert asyncio.run(p.cmd_listening_guide(wid))["guide"] is None
    assert asyncio.run(p.cmd_listening_guide(wid))["guide"] is None and len(llm.calls) == 1, "not in a loop"
    now[0] += ln.GUIDE_RETRY
    assert asyncio.run(p.cmd_listening_guide(wid))["guide"]["text"] == GUIDE and len(llm.calls) == 2
