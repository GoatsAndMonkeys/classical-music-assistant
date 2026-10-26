import asyncio
import json

from classical_core.mb import MBClient, MBError, parse_recording, parse_work
from tests.helpers import MB_FIXTURES

LARGO = "d50d21e7-aefe-4ff0-87e3-f20ce9d202ec"
VLTAVA = "e40b8e05-d10a-4cb9-9219-818ee6b9d329"
SYM9 = "8596e5aa-85e1-4f76-907f-ea02aee39ea3"


def load(kind, mbid):
    return json.loads((MB_FIXTURES / kind / f"{mbid}.json").read_text())


def test_recording_links_credits_dates():
    r = parse_recording(load("recording", LARGO))
    assert len(r.works) == 1 and not r.works[0].partial
    assert r.recorded == ("1961-03-22", "1961-03-24")      # only from the performance relationship
    roles = [(c.name, c.role) for c in r.credits]
    assert ("István Kertész", "conductor") in roles and ("Wiener Philharmoniker", "orchestra") in roles
    assert all(c.role != "balance" for c in r.credits)      # engineers are not performers


def test_recording_without_dates_is_unknown():
    assert parse_recording(load("recording", VLTAVA)).recorded == (None, None)


def _find_work(title_part):
    for p in (MB_FIXTURES / "work").glob("*.json"):
        d = json.loads(p.read_text())
        if title_part in d["title"]:
            return d
    raise AssertionError(title_part)


def test_work_hierarchy_and_series_catalogues():
    sym = parse_work(_find_work("op. 95 „Z Nového světa“"))
    if sym.parents:  # pick the top work, not a movement
        sym = parse_work(load("work", sym.parents[0].mbid))
    assert sym.work_type == "Symphony" and sym.key == "E minor"
    assert [p.ordering for p in sym.parts] == [1, 2, 3, 4]
    assert [c.name for c in sym.composers] == ["Antonín Dvořák"]
    nums = [n for _, n in sym.catalogues]
    assert "op. 95" in nums and "B. 178" in nums and "7" not in nums   # "Classic 100" is not a catalogue
    vltava = parse_work(_find_work("II. Vltava"))
    assert vltava.parents and vltava.parents[0].title.startswith("Má vlast")


def test_client_dedupes_backs_off_and_caches_errors():
    calls = []
    n503 = {"left": 1}

    async def fetch(url, headers):
        calls.append(url)
        assert "ClassicalMusicAssistant" in headers["User-Agent"]
        if n503["left"]:
            n503["left"] -= 1
            return 503, b""
        if "missing" in url:
            return 404, b""
        return 200, b'{"id": "x"}'

    class Cache(dict):
        async def get(self, url):
            return dict.get(self, url)

        async def put(self, url, status, body, ttl):
            self[url] = (status, body)

    async def run():
        c = MBClient("t@example.invalid", cache=Cache(), fetch=fetch, interval=0)
        import classical_core.mb as mbmod
        orig = mbmod.asyncio.sleep

        async def nosleep(_):
            await orig(0)
        mbmod.asyncio.sleep = nosleep
        try:
            a, b = await asyncio.gather(c.get("u1"), c.get("u1"))
            assert a == b == {"id": "x"} and calls.count("u1") == 2   # one 503 retry, one dedupe
            await c.get("u1")
            assert calls.count("u1") == 2                              # cached
            for _ in range(2):
                try:
                    await c.get("missing")
                except MBError:
                    pass
            assert calls.count("missing") == 1                         # error cached
        finally:
            mbmod.asyncio.sleep = orig

    asyncio.run(run())
