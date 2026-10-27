"""MA glue for the Classical plugin (PLAN.md §5.6, §6): worker, events, browse, commands, home rows.

Everything classical lives in `classical_core` (no MA imports); this module only adapts MA's library and
models to it. One worker task, created in a fresh context so it never runs as the user whose request
loaded the plugin, drives indexing; event callbacks only enqueue ids. No SQLite call runs on MA's event loop:
writes go through the `Database` writer thread, browse and command reads through its reader threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as dt
import os
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, EventType, ExternalID, ImageType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    RecommendationFolder,
    Track,
    UniqueList,
)

from classical_core import __version__ as CORE_VERSION
from classical_core import accolades, artwork, curation, triage, views, work_link
from classical_core import browse as tree
from classical_core import dates as work_dates
from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.mb import DEFAULT_BASE, MBClient, MBTransientError
from classical_core.names import LLMClient
from classical_core.store import Database, SqliteMBCache

from .ui import NOT_FOUND, CompanionUI

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

SUPPORTED_FEATURES: set[ProviderFeature] = {ProviderFeature.BROWSE, ProviderFeature.RECOMMENDATIONS}

CONF_MUSICBRAINZ = "musicbrainz"
CONF_MB_CONTACT = "mb_contact"
CONF_LLM = "llm_names"
CONF_LLM_BASE_URL = "llm_base_url"
CONF_LLM_MODEL = "llm_model"
CONF_LLM_API_KEY = "llm_api_key"
CONF_LLM_GUIDE = "llm_guide"
CONF_ACC_DATASET = "accolades_dataset"
CONF_ACC_WIKIDATA = "accolades_wikidata"
CONF_ACC_CRITIQUEBRAINZ = "accolades_critiquebrainz"
CONF_ACC_DISCOGS = "accolades_discogs"
CONF_DISCOGS_TOKEN = "discogs_token"
CONF_HEMISPHERE = "hemisphere"
CONF_COVER_ART = "cover_art"
TODAY_TTL = 300                      # seconds the day's shelves are reused (they change with the library)
TODAY_WAIT = 2.5                     # home rows wait this long for fresh shelves (MA gives rows 5 s)
TODAY_KEEP = 8                       # (day, sources, hemisphere) entries kept
NET_CHUNK = 20                         # albums per enrichment job; the job re-queues itself behind other work
CONF_PROVIDERS = "providers"          # MA's raw provider-config key (controllers/config)
PAGE = 500
DEBOUNCE = 2.0                         # seconds to let a burst of library events settle
RECONCILE_RETRY = 600                  # §5.6: removal_pending is rechecked every 10 min
SYNC_POLL = 20.0                       # seconds between library checks while an MA provider sync runs
ALBUM_TRACKS_QUERY = "tracks.item_id IN (SELECT track_id FROM album_tracks WHERE album_id = :album_id)"


class ClassicalProvider(CompanionUI, PluginProvider):
    """Classical index and browse tree."""

    index: Index
    _con: Any
    _db: Database | None
    _mb: MBClient | None
    _jobs: dict[str, None]
    _wake: asyncio.Event
    _worker_task: asyncio.Task | None
    _unsub: list[Callable[[], None]]
    _retry_handle: asyncio.TimerHandle | None
    _state: dict[str, Any]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for this provider."""
        return (
            ConfigEntry(
                key=CONF_MB_CONTACT,
                type=ConfigEntryType.STRING,
                label="Your email address or website, for looking things up online",
                description="Composers, works, dates and album covers are looked up in MusicBrainz, a free "
                "music encyclopedia, which asks every app for a way to reach the person running it. It is "
                "sent only to MusicBrainz and the other music sites you turn on. Until this is filled in, "
                "the plugin works from your music files alone.",
                default_value="",
                required=False,
            ),
            ConfigEntry(
                key=CONF_MUSICBRAINZ,
                type=ConfigEntryType.BOOLEAN,
                label="Look up composers and works on MusicBrainz",
                description="Fills in catalogue numbers, dates and missing details your files don't carry. "
                "Needs the email address or website above.",
                default_value=True,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_LLM,
                type=ConfigEntryType.BOOLEAN,
                label="Ask a language model about composer names nothing else resolves",
                description="Off by default. Only the unresolved names and a little album context are sent. "
                "Answers are proposals: applied only when confident and confirmed by MusicBrainz or the "
                "built-in composer list, otherwise listed for review.",
                default_value=False,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_LLM_GUIDE,
                type=ConfigEntryType.BOOLEAN,
                label="Write a short listening guide for each work with the language model",
                description="Off by default. Written once per work, when its page is first opened, from the "
                "work's Wikipedia summary, dates and movement titles only; shown labelled as AI-written. "
                "Uses the API settings below.",
                default_value=False,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_LLM_BASE_URL,
                type=ConfigEntryType.STRING,
                label="API base URL: https://api.anthropic.com for Claude, or OpenAI-compatible (e.g. http://localhost:11434/v1)",
                default_value="",
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_LLM_MODEL,
                type=ConfigEntryType.STRING,
                label="Model name (e.g. claude-opus-5-5)",
                default_value="",
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_LLM_API_KEY,
                type=ConfigEntryType.SECURE_STRING,
                label="API key (optional)",
                default_value="",
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_COVER_ART,
                type=ConfigEntryType.BOOLEAN,
                label="Find covers for albums that have none",
                description="When an album has no picture of its own and MusicBrainz knows which release it "
                "is, its cover is shown in Music Assistant. Nothing is added to your music folders. Needs the "
                "email address or website above.",
                default_value=True,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ACC_DATASET,
                type=ConfigEntryType.BOOLEAN,
                label="Show awards (Grammy, Gramophone, Diapason d'Or)",
                description="Uses a list that comes with the plugin; nothing is looked up online. An award is "
                "shown only when the work, the performers and the year all agree; doubtful matches are left "
                "for review.",
                default_value=True,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ACC_WIKIDATA,
                type=ConfigEntryType.BOOLEAN,
                label="Also look up album awards on Wikidata",
                description="Off unless turned on. Asks Wikidata about albums that MusicBrainz links to it. "
                "Needs the email address or website above.",
                default_value=False,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ACC_CRITIQUEBRAINZ,
                type=ConfigEntryType.BOOLEAN,
                label="Show reviews from CritiqueBrainz",
                description="Short excerpts of listener reviews, with the reviewer's name and a link to the "
                "full review.",
                default_value=False,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_ACC_DISCOGS,
                type=ConfigEntryType.BOOLEAN,
                label="Show Discogs ratings",
                description="Star ratings from Discogs users. Needs a Discogs account and its personal access "
                "token below.",
                default_value=False,
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_DISCOGS_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                label="Discogs personal access token",
                description="Found in your Discogs account under Settings, Developers.",
                default_value="",
                required=False,
                depends_on=CONF_ACC_DISCOGS,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_HEMISPHERE,
                type=ConfigEntryType.STRING,
                label="Where you live, for seasonal suggestions",
                description="Spring, summer, autumn and winter suggestions follow your half of the world. "
                "Holidays and anniversaries stay on their dates.",
                options=[ConfigValueOption(title="Northern hemisphere", value="north"),
                         ConfigValueOption(title="Southern hemisphere", value="south")],
                default_value="north",
                required=False,
            ),
            *self._ui_config_entries(),
        )

    async def handle_async_init(self) -> None:
        """Open the index (schema migrations run here)."""
        self._jobs = {}
        self._wake = asyncio.Event()
        self._worker_task = None
        self._watch_task = None
        self._unsub = []
        self._retry_handle = None
        self._state = {"current": None, "last_scan": None, "albums_indexed": 0, "errors": 0}
        db_path = os.path.join(self.mass.storage_path, "classical", "index.db")
        self._db = Database(db_path)
        await self._db.open()
        self._con = self._db.con
        self._mb = None
        self._mb_note = None
        if self.config.get_value(CONF_MUSICBRAINZ, True):
            contact = mb_contact(self.config.get_value(CONF_MB_CONTACT, ""))
            if contact:
                self._mb = MBClient(contact, cache=SqliteMBCache(self._con, run=self._db.write))
            else:
                # MB's policy wants a real contact in the User-Agent: no placeholder, no lookups without one
                self._mb_note = "add your email address or website in the Classical settings to turn MusicBrainz on"
                self.logger.warning("Classical: MusicBrainz lookups are off: %s", self._mb_note)
        self._acc_dataset = bool(self.config.get_value(CONF_ACC_DATASET, True))
        self._net = self._net_clients(mb_contact(self.config.get_value(CONF_MB_CONTACT, "")))
        self._wiki = self._wiki_clients(mb_contact(self.config.get_value(CONF_MB_CONTACT, ""))) if self._mb else {}
        self._caa = None
        self._art_after = None
        if self._mb is not None and self.config.get_value(CONF_COVER_ART, True):
            self._caa = artwork.CAAClient(mb_contact(self.config.get_value(CONF_MB_CONTACT, "")) or "",
                                          cache=SqliteMBCache(self._con, run=self._db.write, table="web_cache"))
        # one client for both opt-in uses; the index only sees it when name resolution is on
        self._llm = self._guide_llm = None
        names_on, guide_on = self.config.get_value(CONF_LLM, False), self.config.get_value(CONF_LLM_GUIDE, False)
        if names_on or guide_on:
            base = str(self.config.get_value(CONF_LLM_BASE_URL, "") or "").strip()
            model = str(self.config.get_value(CONF_LLM_MODEL, "") or "").strip()
            if base and model:
                key = str(self.config.get_value(CONF_LLM_API_KEY, "") or "") or None
                client = LLMClient(base, model, api_key=key)
                self._llm = client if names_on else None
                self._guide_llm = client if guide_on else None
            else:
                self.logger.warning("Classical: the language-model options need a base URL and a model")
        self.index = Index(self._con, self._mb, db=self._db, llm=self._llm)

    def _net_clients(self, contact: str | None) -> dict[str, MBClient]:
        """Clients for the opt-in accolade sources (docs/accolades.md): one rate-limited, cached client per host,
        identified by the MusicBrainz contact. Without a contact every network source stays off."""
        if not contact:
            return {}
        cache = SqliteMBCache(self._con, run=self._db.write, table="web_cache")
        out: dict[str, MBClient] = {}
        if self.config.get_value(CONF_ACC_WIKIDATA, False):
            out["wikidata"] = MBClient(contact, cache=cache, base_url="https://www.wikidata.org", interval=1.0)
        if self.config.get_value(CONF_ACC_CRITIQUEBRAINZ, False):
            out["critiquebrainz"] = MBClient(contact, cache=cache, base_url="https://critiquebrainz.org",
                                             interval=1.0)
        token = str(self.config.get_value(CONF_DISCOGS_TOKEN, "") or "").strip()
        if self.config.get_value(CONF_ACC_DISCOGS, False) and token:
            # Discogs: 60 authenticated requests a minute; its data may be kept 6 hours (API terms)
            out["discogs"] = MBClient(contact, cache=cache, base_url="https://api.discogs.com", interval=1.1,
                                      good_ttl=accolades.DISCOGS_TTL,
                                      extra_headers={"Authorization": f"Discogs token={token}"})
        return out

    def _wiki_clients(self, contact: str | None) -> dict[str, MBClient]:
        """Wikidata and English Wikipedia for work descriptions (MA frontend's work page, ma_links.py): cached
        in `web_cache`, 1 request a second each, identified by the MusicBrainz contact like every source."""
        if not contact:
            return {}
        cache = SqliteMBCache(self._con, run=self._db.write, table="web_cache")
        return {"wikidata": MBClient(contact, cache=cache, base_url="https://www.wikidata.org", interval=1.0),
                "wikipedia": MBClient(contact, cache=cache, base_url="https://en.wikipedia.org", interval=1.0)}

    async def loaded_in_mass(self) -> None:
        """Register commands and subscriptions, start the worker and queue the initial scan."""
        reg = self.mass.register_api_command
        self._unsub += [
            reg("classical/status", self.cmd_status, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/search", self.cmd_search, required_scope=Scope.LIBRARY_READ),
            reg("classical/composer", self.cmd_composer, required_scope=Scope.LIBRARY_READ),
            reg("classical/reindex", self.cmd_reindex, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/merge_composers", self.cmd_merge_composers, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/composer_name", self.cmd_composer_name, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/separate_work", self.cmd_separate_work, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/join_discs", self.cmd_join_discs, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/separate_discs", self.cmd_separate_discs, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/box_decide", self.cmd_box_decide, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/accolade_decide", self.cmd_accolade_decide, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/work_dates", self.cmd_work_dates, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/work_mb_link", self.cmd_work_mb_link, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/review_apply", self.cmd_review_apply, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/review_undo", self.cmd_review_undo, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/review_history", self.cmd_review_history, required_scope=Scope.LIBRARY_MANAGE),
            self.mass.subscribe(self._on_sync_completed, EventType.MUSIC_SYNC_COMPLETED),
            self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED),
            self.mass.subscribe(
                self._on_media_item,
                (EventType.MEDIA_ITEM_ADDED, EventType.MEDIA_ITEM_UPDATED, EventType.MEDIA_ITEM_DELETED),
            ),
        ]
        self._unsub += self._register_ui()
        # a fresh context: no current user or impersonation inherited from the loading request (§5.6)
        self._worker_task = asyncio.get_running_loop().create_task(
            self._worker(), name="classical-worker", context=contextvars.Context()
        )
        self._watch_task = asyncio.get_running_loop().create_task(
            self._sync_watch(), name="classical-sync-watch", context=contextvars.Context()
        )
        self._enqueue("providers")
        self._enqueue("scan")

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the worker after its current album, then close everything."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._retry_handle:
            self._retry_handle.cancel()
        for task in (self._watch_task, self._worker_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._mb:
            await self._mb.close()
        for client in (getattr(self, "_net", None) or {}).values():
            await client.close()
        for client in (getattr(self, "_wiki", None) or {}).values():
            await client.close()
        if getattr(self, "_caa", None) is not None:
            await self._caa.close()
        if getattr(self, "_artists", None) is not None:
            await self._artists.close()
        await self._db.close()
        # is_removed: the index database is kept (rollback leaves it untouched, §7)

    # ---- job queue ---------------------------------------------------------------------------------------

    def _enqueue(self, key: str) -> None:
        # a forced scan covers a normal one; a queued forced scan is never downgraded
        if key == "scan" and "scan:force" in self._jobs:
            return
        if key == "scan:force":
            self._jobs.pop("scan", None)
        self._jobs[key] = None
        self._wake.set()

    async def _write(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run `fn(*args)` on the database writer thread (inline when there is no Database: tests)."""
        db = getattr(self, "_db", None)
        return fn(*args) if db is None else await db.write(fn, *args)

    async def _read(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run `fn(index, *args)` on a reader thread against a read-only Index."""
        db = getattr(self, "_db", None)
        if db is None:
            return fn(self.index, *args)
        return await db.read(lambda con, *a: fn(Index(con), *a), *args)

    async def _worker(self) -> None:
        while True:
            await self._wake.wait()
            await asyncio.sleep(DEBOUNCE)
            self._wake.clear()
            while self._jobs:
                key = min(self._jobs, key=job_order)
                del self._jobs[key]
                self._state["current"] = key
                try:
                    await self._run(key)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._state["errors"] += 1
                    self.logger.exception("Classical job %s failed", key)
                finally:
                    self._state["current"] = None
                await asyncio.sleep(0)

    async def _run(self, key: str) -> None:
        kind, _, arg = key.partition(":")
        if kind == "scan":
            await self._scan(force=arg == "force")
        elif kind == "album":
            await self._index_album_id(arg)
            self._enqueue("accolades")
            self._enqueue("dates")
            self._enqueue("art")
        elif kind == "accolades":
            stats = await self._write(accolades.refresh, self._con, None, getattr(self, "_acc_dataset", True))
            self.logger.debug("Classical accolades: %s", stats)
            if self._mb is not None or getattr(self, "_net", None):
                self._enqueue("accolades_net")
        elif kind == "accolades_net":
            await self._enrich()
        elif kind == "dates":
            stats = await self._write(work_dates.refresh, self._con, self._mb_base())
            self.logger.debug("Classical work dates: %s", stats)
            if self._mb is not None:
                self._enqueue("dates_net")
            self._enqueue("worklink")
        elif kind == "dates_net":
            await self._dates_net()
        elif kind == "art":
            await self._art()
        elif kind == "worklink":
            stats, due = await self._write(work_link.refresh, self._con, self._mb_base(), work_link.CHUNK)
            self.logger.debug("Classical work links: %s", stats)
            if stats.get("linked"):
                self._enqueue("dates")          # a new MusicBrainz work id: its dates
            self._wl_due = due
            if due and self._mb is not None:
                self._enqueue("worklink_net")
        elif kind == "worklink_net":
            await self._worklink_net()
        elif kind == "changes":
            await self._changes()
        elif kind == "remove":
            await self._write(self.index.remove_album, arg)
        elif kind == "track":
            for aid in await self._write(self.index.albums_of_track, arg):
                self._enqueue(f"album:{aid}")
        elif kind == "providers":
            await self._reconcile_providers()

    # ---- library → index ---------------------------------------------------------------------------------

    def _job_row(self, job_id: str) -> tuple[str | None, bool] | None:
        """(cursor, forced) of an interrupted scan, or None."""
        r = self._con.execute("SELECT cursor, state, kind FROM job WHERE id=?", (job_id,)).fetchone()
        return (r["cursor"], r["kind"] == "scan_force") if r and r["state"] == "running" else None

    def _set_job(self, job_id: str, state: str, cursor: str | None = None, error: str | None = None,
                 force: bool = False) -> None:
        self._con.execute(
            "INSERT INTO job(id, kind, scope, cursor, state, started_at, error) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, cursor=excluded.cursor, state=excluded.state, "
            "error=excluded.error",
            (job_id, "scan_force" if force else "scan", "library", cursor, state, time.time(), error),
        )

    async def _scan(self, force: bool = False) -> None:
        """Reconcile the whole library: index every album, drop vanished ones.

        A normal scan resumes an interrupted one from job.cursor (forced, if that one was forced); a forced
        scan always starts from the first album, so a reindex never skips what an earlier scan passed."""
        start = 0
        prev = await self._write(self._job_row, "scan")
        if prev is not None and not force:
            cursor, force = prev
            start = int(cursor) if cursor and cursor.isdigit() else 0
            if start:
                self.logger.info("Resuming classical %sscan after album %s", "forced " if force else "", start)
        await self._write(self._set_job, "scan", "running", str(start), None, force)
        n = 0
        while True:
            rows = await self.mass.music.database.get_rows_from_query(
                "SELECT item_id FROM albums WHERE item_id > :c ORDER BY item_id", {"c": start}, limit=PAGE
            )
            if not rows:
                break
            for row in rows:
                start = int(row["item_id"])
                try:
                    if await self._index_album_id(str(start), force=force):
                        n += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # one bad album must not abort the scan; the cursor still advances past it, or every
                    # resumed scan would retry it first and fail again
                    self._state["errors"] += 1
                    self.logger.exception("Classical: indexing album %s failed", start)
                await self._write(self._set_job, "scan", "running", str(start), None, force)
        # albums the index knows but MA no longer has
        present = set()
        known = await self._write(self.index.album_ids)
        for i in range(0, len(known), PAGE):
            chunk = [int(a) for a in known[i : i + PAGE] if a.isdigit()]
            if chunk:
                q = f"SELECT item_id FROM albums WHERE item_id IN ({','.join(str(c) for c in chunk)})"
                present |= {str(r["item_id"]) for r in await self.mass.music.database.get_rows_from_query(q, {}, limit=PAGE)}
        for aid in known:
            if aid not in present:
                await self._write(self.index.remove_album, aid)
        await self._write(self._set_job, "scan", "done", None, None, force)
        self._state["last_scan"] = time.time()
        self._enqueue("accolades")
        self._enqueue("dates")
        self._enqueue("art")
        self.logger.info("Classical scan done: %d albums (re)indexed; %s", n, await self._read(Index.status))

    async def _sync_watch(self) -> None:
        """Watch the library while MA syncs a provider.

        MA 2.10.8 suppresses per-item MEDIA_ITEM_* events during a provider sync and only signals
        MUSIC_SYNC_COMPLETED at the end, so a first sync of a large library would leave the index empty for
        its whole run. While a sync task is active, a "changes" job is queued every SYNC_POLL seconds."""
        while True:
            await asyncio.sleep(SYNC_POLL)
            try:
                active = bool(self.mass.music.active_sync_tasks)
            except AttributeError:  # an MA without the property: rely on MUSIC_SYNC_COMPLETED alone
                return
            if active:
                self._enqueue("changes")

    async def _changes(self) -> None:
        """Queue the albums whose MA track count differs from the index (new or still filling up).

        Metadata-only changes are left to the full scan that follows MUSIC_SYNC_COMPLETED."""
        rows = await self.mass.music.database.get_rows_from_query(
            "SELECT album_id, COUNT(*) AS n FROM album_tracks GROUP BY album_id", {}, limit=0
        )
        known = await self._write(self.index.album_track_counts)
        for r in rows:
            aid = str(r["album_id"])
            if known.get(aid) != int(r["n"]):
                self._enqueue(f"album:{aid}")

    async def _album_tracks(self, album_id: int) -> list[Track]:
        out: list[Track] = []
        offset = 0
        while True:
            page = await self.mass.music.tracks.get_library_items_by_query(
                limit=PAGE,
                offset=offset,
                extra_query_parts=[ALBUM_TRACKS_QUERY],
                extra_query_params={"album_id": album_id, "preferred_album_id": album_id},
            )
            out += page
            if len(page) < PAGE:
                return out
            offset += PAGE

    def _mapping_in(self, pm: Any) -> MappingIn:
        prov = self.mass.get_provider(pm.provider_instance)
        path = None
        if prov is not None and hasattr(prov, "get_absolute_path"):
            with contextlib.suppress(Exception):
                path = prov.get_absolute_path(pm.item_id)
        return MappingIn(
            provider_instance=pm.provider_instance,
            item_id=str(pm.item_id),
            fingerprint=pm.details or None,
            path=path,
            available=prov is not None and pm.available,
            domain=getattr(pm, "provider_domain", None) or None,
        )

    async def _index_album_id(self, album_id: str, force: bool = False) -> bool:
        try:
            album = await self.mass.music.albums.get_library_item(int(album_id))
        except (MediaNotFoundError, ValueError):
            await self._write(self.index.remove_album, album_id)
            return False
        tracks = await self._album_tracks(int(album_id))
        ain = AlbumIn(
            library_album_id=str(album.item_id),
            name=album.name,
            artist=album.artist_str if album.artists else "",
            year=album.year,
            image=album.image.to_dict() if album.image else None,
            tracks=[
                TrackIn(
                    library_track_id=str(t.item_id),
                    disc=t.disc_number or None,
                    position=t.track_number or None,
                    title=t.name,
                    artist=t.artist_str if t.artists else "",
                    duration=int(t.duration or 0),
                    mbid=t.mbid,
                    mappings=[self._mapping_in(pm) for pm in t.provider_mappings],
                    isrcs=external_ids(t, "isrc"),
                    performers=sorted(str(p) for p in (getattr(getattr(t, "metadata", None), "performers", None) or ()) if p),
                    artists=[a.name for a in t.artists or [] if getattr(a, "name", None)],
                )
                for t in tracks
            ],
            barcodes=external_ids(album, "barcode"),
        )
        stats = await self.index.index_album(ain, force=force)
        await self._write(accolades.set_album_ids, self._con, ain.library_album_id, *album_ids(album))
        if stats is not None:
            self._state["albums_indexed"] += 1
            self.logger.debug("Indexed album %s (%s): %s", album_id, album.name, stats)
        return stats is not None

    async def _enrich(self) -> None:
        """One chunk of network enrichment (MB url relations; the opt-in Wikidata, CritiqueBrainz, Discogs).
        Re-queues itself while albums are due; a transient failure leaves the rest for the next run."""
        net = getattr(self, "_net", None) or {}
        if self._mb is None and not net:
            return
        due = await self._write(accolades.album_for_enrichment, self._con, NET_CHUNK)
        for album in due:
            try:
                res = await accolades.fetch_album(album, mb=self._mb, **net)
            except MBTransientError as err:
                self.logger.info("Classical accolades: lookups deferred (%s)", err)
                return
            await self._write(accolades.store_net, self._con, album["album_library_id"], res)
        if len(due) == NET_CHUNK:
            self._enqueue("accolades_net")

    async def _art(self) -> None:
        """One chunk of album art (classical_core/artwork.py): albums without an image get their MusicBrainz
        release's Cover Art Archive front, added to MA's library album. Walks the albums in id order and
        re-queues itself; a transient failure stops the walk until the next scan or album change."""
        caa = getattr(self, "_caa", None)
        if caa is None:
            return
        due = await self._write(artwork.due, self._con, self._art_after, artwork.CHUNK)
        for album in due:
            try:
                art = await artwork.find(album, caa, self._mb)
            except MBTransientError as err:
                self.logger.info("Classical album art: lookups deferred (%s)", err)
                return            # resumes from here: `_art_after` still names the last album done
            if art is not None:
                await self._apply_art(album["album_library_id"], art)
            self._art_after = album["album_library_id"]
        if len(due) == artwork.CHUNK:
            self._enqueue("art")
        else:
            self._art_after = None

    async def _apply_art(self, album_id: str, art: artwork.Art) -> bool:
        """Add the cover to MA's library album (a remote image in MA's own metadata; with a confident match
        also the MusicBrainz ids MA lacks, so its own metadata providers can use them). Never replaces an
        image MA has."""
        try:
            album = await self.mass.music.albums.get_library_item(int(album_id))
        except (MediaNotFoundError, ValueError):
            return False
        if getattr(album, "image", None) is not None:
            return False
        if art.confident:
            have = album_ids_map(album)
            for kind, value in ((ExternalID.MB_ALBUM, art.release), (ExternalID.MB_RELEASEGROUP, art.release_group)):
                if value and not have.get(str(getattr(kind, "value", kind))):
                    album.add_external_id(kind, value)
        album.metadata.add_image(MediaItemImage(type=ImageType.THUMB, path=art.url, provider=artwork.PROVIDER,
                                                remotely_accessible=True))
        await self.mass.music.albums.update_item_in_library(album.item_id, album)
        await self._write(artwork.store_image, self._con, album_id, art.url)
        self.logger.debug("Classical album art for %s (%s): %s", album_id, album.name, art.url)
        return True

    def _mb_base(self) -> str:
        return self._mb.base_url if getattr(self, "_mb", None) is not None else DEFAULT_BASE

    async def _dates_net(self) -> None:
        """One chunk of MusicBrainz work-date lookups (MB-linked works only, never a search; MB on only).
        The answers land in the MB cache; the offline 'dates' job then reads them and re-queues this one,
        until nothing is due. A transient failure leaves the rest for the next scan."""
        if self._mb is None:
            return
        due = await self._write(work_dates.mb_due, self._con, self._mb_base(), work_dates.MB_CHUNK)
        if not due:
            return
        n, err = await work_dates.fetch_mb(self._mb, due)
        if err:
            self.logger.info("Classical work dates: lookups deferred (%s)", err)
            if not n:
                return
        self._enqueue("dates")

    async def _worklink_net(self) -> None:
        """One chunk of MusicBrainz requests for the work-linking job (MB on only): composer browse pages,
        work searches and lookups, into the MB cache; the offline 'worklink' job then reads them. Every other
        job runs first (job_order), so a long import is never held up by more than one chunk; a transient
        failure leaves the rest for the next scan."""
        due, self._wl_due = getattr(self, "_wl_due", None) or [], []
        if self._mb is None or not due:
            return
        n, err = await work_link.fetch(self._mb, due)
        if err:
            self.logger.info("Classical work links: lookups deferred (%s)", err)
            if not n:
                return
        self._enqueue("worklink")

    # ---- provider presence (§5.6) ------------------------------------------------------------------------

    async def _reconcile_providers(self) -> None:
        configured = set((self.mass.config.get(CONF_PROVIDERS) or {}).keys())
        loaded = {p.instance_id for p in self.mass.providers}
        pending = False
        for pi in await self._write(self.index.provider_instances):
            if pi in loaded:
                state = "configured"
            elif pi in configured:
                state = "unavailable"
            else:
                left = await self.mass.music.database.get_count_from_query(
                    "SELECT item_id FROM provider_mappings WHERE provider_instance = :p", {"p": pi}
                )
                if left:
                    state = "removal_pending"
                    pending = True
                else:
                    if await self._write(self.index.provider_state, pi) != "removed":
                        for aid in await self._write(self.index.retract_provider, pi):
                            self._enqueue(f"album:{aid}")
                        self.logger.info("Provider %s removed: its evidence was retracted", pi)
                    continue
            if await self._write(self.index.set_provider_state, pi, state):
                self.logger.info("Provider %s is now %s", pi, state)
        if pending and self._retry_handle is None:
            loop = asyncio.get_running_loop()

            def retry() -> None:
                self._retry_handle = None
                self._enqueue("providers")

            self._retry_handle = loop.call_later(RECONCILE_RETRY, retry)

    # ---- events (enqueue only) ---------------------------------------------------------------------------

    def _on_sync_completed(self, event: MassEvent) -> None:
        self._enqueue("providers")
        self._enqueue("scan")

    def _on_providers_updated(self, event: MassEvent) -> None:
        self._enqueue("providers")

    def _on_media_item(self, event: MassEvent) -> None:
        # MA signals these with object_id = the item's uri ("library://album/12") and data = the item
        item = event.data
        uri = event.object_id if isinstance(event.object_id, str) else ""
        if uri.startswith("library://"):
            mt, _, item_id = uri.removeprefix("library://").partition("/")
        elif item is not None and getattr(item, "provider", None) == "library":
            mt = getattr(item, "media_type", None)
            mt, item_id = str(getattr(mt, "value", mt)), str(item.item_id)
        else:
            return
        if not item_id:
            return
        if mt == "album":
            kind = "remove" if event.event == EventType.MEDIA_ITEM_DELETED else "album"
            self._enqueue(f"{kind}:{item_id}")
        elif mt == "track":
            # the albums holding the track are looked up by the worker (no SQLite on the event loop)
            self._enqueue(f"track:{item_id}")
            album = getattr(item, "album", None)
            if album is not None and getattr(album, "provider", None) == "library":
                self._enqueue(f"album:{album.item_id}")

    # ---- access ------------------------------------------------------------------------------------------

    def _vis(self) -> list[str] | None:
        """The calling user's visible music sources (None = all)."""
        user = get_current_user()
        if user is None:
            return None
        try:
            from music_assistant.helpers.provider_access import visible_music_sources
        except ImportError:  # 2.10.x: the per-user provider filter, empty = all
            return list(user.provider_filter) or None
        return visible_music_sources(self.mass, user)

    # ---- browse ------------------------------------------------------------------------------------------

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the classical tree (path: classical://...)."""
        sub = path.split("://", 1)[1] if "://" in path else path
        vis = self._vis()  # on the loop: the current user is a context variable
        try:
            nodes = await self._read(tree.browse, sub, vis)
        except tree.NotFound as err:
            raise MediaNotFoundError(f"Not found: {path}") from err
        return await self._to_items(nodes)

    async def _to_items(self, nodes: list[tree.Node]) -> list[MediaItemType | BrowseFolder]:
        out: list[MediaItemType | BrowseFolder] = []
        for n in nodes:
            if n.kind == "track":
                t = await self._movement_track(n)
                if t is not None:
                    out.append(t)
                continue
            out.append(self._folder(n))
        return out

    def _folder(self, n: tree.Node) -> BrowseFolder:
        return BrowseFolder(
            item_id=n.item_id or n.path,
            provider=self.instance_id,
            path=f"{self.instance_id}://{n.path}",
            name=n.name,
            version=n.version,
            sort_name=n.sort_name,
            is_playable=n.playable,
            image=MediaItemImage.from_dict(n.image) if n.image else None,
        )

    async def _movement_track(self, n: tree.Node) -> Track | None:
        """A copy of the library track with the movement as its name and the work in `version` (§6)."""
        try:
            lib = await self.mass.music.tracks.get_library_item(int(n.track_id))
        except (MediaNotFoundError, ValueError, TypeError):
            return None
        t = Track.from_dict(lib.to_dict())
        t.name = n.name or t.name
        t.version = n.version
        return t

    # ---- home rows ---------------------------------------------------------------------------------------

    def _hemisphere(self) -> str:
        try:
            v = str(self.config.get_value(CONF_HEMISPHERE, "north") or "north")
        except Exception:  # noqa: BLE001 - no config (tests): the default
            v = "north"
        return v if v in curation.HEMISPHERES else "north"

    async def _today(self, day: dt.date | None = None) -> list[dict]:
        """The day's shelves (classical_core.curation), cached per day, visible sources and hemisphere.

        `day` defaults to the MA server's local date (its TZ; UTC in a container without TZ); the
        companion UI passes the browser's own date. One computation per key runs at a time (callers
        share it), a stale entry is served while it refreshes, and a few days are kept side by side
        so a UI in another timezone doesn't evict the server's day."""
        vis = self._vis()  # on the loop
        day = day or dt.date.today()
        hemi = self._hemisphere()
        key = (day.isoformat(), None if vis is None else tuple(sorted(vis)), hemi)
        cache: dict = self.__dict__.setdefault("_today_cache", {})
        tasks: dict = self.__dict__.setdefault("_today_tasks", {})
        hit = cache.get(key)
        fresh = hit is not None and time.monotonic() - hit[0] < TODAY_TTL
        if fresh:
            return hit[1]
        task = tasks.get(key)
        if task is None:
            task = tasks[key] = asyncio.ensure_future(self._today_compute(key, day, hemi, vis))
            task.add_done_callback(lambda t, k=key: (tasks.pop(k, None), t.cancelled() or t.exception()))
        if hit is not None:
            return hit[1]                       # stale: serve it, the refresh fills the cache
        return await asyncio.shield(task)       # an MA timeout must not cancel the shared computation

    async def _today_compute(self, key: tuple, day: dt.date, hemi: str, vis) -> list[dict]:
        try:
            shelves = await self._read(lambda ix: curation.shelves(ix, day, hemi, vis))
        except Exception as err:
            self.logger.warning("Classical: today's shelves failed: %s", err)
            raise
        cache: dict = self.__dict__.setdefault("_today_cache", {})
        cache.pop(key, None)
        cache[key] = (time.monotonic(), shelves)
        while len(cache) > TODAY_KEEP:
            cache.pop(next(iter(cache)))        # oldest first
        return shelves

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Home rows: today's shelves (on this day, the season, the weekday, acclaimed), recently added
        works, composers in your library. The shelves get a short wait; on a cold cache their names come
        from the date alone and the picks are computed in the background for the items call. "Today"
        is the MA server's local date (set TZ on a container)."""
        today: list[RecommendationFolder] = []
        task = asyncio.ensure_future(self._today())
        done, _ = await asyncio.wait({task}, timeout=TODAY_WAIT)
        if task in done and not task.cancelled() and task.exception() is None:
            for s in task.result():
                today.append(RecommendationFolder(item_id=f"today/{s['id']}", provider=self.instance_id,
                                                  name=s["title"], subtitle=s["subtitle"]))
        elif task in done:
            pass                                # logged by _today_compute; the other rows still show
        else:
            self._today_pending = task          # keep a reference; it fills the cache when it finishes
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            today = [RecommendationFolder(item_id=f"today/{sid}", provider=self.instance_id, name=title)
                     for sid, title in curation.titles(dt.date.today())]
        return [
            *today,
            RecommendationFolder(item_id="recent_works", provider=self.instance_id, name="Recently added works",
                                 translation_key="classical_recent_works"),
            RecommendationFolder(item_id="composers", provider=self.instance_id, name="Composers in your library",
                                 translation_key="classical_composers"),
        ]

    async def get_recommendation_items(self, item_id: str) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Items for one home row."""
        vis = self._vis()
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList()
        if item_id.startswith("today/"):
            shelf = next((s for s in await self._today() if s["id"] == item_id[6:]), None)
            for p in shelf["picks"] if shelf else []:
                items.append(self._folder(tree.Node(
                    "folder", tree.recording_item_id(p["work_id"], p["recording_id"]), p["title"],
                    version=f"{p['composer'] or ''} · {p['reason']}",
                    item_id=tree.recording_item_id(p["work_id"], p["recording_id"]), playable=True,
                    image=p.get("image"))))
        elif item_id == "recent_works":
            def recent(ix: Index) -> list[tuple[dict, dict]]:
                out = []
                for w in ix.recently_added_works(vis, limit=20):
                    best = ix.best_recording(w["id"], vis)
                    if best is not None:
                        out.append((w, best[0]))
                return out

            for w, r in await self._read(recent):
                items.append(self._folder(tree.Node(
                    "folder", f"works/{w['id']}/{r['id']}", w["title"], version=f"{w['composer'] or ''} · {r['label']}",
                    item_id=tree.recording_item_id(w["id"], r["id"]), playable=True, image=r.get("image"))))
        elif item_id == "composers":
            comps = sorted(await self._read(Index.composers, vis), key=lambda c: -c["works"])[:20]
            for c in comps:
                items.append(self._folder(tree.Node("folder", f"composers/{c['id']}", c["name"],
                                                    version=f"{c['works']} works")))
        return items

    # ---- API commands (M1a) ------------------------------------------------------------------------------

    @staticmethod
    def _work_result(ix: Index, w: dict, vis: list[str] | None) -> dict[str, Any]:
        res: dict[str, Any] = {
            "id": w["id"], "kind": "work", "title": w["title"], "composer": w.get("composer"),
            "work_type": w.get("work_type"), "catalogue": w.get("catalogue_display"),
            "recordings": w.get("recordings"), "browse_path": f"{tree.SCHEME}works/{w['id']}",
        }
        best = ix.best_recording(w["id"], vis)
        if best:
            r, reason = best
            res.update(play_uri=tree.play_uri(w["id"], r["id"]), recording_id=r["id"], recording_label=r["label"],
                       selection_reason=reason)
        return res

    async def cmd_search(self, query: str, kind: str = "work", limit: int = 25) -> list[dict[str, Any]]:
        """Search composers, works or recordings; results carry browse_path and (works, recordings) play_uri."""
        if kind not in ("composer", "work", "recording"):
            raise InvalidDataError("kind must be composer, work or recording")
        vis = self._vis()
        lim = max(1, min(int(limit), 200))

        def search(ix: Index) -> list[dict[str, Any]]:
            hits = ix.search(query, kind, vis, limit=lim)
            if kind == "composer":
                return [{"id": c["id"], "kind": "composer", "name": c["name"], "works": c["works"],
                         "browse_path": f"{tree.SCHEME}composers/{c['id']}"} for c in hits]
            if kind == "work":
                return [self._work_result(ix, w, vis) for w in hits]
            return [{"id": r["id"], "kind": "recording", "label": r["label"], "work_id": r["work_id"],
                     "work_title": r["work_title"], "composer": r["composer"], "album": r.get("album"),
                     "browse_path": f"{tree.SCHEME}works/{r['work_id']}/{r['id']}",
                     "play_uri": tree.play_uri(r["work_id"], r["id"])} for r in hits]

        return await self._read(search)

    async def cmd_composer(self, id: str | None = None, name: str | None = None) -> dict[str, Any]:
        """One composer (by id or name) with their works by type."""
        vis = self._vis()

        def composer(ix: Index) -> dict[str, Any] | None:
            comp = None
            if id:
                comp = next((c for c in ix.composers(vis) if c["id"] == ix.resolve("composer", id)), None)
            elif name:
                hits = ix.search(name, "composer", vis, limit=1)
                comp = hits[0] if hits else None
            if comp is None:
                return None
            works = ix.works(comp["id"], vis=vis)
            return {
                "id": comp["id"], "kind": "composer", "name": comp["name"], "born": comp.get("born"),
                "died": comp.get("died"), "browse_path": f"{tree.SCHEME}composers/{comp['id']}",
                "types": [{"type": t, "works": n} for t, n in ix.composer_types(comp["id"], vis)],
                "works": [self._work_result(ix, w, vis) for w in works[:200]],
            }

        res = await self._read(composer)
        if res is None:
            raise MediaNotFoundError("Composer not found")
        return res

    async def cmd_status(self) -> dict[str, Any]:
        """Index counts and worker state (household-wide, so LIBRARY_MANAGE)."""
        return {
            "core_version": CORE_VERSION, "ma_version": self.mass.version, **await self._read(Index.status),
            "worker": {**self._state, "queued": list(self._jobs),
                       "running": bool(self._worker_task and not self._worker_task.done())},
            "musicbrainz": self._mb is not None,
            **({"musicbrainz_note": self._mb_note} if getattr(self, "_mb_note", None) else {}),
        }

    async def cmd_reindex(self, force: bool = False) -> dict[str, Any]:
        """Queue a full library scan (force re-reads every file and MB response)."""
        self._enqueue("scan:force" if force else "scan")
        return {"queued": list(self._jobs)}

    @staticmethod
    def _user_name() -> str | None:
        user = get_current_user()
        return getattr(user, "username", None) or getattr(user, "user_id", None) if user else None

    async def cmd_merge_composers(self, source_id: str, target_id: str) -> dict[str, Any]:
        """A user correction: every occurrence of one composer becomes another (§5.11 f)."""
        try:
            # the surviving id comes back from the writer thread: no SQLite on the event loop
            into = await self._write(self.index.merge_composers, source_id, target_id, self._user_name())
        except ValueError as err:
            raise InvalidDataError(str(err)) from err
        return {"merged": source_id, "into": into}

    async def _decide(self, actions: list[dict[str, Any]], label: str | None = None) -> dict[str, Any]:
        """Apply review decisions as one undoable batch (triage.apply: visibility checked per target, one
        transaction), then queue the jobs they need."""
        user = self._user_name()
        vis = self._vis()  # on the loop: the current user is a context variable

        def apply(ix: Index) -> dict:
            ix.con.execute("BEGIN IMMEDIATE")
            try:
                out = triage.apply(ix, actions, user, vis, label)
                ix.con.execute("COMMIT")
                return out
            except BaseException:
                ix.con.execute("ROLLBACK")
                raise

        try:
            out = await self._write(apply, self.index)
        except views.NotFound:
            raise InvalidDataError(NOT_FOUND) from None
        except ValueError as err:
            raise InvalidDataError(str(err)) from err
        for job in out["jobs"]:
            self._enqueue(job)
        return out

    async def cmd_review_apply(self, actions: list[dict[str, Any]], label: str | None = None,
                               dismiss_filter: dict[str, Any] | None = None) -> dict[str, Any]:
        """Review decisions in one batch: hide/show items, rejections for many items at once, or a single
        accept or credit mapping. `dismiss_filter` hides every open item a triage filter shows."""
        actions = list(actions or [])
        if dismiss_filter is not None:
            if not isinstance(dismiss_filter, dict):
                raise InvalidDataError("dismiss_filter must be an object")
            vis = self._vis()
            f = {k: dismiss_filter.get(k) for k in ("kind", "reason", "composer", "album", "q")}
            try:
                ids = await self._read(lambda ix: triage.items_for(ix, vis, **f))
            except ValueError as err:
                raise InvalidDataError(str(err)) from err
            actions.append({"op": "dismiss", "items": ids})
        out = await self._decide(actions, label)
        return {**out, "queued": list(self._jobs)}

    async def cmd_review_undo(self, batch: int) -> dict[str, Any]:
        """Undo one of the caller's own decisions (refused while a later decision on the same thing stands)."""
        user = self._user_name()

        def apply(ix: Index) -> dict:
            ix.con.execute("BEGIN IMMEDIATE")
            try:
                out = triage.undo(ix, int(batch), user)
                ix.con.execute("COMMIT")
                return out
            except BaseException:
                ix.con.execute("ROLLBACK")
                raise

        try:
            out = await self._write(apply, self.index)
        except views.NotFound:
            raise InvalidDataError(NOT_FOUND) from None
        except (TypeError, ValueError) as err:
            raise InvalidDataError(str(err)) from err
        for job in out["jobs"]:
            self._enqueue(job)
        return {**out, "queued": list(self._jobs)}

    async def cmd_review_history(self, limit: int = 30) -> dict[str, Any]:
        """The caller's recent review decisions, newest first, with whether each can be undone now."""
        user = self._user_name()
        try:
            n = int(limit)
        except (TypeError, ValueError) as err:
            raise InvalidDataError("limit must be a number") from err
        return {"items": await self._read(lambda ix: triage.history(ix, user, n))}

    async def cmd_composer_name(self, name: str, composer_id: str, album_id: str | None = None) -> dict[str, Any]:
        """A user correction: this credit string always means this composer (with `album_id`, only on that
        album); affected albums re-derive. Undoable from the review page."""
        args = {"op": "composer_name", "name": name, "composer_id": composer_id}
        if album_id:
            args["album_id"] = album_id
        out = await self._decide([args])
        return {"albums": out["results"][0]["albums"], "batch": out["batch"], "queued": list(self._jobs)}

    async def cmd_accolade_decide(self, entry_ref: str, recording_id: str, decision: str) -> dict[str, Any]:
        """A user decision on a doubtful award match: 'confirm' (same recording: show it), 'reject' (never
        show it here) or 'clear'. Only a recording the caller can see. Undoable from the review page."""
        out = await self._decide([{"op": "accolade_decide", "entry": entry_ref, "recording_id": recording_id,
                                   "decision": decision}])
        return {**out["results"][0], "batch": out["batch"], "queued": list(self._jobs)}

    async def cmd_separate_work(self, recording_id: str) -> dict[str, Any]:
        """A user correction: this recording's work was wrongly merged with another album's; keep them apart.
        Only a recording the caller can see: a hidden or unknown id answers the same 'Not found'."""
        user = self._user_name()
        vis = self._vis()  # on the loop: the current user is a context variable

        def apply(ix: Index) -> list[str]:
            ix.con.execute("BEGIN IMMEDIATE")
            try:
                views.recording(ix, recording_id, vis)      # NotFound when hidden
                out = ix.separate_work(recording_id, user)
                ix.con.execute("COMMIT")
                return out
            except BaseException:
                ix.con.execute("ROLLBACK")
                raise

        try:
            albums = await self._write(apply, self.index)
        except views.NotFound:
            raise InvalidDataError(NOT_FOUND) from None
        except ValueError as err:
            raise InvalidDataError(str(err)) from err
        self._enqueue("scan")
        return {"albums": len(albums), "queued": list(self._jobs)}

    async def cmd_work_dates(self, work_id: str, composed: str | None = None, premiered: str | None = None,
                             premiered_place: str | None = None) -> dict[str, Any]:
        """A user correction of a work's composition or premiere date ('1804–05', '1860s', 'c. 1805',
        '1805-11-20'); '' returns to the automatic sources, 'unknown' hides them. Survives re-index. Only a
        work the caller can see."""
        user = self._user_name()
        vis = self._vis()  # on the loop: the current user is a context variable

        def apply(ix: Index) -> dict:
            ix.con.execute("BEGIN IMMEDIATE")
            try:
                wid = views.work(ix, work_id, vis)["work"]["id"]      # NotFound when hidden
                work_dates.set_user_dates(ix.con, wid, user, composed, premiered, premiered_place)
                ix.con.execute("COMMIT")
            except BaseException:
                ix.con.execute("ROLLBACK")
                raise
            return {"work_id": wid, "dates": work_dates.describe(ix.work(wid) or {})}

        try:
            return await self._write(apply, self.index)
        except views.NotFound:
            raise InvalidDataError(NOT_FOUND) from None
        except ValueError as err:
            raise InvalidDataError(str(err)) from err

    async def cmd_work_mb_link(self, work_id: str, decision: str, mbid: str | None = None,
                               mbids: list[str] | None = None) -> dict[str, Any]:
        """A user decision on a proposed MusicBrainz work link: 'accept' one `mbid`, or 'reject' `mbid` /
        `mbids` (a rejected candidate is never proposed again). Survives re-index; undoable from the review
        page. Only a work the caller can see, and only an id the linking job linked or proposed for it."""
        args: dict[str, Any] = {"op": "work_mb_link", "work_id": work_id, "decision": decision}
        if mbid:
            args["mbid"] = mbid
        if mbids:
            args["mbids"] = list(mbids)
        out = await self._decide([args])
        return {**out["results"][0], "batch": out["batch"]}

    async def cmd_join_discs(self, first_recording_id: str, second_recording_id: str,
                             decision: str = "together") -> dict[str, Any]:
        """A user correction on two discs of a set ripped as separate albums: the second recording continues
        the first ('together') or is another recording ('separate'). Survives re-index; undoable from the
        review page."""
        out = await self._decide([{"op": "join_discs", "first_recording_id": first_recording_id,
                                   "second_recording_id": second_recording_id, "decision": decision}])
        return {"recordings": out["results"][0]["recordings"], "batch": out["batch"], "queued": list(self._jobs)}

    async def cmd_separate_discs(self, recording_id: str) -> dict[str, Any]:
        """A user correction: the discs this recording spans are separate recordings. Undoable from the review
        page (Recent decisions)."""
        out = await self._decide([{"op": "separate_discs", "recording_id": recording_id}])
        return {"recordings": out["results"][0]["recordings"], "batch": out["batch"], "queued": list(self._jobs)}

    async def cmd_box_decide(self, box_id: str, decision: str = "not_box") -> dict[str, Any]:
        """A user correction: these albums are not a box set (their discs show as ordinary albums again).
        Survives re-index; undoable from the review page (Recent decisions)."""
        out = await self._decide([{"op": "box_decide", "box_id": box_id, "decision": decision}])
        return {**out["results"][0], "batch": out["batch"], "queued": list(self._jobs)}

def job_order(key: str) -> tuple[bool, bool, bool]:
    """The worker's order: removals and provider checks first, then single albums, then scans, then accolades,
    work dates and work links (the dataset refreshes and the network lookups wait until the library is
    indexed)."""
    return (not key.startswith(("providers", "remove:")), key.startswith(("accolades", "dates", "worklink", "art")),
            key.startswith("scan"))


def external_ids(item: Any, kind: str) -> list[str]:
    """Every value of one external id type ('isrc', 'barcode') on an MA media item, sorted."""
    ext = getattr(item, "external_ids", None)
    out: set[str] = set()
    if isinstance(ext, (set, frozenset, list, tuple)):
        for x in ext:
            if isinstance(x, (tuple, list)) and len(x) == 2 and x[1] and str(getattr(x[0], "value", x[0])) == kind:
                out.add(str(x[1]).strip())
    return sorted(out - {""})


def album_ids_map(album: Any) -> dict[str, str]:
    """An MA item's external ids as {type value: id} (empty ids left out)."""
    ids: dict[str, str] = {}
    ext = getattr(album, "external_ids", None)
    if isinstance(ext, (set, frozenset, list, tuple)):
        for item in ext:
            if isinstance(item, (tuple, list)) and len(item) == 2 and item[1]:
                ids[str(getattr(item[0], "value", item[0]))] = str(item[1])
    return ids


def album_ids(album: Any) -> tuple[str | None, ...]:
    """(label, barcode, MB release, MB release group, Discogs release) from an MA album; any may be None."""
    ids = album_ids_map(album)
    label = getattr(getattr(album, "metadata", None), "label", None)
    return (label if isinstance(label, str) and label.strip() else None, ids.get("barcode"),
            ids.get("musicbrainz_albumid"), ids.get("musicbrainz_releasegroupid"), ids.get("discogs"))


def mb_contact(value: Any) -> str | None:
    """The configured MusicBrainz contact if it looks like one (an email address or a URL), else None."""
    v = str(value or "").strip()
    if "@" in v.strip("@") or "://" in v or v.startswith("www."):
        return v
    return None
