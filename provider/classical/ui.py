"""Companion UI glue (PLAN.md §6, M3): the static route and the read commands the page calls.

A mixin of `ClassicalProvider`, kept apart from `glue.py` so the indexing glue stays small. The commands are
ordinary MA API commands (websocket, `POST /api`, HA automations all reach them) with `LIBRARY_READ`. Each
takes the caller's visible sources on the loop, builds its view on a reader thread with `classical_core.views`,
then maps images to image-proxy URLs. Corrections reuse the existing `classical/merge_composers` and
`classical/composer_name` commands, `classical/separate_work`, `classical/join_discs` and
`classical/separate_discs` (LIBRARY_MANAGE).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Callable
from typing import Any

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from classical_core import __version__ as CORE_VERSION
from classical_core import curation, streaming, triage, views
from classical_core.index import Index

from .web import ROUTE, ClassicalWeb, image_urls

UI_PATH = "/classical/"
NOT_FOUND = "Not found"


def can(user: Any, scope: Any) -> bool:
    """Whether `user` holds `scope` (MA's own role check; no user means an internal call: allowed)."""
    if user is None:
        return True
    try:
        from music_assistant.controllers.webserver.helpers.auth_middleware import has_scope
    except ImportError:  # pragma: no cover - every supported MA has it
        return False
    return bool(has_scope(user, scope))


class CompanionUI:
    """Mixin: needs `mass`, `logger`, `_read`, `_vis`, `_today`, `_hemisphere` from the provider."""

    mass: Any
    logger: Any
    _web: ClassicalWeb | None = None

    def _ui_url(self) -> str:
        try:
            base = str(self.mass.webserver.base_url or "").rstrip("/")
        except Exception:  # noqa: BLE001 - no webserver yet: show the bare path
            base = ""
        return f"{base}{UI_PATH}"

    def _ui_config_entries(self) -> tuple[ConfigEntry, ...]:
        # MA's frontend can't link to a plugin page (§2.6), so the address is shown in the settings
        return (ConfigEntry(key="ui_link", type=ConfigEntryType.LABEL, label=f"Companion UI: {self._ui_url()}",
                            required=False),)

    def _register_ui(self) -> list[Callable[[], None]]:
        """Register the UI's commands and its static route; returns the unregister handles."""
        reg = self.mass.register_api_command
        handles: list[Callable[[], None]] = []
        try:
            self._register_ui_into(handles, reg)
        except Exception:
            # a half-registered UI would block the next load ("already registered"): undo what was done
            for undo in reversed(handles):
                undo()
            raise
        self.logger.info("Classical companion UI: %s", self._ui_url())
        return handles

    def _register_ui_into(self, handles: list[Callable[[], None]], reg: Callable[..., Callable[[], None]]) -> None:
        for command, handler in (
            ("classical/ui_info", self.cmd_ui_info),
            ("classical/composers", self.cmd_composers),
            ("classical/works", self.cmd_works),
            ("classical/work", self.cmd_work),
            ("classical/recording", self.cmd_recording),
            ("classical/performers", self.cmd_performers),
            ("classical/performer", self.cmd_performer),
            ("classical/review", self.cmd_review),
            ("classical/review_triage", self.cmd_review_triage),
            ("classical/accolades", self.cmd_accolades),
            ("classical/today", self.cmd_today),
            ("classical/streaming", self.cmd_streaming),
        ):
            handles.append(reg(command, handler, required_scope=Scope.LIBRARY_READ))
        # MA's own music/library/add_item scope: adding an album changes the shared library
        handles.append(reg("classical/add_album", self.cmd_add_album, required_scope=Scope.LIBRARY_WRITE))
        self._web = ClassicalWeb(CORE_VERSION)
        ws = self.mass.webserver
        try:
            handles.append(ws.register_dynamic_route(ROUTE, self._web.handle, "*"))
        except RuntimeError:
            # a route left behind by a reload that crashed before unload: replace it once
            ws.unregister_dynamic_route(ROUTE, "*")
            handles.append(ws.register_dynamic_route(ROUTE, self._web.handle, "*"))

    # ---- helpers -----------------------------------------------------------------------------------------

    def _image_id(self, provider: str, path: str) -> str:
        return self.mass.metadata.compute_image_id(provider, path)

    async def _view(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        """Build a view on a reader thread; not-found and bad arguments become MA's errors."""
        vis = self._vis()  # on the loop: the current user is a context variable

        def build(ix: Index) -> Any:
            return fn(ix, *args, vis=vis, **kw)

        try:
            res = await self._read(build)
        except views.NotFound:
            # not MediaNotFoundError: MA 2.10.8's `POST /api` answers that with a 500 and logs a traceback for
            # every stale or probed id; InvalidDataError is a plain 400. The text never says hidden vs unknown.
            raise InvalidDataError(NOT_FOUND) from None
        except ValueError as err:
            raise InvalidDataError(str(err)) from err
        return image_urls(res, self._image_id)

    # ---- commands ----------------------------------------------------------------------------------------

    async def cmd_ui_info(self) -> dict[str, Any]:
        """Who is asking and what they may do; the page hides controls the server would refuse anyway."""
        user = get_current_user()
        return {
            "version": CORE_VERSION,
            "user": {"id": getattr(user, "user_id", None), "username": getattr(user, "username", None),
                     "display_name": getattr(user, "display_name", None) or getattr(user, "username", None),
                     "role": str(getattr(getattr(user, "role", None), "value", getattr(user, "role", "")) or "")}
            if user else None,
            "can_correct": can(user, Scope.LIBRARY_MANAGE),
            # classical/add_album needs LIBRARY_WRITE (as MA's music/library/add_item); shown only then
            "can_add": can(user, Scope.LIBRARY_WRITE),
            "streaming": [{"instance": p.instance_id, "name": getattr(p, "name", None) or p.instance_id}
                          for p in self._streaming_providers(self._vis())],
            "can_status": can(user, Scope.LIBRARY_MANAGE),
            "ui_url": self._ui_url(),
        }

    async def cmd_composers(self) -> dict[str, Any]:
        """Composers A–Z with life dates and work counts."""
        return await self._view(views.composers)

    async def cmd_works(self, composer_id: str | None = None, work_type: str | None = None, q: str | None = None,
                        date_kind: str = "composed", date_from: int | None = None, date_to: int | None = None,
                        performer_id: str | None = None, period: str | None = None, decade: int | None = None,
                        sort: str | None = None, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """Works filtered by composer, type, text, performer, a composed/premiered/recorded/released year range,
        period and decade; sorted by catalogue or by date."""
        return await self._view(views.works, composer_id=composer_id, work_type=work_type, q=q, date_kind=date_kind,
                                date_from=date_from, date_to=date_to, performer_id=performer_id, period=period,
                                decade=decade, sort=sort, offset=offset, limit=limit)

    async def cmd_work(self, id: str, scope: str | None = None) -> dict[str, Any]:
        """A work with all its recordings, facets for filtering, and the recording Play picks."""
        return await self._view(views.work, id, scope=scope)

    async def cmd_recording(self, id: str) -> dict[str, Any]:
        """One recording: full credits, album, movements in order with track URIs."""
        return await self._view(views.recording, id)

    async def cmd_performers(self, role: str = "all", with_id: str | None = None,
                             q: str | None = None) -> dict[str, Any]:
        """Performers by role bucket; `with_id` keeps those who recorded with that performer."""
        return await self._view(views.performers, role=role, with_id=with_id, q=q)

    async def cmd_performer(self, id: str, with_id: str | None = None) -> dict[str, Any]:
        """A performer's works and partners (optionally only recordings shared with `with_id`)."""
        return await self._view(views.performer, id, with_id=with_id)

    async def cmd_accolades(self, recording_id: str | None = None, work_id: str | None = None) -> dict[str, Any]:
        """Awards, reviews and ratings of one recording (with review-site links), or of every recording of a
        work with its acclaim and pick score."""
        return await self._view(views.accolades, recording_id=recording_id, work_id=work_id)

    async def cmd_today(self, date: str | None = None) -> dict[str, Any]:
        """Today's shelves (or another day's, `date` as YYYY-MM-DD): on this day, the season, the weekday,
        acclaimed recordings. Each pick plays its recording as a whole work."""
        try:
            day = dt.date.fromisoformat(date) if date else dt.date.today()
        except (TypeError, ValueError):
            raise InvalidDataError("date must be YYYY-MM-DD") from None
        res = curation.header(day, self._hemisphere()) | {"shelves": await self._today(day)}
        return image_urls(res, self._image_id)

    def _streaming_providers(self, vis: list[str] | None) -> list[Any]:
        """The caller's loaded streaming providers that can search (none: the section is not offered)."""
        out = []
        for prov in list(getattr(self.mass.music, "providers", None) or []):
            if getattr(prov, "is_streaming_provider", False) is not True:
                continue
            if ProviderFeature.SEARCH not in (getattr(prov, "supported_features", None) or ()):
                continue
            if vis is not None and prov.instance_id not in vis:
                continue
            out.append(prov)
        return sorted(out, key=lambda p: p.instance_id)

    async def cmd_streaming(self, work_id: str, provider: str | None = None) -> dict[str, Any]:
        """Other recordings of a work on the caller's streaming services (PLAN.md §5.10): each provider
        instance is searched itself (not MA's global search, which keeps one instance per service), and
        albums holding the work that are not in the library yet are listed. Nothing is added here; the
        page's "Add to library" calls `classical/add_album`."""
        vis = self._vis()
        provs = [p for p in self._streaming_providers(vis) if provider in (None, p.instance_id)]

        def facts(ix: Index) -> tuple[dict, set[tuple[str, str]]]:
            w = views.work(ix, work_id, vis=vis)["work"]   # NotFound for a work the caller cannot see
            marks = ",".join("?" * len(provs))
            have = {(r[0], r[1]) for r in ix._q(
                f"SELECT provider_instance, item_id FROM mapping_state WHERE status<>'gone' "
                f"AND provider_instance IN ({marks})", [p.instance_id for p in provs])} if provs else set()
            return w, have

        try:
            w, have = await self._read(facts)
        except views.NotFound:
            raise InvalidDataError(NOT_FOUND) from None
        composer = (w.get("composers") or [{}])[0].get("name")
        query = streaming.search_query(w["title"], composer)

        async def one(prov: Any) -> dict[str, Any]:
            entry: dict[str, Any] = {"instance": prov.instance_id, "name": getattr(prov, "name", None)
                                     or prov.instance_id, "albums": [], "error": None}
            try:
                res = await asyncio.wait_for(prov.search(query, [MediaType.TRACK], limit=50), timeout=20)
            except Exception as err:  # noqa: BLE001 - one service failing leaves the others
                self.logger.debug("Streaming search on %s for %r: %r", prov.instance_id, query, err)
                entry["error"] = "Search failed"
                return entry
            entry["albums"] = streaming.match_search(
                w["title"], composer, [_track_dict(t, prov.instance_id) for t in getattr(res, "tracks", None) or []],
                have)
            return entry

        found = await asyncio.gather(*(one(p) for p in provs))
        return image_urls({"work": {"id": w["id"], "title": w["title"]}, "query": query, "providers": list(found)},
                          self._image_id)

    async def cmd_add_album(self, provider: str, item_id: str) -> dict[str, Any]:
        """Add one album from a streaming service the caller can see to MA's library, with its tracks.

        MA's `music/library/add_item` adds only the album unless the provider imports album tracks (off by
        default), and the classical index reads library tracks, so the tracks are imported here too. The
        provider must be one of the caller's visible, searchable streaming services: the page cannot name
        any other provider instance or a non-album item."""
        prov = next((p for p in self._streaming_providers(self._vis()) if p.instance_id == provider), None)
        if prov is None or not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 512:
            raise InvalidDataError(NOT_FOUND)
        try:
            item = await self.mass.music.get_item(MediaType.ALBUM, item_id, prov.instance_id)
        except MediaNotFoundError:
            raise InvalidDataError(NOT_FOUND) from None
        if getattr(item, "media_type", None) != MediaType.ALBUM:
            raise InvalidDataError(NOT_FOUND)
        lib = await self.mass.music.add_item_to_library(item)
        tracks = False
        if not prov.library_sync_album_tracks_enabled():    # else MA imports them itself
            await prov.import_album_tracks(item_id, lib)
            tracks = True
        return {"library_album_id": str(getattr(lib, "item_id", "")), "imported_tracks": tracks}

    async def cmd_review(self) -> dict[str, Any]:
        """The review queue grouped by reason, with correction actions for composer identities."""
        return await self._view(views.review)

    async def cmd_review_triage(self, kind: str | None = None, reason: str | None = None,
                                composer: str | None = None, album: str | None = None, q: str | None = None,
                                status: str | None = None, sort: str = "impact", offset: int = 0,
                                limit: int = 50) -> dict[str, Any]:
        """The review queue as decision cards: counts by kind, composer and album facets, one page of cards.
        Actions are listed only for a caller who may decide (classical/review_apply enforces it anyway)."""
        return await self._view(triage.view, can_correct=can(get_current_user(), Scope.LIBRARY_MANAGE),
                                sort=sort, offset=offset, limit=limit, kind=kind, reason=reason,
                                composer=composer, album=album, q=q, status=status)


def _track_dict(t: Any, instance: str) -> dict[str, Any]:
    """An MA search result track as `streaming.match_search` reads it."""
    alb = getattr(t, "album", None)
    image = None
    with contextlib.suppress(Exception):
        img = getattr(alb, "image", None)
        image = img.to_dict() if img is not None else None
    return {
        "provider_instance": instance, "item_id": str(getattr(t, "item_id", "")), "uri": getattr(t, "uri", None),
        "title": getattr(t, "name", "") or "", "duration": int(getattr(t, "duration", 0) or 0),
        "artists": [a.name for a in getattr(t, "artists", None) or [] if getattr(a, "name", None)],
        "album": {"uri": getattr(alb, "uri", None), "item_id": str(getattr(alb, "item_id", "") or ""),
                  "name": getattr(alb, "name", "") or "",
                  "year": getattr(alb, "year", None), "image": image,
                  "artists": [a.name for a in getattr(alb, "artists", None) or [] if getattr(a, "name", None)]}
        if alb is not None else {},
    }
