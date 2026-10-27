"""Which MusicBrainz release an untagged CD rip is, for its cover only: found by title and track layout,
confirmed by the tracklist (artwork.py).

Most rips the index cannot place have no usable album artist ('Various Artists' on 'I Puritani (Disc 2)'),
so the index's title + artist release search never finds them. Here the album's cleaned title is searched
with its per-disc track count (`tracksmedium`), and a candidate release is accepted only when its tracklist
confirms it: the same number of tracks, nearly every track within `LENGTH_SLACK_S` seconds of ours, and a
composer or performer of ours credited on the release or its tracks (the per-track artists stand in for the
album artist). Two pressings of one recording share timings, so ties are expected: when every confirmed
release (and every unchecked rival that credits our performers) belongs to one release group, the group's
cover is used. Releases from two groups, or rivals left unchecked outside the group, are no answer.

Broadcasts and concert recordings (radio rips titled by date or venue) are never searched: they are not
commercial CDs, and a cover found for one would belong to some other recording.

Pure functions: artwork.py fetches through the index's rate-limited, cached MusicBrainz client.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .release import _COMPOSER_PREFIX, PLACEHOLDER_ARTISTS, _lucene
from .text import norm_key

LENGTH_SLACK_S = 3        # per-track length tolerance (seconds); CD rips and MB's disc-ID lengths agree to ~1 s
LENGTH_SHARE = 0.9        # share of tracks that must agree within the slack
LENGTH_OUTLIER_S = 15     # no track may be further off than this (another take, a bonus track)
MB_LENGTH_SHARE = 0.9     # share of the release's tracks that must have a length in MB at all
MIN_TRACKS = 4            # fewer tracks are too little to tell recordings apart by their timings
MIN_TITLE_LEN = 4
MIN_SCORE = 80            # MB search score for a candidate worth a tracklist lookup
MAX_LOOKUPS = 10          # release lookups per album (1/s); more fitting candidates than this is a guess

# radio and concert rips: '240127 BSO Lady Macbeth', '0711 Mahler 6 Gergiev', '… — Barbican Hall, 2019-05-30',
# 'RISURREZIONE- RAI Torino October 22, 1971', 'Tempest BBC', 'I Puritani Edinburgh 8/9/60 (1)'
_BROADCAST = [
    re.compile(r"^\s*\d{2,7}(?:\s|$)"),                                                     # date/year prefix
    re.compile(r"\s—\s"),                                                              # 'Programme — Venue, date'
    re.compile(r"\b(?:bbc|prom|proms|rai|radio|broadcast|live|concert|recital|festival|bayreuth|salzburg|"
               r"tanglewood|wigmore|carnegie|met|metropolitan)\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"),                                 # 8/9/60, 2003.12.08
    re.compile(r"\b(?:1[89]|20)\d\d[-.]\d\d\b"),                                       # 2003-12, 2019-05-30
    re.compile(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|"
               r"december)\s+\d{1,2},?\s+\d{4}\b", re.IGNORECASE),
    re.compile(r"\b(?:1[89]|20)\d\d\b"),                                               # 'Norma 1971', '(1961)'
]
_DISC_MARK = re.compile(
    r"[\s,:;=–—-]*[(\[]?\s*(?:\b(?:disc|disk|cd)\s*\d{1,2}(?:\s*(?:/|of)\s*\d{1,2})?\b|\b\d{1,2}\s*(?:/|of)\s*\d{1,2}\b"
    r"|\b(?:act|akt|atto|acte)\s*(?:[ivx]{1,4}|\d)\b)\s*[)\]]?", re.IGNORECASE)
_TRAILING_NO = re.compile(r"(?:\s+|[-–]\s*)\d{1,2}\s*$")
_BRACKETED = re.compile(r"\s*[(\[][^()\[\]]*[)\]]")
_UNCLOSED = re.compile(r"\s*[(\[][^()\[\]]*$")
_NUMBERED = re.compile(r"^\d{1,2}\.\s+")
_SPLIT = re.compile(r"\s*(?:;|/|,|&|\+| - | – | — |\band\b|\bfeat\.?)\s*", re.IGNORECASE)
_GENERIC = {"orchestra", "orchestre", "orchester", "symphony", "symphonic", "philharmonic",
            "philharmonia", "chorus", "choir", "coro", "chor", "ensemble", "quartet", "opera", "teatro", "theatre",
            "theater", "radio", "sinfonica", "sinfonia", "staatskapelle", "academy", "consort", "soloists", "various",
            "artists", "unknown", "milano", "london", "berlin", "vienna", "wien", "paris", "roma", "rome", "national",
            "chamber", "festival", "singers", "voices", "players", "conductor", "soprano", "tenor", "baritone",
            "bass", "mezzo", "interpretes", "varios", "artista", "desconocido", "scala"}


def looks_broadcast(name: str | None) -> bool:
    """A radio or concert rip by its title (dates, venues, broadcasters): never matched to a release."""
    return any(p.search(name or "") for p in _BROADCAST)


def clean_title(name: str | None) -> str:
    """The release title a rip's album name stands for: no disc marker, no 'Composer:' prefix, no trailing
    '- Composer', no bracketed cast list. 'Bellini: I Puritani (Disc 2)' -> 'I Puritani',
    'La Sonnambula (Sutherland, Pavarotti, Ghiaurov, Bonynge) CD 1' -> 'La Sonnambula'."""
    t = _NUMBERED.sub("", (name or "").strip())
    for _ in range(3):
        before = t
        t = _DISC_MARK.sub("", t).strip()
        t = _BRACKETED.sub("", t).strip()
        t = _UNCLOSED.sub("", t).strip()
        t = _TRAILING_NO.sub("", t).strip(" -–—,:;=")
        if t == before:
            break
    m = _COMPOSER_PREFIX.match(t)
    if m:
        t = t[m.end():]
    t = re.split(r"\s+[-–—]\s+", t)[0]       # 'I Puritani - Vincenzo Bellini', 'Leonore - Fidelio' -> first part
    return t.strip(" -–—,:;=")


def bracketed_names(name: str | None) -> list[str]:
    """The cast or conductor a rip names in brackets: 'Fidelio (Klemperer, disc 1)' -> ['Klemperer']."""
    out = []
    for inner in re.findall(r"[(\[]([^()\[\]]*)[)\]]?", name or ""):
        inner = _DISC_MARK.sub("", inner)
        out += [p for p in _SPLIT.split(inner) if p and not re.search(r"\d", p)]
    return out


def searchable(title: str) -> bool:
    k = norm_key(title)
    return len(k) >= MIN_TITLE_LEN and not k.isdigit() and k not in PLACEHOLDER_ARTISTS and not k.startswith(
        ("unknown", "untitled", "no title", "titulo desconocido", "onbekende"))


def search_url(base_url: str, title: str, tracks_on_medium: int) -> str:
    q = f'release:"{_lucene(title)}" AND tracksmedium:{int(tracks_on_medium)}'
    return f"{base_url.rstrip('/')}/release?query={urllib.parse.quote(q)}&limit=100&fmt=json"


def lookup_url(base_url: str, mbid: str) -> str:
    return f"{base_url.rstrip('/')}/release/{mbid}?inc=recordings+artist-credits+release-groups&fmt=json"


# ---- names ----------------------------------------------------------------------------------------------


def name_keys(names: list[str | None]) -> set[str]:
    """Surname-like keys of people: the last word (4+ letters) of every name in `names`, after splitting credits
    like 'Riccard MUTI, M.CABALLE; A.Kraus'. Orchestras, choirs and placeholders ('Various Artists') give none."""
    out: set[str] = set()
    for raw in names:
        for part in _SPLIT.split(raw or ""):
            words = norm_key(part).split()
            if any(w in _GENERIC for w in words):
                continue                    # an orchestra or choir: its name says little about the recording
            words = [w for w in words if len(w) >= 4]
            if words:
                out.add(words[-1])
    return out


def _credit_names(credits: list | None) -> list[str]:
    out = []
    for ac in credits or []:
        if isinstance(ac, dict):
            out.append(ac.get("name"))
            a = ac.get("artist") or {}
            out.append(a.get("name"))
            sort = a.get("sort-name") or ""
            out.append(sort.split(",", 1)[0] if "," in sort else sort)
    return [n for n in out if n]


def release_name_keys(release: dict) -> set[str]:
    names = _credit_names(release.get("artist-credit"))
    for m in release.get("media") or []:
        for t in m.get("tracks") or []:
            names += _credit_names(t.get("artist-credit"))
            names += _credit_names((t.get("recording") or {}).get("artist-credit"))
    return name_keys(names)


# ---- the album side -------------------------------------------------------------------------------------


@dataclass(slots=True)
class Rip:
    """One album as the index knows it: name, per-disc track lengths in position order, people."""
    name: str
    discs: dict[int, list[int | None]]                 # disc -> lengths (s) of tracks 1..n
    performers: set[str] = field(default_factory=set)  # name_keys of album/track artists and credited performers
    composers: set[str] = field(default_factory=set)


def rip_from_tracks(name: str, album_artist: str | None, tracks: list[tuple[int | None, int | None, int | None, str | None]],
                    performers: list[str], composers: list[str]) -> Rip | None:
    """`tracks`: (disc, position, duration s, track artist). None when the positions are not 1..n on every
    disc (a partial rip, or two releases MA filed under one album name)."""
    discs: dict[int, dict[int, int | None]] = {}
    for disc, pos, dur, _ in tracks:
        if pos is None:
            return None
        d = discs.setdefault(disc or 1, {})
        if pos in d:
            return None
        d[pos] = dur
    if not discs:
        return None
    out: dict[int, list[int | None]] = {}
    for d, by_pos in discs.items():
        if sorted(by_pos) != list(range(1, len(by_pos) + 1)):
            return None
        out[d] = [by_pos[p] for p in range(1, len(by_pos) + 1)]
    people = [t[3] for t in tracks] + list(performers) + bracketed_names(name)
    if norm_key(album_artist) not in PLACEHOLDER_ARTISTS:
        people.append(album_artist)
    return Rip(name, out, name_keys(people), name_keys(composers))


def wanted(rip: Rip) -> bool:
    n = sum(len(v) for v in rip.discs.values())
    return (n >= MIN_TRACKS and not looks_broadcast(rip.name) and searchable(clean_title(rip.name))
            and all(x for v in rip.discs.values() for x in v))


def first_disc_count(rip: Rip) -> int:
    return len(rip.discs[min(rip.discs)])


# ---- candidates and confirmation ------------------------------------------------------------------------


def _layout_fits(media_counts: list[Any], rip: Rip) -> bool:
    if not media_counts or any(not isinstance(c, int) for c in media_counts):
        return False
    ours = [len(rip.discs[d]) for d in sorted(rip.discs)]
    if len(ours) == 1:
        return ours[0] in media_counts                      # one disc, maybe of a set
    if sorted(rip.discs) != list(range(1, len(ours) + 1)):
        return False
    return media_counts == ours


def candidates(search: dict, rip: Rip) -> list[dict]:
    """Search hits whose title fits and whose disc layout fits, best first (performers credited on the
    release, then the composer, then MB's score)."""
    key = norm_key(clean_title(rip.name))
    out = []
    for r in search.get("releases") or []:
        if not r.get("id") or int(r.get("score") or 0) < MIN_SCORE:
            continue
        if f" {key} " not in f" {norm_key(r.get('title'))} ":
            continue
        if not _layout_fits([m.get("track-count") for m in r.get("media") or []], rip):
            continue
        out.append(r)
    keys = [name_keys(_credit_names(r.get("artist-credit"))) for r in out]
    order = sorted(range(len(out)), key=lambda i: (-len(keys[i] & rip.performers), -len(keys[i] & rip.composers),
                                                   -int(out[i].get("score") or 0)))
    return [out[i] for i in order]


def credits_ours(hit: dict, rip: Rip) -> bool:
    """A search hit credits one of our performers (release level): it may be another issue of our recording."""
    return bool(name_keys(_credit_names(hit.get("artist-credit"))) & rip.performers)


def _lengths_fit(ours: list[int | None], theirs: list[Any]) -> bool:
    if len(ours) != len(theirs):
        return False
    known = [(o, t / 1000) for o, t in zip(ours, theirs, strict=True) if o and isinstance(t, (int, float)) and t > 0]
    if len(known) < MB_LENGTH_SHARE * len(ours):
        return False
    close = sum(abs(o - t) <= LENGTH_SLACK_S for o, t in known)
    return close >= LENGTH_SHARE * len(known) and all(abs(o - t) <= LENGTH_OUTLIER_S for o, t in known)


def _medium_lengths(medium: dict) -> list[Any]:
    tracks = sorted(medium.get("tracks") or [], key=lambda t: int(t.get("position") or 0))
    return [t.get("length") or (t.get("recording") or {}).get("length") for t in tracks]


def confirms(release: dict, rip: Rip) -> bool:
    """The release's tracklist is our rip's: track counts, per-track lengths, and a composer or performer."""
    media = [_medium_lengths(m) for m in release.get("media") or []]
    if len(rip.discs) == 1:
        ours = next(iter(rip.discs.values()))
        timing = any(_lengths_fit(ours, m) for m in media)
    else:
        ours_all = [rip.discs[d] for d in sorted(rip.discs)]
        timing = len(media) == len(ours_all) and all(_lengths_fit(o, m) for o, m in zip(ours_all, media, strict=True))
    if not timing:
        return False
    names = release_name_keys(release)
    return bool(names & (rip.performers | rip.composers))


@dataclass(frozen=True, slots=True)
class Match:
    release: str | None        # the one release, when it alone fits
    release_group: str
    confirmed: tuple[str, ...]


def decide(hits: list[dict], looked_up: dict[str, dict], rip: Rip) -> Match | None:
    """The answer from the ranked `hits` and the releases looked up so far ({mbid: release})."""
    confirmed = [m for m, rel in looked_up.items() if confirms(rel, rip)]
    if not confirmed:
        return None
    groups = {(looked_up[m].get("release-group") or {}).get("id") for m in confirmed}
    if len(groups) != 1 or None in groups:
        return None
    group = next(iter(groups))
    unchecked = [h for h in hits if h["id"] not in looked_up]
    if any(credits_ours(h, rip) and (h.get("release-group") or {}).get("id") != group for h in unchecked):
        return None             # another issue of our recording may be among the unchecked: no guessing
    sibling_unchecked = any((h.get("release-group") or {}).get("id") == group for h in unchecked)
    one = confirmed[0] if len(confirmed) == 1 and not sibling_unchecked else None
    return Match(one, group, tuple(sorted(confirmed)))
