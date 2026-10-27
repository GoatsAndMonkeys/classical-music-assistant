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
