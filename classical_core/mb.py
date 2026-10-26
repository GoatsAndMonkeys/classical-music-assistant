"""Tier 2: MusicBrainz client and JSON → facts mapping (PLAN.md §5.3, §5.6).

The client talks to the public MusicBrainz API (or a configured mirror) at one request per second with our
own User-Agent, deduplicates requests in flight, backs off on 503 and network errors, and caches results
(good: 90 days, errors: 1 day) through a pluggable cache. MusicBrainz data is CC0.

The parsers are pure functions over the JSON, so they are unit-tested against recorded fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

RECORDING_INC = "work-rels+artist-rels"
WORK_INC = "work-rels+artist-rels+series-rels+aliases"
DEFAULT_BASE = "https://musicbrainz.org/ws/2"
GOOD_TTL = 90 * 86400
ERROR_TTL = 86400
MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

log = logging.getLogger("classical.mb")

# Sethster's MB role table (sethster:server/sethster_server/cd.py:46-48, Apache-2.0), plus the roles the
# recording grouping (§5.1) compares.
MB_ROLE = {"conductor": "conductor", "performing orchestra": "orchestra", "chorus master": "chorus master",
           "concertmaster": "concertmaster", "producer": "producer", "engineer": "engineer",
           "recording": "recording engineer", "mix": "mixing engineer", "mastering": "mastering engineer"}
_PERFORMANCE_TYPES = {"conductor", "performing orchestra", "performer", "vocal", "instrument", "chorus master",
                      "concertmaster"}


def is_mbid(value: str | None) -> bool:
    return bool(value) and bool(MBID_RE.match(value.strip().lower()))


def mb_role(rel: dict) -> str:
    """Sethster's `_mb_role` (cd.py:155-166): vocal parts and instruments become their attribute."""
    t = (rel.get("type") or "").strip()
    attrs = [a for a in rel.get("attributes") or [] if isinstance(a, str)]
    if t == "vocal":
        voice = next((a for a in attrs if a.endswith(" vocals")), None)
        if voice:
            v = voice[: -len(" vocals")]
            return "chorus" if v in ("choir", "background") else v
        return "vocals"
    if t == "instrument":
        return attrs[0] if attrs else "instrument"
    return MB_ROLE.get(t, t or "performer")


def edge_key(*parts: Any) -> str:
    """Deterministic key of one relationship (§4: type, target, sorted attributes, dates, ordering)."""
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---- parsed shapes -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Credit:
    name: str
    mbid: str | None
    role: str          # conductor | orchestra | chorus | soprano | piano | performer | ...
    kind: str          # person | group
    ord: int
    edge: str


@dataclass(frozen=True, slots=True)
class WorkLink:
    work_mbid: str
    title: str
    ord: int
    partial: bool
    live: bool
    medley: bool
    begin: str | None   # recording date range from the performance relationship
    end: str | None
    edge: str


@dataclass(slots=True)
class RecordingInfo:
    mbid: str
    title: str
    length_ms: int | None
    works: list[WorkLink] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)

    @property
    def recorded(self) -> tuple[str | None, str | None]:
        """Recording dates come only from performance relationships (§5.3 step 4)."""
        for w in self.works:
            if w.begin or w.end:
                return w.begin, w.end or w.begin
        return None, None


@dataclass(frozen=True, slots=True)
class WorkRef:
    mbid: str
    title: str
    ordering: int | None
    edge: str


@dataclass(slots=True)
class WorkInfo:
    mbid: str
    title: str
    work_type: str | None
    key: str | None
    languages: list[str] = field(default_factory=list)
    composers: list[Credit] = field(default_factory=list)
    credits: list[Credit] = field(default_factory=list)       # librettist, lyricist, arranger, orchestrator
    parents: list[WorkRef] = field(default_factory=list)      # "parts", backward: this work is part of
    parts: list[WorkRef] = field(default_factory=list)        # "parts", forward, ordered
    arrangement_of: list[WorkRef] = field(default_factory=list)
    revision_of: list[WorkRef] = field(default_factory=list)
    catalogues: list[tuple[str, str]] = field(default_factory=list)  # (series name, number)
    aliases: list[str] = field(default_factory=list)
    title_en: str | None = None   # primary English alias, if any


def _date(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _attrs(rel: dict) -> set[str]:
    return {a for a in rel.get("attributes") or [] if isinstance(a, str)}


def parse_recording(data: dict) -> RecordingInfo:
    rec = RecordingInfo(mbid=data["id"], title=data.get("title") or "", length_ms=data.get("length"))
    rels = data.get("relations") or []
    wi = 0
    ci = 0
    for r in rels:
        if r.get("target-type") == "work" and r.get("type") == "performance" and r.get("work"):
            a = _attrs(r)
            w = r["work"]
            rec.works.append(WorkLink(
                work_mbid=w["id"], title=w.get("title") or "", ord=wi, partial="partial" in a, live="live" in a,
                medley="medley" in a, begin=_date(r.get("begin")), end=_date(r.get("end")),
                edge=edge_key(r.get("type-id"), w["id"], sorted(a), r.get("begin"), r.get("end"),
                              r.get("ordering-key"))))
            wi += 1
        elif r.get("target-type") == "artist" and r.get("artist") and r.get("direction") == "backward":
            if r.get("type") not in _PERFORMANCE_TYPES:
                continue  # engineers, producers, "balance", ...
            role = mb_role(r)
            art = r["artist"]
            kind = "group" if (art.get("type") or "").lower() in ("orchestra", "choir", "group") else "person"
            if role == "performer" and kind == "group":
                role = "ensemble"
            rec.credits.append(Credit(
                name=art.get("name") or "", mbid=art.get("id"), role=role, kind=kind, ord=ci,
                edge=edge_key(r.get("type-id"), art.get("id"), sorted(_attrs(r)), r.get("begin"), r.get("end"))))
            ci += 1
    return rec


_KEY_ATTR = re.compile(r"^([A-G])(?:[- ]?(flat|sharp))?\s+(major|minor)$", re.IGNORECASE)


def parse_work(data: dict) -> WorkInfo:
    key = None
    for a in data.get("attributes") or []:
        if (a.get("type") or "").lower() == "key" and a.get("value"):
            m = _KEY_ATTR.match(a["value"].strip())
            key = (f"{m.group(1).upper()}{'-' + m.group(2).lower() if m.group(2) else ''} {m.group(3).lower()}"
                   if m else a["value"])
    info = WorkInfo(mbid=data["id"], title=data.get("title") or "", work_type=data.get("type"), key=key,
                    languages=list(data.get("languages") or []),
                    aliases=sorted({a["name"] for a in data.get("aliases") or [] if a.get("name")}))
    for a in data.get("aliases") or []:
        if a.get("locale") == "en" and a.get("primary") and a.get("name"):
            info.title_en = a["name"]
    ci = 0
    for r in data.get("relations") or []:
        t = r.get("type")
        tt = r.get("target-type")
        if tt == "artist" and r.get("artist") and r.get("direction") == "backward":
            art = r["artist"]
            c = Credit(name=art.get("name") or "", mbid=art.get("id"), role=t or "", kind="person", ord=ci,
                       edge=edge_key(r.get("type-id"), art.get("id"), sorted(_attrs(r))))
            ci += 1
            if t == "composer":
                info.composers.append(c)
            elif t in ("librettist", "lyricist", "arranger", "orchestrator", "translator"):
                info.credits.append(c)
        elif tt == "work" and r.get("work"):
            w = r["work"]
            ref = WorkRef(mbid=w["id"], title=w.get("title") or "", ordering=r.get("ordering-key"),
                          edge=edge_key(r.get("type-id"), w["id"], r.get("direction"), r.get("ordering-key")))
            back = r.get("direction") == "backward"
            if t == "parts":
                (info.parents if back else info.parts).append(ref)
            elif t == "arrangement" and not back:
                # MB: "<arrangement> is an arrangement of <original>" is stored forward on the arrangement
                info.arrangement_of.append(ref)
            elif t == "revision of" and not back:
                info.revision_of.append(ref)
        elif tt == "series" and r.get("series") and (r["series"].get("type") or "").lower() == "catalogue":
            num = (r.get("attribute-values") or {}).get("number")
            if num:
                info.catalogues.append((r["series"].get("name") or "", str(num)))
    info.parts.sort(key=lambda p: (p.ordering is None, p.ordering or 0))
    return info


# ---- client --------------------------------------------------------------------------------------------


class MBCache(Protocol):
    async def get(self, url: str) -> tuple[int, bytes] | None: ...
    async def put(self, url: str, status: int, body: bytes, ttl: int) -> None: ...


class MBError(Exception):
    """A failed or partial MB response: callers keep the previous assertions (§4)."""


class MBClient:
    """1 req/s, in-flight dedupe, 503/network backoff, cached. `fetch` is injectable for tests."""

    def __init__(self, contact: str, cache: MBCache | None = None, base_url: str = DEFAULT_BASE,
                 fetch: Callable[[str, dict[str, str]], Awaitable[tuple[int, bytes]]] | None = None,
                 interval: float = 1.0, session: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.interval = interval
        self.headers = {"User-Agent": f"ClassicalMusicAssistant/0.1 ( {contact} )", "Accept": "application/json"}
        self._fetch = fetch or self._aiohttp_fetch
        self._session = session
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._inflight: dict[str, asyncio.Future[dict]] = {}

    async def _aiohttp_fetch(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        import aiohttp  # shipped with MA

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        async with self._session.get(url, headers=headers) as resp:
            return resp.status, await resp.read()

    async def close(self) -> None:
        if self._session is not None and getattr(self._session, "close", None):
            await self._session.close()
            self._session = None

    def url(self, entity: str, mbid: str, inc: str) -> str:
        return f"{self.base_url}/{entity}/{mbid}?inc={inc}&fmt=json"

    async def recording(self, mbid: str) -> dict:
        return await self.get(self.url("recording", mbid, RECORDING_INC))

    async def work(self, mbid: str) -> dict:
        return await self.get(self.url("work", mbid, WORK_INC))

    async def get(self, url: str) -> dict:
        if url in self._inflight:
            return await asyncio.shield(self._inflight[url])
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._inflight[url] = fut
        try:
            result = await self._get(url)
            fut.set_result(result)
            return result
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(url, None)

    async def _get(self, url: str) -> dict:
        if self.cache is not None:
            hit = await self.cache.get(url)
            if hit is not None:
                status, body = hit
                if status == 200:
                    return json.loads(body)
                raise MBError(f"cached error {status} for {url}")
        delay = 2.0
        for attempt in range(4):
            async with self._lock:
                wait = self.interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    status, body = await self._fetch(url, self.headers)
                except (TimeoutError, OSError) as e:
                    status, body = -1, str(e).encode()
                finally:
                    self._last = time.monotonic()
            if status == 200:
                try:
                    data = json.loads(body)
                except ValueError as e:
                    raise MBError(f"bad JSON from {url}") from e
                if self.cache is not None:
                    await self.cache.put(url, 200, body, GOOD_TTL)
                return data
            if status in (503, -1, 429) and attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if self.cache is not None and status in (400, 404):
                await self.cache.put(url, status, b"", ERROR_TTL)
            raise MBError(f"HTTP {status} for {url}")
        raise MBError(f"gave up on {url}")
