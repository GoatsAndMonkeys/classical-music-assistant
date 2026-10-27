"""Glue tests that run without Music Assistant installed: MA's modules are stubbed."""

import asyncio
import importlib
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

from classical_core.index import Index
from classical_core.store import connect, migrate

MA_MODULES = [
    "music_assistant", "music_assistant.controllers", "music_assistant.controllers.webserver",
    "music_assistant.controllers.webserver.helpers", "music_assistant.controllers.webserver.helpers.auth_middleware",
    "music_assistant.models", "music_assistant.models.plugin", "music_assistant_models",
    "music_assistant_models.auth", "music_assistant_models.config_entries", "music_assistant_models.enums",
    "music_assistant_models.errors", "music_assistant_models.media_items",
]


class MediaNotFoundError(Exception):
    pass


@pytest.fixture
def glue(monkeypatch):
    for name in MA_MODULES:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: MagicMock(name=attr)
        monkeypatch.setitem(sys.modules, name, mod)
    sys.modules["music_assistant.models.plugin"].PluginProvider = type("PluginProvider", (), {})
    sys.modules["music_assistant_models.errors"].MediaNotFoundError = MediaNotFoundError
    sys.modules["music_assistant_models.errors"].InvalidDataError = type("InvalidDataError", (Exception,), {})
    for name in ("provider.classical.glue", "provider.classical", "provider"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield importlib.import_module("provider.classical.glue")
    for name in ("provider.classical.glue", "provider.classical", "provider"):
        sys.modules.pop(name, None)


def test_one_failing_album_does_not_block_the_scan(glue, tmp_path):
    con = connect(tmp_path / "index.db")
    migrate(con)
    p = object.__new__(glue.ClassicalProvider)
    p._con, p.index, p.logger = con, Index(con, None), logging.getLogger("test")
    p._state = {"errors": 0, "last_scan": None, "albums_indexed": 0}

    async def rows(query, params, limit):
        if "IN (" in query:
            return []
        return [{"item_id": i} for i in (1, 2, 3) if i > params["c"]]

    p.mass = MagicMock()
    p.mass.music.database.get_rows_from_query = rows
    seen = []

    async def index_album_id(aid, force=False):
        seen.append(aid)
        if aid == "2":
            raise KeyError("broken album")
        return True

    p._index_album_id = index_album_id
    asyncio.run(p._scan())
    assert seen == ["1", "2", "3"] and p._state["errors"] == 1
    assert con.execute("SELECT state FROM job WHERE id='scan'").fetchone()[0] == "done"


def _provider(glue, tmp_path, ids=(1, 2, 3)):
    con = connect(tmp_path / "index.db")
    migrate(con)
    p = object.__new__(glue.ClassicalProvider)
    p._con, p.index, p.logger = con, Index(con, None), logging.getLogger("test")
    p._state = {"errors": 0, "last_scan": None, "albums_indexed": 0}

    async def rows(query, params, limit):
        if "IN (" in query:
            return []
        return [{"item_id": i} for i in ids if i > params["c"]]

    p.mass = MagicMock()
    p.mass.music.database.get_rows_from_query = rows
    seen = []

    async def index_album_id(aid, force=False):
        seen.append((aid, force))
        return True

    p._index_album_id = index_album_id
    return p, con, seen


def test_forced_reindex_starts_fresh_not_from_an_interrupted_cursor(glue, tmp_path):
    p, con, seen = _provider(glue, tmp_path)
    con.execute("INSERT INTO job(id, kind, scope, cursor, state, started_at) VALUES "
                "('scan', 'scan', 'library', '2', 'running', 0)")
    asyncio.run(p._scan(force=True))
    assert seen == [("1", True), ("2", True), ("3", True)]


def test_interrupted_forced_scan_resumes_forced(glue, tmp_path):
    p, con, seen = _provider(glue, tmp_path)
    con.execute("INSERT INTO job(id, kind, scope, cursor, state, started_at) VALUES "
                "('scan', 'scan_force', 'library', '1', 'running', 0)")
    asyncio.run(p._scan())
    assert seen == [("2", True), ("3", True)]
    seen.clear()
    asyncio.run(p._scan())                         # finished: the next normal scan is normal and complete
    assert seen == [("1", False), ("2", False), ("3", False)]


def test_forced_scan_supersedes_queued_scan(glue):
    p = object.__new__(glue.ClassicalProvider)
    p._jobs, p._wake = {}, asyncio.Event()
    p._enqueue("scan")
    p._enqueue("scan:force")
    p._enqueue("scan")
    assert list(p._jobs) == ["scan:force"]


def test_mb_contact_must_be_real(glue):
    assert glue.mb_contact("") is None and glue.mb_contact("  ") is None
    assert glue.mb_contact("no contact configured") is None
    assert glue.mb_contact("me@example.org") == "me@example.org"
    assert glue.mb_contact("https://example.org/me") == "https://example.org/me"


# ---- live indexing while MA syncs (MA suppresses per-item events during a provider sync) ----


def _queue(glue):
    p = object.__new__(glue.ClassicalProvider)
    p._jobs, p._wake = {}, asyncio.Event()
    return p


def test_changes_job_queues_new_and_growing_albums_only(glue, tmp_path):
    from classical_core.index import AlbumIn, MappingIn, TrackIn

    p, _, _ = _provider(glue, tmp_path)
    p._jobs, p._wake = {}, asyncio.Event()
    asyncio.run(p.index.index_album(AlbumIn("1", "Done", "", None, None, [
        TrackIn(f"t{i}", 1, i + 1, f"T{i}", mappings=[MappingIn("filesystem_local", f"1/{i}", "f", None)])
        for i in range(2)])))
    asyncio.run(p.index.index_album(AlbumIn("2", "Filling", "", None, None, [
        TrackIn("u0", 1, 1, "U0", mappings=[MappingIn("filesystem_local", "2/0", "f", None)])])))

    async def rows(query, params, limit):
        assert "album_tracks" in query and limit == 0
        return [{"album_id": 1, "n": 2}, {"album_id": 2, "n": 3}, {"album_id": 3, "n": 1}]

    p.mass.music.database.get_rows_from_query = rows
    asyncio.run(p._changes())
    assert sorted(p._jobs) == ["album:2", "album:3"]


def test_sync_watch_queues_changes_only_while_a_sync_runs(glue, monkeypatch):
    p = _queue(glue)
    p.mass = MagicMock()
    states = iter([[], ["task"], []])
    ticks = []

    async def fake_sleep(_):
        ticks.append(dict(p._jobs))
        try:
            p.mass.music.active_sync_tasks = next(states)
        except StopIteration:
            raise asyncio.CancelledError from None

    monkeypatch.setattr(glue.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(p._sync_watch())
    # tick 1: no sync, nothing queued; tick 2: sync active, changes queued
    assert ticks[1] == {} and ticks[2] == {"changes": None}


def test_media_item_events_by_library_uri(glue):
    p = _queue(glue)
    ev = MagicMock()
    ev.event, ev.data, ev.object_id = "added", None, "library://album/12"
    p._on_media_item(ev)
    ev.object_id = "library://track/7"
    ev.data = types.SimpleNamespace(provider="library", album=types.SimpleNamespace(provider="library", item_id=12))
    p._on_media_item(ev)
    ev.event, ev.object_id, ev.data = glue.EventType.MEDIA_ITEM_DELETED, "library://album/5", None
    p._on_media_item(ev)
    ev.object_id = "filesystem_local://track/abc"
    p._on_media_item(ev)
    assert sorted(p._jobs) == ["album:12", "remove:5", "track:7"]


def test_media_item_event_from_item_without_uri(glue):
    p = _queue(glue)
    ev = MagicMock()
    ev.event, ev.object_id = "updated", None
    ev.data = types.SimpleNamespace(provider="library", media_type=types.SimpleNamespace(value="album"), item_id=9)
    p._on_media_item(ev)
    assert list(p._jobs) == ["album:9"]


def test_merge_command_reads_nothing_on_the_loop_and_rejects_unknown_ids(glue, tmp_path):
    p, _con, _ = _provider(glue, tmp_path)
    p.index.resolve = MagicMock(side_effect=AssertionError("SQLite touched on the event loop"))
    calls = []

    async def write(fn, *args):
        calls.append(fn.__name__)
        return "c-into" if fn.__name__ == "merge_composers" else None

    p._write = write
    p._user_name = staticmethod(lambda: "seth")
    assert asyncio.run(p.cmd_merge_composers("c-src", "c-dst")) == {"merged": "c-src", "into": "c-into"}
    assert calls == ["merge_composers"]

    async def refuse(fn, *args):
        raise ValueError("no composer c-dst")

    p._write = refuse
    with pytest.raises(glue.InvalidDataError):
        asyncio.run(p.cmd_merge_composers("c-src", "c-dst"))


def test_separate_work_command_rescans_and_rejects_unmerged_recordings(glue, tmp_path):
    p, _con, _ = _provider(glue, tmp_path)
    p._user_name = staticmethod(lambda: "seth")

    async def write(fn, *args):
        return ["album-1"]

    p._write = write
    p._enqueue = MagicMock()
    p._jobs = {}
    assert asyncio.run(p.cmd_separate_work("r-1"))["albums"] == 1
    p._enqueue.assert_called_once_with("scan")

    async def refuse(fn, *args):
        raise ValueError("this recording was not merged from another album's work")

    p._write = refuse
    with pytest.raises(glue.InvalidDataError):
        asyncio.run(p.cmd_separate_work("r-1"))
