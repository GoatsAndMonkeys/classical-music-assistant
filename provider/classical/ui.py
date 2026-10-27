"""Companion UI glue (PLAN.md §6, M3): the static route and the read commands the page calls.

A mixin of `ClassicalProvider`, kept apart from `glue.py` so the indexing glue stays small. The commands are
ordinary MA API commands (websocket, `POST /api`, HA automations all reach them) with `LIBRARY_READ`. Each
takes the caller's visible sources on the loop, builds its view on a reader thread with `classical_core.views`,
then maps images to image-proxy URLs. Corrections reuse the existing `classical/merge_composers` and
`classical/composer_name` commands and `classical/separate_work` (LIBRARY_MANAGE).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import InvalidDataError

from classical_core import __version__ as CORE_VERSION
from classical_core import views
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
    """Mixin: needs `mass`, `logger`, `_read`, `_vis` from the provider."""

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
            ("classical/accolades", self.cmd_accolades),
        ):
            handles.append(reg(command, handler, required_scope=Scope.LIBRARY_READ))
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
            "can_status": can(user, Scope.LIBRARY_MANAGE),
            "ui_url": self._ui_url(),
        }

    async def cmd_composers(self) -> dict[str, Any]:
        """Composers A–Z with life dates and work counts."""
        return await self._view(views.composers)

    async def cmd_works(self, composer_id: str | None = None, work_type: str | None = None, q: str | None = None,
                        date_kind: str = "composed", date_from: int | None = None, date_to: int | None = None,
                        performer_id: str | None = None, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """Works filtered by composer, type, text, performer and a composed/recorded/released year range."""
        return await self._view(views.works, composer_id=composer_id, work_type=work_type, q=q, date_kind=date_kind,
                                date_from=date_from, date_to=date_to, performer_id=performer_id, offset=offset,
                                limit=limit)

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

    async def cmd_review(self) -> dict[str, Any]:
        """The review queue grouped by reason, with correction actions for composer identities."""
        return await self._view(views.review)
