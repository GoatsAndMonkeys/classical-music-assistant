"""MusicBrainz release matching for albums with no MBIDs at all (PLAN.md §5.11, Tier 2 fallback).

An album's title, artist and track layout are searched as a release; only one unambiguous, high-scoring
release whose title, artist, per-disc track counts *and* track lengths all agree is accepted. Its track
recordings then feed the ordinary recording → work chain. Anything less certain is left for review: a
wrong release would merge recordings with works they are not, which is worse than not knowing.

Pure functions: the index fetches (through the rate-limited, cached `MBClient`) and stores the result.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .text import norm_key

MIN_SCORE = 95          # MB's own search score for the accepted release
RIVAL_SCORE = 90        # another release this close that also fits makes the match ambiguous
LENGTH_SLACK_S = 5      # per-track length tolerance: max(5 s, 5 %)
LENGTH_SHARE = 0.8      # share of tracks (with lengths on both sides) that must agree
_VARIOUS = {"various artists", "various", "unknown artist", "artista desconocido", "[unknown]", ""}
_DISC = re.compile(r"[\s,:;–—-]*[(\[]?\s*\b(?:disc|disk|cd)\s*(\d{1,2})\s*(?:of\s*\d{1,2}\s*)?[)\]]?\s*$",
                   re.IGNORECASE)
_COMPOSER_PREFIX = re.compile(r"^[^:]{2,40}:\s*(?=\S)")


def _lucene(s: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


def split_disc(name: str | None) -> tuple[str, int | None]:
    """'Richard Wagner: Der Fliegende Holländer Disc 2' -> ('Richard Wagner: Der Fliegende Holländer', 2)."""
    t = (name or "").strip()
    m = _DISC.search(t)
    if m and m.start() > 0:
        return t[:m.start()].strip(), int(m.group(1))
    return t, None


def title_keys(name: str | None) -> set[str]:
    """The release titles an album name may stand for: itself without a disc marker, and without a leading
    'Composer:' (MB titles a classical release 'Der fliegende Holländer', a rip says 'Wagner: …')."""
    base, _ = split_disc(name)
    keys = {norm_key(base)}
    m = _COMPOSER_PREFIX.match(base)
    if m:
        keys.add(norm_key(base[m.end():]))
    return {k for k in keys if k}


def wanted(name: str | None, artist: str | None) -> bool:
    """A release search needs a real title and a real artist."""
    base, _ = split_disc(name)
    return bool(norm_key(base)) and norm_key(artist) not in _VARIOUS and not norm_key(base).startswith(
        ("unknown album", "titulo desconocido", "no title"))


def search_url(base_url: str, name: str, artist: str) -> str:
    base, _ = split_disc(name)
    m = _COMPOSER_PREFIX.match(base)
    title = base[m.end():] if m else base
    q = f'release:"{_lucene(title)}" AND artist:"{_lucene(artist)}"'
    return f"{base_url.rstrip('/')}/release?query={urllib.parse.quote(q)}&limit=25&fmt=json"


def lookup_url(base_url: str, mbid: str) -> str:
    return f"{base_url.rstrip('/')}/release/{mbid}?inc=recordings+artist-credits&fmt=json"


def _credit_keys(release: dict) -> set[str]:
    keys = set()
    for ac in release.get("artist-credit") or []:
        if isinstance(ac, dict):
            keys.add(norm_key(ac.get("name")))
            a = ac.get("artist") or {}
            keys.add(norm_key(a.get("name")))
            sort = a.get("sort-name") or ""
            if "," in sort:
                last, first = (p.strip() for p in sort.split(",", 1))
                keys.add(norm_key(f"{first} {last}"))
    return keys - {""}


def _artist_fits(release: dict, artist: str) -> bool:
    credited = _credit_keys(release)
    parts = {norm_key(p) for p in re.split(r"\s*(?:;|/|,|&| - )\s*", artist or "")} - {""}
    return bool(parts & credited) or norm_key(artist) in credited


def _counts_fit(release: dict, disc_counts: dict[int, int], marker: int | None) -> bool:
    media = [m.get("track-count") for m in release.get("media") or []]
    if not media or any(not isinstance(c, int) for c in media):
        return False
    ours = [disc_counts[d] for d in sorted(disc_counts)]
    if marker is not None and len(ours) == 1:
        return marker <= len(media) and media[marker - 1] == ours[0]   # one disc of a set
    if len(ours) > 1 and sorted(disc_counts) != list(range(1, len(ours) + 1)):
        return False    # discs 2 and 3 of a set: which media they are is a guess
    return media == ours


@dataclass(slots=True)
class Pick:
    mbid: str | None
    why: str                # 'ok', 'none', 'ambiguous'
    rivals: list[str]


def pick_release(search: dict, name: str, artist: str, disc_counts: dict[int, int]) -> Pick:
    """The one release the album is, or why not."""
    keys = title_keys(name)
    _, marker = split_disc(name)
    fits = []
    for r in search.get("releases") or []:
        score = int(r.get("score") or 0)
        if score < RIVAL_SCORE or not r.get("id"):
            continue
        if norm_key(r.get("title")) not in keys or not _artist_fits(r, artist):
            continue
        if not _counts_fit(r, disc_counts, marker):
            continue
        fits.append((score, r["id"]))
    if not fits:
        return Pick(None, "none", [])
    if len(fits) > 1:
        return Pick(None, "ambiguous", [m for _, m in fits])
    score, mbid = fits[0]
    return Pick(mbid, "ok", []) if score >= MIN_SCORE else Pick(None, "low score", [mbid])


def track_recordings(release: dict, tracks: list[tuple[str, int | None, int | None, int]],
                     marker: int | None) -> tuple[dict[str, str], str]:
    """{library_track_id: recording MBID} for `tracks` (id, disc, position, duration s) by disc and position,
    or ({}, reason) when the release's track lengths do not confirm it."""
    media = release.get("media") or []
    by_pos: dict[tuple[int, int], dict] = {}
    for mi, m in enumerate(media, 1):
        for t in m.get("tracks") or []:
            try:
                by_pos[(mi, int(t.get("position") or t.get("number")))] = t
            except (TypeError, ValueError):
                continue
    out: dict[str, str] = {}
    agree = total = 0
    for tid, disc, pos, dur in tracks:
        if pos is None:
            return {}, "track positions missing"
        t = by_pos.get((marker or disc or 1, pos))
        rec = (t or {}).get("recording") or {}
        if not rec.get("id"):
            return {}, "release track missing"
        length = t.get("length") or rec.get("length")
        fits = True
        if dur and isinstance(length, (int, float)):
            total += 1
            fits = abs(length / 1000 - dur) <= max(LENGTH_SLACK_S, 0.05 * dur)
            agree += fits
        if fits:        # a track whose length disagrees may be another take or a bonus: never linked
            out[tid] = rec["id"]
    if total < max(1, len(tracks) // 2) or agree < LENGTH_SHARE * total:
        return {}, "track lengths do not match"
    return out, "ok"
