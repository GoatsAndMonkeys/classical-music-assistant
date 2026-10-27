"""Liner notes (classical_core/liner_notes.py): what the notes are about, the cache, the listening guide."""

import asyncio
import json

import pytest

from classical_core import liner_notes as ln
from classical_core.names import ANTHROPIC_VERSION, LLMClient, LLMRefused, reply_text
from tests.unit.test_views import BPH, KARAJAN, MUTTER, WORK5, Lib

KARAJAN_MB = "d2ced2f1-6b58-47cf-ae87-5943e2ab6d99"
BPH_MB = "dea28aa9-1086-4ffa-8739-0ccc759de1ce"
REC_MB = "0b5e6b2c-0000-4000-8000-00000000000a"
GUIDE = " ".join(["The first movement opens with four famous notes and drives forward relentlessly."] * 6)


@pytest.fixture
def lib(tmp_path):
    lib = Lib(tmp_path)
    lib.add(WORK5, [KARAJAN, BPH, MUTTER], movements=("I. Allegro con brio", "II. Andante con moto"),
            recorded="1963")
    return lib


def credit(name, mbid, role, kind, ord_):
    return json.dumps({"name": name, "mbid": mbid, "role": role, "kind": kind, "ord": ord_, "edge": f"e{ord_}"})


def with_mb_credits(lib, *credits):
    lib.ix.con.execute("UPDATE mapping_state SET mbid=?", (REC_MB,))
    for c in credits:
        lib.ix.con.execute("INSERT INTO evidence(entity, entity_id, field, value, source, source_ref, edge, "
                           "observed_at) VALUES ('mb_recording', ?, 'credit', ?, 'mb', 'u', 'e', 1)", (REC_MB, c))


def test_refs_takes_performer_ids_only_from_exactly_named_musicbrainz_credits(lib):
    with_mb_credits(lib, credit("Herbert von Karajan", KARAJAN_MB, "conductor", "person", 0),
                    credit("Berlin Philharmonic", BPH_MB, "orchestra", "group", 1))    # another spelling
    wid = lib.work_id(WORK5)
    lib.ix.con.execute("INSERT INTO alias(entity, entity_id, kind, value) VALUES ('work', ?, 'mbid', ?)",
                       (wid, "7f8c5e2a-0000-4000-8000-000000000067"))
    ref = ln.refs(lib.ix, wid)
    assert ref["work_mbid"] == "7f8c5e2a-0000-4000-8000-000000000067"
    assert ref["movements"] == ["I. Allegro con brio", "II. Andante con moto"]
    who = {p["name"]: (p["group"], p["mbid"]) for p in ref["performers"]}
    assert who == {"Herbert von Karajan": ("conductors", KARAJAN_MB), "Berliner Philharmoniker": ("orchestras", None),
                   "Anne-Sophie Mutter": ("soloists", None)}
    assert [c["name"] for c in ref["composers"]] == ["Ludwig van Beethoven"] and ref["composers"][0]["mbid"] is None
    assert ln.refs(lib.ix, wid, "r_unknown")["recording_id"] == ref["recording_id"], "an unknown recording: the pick"
    with pytest.raises(ln.views.NotFound):
        ln.refs(lib.ix, "w_nope")
    assert ln.performer_mbid(lib.ix, lib.pid("Herbert von Karajan")) == ("Herbert von Karajan", KARAJAN_MB)
    assert ln.performer_mbid(lib.ix, lib.pid("Anne-Sophie Mutter"))[1] is None


def test_the_cache_expires_after_the_ttl_but_keeps_the_stale_value(lib):
    con = lib.ix.con
    assert ln.cached(con, "artist:x", 100.0) == (False, None)
    ln.store(con, "artist:x", {"text": "bio"}, 100.0)
    assert ln.cached(con, "artist:x", 100.0 + ln.NOTES_TTL) == (True, {"text": "bio"})
    assert ln.cached(con, "artist:x", 101.0 + ln.NOTES_TTL) == (False, {"text": "bio"})
    ln.store(con, "artist:y", None, 100.0)
    assert ln.cached(con, "artist:y", 200.0) == (True, None), "a definitive none is cached too"


def test_the_guide_prompt_holds_the_supplied_facts_and_nothing_else(lib):
    ref = ln.refs(lib.ix, lib.work_id(WORK5))
    facts = ln.guide_facts(ref, {"text": "The Fifth is a symphony in four movements.", "url": "https://x"})
    assert facts == {"title": WORK5, "composers": ["Ludwig van Beethoven"], "key": "C minor", "catalogue": "Op. 67",
                     "summary": "The Fifth is a symphony in four movements.",
                     "movements": ["I. Allegro con brio", "II. Andante con moto"]}
    system, user = ln.guide_prompt(facts)
    assert json.loads(user) == facts, "the user turn is exactly the facts"
    assert "Karajan" not in user and "Mutter" not in user and "https://x" not in user
    assert "only the facts" in system and "150 to 250 words" in system


def test_clean_guide_strips_markdown_and_rejects_a_non_answer():
    text = ln.clean_guide("# Guide\n\n**Allegro**: " + GUIDE + "\n\n\n*Andante*: calm.")
    assert text.startswith("Allegro: The first") and "#" not in text and "*" not in text
    assert text.count("\n\n") == 1
    for bad in ("NO GUIDE", "NO GUIDE.", "", "Too short to be a guide."):
        with pytest.raises(LLMRefused):
            ln.clean_guide(bad)
    assert len(ln.clean_guide(GUIDE * 10)) <= ln.GUIDE_MAX_CHARS


def test_guide_retry_rules():
    assert ln.guide_due(None, 0)
    assert not ln.guide_due({"status": "ok", "created_at": 0}, 1e9)
    assert not ln.guide_due({"status": "failed", "created_at": 0}, 1e9), "a refusal is never asked again"
    assert not ln.guide_due({"status": "error", "created_at": 0}, ln.GUIDE_RETRY - 1)
    assert ln.guide_due({"status": "error", "created_at": 0}, ln.GUIDE_RETRY)


def test_guide_store_round_trip(lib):
    ln.guide_put(lib.ix.con, "m1", "claude-opus-5-5", 5.0, GUIDE)
    assert ln.guide_get(lib.ix.con, "m1") == {"text": GUIDE, "model": "claude-opus-5-5", "status": "ok",
                                               "error": None, "created_at": 5.0}
    assert ln.guide_get(lib.ix.con, "m2") is None


def test_reply_text_reads_text_blocks_and_raises_on_refusal_or_cut_off():
    claude = {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "hmm"},
                                                     {"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]}
    assert reply_text(claude, True) == "Hello there"
    for stop in ("refusal", "max_tokens"):
        with pytest.raises(LLMRefused, match=stop):
            reply_text({"stop_reason": stop, "content": [{"type": "text", "text": "partial"}]}, True)
    with pytest.raises(LLMRefused):
        reply_text({"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "x"}]}, True)
    assert reply_text({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}, False) == "ok"
    for fin in ("length", "content_filter"):
        with pytest.raises(LLMRefused):
            reply_text({"choices": [{"message": {"content": "part"}, "finish_reason": fin}]}, False)


def test_write_guide_sends_one_claude_request_without_temperature():
    sent = []

    async def fetch(url, headers, body):
        sent.append((url, headers, json.loads(body)))
        return 200, json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": GUIDE}]}).encode()

    llm = LLMClient("https://api.anthropic.com", "claude-opus-5-5", api_key="sk-ant-test", fetch=fetch)
    facts = {"title": WORK5, "movements": ["I. Allegro"]}
    assert asyncio.run(ln.write_guide(llm, facts)) == GUIDE
    [(url, headers, body)] = sent
    assert url == "https://api.anthropic.com/v1/messages" and headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert body["max_tokens"] == ln.GUIDE_MAX_TOKENS and "temperature" not in body and "output_config" not in body
    assert body["system"] == ln.GUIDE_SYSTEM and json.loads(body["messages"][0]["content"]) == facts


def test_write_guide_openai_compatible_and_refusal():
    async def fetch(url, headers, body):
        assert url == "http://llm.test/v1/chat/completions" and "temperature" not in json.loads(body)
        return 200, json.dumps({"choices": [{"message": {"content": "NO GUIDE"}, "finish_reason": "stop"}]}).encode()

    with pytest.raises(LLMRefused):
        asyncio.run(ln.write_guide(LLMClient("http://llm.test/v1", "m", fetch=fetch), {"title": "x"}))
