"""Album art for albums MA shows without any (no embedded art, no cover file): the Cover Art Archive front image
of the MusicBrainz release the index already knows the album is.

Nothing is written next to the music. The image is handed to MA as a remote `MediaItemImage` on the library
album (MA's own metadata), and the Classical view reads it from there like any other album image.

Which release: one the album is confidently linked to (MA's MusicBrainz ids from tags, or the index's release
match, which track lengths confirm). Failing that, a release group alone is enough for a picture: when every
release the album's cached release search fits (title and artist) belongs to one release group, that group's
cover is the album's, even if which pressing it is stays open. Such a group is used for the picture only and is
never written to MA as the album's id.

Requests: the Cover Art Archive is asked with HEAD and no redirect (the 307 alone says the cover exists), at
most one a second, with the MusicBrainz contact in the User-Agent; answers are cached in `web_cache` (found 90
days, missing 30). MusicBrainz is asked only for the release group of a known release whose own cover is
missing, through the index's rate-limited, cached client; the release search is read from the cache only, never
sent anew. Cover images are the copyright of their owners; the archive serves them for identification, and they
are shown, not stored, here.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import release as mbrel
from .mb import MBCache, MBError, MBTransientError, is_mbid, parse_retry_after
from .text import norm_key

CAA_BASE = "https://coverartarchive.org"
SIZE = 500
FOUND_TTL = 90 * 86400
MISSING_TTL = 30 * 86400
CHUNK = 20                  # albums per art job; the job re-queues itself behind other work
PROVIDER = "coverartarchive"   # MA's own Cover Art Archive provider domain: the image's owner in MA
_REDIRECTS = (301, 302, 303, 307, 308)


def front_url(kind: str, mbid: str, size: int = SIZE, base: str = CAA_BASE) -> str:
    """'https://coverartarchive.org/release/<mbid>/front-500' (kind: 'release' or 'release-group')."""
    if kind not in ("release", "release-group"):
        raise ValueError(kind)
    return f"{base.rstrip('/')}/{kind}/{mbid}/front-{size}"


def release_group_url(mb_base: str, release_mbid: str) -> str:
    return f"{mb_base.rstrip('/')}/release/{release_mbid}?inc=release-groups&fmt=json"


def release_group_of(release: dict) -> str | None:
    rg = (release.get("release-group") or {}).get("id")
    return rg if is_mbid(rg) else None


def image(url: str) -> dict[str, Any]:
    """The `MediaItemImage` dict MA and the Classical view store for an album thumb."""
    return {"type": "thumb", "path": url, "provider": PROVIDER, "remotely_accessible": True}


def group_from_search(search: dict, name: str, artist: str) -> str | None:
    """The one release group every fitting release of a release search belongs to (picture only), or None.

    Fitting: MB's score at least `RIVAL_SCORE`, the title one of the album's title keys, the artist credited.
    The best fit must score `MIN_SCORE`; any fit without a release group, or two groups, is no answer."""
    keys = mbrel.title_keys(name)
    best, groups = 0, set()
    for r in search.get("releases") or []:
        score = int(r.get("score") or 0)
        if score < mbrel.RIVAL_SCORE or norm_key(r.get("title")) not in keys or not mbrel._artist_fits(r, artist):
            continue
        groups.add((r.get("release-group") or {}).get("id"))
        best = max(best, score)
    if best < mbrel.MIN_SCORE or len(groups) != 1:
        return None
    rg = next(iter(groups))
    return rg if is_mbid(rg) else None


# ---- which albums ---------------------------------------------------------------------------------------


def due(con, after: str | None, limit: int) -> list[dict[str, Any]]:
    """Albums with no image that have a release, a release group, or a searchable name, after `after`
    (album_library_id order, as text). The image check happens again before anything is written."""
    rows = con.execute(
        "SELECT album_library_id, name, artist, release_mbid, mb_release, mb_release_group FROM album_info "
        "WHERE image_json IS NULL AND album_library_id > ? ORDER BY album_library_id LIMIT ?",
        (after or "", limit)).fetchall()
    return [dict(r) for r in rows]


# ---- Cover Art Archive client ---------------------------------------------------------------------------


class CAAClient:
    """HEAD requests to the Cover Art Archive: 1 a second, cached, Retry-After honoured. `fetch(url, headers)`
    returns (status, headers) and is injectable for tests."""

    def __init__(self, contact: str, cache: MBCache | None = None, base_url: str = CAA_BASE,
                 fetch: Callable[[str, dict[str, str]], Awaitable[tuple[int, dict[str, str]]]] | None = None,
                 interval: float = 1.0, session: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.interval = interval
        self.headers = {"User-Agent": f"ClassicalMusicAssistant/0.1 ( {contact} )"}
        self._fetch = fetch or self._aiohttp_head
        self._session = session
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._not_before = 0.0

    async def _aiohttp_head(self, url: str, headers: dict[str, str]) -> tuple[int, dict[str, str]]:
        import aiohttp  # shipped with MA

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        async with self._session.head(url, headers=headers, allow_redirects=False) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}

    async def close(self) -> None:
        if self._session is not None and getattr(self._session, "close", None):
            await self._session.close()
            self._session = None

    async def exists(self, url: str) -> bool:
        """Whether the archive has this image. A 404 is a cached no; anything but an answer raises
        `MBTransientError` (not cached)."""
        if self.cache is not None:
            hit = await self.cache.get(url)
            if hit is not None:
                return hit[0] == 200
        async with self._lock:
            now = time.monotonic()
            wait = max(self.interval - (now - self._last), self._not_before - now)
            if wait > 60:
                raise MBTransientError(f"Cover Art Archive asked us to wait {wait:.0f}s; {url} deferred")
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                status, headers = await self._fetch(url, self.headers)
            except Exception as e:  # aiohttp.ClientError, timeouts: try again on the next run
                raise MBTransientError(f"Cover Art Archive: {e!r} for {url}") from e
            finally:
                self._last = time.monotonic()
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if status in _REDIRECTS and headers.get("location") or status == 200:
            found = True
        elif status in (400, 404):
            found = False
        else:
            retry = parse_retry_after(headers.get("retry-after"))
            if retry is not None:
                self._not_before = max(self._not_before, time.monotonic() + retry)
            raise MBTransientError(f"Cover Art Archive: HTTP {status} for {url}")
        if self.cache is not None:
            await self.cache.put(url, 200 if found else 404, b"", FOUND_TTL if found else MISSING_TTL)
        return found


# ---- finding one album's cover --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Art:
    url: str
    release: str | None          # the release the album is (confident), if known
    release_group: str | None    # its group, or the group the album's release search points to
    confident: bool              # the ids may be written to MA as the album's own


async def find(album: dict[str, Any], caa: CAAClient, mb: Any = None) -> Art | None:
    """The cover for one `due` row, or None. Raises `MBTransientError` when an answer has to wait."""
    release = next((r for r in (album.get("mb_release"), album.get("release_mbid")) if is_mbid(r)), None)
    group = album.get("mb_release_group") if is_mbid(album.get("mb_release_group")) else None
    confident = True
    if release:
        url = front_url("release", release, base=caa.base_url)
        if await caa.exists(url):
            return Art(url, release, group, True)
        if group is None and mb is not None:
            try:
                group = release_group_of(await mb.get(release_group_url(mb.base_url, release)))
            except MBTransientError:
                raise
            except MBError:
                group = None
    if release is None and group is None:
        group, confident = await _group_from_cached_search(album, mb), False
    if group:
        url = front_url("release-group", group, base=caa.base_url)
        if await caa.exists(url):
            return Art(url, release, group, confident)
    return None


async def _group_from_cached_search(album: dict[str, Any], mb: Any) -> str | None:
    """The release search the index made for this album, read from the cache only (no new search)."""
    name, artist = album.get("name") or "", album.get("artist") or ""
    if mb is None or getattr(mb, "cache", None) is None or not mbrel.wanted(name, artist):
        return None
    hit = await mb.cache.get(mbrel.search_url(mb.base_url, name, artist))
    if hit is None or hit[0] != 200:
        return None
    try:
        return group_from_search(json.loads(hit[1]), name, artist)
    except (ValueError, TypeError, AttributeError):
        return None


def store_image(con, album_library_id: str, url: str) -> None:
    """The Classical view's copy, until the album's next index pass reads the same image back from MA."""
    con.execute("UPDATE album_info SET image_json=? WHERE album_library_id=? AND image_json IS NULL",
                (json.dumps(image(url)), album_library_id))
