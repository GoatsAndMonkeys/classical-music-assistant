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
    p._jobs, p._wake = {}, asyncio.Event()

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
    p._jobs, p._wake = {}, asyncio.Event()

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


def test_separate_work_refuses_a_recording_the_caller_cannot_see(glue, tmp_path, monkeypatch):
    """A hidden recording answers the same 'Not found' as an unknown one, and nothing is changed."""
    p, _con, _ = _provider(glue, tmp_path)
    p._user_name = staticmethod(lambda: "seth")
    p._vis = lambda: ["filesystem--other"]
    ix = p.index = MagicMock()

    async def write(fn, *args):
        return fn(*args)

    def hidden(_ix, rid, vis):
        assert vis == ["filesystem--other"]
        raise glue.views.NotFound(rid)

    monkeypatch.setattr(glue.views, "recording", hidden)
    p._write = write
    p._enqueue = MagicMock()
    with pytest.raises(glue.InvalidDataError, match="Not found"):
        asyncio.run(p.cmd_separate_work("r-1"))
    ix.separate_work.assert_not_called()
    p._enqueue.assert_not_called()


def test_disc_commands_check_both_recordings_and_queue_accolades(glue, tmp_path, monkeypatch):
    """Joining discs checks the caller can see both recordings, runs the correction and requeues the
    dataset matches; a hidden one answers 'Not found' with nothing changed, a refusal is invalid data."""
    p, _con, _ = _provider(glue, tmp_path)
    p._user_name = staticmethod(lambda: "seth")
    p._vis = lambda: ["filesystem--x"]
    ix = p.index = MagicMock()
    ix.join_discs.return_value = ["r-1"]
    ix.separate_discs.return_value = ["r-1", "r-2"]
    seen = []

    async def write(fn, *args):
        return fn(*args)

    monkeypatch.setattr(glue.views, "recording", lambda _ix, rid, vis: seen.append(rid) or {"id": rid})
    p._write = write
    p._enqueue = MagicMock()
    p._jobs = {}
    assert asyncio.run(p.cmd_join_discs("r-1", "r-2"))["recordings"] == ["r-1"]
    assert seen == ["r-1", "r-2"]
    ix.join_discs.assert_called_once_with("r-1", "r-2", "seth", "together")
    p._enqueue.assert_called_once_with("accolades")
    assert asyncio.run(p.cmd_separate_discs("r-1"))["recordings"] == ["r-1", "r-2"]
    ix.separate_discs.assert_called_once_with("r-1", "seth")

    def hidden(_ix, rid, vis):
        if rid == "r-2":
            raise glue.views.NotFound(rid)
        return {"id": rid}

    monkeypatch.setattr(glue.views, "recording", hidden)
    ix.join_discs.reset_mock()
    p._enqueue.reset_mock()
    with pytest.raises(glue.InvalidDataError, match="Not found"):
        asyncio.run(p.cmd_join_discs("r-1", "r-2", "separate"))
    ix.join_discs.assert_not_called()
    p._enqueue.assert_not_called()

    monkeypatch.setattr(glue.views, "recording", lambda _ix, rid, vis: {"id": rid})
    ix.join_discs.side_effect = ValueError("these recordings are not of the same work")
    with pytest.raises(glue.InvalidDataError, match="same work"):
        asyncio.run(p.cmd_join_discs("r-1", "r-3"))


def test_write_commands_need_library_manage(glue, tmp_path):
    p, _con, _ = _provider(glue, tmp_path)
    p.mass, p._unsub = MagicMock(), []
    p._register_ui = MagicMock(return_value=[])
    p._enqueue = MagicMock()

    async def idle():
        return None

    p._worker = idle
    asyncio.run(p.loaded_in_mass())
    scopes = {c.args[0]: c.kwargs["required_scope"] for c in p.mass.register_api_command.call_args_list}
    for name in ("classical/reindex", "classical/merge_composers", "classical/composer_name",
                 "classical/separate_work", "classical/join_discs", "classical/separate_discs",
                 "classical/work_dates", "classical/work_mb_link"):
        assert scopes[name] == glue.Scope.LIBRARY_MANAGE, name


# ---- accolades (M3b) -----------------------------------------------------------------------------------------


def test_job_order_puts_accolades_after_albums_and_scans(glue):
    keys = ["accolades_net", "scan", "accolades", "album:7", "remove:3", "providers", "changes"]
    assert sorted(keys, key=glue.job_order)[:2] == ["remove:3", "providers"]
    order = sorted(keys, key=glue.job_order)
    assert order.index("album:7") < order.index("scan") < order.index("accolades")
    assert order.index("scan") < order.index("accolades_net")


def test_job_order_puts_the_dates_jobs_after_albums_and_scans(glue):
    order = sorted(["dates_net", "dates", "album:7", "scan", "changes"], key=glue.job_order)
    assert order.index("album:7") < order.index("scan") < order.index("dates")
    assert order.index("scan") < order.index("dates_net")


def test_job_order_puts_the_work_link_jobs_after_albums_scans_and_changes(glue):
    order = sorted(["worklink_net", "worklink", "album:7", "scan", "changes"], key=glue.job_order)
    assert order.index("album:7") < order.index("scan") < order.index("worklink")
    assert order.index("changes") < order.index("worklink_net")


def test_work_link_jobs_chain_and_stay_offline_without_musicbrainz(glue, tmp_path, monkeypatch):
    """'dates' queues 'worklink'; requests due queue 'worklink_net' only with MusicBrainz on; a new link
    queues 'dates'; a transient failure stops the chunk."""
    p, _con, _ = _provider(glue, tmp_path)
    p._mb = None

    async def write(fn, *args):
        return fn(*args)

    p._write = write
    monkeypatch.setattr(glue.work_dates, "refresh", lambda *a: {})
    monkeypatch.setattr(glue.work_link, "refresh", lambda *a: ({"linked": 1}, ["https://mb/work?artist=x"]))
    asyncio.run(p._run("dates"))
    assert "worklink" in p._jobs and "dates_net" not in p._jobs
    p._jobs.clear()
    asyncio.run(p._run("worklink"))
    assert set(p._jobs) == {"dates"}                    # MusicBrainz off: nothing to fetch
    asyncio.run(p._run("worklink_net"))
    assert set(p._jobs) == {"dates"}
    p._jobs.clear()
    p._mb = types.SimpleNamespace(base_url="https://mb")
    got = []

    async def fetch(mb, urls):
        got.append(urls)
        return 0, "503"

    monkeypatch.setattr(glue.work_link, "fetch", fetch)
    asyncio.run(p._run("worklink"))
    assert set(p._jobs) == {"dates", "worklink_net"}
    p._jobs.clear()
    asyncio.run(p._run("worklink_net"))
    assert got == [["https://mb/work?artist=x"]] and not p._jobs     # deferred: nothing re-queued


def test_work_mb_link_command_checks_visibility(glue, tmp_path, monkeypatch):
    p, _con, _ = _provider(glue, tmp_path)
    p._user_name = staticmethod(lambda: "seth")
    p._vis = lambda: ["filesystem--x"]
    p.index = MagicMock()
    calls = []

    async def write(fn, *args):
        return fn(*args)

    p._write = write
    monkeypatch.setattr(glue.work_link, "set_user_link", lambda con, wid, user, d, ids: calls.append((wid, user, d, ids)))
    monkeypatch.setattr(glue.views, "work", lambda _ix, wid, vis: {"work": {"id": wid + "-live"}})
    asyncio.run(p.cmd_work_mb_link("w-1", "reject", mbids=["a", "b"]))
    assert calls == [("w-1-live", "seth", "reject", ["a", "b"])] and "dates" in p._jobs

    def hidden(_ix, wid, vis):
        raise glue.views.NotFound(wid)

    monkeypatch.setattr(glue.views, "work", hidden)
    with pytest.raises(glue.InvalidDataError, match="Not found"):
        asyncio.run(p.cmd_work_mb_link("w-2", "accept", mbid="a"))
    assert len(calls) == 1


def test_work_dates_command_checks_visibility_and_reports_bad_dates(glue, tmp_path, monkeypatch):
    """A hidden work answers 'Not found' with nothing written; an unreadable date is invalid data."""
    p, _con, _ = _provider(glue, tmp_path)
    p._user_name = staticmethod(lambda: "seth")
    p._vis = lambda: ["filesystem--x"]
    ix = p.index = MagicMock()
    ix.work.return_value = {"composed_start": "1805", "composed_source": "user"}
    calls = []

    async def write(fn, *args):
        return fn(*args)

    p._write = write
    monkeypatch.setattr(glue.work_dates, "set_user_dates", lambda con, wid, user, *a: calls.append((wid, user, a)))
    monkeypatch.setattr(glue.views, "work", lambda _ix, wid, vis: {"work": {"id": wid + "-live"}})
    out = asyncio.run(p.cmd_work_dates("w-1", composed="1805"))
    assert out["work_id"] == "w-1-live" and out["dates"]["text"] == "composed 1805"
    assert calls == [("w-1-live", "seth", ("1805", None, None))]

    def hidden(_ix, wid, vis):
        assert vis == ["filesystem--x"]
        raise glue.views.NotFound(wid)

    monkeypatch.setattr(glue.views, "work", hidden)
    with pytest.raises(glue.InvalidDataError, match="Not found"):
        asyncio.run(p.cmd_work_dates("w-2", composed="1805"))
    assert len(calls) == 1

    def bad(*_a):
        raise ValueError("cannot read composed date 'soon'")

    monkeypatch.setattr(glue.views, "work", lambda _ix, wid, vis: {"work": {"id": wid}})
    monkeypatch.setattr(glue.work_dates, "set_user_dates", bad)
    with pytest.raises(glue.InvalidDataError, match="cannot read"):
        asyncio.run(p.cmd_work_dates("w-1", composed="soon"))


def test_album_ids_reads_external_ids_and_label(glue):
    kind = types.SimpleNamespace
    album = kind(external_ids=[(kind(value="barcode"), "0028947"), (kind(value="musicbrainz_releasegroupid"), "rg-1"),
                               (kind(value="discogs"), "123"), ("musicbrainz_albumid", "")],
                 metadata=kind(label="  "))
    assert glue.album_ids(album) == (None, "0028947", None, "rg-1", "123")
    assert glue.album_ids(kind(external_ids=None, metadata=kind(label="DG"))) == ("DG", None, None, None, None)
    assert glue.album_ids(object()) == (None, None, None, None, None)


def test_enrich_stores_each_album_and_stops_on_a_transient_error(glue, tmp_path, monkeypatch):
    p, _con, _ = _provider(glue, tmp_path)
    p._mb, p._net = object(), {}
    stored, due = [], [{"album_library_id": str(i)} for i in range(glue.NET_CHUNK)]

    async def fetch(album, mb, **net):
        if album["album_library_id"] == "2":
            raise glue.MBTransientError("503")
        return album["album_library_id"]

    monkeypatch.setattr(glue.accolades, "album_for_enrichment", lambda con, limit: due[:limit])
    monkeypatch.setattr(glue.accolades, "store_net", lambda con, aid, res: stored.append((aid, res)))
    monkeypatch.setattr(glue.accolades, "fetch_album", fetch)
    asyncio.run(p._enrich())
    assert stored == [("0", "0"), ("1", "1")] and "accolades_net" not in p._jobs   # deferred to the next run

    due[2] = {"album_library_id": "x"}
    stored.clear()
    asyncio.run(p._enrich())
    assert len(stored) == glue.NET_CHUNK and "accolades_net" in p._jobs           # a full chunk: more to do

    p._mb = None
    monkeypatch.setattr(glue.accolades, "album_for_enrichment", MagicMock(side_effect=AssertionError))
    asyncio.run(p._enrich())                                                       # nothing enabled: no work


def test_accolade_decide_checks_visibility_and_requeues(glue, tmp_path, monkeypatch):
    p, _con, _ = _provider(glue, tmp_path)
    p._user_name = staticmethod(lambda: "seth")
    p._vis = lambda: ["filesystem_local"]
    p.index = MagicMock()
    decided = []

    async def write(fn, *args):
        return fn(*args)

    monkeypatch.setattr(glue.views, "recording", lambda ix, rid, vis: {"id": rid})
    monkeypatch.setattr(glue.accolades, "decide",
                        lambda con, e, r, d, u: decided.append((e, r, d, u)) or {"decision": d})
    p._write = write
    out = asyncio.run(p.cmd_accolade_decide("abc123", "r-1", "confirm"))
    assert out["decision"] == "confirm" and "accolades" in out["queued"]
    assert decided == [("abc123", "r-1", "confirm", "seth")]

    def hidden(_ix, rid, vis):
        raise glue.views.NotFound(rid)

    monkeypatch.setattr(glue.views, "recording", hidden)
    with pytest.raises(glue.InvalidDataError, match="Not found"):
        asyncio.run(p.cmd_accolade_decide("abc123", "r-2", "reject"))
    assert len(decided) == 1

    monkeypatch.setattr(glue.views, "recording", lambda ix, rid, vis: {"id": rid})
    monkeypatch.setattr(glue.accolades, "decide", MagicMock(side_effect=ValueError("unknown decision")))
    with pytest.raises(glue.InvalidDataError):
        asyncio.run(p.cmd_accolade_decide("abc123", "r-1", "maybe"))
