"""Links from the classical index to Music Assistant itself, for the Classical view in MA's own frontend.

* `ArtistLinks`: which MA library artist a composer or performer is, so MA's frontend can show that artist's
  image, heart and page. Matched by MusicBrainz artist id (composers) or by the index's own name keys (every
  spelling a tag gave the person), exact after `ident_key`: never fuzzy. Kept in memory and rebuilt in the
  background when older than `TTL`; a view never waits for it beyond `FIRST_WAIT` on the first call.
* `work_description`: the English Wikipedia summary of a work, reached only through links others curated:
  the work's MusicBrainz id → its Wikidata url-relation → the enwiki sitelink → Wikipedia's REST summary.
  Text is CC BY-SA 4.0: it is returned with its source URL and licence for attribution, and every request
  goes through the cached, rate-limited `MBClient` (the `web_cache` table). No search, no scraping.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from classical_core.accolades import parse_url_rels, wikidata_qid
from classical_core.index import Index
from classical_core.text import ident_key

TTL = 900.0
FIRST_WAIT = 3.0
LICENSE = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
MAX_EXTRACT = 1200
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _mb_artist_ids(artist: Any) -> list[str]:
    out = []
    for ext in getattr(artist, "external_ids", None) or ():
        try:
            kind, value = ext
        except (TypeError, ValueError):
            continue
        name = str(getattr(kind, "value", kind) or "").lower()
        if "musicbrainz" in name and "artist" in name and isinstance(value, str) and _MBID.match(value.lower()):
            out.append(value.lower())
    return out


def artist_ref(artist: Any) -> dict[str, Any]:
    """The few fields MA's frontend needs to show and link an artist (image as MA's own MediaItemImage)."""
    image = None
    with contextlib.suppress(Exception):
        img = getattr(artist, "image", None)
        image = img.to_dict() if img is not None else None
    item_id = str(getattr(artist, "item_id", ""))
    return {"item_id": item_id, "provider": "library", "name": getattr(artist, "name", "") or "",
            "uri": getattr(artist, "uri", None) or f"library://artist/{item_id}", "image": image,
            "favorite": bool(getattr(artist, "favorite", False))}


def match_artists(ix: Index, artists: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Composer and person ids → one MA artist ref each.

    `artists` are `artist_ref`s plus `keys` (ident_key of the name) and `mbids`. An entity matches an artist
    whose MusicBrainz id is the composer's, else whose name key is one of the entity's name aliases (or its
    display name). Several candidates: one with an image, then the one named exactly like the entity, then the
    lowest MA id. An artist name shared by two entities (a composer and a person may be the same human) maps
    to both."""
    by_key: dict[str, list[dict]] = {}
    by_mbid: dict[str, list[dict]] = {}
    for a in artists:
        for k in a.get("keys") or ():
            by_key.setdefault(k, []).append(a)
        for m in a.get("mbids") or ():
            by_mbid.setdefault(m, []).append(a)
    names: dict[str, str] = {}
    keys: dict[str, set[str]] = {}
    for r in ix._q("SELECT id, name FROM composer UNION ALL SELECT id, name FROM person"):
        names[r[0]] = r[1] or ""
        if k := ident_key(r[1]):
            keys.setdefault(r[0], set()).add(k)
    mbids: dict[str, set[str]] = {}
    for r in ix._q("SELECT entity_id, kind, value FROM alias WHERE entity IN ('composer', 'person') "
                   "AND kind IN ('name', 'mbid')"):
        if r[0] not in names:
            continue
        (keys if r[1] == "name" else mbids).setdefault(r[0], set()).add(r[2] if r[1] == "name" else r[2].lower())
    out: dict[str, dict[str, Any]] = {}
    for eid, name in names.items():
        cands = [a for m in sorted(mbids.get(eid, ())) for a in by_mbid.get(m, [])]
        if not cands:
            cands = [a for k in sorted(keys.get(eid, ())) for a in by_key.get(k, [])]
        if not cands:
            continue
        exact = ident_key(name)

        def rank(a: dict, exact: str = exact) -> tuple:
            iid = a["item_id"]
            return (a.get("image") is None, exact not in (a.get("keys") or ()),
                    int(iid) if iid.isdigit() else 1 << 62, iid)

        best = min(cands, key=rank)
        out[eid] = {k: v for k, v in best.items() if k not in ("keys", "mbids")}
    return out


def attach(res: Any, links: dict[str, dict[str, Any]]) -> Any:
    """Add `ma_artist` to every composer/person dict (an `id` like c_… or p_… beside a `name`) that has one."""
    if not links:
        return res
    stack = [res]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            nid = node.get("id")
            if isinstance(nid, str) and nid[:2] in ("c_", "p_") and "name" in node and nid in links:
                node["ma_artist"] = links[nid]
            stack.extend(v for v in node.values() if isinstance(v, dict | list))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, dict | list))
    return res


class ArtistLinks:
    """The composer/performer → MA artist map, refreshed in the background."""

    def __init__(self, mass: Any, read: Callable[[Callable[[Index], Any]], Awaitable[Any]], logger: Any,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.mass, self._read, self.logger, self._clock = mass, read, logger, clock
        self.links: dict[str, dict[str, Any]] = {}
        self._at: float | None = None
        self._task: asyncio.Task | None = None

    async def _build(self) -> dict[str, dict[str, Any]]:
        artists = []
        async for a in self.mass.music.artists.iter_library_items():
            ref = artist_ref(a)
            ref["keys"] = [k] if (k := ident_key(ref["name"])) else []
            ref["mbids"] = _mb_artist_ids(a)
            artists.append(ref)
        return await self._read(lambda ix: match_artists(ix, artists))

    async def _refresh(self) -> None:
        try:
            self.links = await self._build()
        except Exception as err:  # noqa: BLE001 - links are decoration: keep the last map
            self.logger.debug("Classical: MA artist links not refreshed: %r", err)
        finally:
            self._at = self._clock()

    async def get(self) -> dict[str, dict[str, Any]]:
        """The current map; starts a refresh when stale and waits briefly only for the very first one."""
        stale = self._at is None or self._clock() - self._at > TTL
        if stale and (self._task is None or self._task.done()):
            self._task = asyncio.ensure_future(self._refresh())
        if self._at is None and self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._task), FIRST_WAIT)
        return self.links

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


def wikipedia_title(entity: dict, qid: str, site: str = "enwiki") -> str | None:
    ent = (entity.get("entities") or {}).get(qid) or {}
    link = (ent.get("sitelinks") or {}).get(site) or {}
    title = link.get("title")
    return title if isinstance(title, str) and title.strip() else None


def summary_out(data: dict, title: str) -> dict[str, Any] | None:
    """Wikipedia's REST summary as the work page shows it; None for a disambiguation page or no text."""
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return None
    text = (data.get("extract") or "").strip()
    if not text:
        return None
    if len(text) > MAX_EXTRACT:
        cut = text[:MAX_EXTRACT]
        text = cut[: cut.rfind(". ") + 1] if ". " in cut else cut.rstrip() + "…"
    url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page") \
        or f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
    return {"text": text, "title": data.get("title") or title, "url": url, "source": "Wikipedia",
            "license": LICENSE, "license_url": LICENSE_URL}


async def work_description(mbid: str, mb: Any, wikidata: Any, wikipedia: Any) -> dict[str, Any] | None:
    """MB work → Wikidata → English Wikipedia summary (see the module note); None when any link is missing."""
    if not _MBID.match(mbid or ""):
        return None
    data = await mb.get(mb.url("work", mbid, "url-rels"))
    qid = wikidata_qid(parse_url_rels(data))
    if not qid:
        return None
    entity = await wikidata.get(f"{wikidata.base_url}/w/api.php?action=wbgetentities&ids={qid}"
                                "&props=sitelinks&sitefilter=enwiki&format=json")
    title = wikipedia_title(entity, qid)
    if not title:
        return None
    summary = await wikipedia.get(f"{wikipedia.base_url}/api/rest_v1/page/summary/"
                                  f"{quote(title.replace(' ', '_'), safe='')}")
    out = summary_out(summary, title)
    if out is not None:
        out["wikidata"] = f"https://www.wikidata.org/wiki/{qid}"
    return out
