"""MA glue for the Classical plugin (PLAN.md §5.6, §6): worker, events, browse, commands, home rows.

Everything classical lives in `classical_core` (no MA imports); this module only adapts MA's library and
models to it. One worker task, created in a fresh context so it never runs as the user whose request
loaded the plugin, owns the SQLite writer connection; event callbacks only enqueue ids.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, MediaType, ProviderFeature
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
from classical_core import browse as tree
from classical_core.index import AlbumIn, Index, MappingIn, TrackIn
from classical_core.mb import MBClient
from classical_core.store import SqliteMBCache, connect, migrate

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

SUPPORTED_FEATURES: set[ProviderFeature] = {ProviderFeature.BROWSE, ProviderFeature.RECOMMENDATIONS}

CONF_MUSICBRAINZ = "musicbrainz"
CONF_MB_CONTACT = "mb_contact"
CONF_PROVIDERS = "providers"          # MA's raw provider-config key (controllers/config)
PAGE = 500
DEBOUNCE = 2.0                         # seconds to let a burst of library events settle
RECONCILE_RETRY = 600                  # §5.6: removal_pending is rechecked every 10 min
ALBUM_TRACKS_QUERY = "tracks.item_id IN (SELECT track_id FROM album_tracks WHERE album_id = :album_id)"


class ClassicalProvider(PluginProvider):
    """Classical index and browse tree."""

    index: Index
    _con: Any
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
                key=CONF_MUSICBRAINZ,
                type=ConfigEntryType.BOOLEAN,
                label="Look up works and recordings on MusicBrainz",
                default_value=True,
                required=False,
            ),
            ConfigEntry(
                key=CONF_MB_CONTACT,
                type=ConfigEntryType.STRING,
                label="Contact for the MusicBrainz User-Agent (email or URL)",
                default_value="",
                required=False,
                depends_on=CONF_MUSICBRAINZ,
            ),
        )

    async def handle_async_init(self) -> None:
        """Open the index (schema migrations run here)."""
        self._jobs = {}
        self._wake = asyncio.Event()
        self._worker_task = None
        self._unsub = []
        self._retry_handle = None
        self._state = {"current": None, "last_scan": None, "albums_indexed": 0, "errors": 0}
        db_path = os.path.join(self.mass.storage_path, "classical", "index.db")
        self._con = await asyncio.to_thread(connect, db_path)
        await asyncio.to_thread(migrate, self._con)
        self._mb = None
        if self.config.get_value(CONF_MUSICBRAINZ, True):
            contact = str(self.config.get_value(CONF_MB_CONTACT, "") or "") or "no contact configured"
            self._mb = MBClient(contact, cache=SqliteMBCache(self._con))
        self.index = Index(self._con, self._mb)

    async def loaded_in_mass(self) -> None:
        """Register commands and subscriptions, start the worker and queue the initial scan."""
        reg = self.mass.register_api_command
        self._unsub += [
            reg("classical/status", self.cmd_status, required_scope=Scope.LIBRARY_MANAGE),
            reg("classical/search", self.cmd_search, required_scope=Scope.LIBRARY_READ),
            reg("classical/composer", self.cmd_composer, required_scope=Scope.LIBRARY_READ),
            reg("classical/reindex", self.cmd_reindex, required_scope=Scope.LIBRARY_MANAGE),
            self.mass.subscribe(self._on_sync_completed, EventType.MUSIC_SYNC_COMPLETED),
            self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED),
            self.mass.subscribe(
                self._on_media_item,
                (EventType.MEDIA_ITEM_ADDED, EventType.MEDIA_ITEM_UPDATED, EventType.MEDIA_ITEM_DELETED),
            ),
        ]
        # a fresh context: no current user or impersonation inherited from the loading request (§5.6)
        self._worker_task = asyncio.get_running_loop().create_task(
            self._worker(), name="classical-worker", context=contextvars.Context()
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
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        if self._mb:
            await self._mb.close()
        await asyncio.to_thread(self._con.close)
        # is_removed: the index database is kept (rollback leaves it untouched, §7)

    # ---- job queue ---------------------------------------------------------------------------------------

    def _enqueue(self, key: str) -> None:
        self._jobs[key] = None
        self._wake.set()

    async def _worker(self) -> None:
        while True:
            await self._wake.wait()
            await asyncio.sleep(DEBOUNCE)
            self._wake.clear()
            while self._jobs:
                # removals and provider checks first, then single albums, then scans
                key = min(self._jobs, key=lambda k: (not k.startswith(("providers", "remove:")),
                                                     k.startswith("scan")))
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
        elif kind == "remove":
            self.index.remove_album(arg)
        elif kind == "providers":
            await self._reconcile_providers()

    # ---- library → index ---------------------------------------------------------------------------------

    def _job_cursor(self, job_id: str) -> str | None:
        r = self._con.execute("SELECT cursor, state FROM job WHERE id=?", (job_id,)).fetchone()
        return r["cursor"] if r and r["state"] == "running" else None

    def _set_job(self, job_id: str, state: str, cursor: str | None = None, error: str | None = None) -> None:
        self._con.execute(
            "INSERT INTO job(id, kind, scope, cursor, state, started_at, error) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor, state=excluded.state, error=excluded.error",
            (job_id, "scan", "library", cursor, state, time.time(), error),
        )

    async def _scan(self, force: bool = False) -> None:
        """Reconcile the whole library: index every album (resuming from job.cursor), drop vanished ones."""
        cursor = self._job_cursor("scan")
        start = int(cursor) if cursor and cursor.isdigit() else 0
        if start:
            self.logger.info("Resuming classical scan after album %s", start)
        self._set_job("scan", "running", str(start))
        n = 0
        while True:
            rows = await self.mass.music.database.get_rows_from_query(
                "SELECT item_id FROM albums WHERE item_id > :c ORDER BY item_id", {"c": start}, limit=PAGE
            )
            if not rows:
                break
            for row in rows:
                start = int(row["item_id"])
                if await self._index_album_id(str(start), force=force):
                    n += 1
                self._set_job("scan", "running", str(start))
        # albums the index knows but MA no longer has
        present = set()
        known = self.index.album_ids()
        for i in range(0, len(known), PAGE):
            chunk = [int(a) for a in known[i : i + PAGE] if a.isdigit()]
            if chunk:
                q = f"SELECT item_id FROM albums WHERE item_id IN ({','.join(str(c) for c in chunk)})"
                present |= {str(r["item_id"]) for r in await self.mass.music.database.get_rows_from_query(q, {}, limit=PAGE)}
        for aid in known:
            if aid not in present:
                self.index.remove_album(aid)
        self._set_job("scan", "done")
        self._state["last_scan"] = time.time()
        self.logger.info("Classical scan done: %d albums (re)indexed; %s", n, self.index.status())

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
        )

    async def _index_album_id(self, album_id: str, force: bool = False) -> bool:
        try:
            album = await self.mass.music.albums.get_library_item(int(album_id))
        except (MediaNotFoundError, ValueError):
            self.index.remove_album(album_id)
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
                )
                for t in tracks
            ],
        )
        stats = await self.index.index_album(ain, force=force)
        if stats is not None:
            self._state["albums_indexed"] += 1
            self.logger.debug("Indexed album %s (%s): %s", album_id, album.name, stats)
        return stats is not None

    # ---- provider presence (§5.6) ------------------------------------------------------------------------

    async def _reconcile_providers(self) -> None:
        configured = set((self.mass.config.get(CONF_PROVIDERS) or {}).keys())
        loaded = {p.instance_id for p in self.mass.providers}
        pending = False
        for pi in self.index.provider_instances():
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
                    if self.index.provider_state(pi) != "removed":
                        for aid in self.index.retract_provider(pi):
                            self._enqueue(f"album:{aid}")
                        self.logger.info("Provider %s removed: its evidence was retracted", pi)
                    continue
            if self.index.set_provider_state(pi, state):
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
        item = event.data
        if item is None or getattr(item, "provider", None) != "library":
            return
        mt = getattr(item, "media_type", None)
        if mt == MediaType.ALBUM:
            kind = "remove" if event.event == EventType.MEDIA_ITEM_DELETED else "album"
            self._enqueue(f"{kind}:{item.item_id}")
        elif mt == MediaType.TRACK:
            albums = set(self.index.albums_of_track(str(item.item_id)))
            album = getattr(item, "album", None)
            if album is not None and getattr(album, "provider", None) == "library":
                albums.add(str(album.item_id))
            for aid in albums:
                self._enqueue(f"album:{aid}")

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
        try:
            nodes = tree.browse(self.index, sub, self._vis())
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

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Home rows: recently added works, composers in your library."""
        return [
            RecommendationFolder(item_id="recent_works", provider=self.instance_id, name="Recently added works",
                                 translation_key="classical_recent_works"),
            RecommendationFolder(item_id="composers", provider=self.instance_id, name="Composers in your library",
                                 translation_key="classical_composers"),
        ]

    async def get_recommendation_items(self, item_id: str) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Items for one home row."""
        vis = self._vis()
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList()
        if item_id == "recent_works":
            for w in self.index.recently_added_works(vis, limit=20):
                best = self.index.best_recording(w["id"], vis)
                if best is None:
                    continue
                r, _ = best
                items.append(self._folder(tree.Node(
                    "folder", f"works/{w['id']}/{r['id']}", w["title"], version=f"{w['composer'] or ''} · {r['label']}",
                    item_id=tree.recording_item_id(w["id"], r["id"]), playable=True, image=r.get("image"))))
        elif item_id == "composers":
            comps = sorted(self.index.composers(vis), key=lambda c: -c["works"])[:20]
            for c in comps:
                items.append(self._folder(tree.Node("folder", f"composers/{c['id']}", c["name"],
                                                    version=f"{c['works']} works")))
        return items

    # ---- API commands (M1a) ------------------------------------------------------------------------------

    def _work_result(self, w: dict, vis: list[str] | None) -> dict[str, Any]:
        res: dict[str, Any] = {
            "id": w["id"], "kind": "work", "title": w["title"], "composer": w.get("composer"),
            "work_type": w.get("work_type"), "catalogue": w.get("catalogue_display"),
            "recordings": w.get("recordings"), "browse_path": f"{tree.SCHEME}works/{w['id']}",
        }
        best = self.index.best_recording(w["id"], vis)
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
        hits = self.index.search(query, kind, vis, limit=max(1, min(int(limit), 200)))
        if kind == "composer":
            return [{"id": c["id"], "kind": "composer", "name": c["name"], "works": c["works"],
                     "browse_path": f"{tree.SCHEME}composers/{c['id']}"} for c in hits]
        if kind == "work":
            return [self._work_result(w, vis) for w in hits]
        return [{"id": r["id"], "kind": "recording", "label": r["label"], "work_id": r["work_id"],
                 "work_title": r["work_title"], "composer": r["composer"], "album": r.get("album"),
                 "browse_path": f"{tree.SCHEME}works/{r['work_id']}/{r['id']}",
                 "play_uri": tree.play_uri(r["work_id"], r["id"])} for r in hits]

    async def cmd_composer(self, id: str | None = None, name: str | None = None) -> dict[str, Any]:
        """One composer (by id or name) with their works by type."""
        vis = self._vis()
        comp = None
        if id:
            comp = next((c for c in self.index.composers(vis) if c["id"] == self.index.resolve("composer", id)), None)
        elif name:
            hits = self.index.search(name, "composer", vis, limit=1)
            comp = hits[0] if hits else None
        if comp is None:
            raise MediaNotFoundError("Composer not found")
        works = self.index.works(comp["id"], vis=vis)
        return {
            "id": comp["id"], "kind": "composer", "name": comp["name"], "born": comp.get("born"),
            "died": comp.get("died"), "browse_path": f"{tree.SCHEME}composers/{comp['id']}",
            "types": [{"type": t, "works": n} for t, n in self.index.composer_types(comp["id"], vis)],
            "works": [self._work_result(w, vis) for w in works[:200]],
        }

    async def cmd_status(self) -> dict[str, Any]:
        """Index counts and worker state (household-wide, so LIBRARY_MANAGE)."""
        return {
            "core_version": CORE_VERSION, "ma_version": self.mass.version, **self.index.status(),
            "worker": {**self._state, "queued": list(self._jobs),
                       "running": bool(self._worker_task and not self._worker_task.done())},
            "musicbrainz": self._mb is not None,
        }

    async def cmd_reindex(self, force: bool = False) -> dict[str, Any]:
        """Queue a full library scan (force re-reads every file and MB response)."""
        self._enqueue("scan:force" if force else "scan")
        return {"queued": list(self._jobs)}
