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


def synthetic_albums(con, n_albums: int = 20000, n_composers: int = 800, recs_per_album: int = 4,
                     tracks_per_rec: int = 3, compilation_every: int = 5, provider: str = "filesystem_local") -> None:
    """A large derived library written straight into the tables (no tag reading), for timing album reads:
    `n_albums` albums of `recs_per_album` recordings, every `compilation_every`-th album a compilation with a
    different composer per recording. Composer names carry accents so sorting has to fold them."""
    import random
    rnd = random.Random(5481)
    surnames = ["Ålvén", "Bach", "Dvořák", "Éder", "Fauré", "Glière", "Haydn", "Janáček", "Liszt", "Mozart",
                "Saint-Saëns", "Zelenka", "Ödön", "ábel", "Brahms"]
    comps = [(f"c_{i}", f"Anon {surnames[i % len(surnames)]} {i}", f"{surnames[i % len(surnames)]}, Anon {i}")
             for i in range(n_composers)]
    con.executemany("INSERT INTO composer(id, name, sort_name) VALUES (?,?,?)", comps)
    works, wcs, recs, slots, occs, maps, albums, tinfo = [], [], [], [], [], [], [], []
    for a in range(n_albums):
        aid = str(a)
        albums.append((aid, f"Album {rnd.randrange(10**6):06d}", "Various", 1950 + a % 70 if a % 7 else None))
        base = rnd.randrange(n_composers)
        for k in range(recs_per_album):
            ci = (base + k * 37) % n_composers if a % compilation_every == 0 else base
            wid, rid = f"w_{a}_{k}", f"r_{a}_{k}"
            works.append((wid, f"Work {a}.{k}", f"work {a}.{k}"))
            wcs.append((wid, comps[ci][0]))
            recs.append((rid, wid, aid))
            for t in range(tracks_per_rec):
                oid, tid = f"o_{a}_{k}_{t}", f"t_{a}_{k}_{t}"
                occs.append((oid, aid, 1, k * tracks_per_rec + t + 1, tid))
                slots.append((rid, oid, t, rid, t))
                maps.append((provider, tid, tid))
                tinfo.append((tid, f"Track {t}", "Artist", 300))
    con.executemany("INSERT INTO work(id, title, sort_title, created_at) VALUES (?,?,?,0)", works)
    con.executemany("INSERT INTO work_composer(work_id, composer_id) VALUES (?,?)", wcs)
    con.executemany("INSERT INTO recording(id, work_id, album_library_id, tier) VALUES (?,?,?,'auto')", recs)
    con.executemany("INSERT INTO occurrence(id, album_library_id, disc, position, library_track_id) "
                    "VALUES (?,?,?,?,?)", occs)
    con.executemany("INSERT INTO recording_slot(recording_id, occurrence_id, ord, home_id, home_ord) "
                    "VALUES (?,?,?,?,?)", slots)
    con.executemany("INSERT INTO mapping_state(provider_instance, item_id, library_track_id) VALUES (?,?,?)", maps)
    con.executemany("INSERT INTO track_info(library_track_id, title, artist, duration) VALUES (?,?,?,?)", tinfo)
    con.executemany("INSERT INTO album_info(album_library_id, name, artist, year) VALUES (?,?,?,?)", albums)
    con.commit()
