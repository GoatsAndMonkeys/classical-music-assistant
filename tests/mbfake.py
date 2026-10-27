"""Build MusicBrainz JSON in tests (works with parts/parents/catalogues, recordings with performance links)."""

from __future__ import annotations

import json
import re

from classical_core.mb import MBClient


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


class FakeMB:
    def __init__(self) -> None:
        self.works: dict[str, dict] = {}
        self.recs: dict[str, dict] = {}
        self.calls: list[str] = []

    def work(self, wid: str, title: str, type: str | None = None, parent: str | None = None,
             order: int | None = None, cats: tuple[str, ...] = (), composer: str = "Richard Wagner") -> str:
        w = self.works.setdefault(wid, {"id": wid, "relations": []})
        w.update(title=title, type=type)
        w["relations"] += [{"target-type": "artist", "type": "composer", "direction": "backward",
                            "artist": {"id": uid(999), "name": composer}}]
        w["relations"] += [{"target-type": "series", "type": "part of", "series": {"type": "Catalogue",
                            "name": "cat"}, "attribute-values": {"number": c}} for c in cats]
        if parent:
            pw = self.works[parent]
            w["relations"].append({"target-type": "work", "type": "parts", "direction": "backward",
                                   "ordering-key": order, "work": {"id": parent, "title": pw["title"]}})
            pw["relations"].append({"target-type": "work", "type": "parts", "direction": "forward",
                                    "ordering-key": order, "work": {"id": wid, "title": title}})
        return wid

    def recording(self, rid: str, title: str, work: str, conductor: str | None = None,
                  partial: bool = False) -> str:
        rels = [{"target-type": "work", "type": "performance", "attributes": ["partial"] if partial else [],
                 "work": {"id": work, "title": self.works[work]["title"]}}]
        if conductor:
            rels.append({"target-type": "artist", "type": "conductor", "direction": "backward",
                         "artist": {"id": uid(998), "name": conductor, "type": "Person"}})
        self.recs[rid] = {"id": rid, "title": title, "relations": rels}
        return rid

    async def fetch(self, url: str, headers: dict) -> tuple[int, bytes]:
        self.calls.append(url)
        m = re.search(r"/(recording|work)/([0-9a-f-]{36})\?", url)
        src = self.recs if m and m.group(1) == "recording" else self.works
        if m and m.group(2) in src:
            return 200, json.dumps(src[m.group(2)]).encode()
        return 404, b'{"error": "Not Found"}'

    def client(self, cache=None) -> MBClient:
        return MBClient("test@example.invalid", cache=cache, fetch=self.fetch, interval=0)
