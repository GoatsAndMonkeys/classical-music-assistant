"""Shared test helpers: a fake MA library built from the synthetic fixture files, and offline MB."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from classical_core.index import AlbumIn, MappingIn, TrackIn
from classical_core.mb import MBClient
from classical_core.tags import read_tags

ROOT = Path(__file__).resolve().parents[1]
MB_FIXTURES = ROOT / "tests" / "fixtures" / "mb"


def fixture_fetch(calls: list[str] | None = None):
    async def fetch(url: str, headers: dict) -> tuple[int, bytes]:
        if calls is not None:
            calls.append(url)
        m = re.search(r"/(recording|work)/([0-9a-f-]{36})\?", url)
        if m:
            p = MB_FIXTURES / m.group(1) / f"{m.group(2)}.json"
            if p.exists():
                return 200, p.read_bytes()
        return 404, b'{"error": "Not Found"}'

    return fetch


def offline_mb(calls: list[str] | None = None, cache=None) -> MBClient:
    return MBClient("test@example.invalid", cache=cache, fetch=fixture_fetch(calls), interval=0)


def fake_album(album_dir: Path, album_id: str, track_id_base: int, provider: str = "filesystem_local") -> AlbumIn:
    """What MA's library would hold for one folder: album/track rows with positions and file mappings."""
    files = sorted(p for p in album_dir.iterdir() if p.suffix in (".flac", ".mp3", ".m4a"))
    tracks = []
    name = artist = ""
    year = None
    for i, f in enumerate(files):
        t = read_tags(f)
        name = t.album or album_dir.name
        artist = t.albumartist or ""
        year = int(t.release_date[:4]) if t.release_date else None
        tracks.append(TrackIn(
            library_track_id=str(track_id_base + i), disc=t.disc or 1, position=t.track or i + 1,
            title=t.title or f.stem, artist=t.artist or "", duration=180, mbid=None,
            mappings=[MappingIn(provider, f"{album_dir.name}/{f.name}",
                                fingerprint=hashlib.sha1(f.read_bytes()).hexdigest()[:16], path=str(f))]))
    return AlbumIn(album_id, name, artist, year, None, tracks)


def fake_library(lib: Path) -> list[AlbumIn]:
    return [fake_album(d, str(100 + n), 1000 + n * 100) for n, d in enumerate(sorted(p for p in lib.iterdir()
                                                                                       if p.is_dir()))]


def dump(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, default=str)
