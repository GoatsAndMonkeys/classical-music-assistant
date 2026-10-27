"""End-to-end smoke test against a local Music Assistant dev instance (docker/compose.dev.yml).

Onboards a fresh instance (or logs in), adds the local filesystem provider on the synthetic library, the
demo player provider and the Classical plugin, syncs, waits for the Classical index, then browses
Classical -> Composers -> composer -> type -> work -> recording and plays the recording folder (and its
play URI) on a demo player, checking the queue holds the movements in order.

    .venv/bin/python scripts/smoke.py [--url http://localhost:8095] [--state docker/data-stable/smoke.json]

Dev credentials come from CMA_DEV_USER / CMA_DEV_PASSWORD, else from the gitignored docker/.env.local, which is
created with a random password on first use (a throwaway local admin). The token is kept in the --state file,
under the gitignored docker/data-* directory.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

ENV_LOCAL = Path(__file__).resolve().parents[1] / "docker/.env.local"


def dev_credentials() -> tuple[str, str]:
    """The local dev admin: env vars, else docker/.env.local (generated once, never committed)."""
    if os.environ.get("CMA_DEV_USER") and os.environ.get("CMA_DEV_PASSWORD"):
        return os.environ["CMA_DEV_USER"], os.environ["CMA_DEV_PASSWORD"]
    if not ENV_LOCAL.exists():
        import secrets
        ENV_LOCAL.write_text(f"CMA_DEV_USER=dev-admin\nCMA_DEV_PASSWORD={secrets.token_urlsafe(18)}\n")
        ENV_LOCAL.chmod(0o600)
    kv = dict(line.split("=", 1) for line in ENV_LOCAL.read_text().splitlines() if "=" in line)
    return kv["CMA_DEV_USER"], kv["CMA_DEV_PASSWORD"]


USER, PASSWORD = dev_credentials()
LIBRARY_PATH = "/media/classical"


class Api:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self.ws = ws
        self.ids = itertools.count(1)

    async def call(self, command: str, **args):
        mid = str(next(self.ids))
        await self.ws.send_json({"message_id": mid, "command": command, "args": args})
        parts: list = []
        while True:
            msg = await self.ws.receive_json(timeout=120)
            if msg.get("message_id") != mid:
                continue  # events, server info
            if "error_code" in msg:
                raise RuntimeError(f"{command}: {msg.get('error_code')} {msg.get('details')}")
            if msg.get("partial"):
                parts.extend(msg.get("result") or [])
                continue
            res = msg.get("result")
            return parts + (res or []) if parts else res


async def get_token(http: aiohttp.ClientSession, url: str, state: Path) -> str:
    if state.exists():
        return json.loads(state.read_text())["token"]
    async with http.post(f"{url}/setup", json={"username": USER, "password": PASSWORD,
                                               "device_name": "classical smoke"}) as r:
        body = await r.json(content_type=None) if r.status < 500 else {}
    token = body.get("token") or body.get("access_token")
    if not token:
        async with http.post(f"{url}/auth/login", json={"provider_id": "builtin",
                                                        "credentials": {"username": USER, "password": PASSWORD}}) as r:
            body = await r.json(content_type=None)
        token = body.get("token") or body.get("access_token")
    if not token:
        sys.exit(f"could not get a token: {body}")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"token": token}))
    return token


async def ensure_provider(api: Api, domain: str, values: dict | None = None) -> str:
    confs = await api.call("config/providers")
    have = [c for c in confs if c["domain"] == domain]
    if have:
        return have[0]["instance_id"]
    step = await api.call("config/providers/setup", provider_domain=domain)
    while step["type"] == "form":
        step = await api.call("config/flows/submit", flow_id=step["flow_id"], values=values or {})
    if step["type"] != "finish":
        raise RuntimeError(f"setup {domain}: {step}")
    return (step.get("result") or {}).get("instance_id") or domain


async def wait_for(what: str, fn, timeout: float = 180, every: float = 2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        res = await fn()
        if res:
            return res
        await asyncio.sleep(every)
    raise TimeoutError(what)


def find(items: list[dict], pred, what: str) -> dict:
    for it in items:
        if pred(it):
            return it
    raise AssertionError(f"{what} not found in {[i.get('name') for i in items]}")


def show(level: str, items: list[dict]) -> None:
    print(f"  {level}: " + "; ".join(f"{i['name']}" + (f" [{i['version']}]" if i.get("version") else "")
                                     for i in items if i.get("name") != ".."))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8095")
    ap.add_argument("--state", default=str(Path(__file__).resolve().parents[1] / "docker/data-stable/smoke.json"))
    ap.add_argument("--musicbrainz", action="store_true", help="leave MusicBrainz lookups on")
    a = ap.parse_args()
    async with aiohttp.ClientSession() as http:
        token = await get_token(http, a.url, Path(a.state))
        async with http.ws_connect(f"{a.url}/ws", max_msg_size=0) as ws:
            api = Api(ws)
            await ws.receive_json()  # server info
            await api.call("auth", token=token)
            info = await api.call("info")
            print(f"MA {info.get('server_version')} (schema {info.get('schema_version')})")
            try:
                await api.call("config/onboard_complete")
            except RuntimeError:
                pass

            fs = await ensure_provider(api, "filesystem_local", {"content_type": "music", "path": LIBRARY_PATH})
            demo = await ensure_provider(api, "_demo_player_provider")
            cl = await ensure_provider(api, "classical")
            if not a.musicbrainz:
                await api.call("config/providers/save", provider_domain="classical",
                               values={"musicbrainz": False}, instance_id=cl)
            print(f"providers: {fs}, {demo}, {cl}")
            await wait_for("classical loaded", lambda: _loaded(api, cl), 120)

            await api.call("music/sync")
            st = await wait_for("classical index", lambda: _indexed(api), 300)
            print(f"index: {st['albums']} albums, {st['composers']} composers, {st['works']} works, "
                  f"{st['recordings']} recordings, {st['review_items']} review items, mappings {st['mappings']}")

            # Composer -> Work -> Recording
            root = await api.call("music/browse", path="classical://")
            show("classical://", root)
            comps = await api.call("music/browse", path=find(root, lambda i: i["name"] == "Composers", "Composers")["path"])
            show("Composers", comps)
            beet = find(comps, lambda i: "Beethoven" in i["name"], "Beethoven")
            types = await api.call("music/browse", path=beet["path"])
            show(beet["name"], types)
            works = await api.call("music/browse", path=find(types, lambda i: i["name"] == "Symphonies", "Symphonies")["path"])
            show("Symphonies", works)
            work = find(works, lambda i: "No. 5" in i["name"], "Symphony No. 5")
            recs = await api.call("music/browse", path=work["path"])
            show(work["name"], recs)
            rec = find(recs, lambda i: i.get("media_type") == "folder" and i.get("is_playable"), "playable recording")
            movs = await api.call("music/browse", path=rec["path"])
            show(rec["name"], movs)
            movs = [m for m in movs if m.get("media_type") == "track"]
            assert len(movs) == 4, f"expected 4 movements, got {len(movs)}"

            # enqueue the work: the recording folder object, then its play URI string
            players = await api.call("players/all")
            player = find(players, lambda p: p.get("provider") == demo or "demo" in (p.get("provider") or ""), "demo player")
            qid = player["player_id"]
            expect = [m["item_id"] for m in movs]   # the queue holds the library tracks, under their own names
            for label, media in (("folder", rec), ("uri", rec["uri"])):
                await api.call("player_queues/clear", queue_id=qid)
                await api.call("player_queues/play_media", queue_id=qid, media=media, option="replace")
                items = await wait_for("queue", lambda: _queue(api, qid, len(expect)), 30, 1)
                got = [i["media_item"]["item_id"] for i in items]
                assert got == expect, f"queue order via {label}: {got} != {expect}"
                print(f"  play_media({label}) on {player['name']}: queue = "
                      + "; ".join(i["media_item"]["name"] for i in items))
            await api.call("player_queues/stop", queue_id=qid)

            res = await api.call("classical/search", query="beethoven 5")
            print(f"  classical/search 'beethoven 5': {len(res)} hit(s); first play_uri {res[0].get('play_uri') if res else None}")
    print("SMOKE OK")
    return 0


async def _loaded(api: Api, instance_id: str):
    provs = await api.call("providers")
    return any(p["instance_id"] == instance_id and p.get("available", True) for p in provs)


async def _indexed(api: Api):
    st = await api.call("classical/status")
    w = st["worker"]
    if st["albums"] >= 9 and not w["queued"] and not w["current"]:
        return st
    return None


async def _queue(api: Api, qid: str, n: int):
    items = await api.call("player_queues/items", queue_id=qid)
    return items if len(items) >= n else None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
