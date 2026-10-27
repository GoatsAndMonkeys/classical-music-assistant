"""Companion UI glue (provider/classical/ui.py, web.py) without Music Assistant: MA's modules are stubbed."""

import asyncio
import importlib
import sys
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
                     "classical/recording", "classical/performers", "classical/performer", "classical/review"]
    p.mass.webserver.unregister_dynamic_route.assert_called_once_with("/classical/*", "*")
    assert p.mass.webserver.register_dynamic_route.call_count == 2 and handles[-1] is unreg
    assert len(handles) == 9
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
