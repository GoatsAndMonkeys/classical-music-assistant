"""The companion web UI's static route (PLAN.md §6, M3).

MA's dynamic routes are unauthenticated, so this handler serves only fixed static files: the page shell and
its script, stylesheet and icon. Every byte of data goes through MA's authenticated API (`POST /api`, the
same command handlers and scope checks as the websocket). File names are looked up in a fixed table, never
joined from the request, so no path can escape the `web/` folder.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from aiohttp import web

ROUTE = "/classical/*"
PREFIX = "/classical"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
FILES: dict[str, tuple[str, str]] = {
    "app.js": (os.path.join(WEB_DIR, "app.js"), "text/javascript"),
    "app.css": (os.path.join(WEB_DIR, "app.css"), "text/css"),
    "icon.svg": (os.path.join(WEB_DIR, "icon.svg"), "image/svg+xml"),
    "manifest.webmanifest": (os.path.join(WEB_DIR, "manifest.webmanifest"), "application/manifest+json"),
}
INDEX = os.path.join(WEB_DIR, "index.html")
# WebKit (Safari/iOS, Playwright WebKit) logs "Refused to apply a stylesheet ... Content Security Policy" once
# per native <select>: the engine's own shadow stylesheet for the control trips style-src 'self'. Nothing
# of ours is blocked (the page renders identically without the CSP), so style-src stays strict.
CSP = ("default-src 'self'; img-src 'self' data:; connect-src 'self'; script-src 'self'; style-src 'self'; "
       "manifest-src 'self'; base-uri 'self'; form-action 'self'; object-src 'none'; frame-ancestors 'self'")
HEADERS = {
    "Cache-Control": "no-cache",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
IMAGE_SIZE = 256


class ClassicalWeb:
    """Serves the UI's files; `handle` is registered as MA's dynamic route `/classical/*`."""

    def __init__(self, version: str) -> None:
        self.version = version
        self._cache: dict[str, bytes] = {}

    def _read(self, path: str) -> bytes:
        # read once per process; the files ship with the provider and only change with it
        if path not in self._cache:
            with open(path, "rb") as f:
                data = f.read()
            if path == INDEX:
                data = data.replace(b"{{VERSION}}", self.version.encode())
            self._cache[path] = data
        return self._cache[path]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method not in ("GET", "HEAD"):
            return web.Response(status=405, headers={"Allow": "GET, HEAD"})
        path = request.path
        # the route is a prefix match: /classicalfoo reaches this handler too
        if path == PREFIX:
            # relative on purpose: under HA ingress the page lives below /api/hassio_ingress/<token>/
            return web.Response(status=302, headers={**HEADERS, "Location": "classical/"})
        if not path.startswith(PREFIX + "/"):
            return web.Response(status=404)
        name = path[len(PREFIX) + 1:]
        if name in ("", "index.html"):
            file, ctype = INDEX, "text/html"
        elif name in FILES:
            file, ctype = FILES[name]
        else:
            return web.Response(status=404, headers=HEADERS)
        body = await _in_thread(self._read, file)
        charset = "utf-8" if ctype.startswith("text/") else None
        return web.Response(body=b"" if request.method == "HEAD" else body, content_type=ctype, charset=charset,
                            headers=HEADERS)


async def _in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def image_urls(obj: Any, compute_id: Callable[[str, str], str], size: int = IMAGE_SIZE) -> Any:
    """Replace every `image` dict ({provider, path, ...}) in a view with `image_url: imageproxy/<id>?size=`.

    The URL is relative, like everything the page requests, so it works under ingress and reverse proxies."""
    if isinstance(obj, list):
        return [image_urls(x, compute_id, size) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if k == "image":
            url = None
            if isinstance(v, dict) and v.get("path"):
                try:
                    url = f"imageproxy/{compute_id(str(v.get('provider') or ''), str(v['path']))}?size={size}"
                except Exception:  # noqa: BLE001 - an image MA can't resolve is just no image
                    url = None
            out["image_url"] = url
        else:
            out[k] = image_urls(v, compute_id, size)
    return out
