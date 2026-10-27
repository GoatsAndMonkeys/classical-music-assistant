"""Companion UI glue (provider/classical/ui.py, web.py) without Music Assistant: MA's modules are stubbed."""

import asyncio
import datetime as dt
import importlib
import sys
import time
import types
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from classical_core import views

from .test_glue_scan import MA_MODULES, MediaNotFoundError

PROVIDER_MODULES = ("provider.classical.glue", "provider.classical.ui", "provider.classical.web",
                    "provider.classical", "provider")
AUTH = "music_assistant.controllers.webserver.helpers.auth_middleware"


@pytest.fixture
def mods(monkeypatch):
    for name in MA_MODULES:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: MagicMock(name=attr)
        monkeypatch.setitem(sys.modules, name, mod)
    sys.modules["music_assistant.models.plugin"].PluginProvider = type("PluginProvider", (), {})
    sys.modules["music_assistant_models.errors"].MediaNotFoundError = MediaNotFoundError
    sys.modules["music_assistant_models.errors"].InvalidDataError = type("InvalidDataError", (Exception,), {})
    for name in PROVIDER_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    glue = importlib.import_module("provider.classical.glue")
    yield types.SimpleNamespace(glue=glue, ui=sys.modules["provider.classical.ui"],
                                web=sys.modules["provider.classical.web"])
    for name in PROVIDER_MODULES:
        sys.modules.pop(name, None)


def get(web, path, method="GET"):
    handler = web.ClassicalWeb("9.9.9")
    return asyncio.run(handler.handle(make_mocked_request(method, path)))


def test_bare_prefix_redirects_relatively(mods):
    r = get(mods.web, "/classical")
    assert r.status == 302 and r.headers["Location"] == "classical/"


def test_index_has_version_and_strict_headers(mods):
    for path in ("/classical/", "/classical/index.html"):
        r = get(mods.web, path)
        assert r.status == 200 and r.content_type == "text/html"
        assert b"app.js?v=9.9.9" in r.body and b"{{VERSION}}" not in r.body
        assert "script-src 'self'" in r.headers["Content-Security-Policy"]
        assert "unsafe" not in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_static_files_and_types(mods):
    assert get(mods.web, "/classical/app.js").content_type == "text/javascript"
    assert get(mods.web, "/classical/app.css").content_type == "text/css"
    assert get(mods.web, "/classical/icon.svg").content_type == "image/svg+xml"
    assert get(mods.web, "/classical/manifest.webmanifest").content_type == "application/manifest+json"


@pytest.mark.parametrize("path", ["/classical/../glue.py", "/classical/%2e%2e/glue.py", "/classical/web.py",
                                  "/classicalx", "/classical/web/app.js", "/classical/app.js/", "/classical/nope"])
def test_anything_else_is_not_found(mods, path):
    assert get(mods.web, path).status == 404


def test_only_get_and_head(mods):
    assert get(mods.web, "/classical/", "POST").status == 405
    head = get(mods.web, "/classical/app.js", "HEAD")
    assert head.status == 200 and head.body == b""


def test_register_ui_registers_commands_and_replaces_a_stale_route(mods):
    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass, p.logger = MagicMock(), MagicMock()
    p.mass.webserver.base_url = "http://ma.local:8095"
    unreg = MagicMock()
    p.mass.webserver.register_dynamic_route.side_effect = [RuntimeError("exists"), unreg]
    handles = p._register_ui()
    names = [c.args[0] for c in p.mass.register_api_command.call_args_list]
    assert names == ["classical/ui_info", "classical/composers", "classical/works", "classical/work",
                     "classical/recording", "classical/performers", "classical/performer", "classical/review",
                     "classical/review_triage",
                     "classical/accolades", "classical/today", "classical/streaming", "classical/add_album"]
    scopes = {c.args[0]: c.kwargs["required_scope"] for c in p.mass.register_api_command.call_args_list}
    assert scopes["classical/add_album"] is mods.ui.Scope.LIBRARY_WRITE
    assert scopes["classical/streaming"] is mods.ui.Scope.LIBRARY_READ
    p.mass.webserver.unregister_dynamic_route.assert_called_once_with("/classical/*", "*")
    assert p.mass.webserver.register_dynamic_route.call_count == 2 and handles[-1] is unreg
    assert len(handles) == 14
    assert p._ui_url() == "http://ma.local:8095/classical/"


def test_image_urls(mods):
    res = mods.web.image_urls({"items": [{"image": {"provider": "fs", "path": "a.jpg"}}, {"image": None},
                                         {"image": {"provider": "fs", "path": "bad"}}], "n": 1},
                              lambda prov, path: (_ for _ in ()).throw(ValueError()) if path == "bad" else "ID9")
    assert res == {"items": [{"image_url": "imageproxy/ID9?size=256"}, {"image_url": None}, {"image_url": None}],
                   "n": 1}


def test_ui_info_follows_the_users_scope(mods, monkeypatch):
    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass = MagicMock()
    p.mass.webserver.base_url = ""
    user = types.SimpleNamespace(user_id="u1", username="seth", display_name="", role="user")
    monkeypatch.setattr(mods.ui, "get_current_user", lambda: user)
    monkeypatch.setattr(sys.modules[AUTH], "has_scope", lambda u, s: False, raising=False)
    info = asyncio.run(p.cmd_ui_info())
    assert info["user"] == {"id": "u1", "username": "seth", "display_name": "seth", "role": "user"}
    assert info["can_correct"] is False and info["ui_url"] == "/classical/"
    monkeypatch.setattr(sys.modules[AUTH], "has_scope", lambda u, s: True, raising=False)
    assert asyncio.run(p.cmd_ui_info())["can_correct"] is True
    monkeypatch.setattr(mods.ui, "get_current_user", lambda: None)
    info = asyncio.run(p.cmd_ui_info())
    assert info["user"] is None and info["can_correct"] is True


def test_view_maps_errors_and_images(mods):
    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass = MagicMock()
    p.mass.metadata.compute_image_id = lambda prov, path: f"{prov}-{path}"
    p._vis = lambda: {"filesystem_local"}

    async def read(fn):
        return fn("IX")

    p._read = read

    def ok(ix, vis=None):
        return {"ix": ix, "vis": sorted(vis), "image": {"provider": "fs", "path": "x"}}

    def missing(ix, vis=None):
        raise views.NotFound("w_1")

    def bad(ix, vis=None):
        raise ValueError("unknown role")

    assert asyncio.run(p._view(ok)) == {"ix": "IX", "vis": ["filesystem_local"], "image_url": "imageproxy/fs-x?size=256"}
    # a plain 400 on `POST /api` (MediaNotFoundError is a logged 500 there), and the same text for any id
    with pytest.raises(sys.modules["music_assistant_models.errors"].InvalidDataError, match="^Not found$"):
        asyncio.run(p._view(missing))
    with pytest.raises(sys.modules["music_assistant_models.errors"].InvalidDataError):
        asyncio.run(p._view(bad))


def test_register_ui_undoes_a_partial_registration(mods):
    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass, p.logger = MagicMock(), MagicMock()
    undo = [MagicMock() for _ in range(3)]
    p.mass.register_api_command.side_effect = [*undo, RuntimeError("Command classical/work is already registered")]
    with pytest.raises(RuntimeError):
        p._register_ui()
    for u in undo:
        u.assert_called_once_with()
    p.mass.webserver.register_dynamic_route.assert_not_called()


def _today_provider(mods, tmp_path, hemisphere="north"):
    from classical_core.tags import Person
    from tests.unit.test_views import Lib

    lib = Lib(tmp_path)
    bph = Person("Berliner Philharmoniker", "orchestra", "group")
    lib.add("Symphony No. 9 in D minor, Op. 125", [bph])
    lib.add("Danse macabre, Op. 40", [bph], composer="Camille Saint-Saëns")
    lib.add("Violin Concerto in D major, Op. 61", [bph])
    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass, p.logger, p.instance_id = MagicMock(), MagicMock(), "classical"
    p.mass.metadata.compute_image_id = lambda prov, path: "ID"
    p.config = MagicMock()
    p.config.get_value = lambda key, default=None: hemisphere if key == "hemisphere" else default
    p._vis = lambda: None
    reads = []

    async def read(fn, *a):
        reads.append(fn)
        return fn(lib.ix, *a)

    p._read = read
    return p, reads


def test_today_command(mods, tmp_path):
    p, reads = _today_provider(mods, tmp_path, "south")
    res = asyncio.run(p.cmd_today("2027-05-07"))
    assert res["hemisphere"] == "south" and res["season"] == "autumn" and res["weekday"] == "Friday"
    otd = res["shelves"][0]
    assert otd["id"] == "on_this_day" and otd["picks"][0]["reason"].startswith("Premiered on this day in 1824")
    assert otd["picks"][0]["play_uri"].startswith("classical://folder/works/")
    assert asyncio.run(p.cmd_today("2027-05-07")) == res and len(reads) == 1      # cached for the day
    with pytest.raises(sys.modules["music_assistant_models.errors"].InvalidDataError):
        asyncio.run(p.cmd_today("7 May"))


def test_today_cache_is_shared_kept_per_day_and_refreshed_in_the_background(mods, tmp_path, monkeypatch):
    p, reads = _today_provider(mods, tmp_path)
    day, other = dt.date(2027, 5, 7), dt.date(2027, 5, 8)

    async def scenario():
        a, b = await asyncio.gather(p._today(day), p._today(day))     # concurrent callers share one read
        assert a == b and len(reads) == 1
        await p._today(other)                                          # a second day doesn't evict the first
        await p._today(day)
        assert len(reads) == 2
        clock = time.monotonic() + mods.glue.TODAY_TTL + 1
        monkeypatch.setattr(mods.glue, "time", types.SimpleNamespace(monotonic=lambda: clock))
        assert await p._today(day) == a and p._today_tasks              # stale: served at once, refreshed behind
        for _ in range(3):
            await asyncio.sleep(0)                                     # the refresh, then its done callback
        assert len(reads) == 3 and not p._today_tasks
        assert await p._today(day) == a and len(reads) == 3            # the refresh is now fresh

    asyncio.run(scenario())
    for i in range(mods.glue.TODAY_KEEP + 2):
        asyncio.run(p._today(day + dt.timedelta(days=i)))
    assert len(p._today_cache) == mods.glue.TODAY_KEEP


def test_today_failure_is_logged_not_cached(mods, tmp_path):
    p, reads = _today_provider(mods, tmp_path)

    async def boom(fn, *a):
        reads.append(fn)
        raise RuntimeError("index busy")

    p._read = boom
    for _ in range(2):
        with pytest.raises(RuntimeError):
            asyncio.run(p._today(dt.date(2027, 5, 7)))
    assert len(reads) == 2 and not p._today_cache and not p._today_tasks
    p.logger.warning.assert_called()


def test_today_home_rows_and_items(mods, tmp_path, monkeypatch):
    p, _ = _today_provider(mods, tmp_path)
    monkeypatch.setattr(mods.glue, "UniqueList", list)
    real = mods.glue.dt.date
    fixed = type("FixedDate", (real,), {"today": classmethod(lambda cls: real(2026, 10, 31))})
    monkeypatch.setattr(mods.glue, "dt", types.SimpleNamespace(date=fixed))
    folder = mods.glue.RecommendationFolder
    folder.reset_mock()
    asyncio.run(p.get_recommendations())
    rows = [c.kwargs for c in folder.call_args_list]
    assert [r["item_id"] for r in rows][:2] == ["today/season", "today/weekday"]
    assert rows[0]["name"] == "For the season" and rows[0]["subtitle"].startswith("Halloween")
    assert rows[1]["name"] == "For Saturday"
    p._folder = lambda n: n
    [node] = asyncio.run(p.get_recommendation_items("today/season"))
    assert node.name == "Danse macabre, Op. 40" and node.playable
    assert node.path == node.item_id and node.path.startswith("works/") and "Halloween" in node.version
    assert asyncio.run(p.get_recommendation_items("today/nope")) == []


def test_streaming_search_lists_albums_per_service(mods, tmp_path):
    """'Other recordings on Qobuz': each visible streaming service is searched itself; albums already in the
    library, hidden services and services that cannot search are left out; one failing service says so."""
    from tests.unit.test_streaming import local_album, new_index, qobuz_album, run

    tags = {}
    ix = new_index(tmp_path, tags=tags)
    run(ix.index_album(local_album(tags)))
    run(ix.index_album(qobuz_album()))
    [w] = [w for w in ix.works() if "Symphony No. 5" in w["title"]]

    def track(item, title, album, artists, image=None):
        a = [types.SimpleNamespace(name=n) for n in artists]
        alb = types.SimpleNamespace(uri=f"qobuz://album/{album}", name=album, year=2001, artists=a,
                                    image=types.SimpleNamespace(to_dict=lambda: image) if image else None)
        return types.SimpleNamespace(item_id=item, uri=f"qobuz://track/{item}", name=title, duration=400,
                                     artists=a, album=alb)

    searched = []

    def prov(instance, name, tracks=None, fail=False, streaming=True, search=True):
        async def do_search(query, media_types, limit=5):
            searched.append((instance, query))
            if fail:
                raise RuntimeError("down")
            return types.SimpleNamespace(tracks=tracks or [])
        return types.SimpleNamespace(instance_id=instance, name=name, is_streaming_provider=streaming,
                                     supported_features={mods.ui.ProviderFeature.SEARCH} if search else set(),
                                     search=do_search)

    hits = [track("q0", "Symphony No. 5 in C minor, Op. 67: I. Allegro con brio", "Beethoven 5 (Järvi)",
                  ["Paavo Järvi"]),     # in the library already (its track q0 is)
            track("n1", "Symphony No. 5 in C minor, Op. 67: I. Allegro con brio", "Beethoven 5 (Abbado)",
                  ["Claudio Abbado"], image={"provider": "qobuz--abc", "path": "img"}),
            track("n2", "Symphony No. 5 in E minor, Op. 64: I. Andante", "Tchaikovsky 5", ["Tchaikovsky"])]
    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass = MagicMock()
    p.mass.metadata.compute_image_id = lambda prov, path: f"{prov}-{path}"
    p.mass.music.providers = [prov("qobuz--abc", "Qobuz", hits), prov("spotify--s", "Spotify", fail=True),
                              prov("tidal--hidden", "Tidal"), prov("filesystem_local", "Files", streaming=False),
                              prov("ytmusic--y", "YouTube Music", search=False)]
    p._vis = lambda: ["filesystem_local", "qobuz--abc", "spotify--s", "ytmusic--y"]

    async def read(fn):
        return fn(ix)

    p._read = read
    p.logger = MagicMock()
    res = asyncio.run(p.cmd_streaming(w["id"]))
    assert res["query"] == "Beethoven Symphony No. 5 in C Minor, Op. 67"
    assert sorted(i for i, _ in searched) == ["qobuz--abc", "spotify--s"]
    qob, spo = res["providers"]
    assert (spo["instance"], spo["error"], spo["albums"]) == ("spotify--s", "Search failed", [])
    assert [(a["name"], a["performers"], a["image_url"]) for a in qob["albums"]] == [
        ("Beethoven 5 (Abbado)", ["Claudio Abbado"], "imageproxy/qobuz--abc-img?size=256")]
    searched.clear()
    assert [x["instance"] for x in asyncio.run(p.cmd_streaming(w["id"], "qobuz--abc"))["providers"]] == ["qobuz--abc"]
    with pytest.raises(sys.modules["music_assistant_models.errors"].InvalidDataError):
        asyncio.run(p.cmd_streaming("w_missing"))


def test_add_album_only_from_a_visible_streaming_service_and_imports_its_tracks(mods):
    """'Add to library' is classical/add_album (LIBRARY_WRITE): the provider must be one the caller can see,
    and the album's tracks are imported so the index can read them (MA's add_item alone adds no tracks)."""
    added, imported = [], []

    def prov(instance, sync_tracks=False):
        async def import_album_tracks(item_id, album):
            imported.append((instance, item_id, album))
        return types.SimpleNamespace(instance_id=instance, name=instance, is_streaming_provider=True,
                                     supported_features={mods.ui.ProviderFeature.SEARCH},
                                     library_sync_album_tracks_enabled=lambda: sync_tracks,
                                     import_album_tracks=import_album_tracks)

    p = object.__new__(mods.glue.ClassicalProvider)
    p.mass = MagicMock()
    p.mass.music.providers = [prov("qobuz--abc"), prov("tidal--t", sync_tracks=True), prov("spotify--hidden")]
    p._vis = lambda: ["qobuz--abc", "tidal--t"]
    lib = types.SimpleNamespace(item_id=42)

    async def get_item(media_type, item_id, instance):
        if item_id == "gone":
            raise mods.glue.MediaNotFoundError("gone")
        return types.SimpleNamespace(media_type=media_type, item_id=item_id, provider=instance)

    async def add_item_to_library(item):
        added.append((item.provider, item.item_id))
        return lib

    p.mass.music.get_item = get_item
    p.mass.music.add_item_to_library = add_item_to_library
    res = asyncio.run(p.cmd_add_album("qobuz--abc", "a1"))
    assert res == {"library_album_id": "42", "imported_tracks": True}
    assert added == [("qobuz--abc", "a1")] and imported == [("qobuz--abc", "a1", lib)]
    # MA imports the tracks itself when the provider is set to
    assert asyncio.run(p.cmd_add_album("tidal--t", "b2"))["imported_tracks"] is False
    bad = sys.modules["music_assistant_models.errors"].InvalidDataError
    for args in (("spotify--hidden", "x"), ("filesystem_local", "x"), ("qobuz--abc", ""), ("qobuz--abc", "gone"),
                 ("qobuz--abc", None)):
        with pytest.raises(bad):
            asyncio.run(p.cmd_add_album(*args))
    assert len(added) == 2

