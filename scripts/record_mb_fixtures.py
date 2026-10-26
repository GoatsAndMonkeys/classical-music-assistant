#!/usr/bin/env python3
"""Record MusicBrainz JSON fixtures for the Tier 2 tests (PLAN.md §8, "MB JSON → evidence").

Fetches each recording with the same `inc` set the indexer uses, then every linked work and its
parents (bounded walk, 3 levels), at 1 request per second, and writes them under
tests/fixtures/mb/{recording,work}/<mbid>.json. MusicBrainz data is CC0.

Usage: record_mb_fixtures.py CONTACT RECORDING_MBID [RECORDING_MBID ...]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from classical_core.mb import RECORDING_INC, WORK_INC

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "mb"
BASE = "https://musicbrainz.org/ws/2"
_last = 0.0


def get(contact: str, path: str) -> dict:
    global _last
    wait = 1.1 - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(f"{BASE}/{path}", headers={
        "User-Agent": f"ClassicalMusicAssistant-fixtures/0.1 ({contact})", "Accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                _last = time.monotonic()
                return json.load(r)
        except urllib.error.HTTPError as e:
            _last = time.monotonic()
            if e.code != 503 or attempt == 4:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("unreachable")


def save(kind: str, mbid: str, data: dict) -> None:
    p = OUT / kind / f"{mbid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def main() -> int:
    contact, *recordings = sys.argv[1:]
    todo: list[tuple[str, int]] = []
    for rid in recordings:
        rec = get(contact, f"recording/{rid}?inc={RECORDING_INC}&fmt=json")
        save("recording", rid, rec)
        todo += [(r["work"]["id"], 0) for r in rec.get("relations", []) if r.get("work")]
    seen: set[str] = set()
    while todo:
        wid, depth = todo.pop(0)
        if wid in seen:
            continue
        seen.add(wid)
        work = get(contact, f"work/{wid}?inc={WORK_INC}&fmt=json")
        save("work", wid, work)
        if depth >= 3:
            continue
        for r in work.get("relations", []):
            if r.get("type") == "parts" and r.get("direction") == "backward" and r.get("work"):
                todo.append((r["work"]["id"], depth + 1))
    print(f"saved {len(recordings)} recordings and {len(seen)} works to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
