"""What a streaming service says about a library track (PLAN.md §5.10): composers and performer credits.

A streaming track has no file to read, so Tier 1 has nothing. MA 2.10.8 carries what the service gave it in
`Track.metadata.performers` and `Track.artists`, and each provider fills `performers` differently:

* Qobuz: 'Name, Role[, Role]' credits, but MA splits the service's string on every '-', so a hyphenated
  name ('Jean-Efflam Bavouzet, Piano') arrives as two fragments ('Jean', 'Efflam Bavouzet, Piano');
* Apple Music: the composer field split on ', ' (composers, and sometimes a librettist);
* Tidal, OpenSubsonic: contributor names with no roles;
* Spotify, Deezer, YouTube Music: nothing (the composer, when credited, is one of the track artists).

`provider_facts` turns that into the same `TagInfo` shape a tag read gives (composers and performers only;
never a work, a movement or an MBID), so the title tier can use it. Anything unclear is dropped rather than
guessed: a wrong composer would put a recording under someone else's work.
"""

from __future__ import annotations

import re

from .composers import PERFORMING, lookup
from .tags import Person, TagInfo, _person
from .text import is_ensemble, norm_key

# provider domains whose `performers` are 'Name, Role' credits / composer names / bare contributor names
ROLE_CREDITS = {"qobuz"}
COMPOSER_NAMES = {"apple_music"}
BARE_NAMES = {"tidal", "opensubsonic"}
# MA's own non-streaming sources: file providers have tags, the rest have nothing to add
FILE_DOMAINS = {"filesystem_local", "filesystem_smb", "filesystem_nfs"}

_COMPOSER_ROLES = {"composer", "composerlyricist", "composer-lyricist", "composer, lyricist"}
_PERFORMER_ROLES = {"mainartist", "main artist", "featuredartist", "featured artist", "performer", "artist",
                    "soloist", "interprete", "interprète", "vocalist", "vocals"}
_GROUP_ROLES = {"orchestra": "orchestra", "choir": "chorus", "chorus": "chorus", "ensemble": "ensemble"}
# production and paperwork credits: never a performer of the recording
_SKIP_ROLE = re.compile(
    r"engineer|producer|production|mix|master(?:ing|ed)\b|recording|record|edit|publisher|label|liner|a&r|"
    r"author|lyric|writer|librett|translat|programm|design|photo|art\s*direct|copyright|studio|executive|"
    r"assistant|manager|supervis|coordinat|tape|balance|music\s*director|director|other|unknown", re.IGNORECASE)


def domain_of(provider_instance: str, domain: str | None = None) -> str:
    """A provider instance's domain: MA names instances '<domain>--<id>' (or just '<domain>')."""
    return domain or provider_instance.split("--", 1)[0]


def is_streaming(provider_instance: str, domain: str | None = None) -> bool:
    return domain_of(provider_instance, domain) not in FILE_DOMAINS | {"library", "builtin"}


def _composer_ok(name: str) -> bool:
    """A name counts as the composer on its own only when it is a listed composer known for composing."""
    k = lookup(name)
    return k is not None and k.name not in PERFORMING


def _repair(name: str, artists: list[str]) -> str | None:
    """With hyphen-damaged credits, a name is trusted only when an MA artist confirms it: exactly, or as the
    tail of a hyphenated artist name ('Efflam Bavouzet' -> 'Jean-Efflam Bavouzet')."""
    key = norm_key(name)
    for a in artists:
        if norm_key(a) == key:
            return a
    for a in artists:
        if "-" in a and key in (norm_key(a.rsplit("-", 1)[-1]), norm_key(a.split("-", 1)[-1])):
            return a
    return None


def _qobuz(performers: list[str], artists: list[str]) -> tuple[list[str], list[Person], list[str]]:
    entries = [p.strip() for p in performers if p and p.strip()]
    heads = sorted(e for e in entries if "," not in e)
    damaged = bool(heads)
    composers: list[str] = []
    credits: list[Person] = []
    arrangers: list[str] = []
    for e in sorted(entries):
        if "," not in e:
            continue
        name, *roles = (x.strip() for x in e.split(","))
        roles = [r.lower() for r in roles if r]
        if not name or not roles:
            continue
        if damaged:
            fixed = _repair(name, artists)
            if fixed is None:
                # a composer's name is checked against the list instead: as it is, or rejoined with one of
                # the fragments that lost their tail ('Camille Saint' + 'Saëns, Composer')
                if any(r in _COMPOSER_ROLES for r in roles):
                    known = [k for k in [name] + [f"{h}-{name}" for h in heads] if _composer_ok(k)]
                    if len({lookup(k).name for k in known}) == 1:  # type: ignore[union-attr]
                        composers.append(lookup(known[0]).name)  # type: ignore[union-attr]
                continue
            name = fixed
        if any(r in _COMPOSER_ROLES for r in roles):
            composers.append(name)
            continue
        if "arranger" in roles:
            arrangers.append(name)
            continue
        if "conductor" in roles:
            credits.append(Person(name, "conductor", "person"))
            continue
        group = next((_GROUP_ROLES[r] for r in roles if r in _GROUP_ROLES), None)
        if group:
            credits.append(Person(name, group, "group"))
            continue
        playing = [r for r in roles if not _SKIP_ROLE.search(r)]
        if not playing:
            continue
        role = next((r for r in playing if r not in _PERFORMER_ROLES), "performer")
        p = _person(f"{name} ({role})", "performer")
        if p is not None:
            credits.append(p)
    return list(dict.fromkeys(composers)), credits, list(dict.fromkeys(arrangers))


def _artist_credits(artists: list[str], composers: list[str]) -> list[Person]:
    """Performer credits from MA's track artists when the service gave no roles: groups by name, everyone
    else as a performer. A listed composer among the artists is the composer, not a performer."""
    taken = {norm_key(c) for c in composers}
    out: list[Person] = []
    for a in artists:
        if not a or norm_key(a) in taken or _composer_ok(a):
            continue
        p = _person(a, "performer")
        if p is None:
            continue
        if p.kind == "person" and is_ensemble(a, "performer"):
            p = Person(a, "ensemble", "group")
        out.append(p)
    return out


def provider_facts(domain: str, performers: list[str], artists: list[str]) -> TagInfo | None:
    """Composers and credits for one streaming track, or None when the service said nothing usable."""
    performers = [p for p in performers or [] if isinstance(p, str) and p.strip()]
    artists = [a for a in artists or [] if isinstance(a, str) and a.strip()]
    composers: list[str] = []
    credits: list[Person] = []
    arrangers: list[str] = []
    if domain in ROLE_CREDITS:
        composers, credits, arrangers = _qobuz(performers, artists)
    elif domain in COMPOSER_NAMES:
        names = sorted({p.strip() for p in performers})
        known = [n for n in names if lookup(n) is not None]
        # 'Wolfgang Amadeus Mozart, Lorenzo Da Ponte': the listed composer, not the librettist; several
        # unlisted names say nothing about which one composed
        composers = known if known else (names if len(names) == 1 else [])
    elif domain in BARE_NAMES:
        composers = sorted({lookup(p).name for p in performers if _composer_ok(p)})  # type: ignore[union-attr]
        composers = composers if len(composers) == 1 else []
    if not credits:
        credits = _artist_credits(artists, composers)
    if not composers and not credits:
        return None
    return TagInfo(composers=composers, performers=credits, arrangers=arrangers)


_NAMES = {"qobuz": "Qobuz", "apple_music": "Apple Music", "spotify": "Spotify", "tidal": "Tidal",
          "deezer": "Deezer", "ytmusic": "YouTube Music", "opensubsonic": "Subsonic", "soundcloud": "SoundCloud",
          "jellyfin": "Jellyfin", "plex": "Plex", "builtin": "Built-in", "filesystem_local": "Local files",
          "filesystem_smb": "Network share", "filesystem_nfs": "Network share"}


def source(provider_instance: str) -> dict:
    """A recording's provider badge: {'instance', 'domain', 'name', 'streaming'}."""
    d = domain_of(provider_instance)
    return {"instance": provider_instance, "domain": d, "name": _NAMES.get(d, d.replace("_", " ").title()),
            "streaming": is_streaming(provider_instance)}


# ---- "Other recordings on <service>" (§5.10): suggestions from a provider search, never added on their own --


def search_query(work_title: str, composer: str | None) -> str:
    """'Beethoven Symphony No. 5 in C minor, Op. 67': the composer's surname and the work's title."""
    from .text import surname

    sn = surname(composer or "", particles=False) if composer else ""
    return " ".join(x for x in (sn, work_title) if x).strip()


def _work_fits(title: str, work_title: str, cats: list) -> bool:
    from . import catalogue as cat
    from .titles import parse_title, strip_track_number

    t = strip_track_number(title or "")
    p = parse_title(t)
    head = p.work if p else t
    if cats:
        theirs = {c.key for c in cat.parse_catalogues(head)}
        return bool(theirs) and theirs == {c.key for c in cats}
    a, b = norm_key(head), norm_key(work_title)
    return bool(a and b) and (a == b or a.startswith(b + " "))


def match_search(work_title: str, composer: str | None, tracks: list[dict], in_library: set[tuple[str, str]],
                 limit: int = 12) -> list[dict]:
    """The albums among a provider's track search results that hold this work: a track title naming the
    work (by catalogue number when the work has one, else by title) and the composer named somewhere on the
    track or its album. Albums with a track already in the library are left out. Most matching tracks first.

    `tracks`: {'provider_instance', 'item_id', 'uri', 'title', 'artists', 'duration', 'album': {'uri', 'name',
    'artists', 'year', 'image'}}; `in_library`: (provider_instance, item_id) of library tracks."""
    from . import catalogue as cat
    from .text import surname

    cats = cat.parse_catalogues(work_title)
    sk = norm_key(surname(composer or "", particles=False)) if composer else ""
    albums: dict[str, dict] = {}
    skip: set[str] = set()
    for t in tracks:
        alb = t.get("album") or {}
        key = alb.get("uri") or ""
        if not key:
            continue
        if (t.get("provider_instance"), str(t.get("item_id"))) in in_library:
            skip.add(key)
            continue
        if not _work_fits(t.get("title") or "", work_title, cats):
            continue
        text = norm_key(" ".join([t.get("title") or "", *(t.get("artists") or []), alb.get("name") or "",
                                  *(alb.get("artists") or [])]))
        if sk and not re.search(rf"\b{re.escape(sk)}\b", text):
            continue
        a = albums.setdefault(key, {"uri": key, "name": alb.get("name") or "", "year": alb.get("year"),
                                    "artist": ", ".join(alb.get("artists") or []), "image": alb.get("image"),
                                    "provider_instance": t.get("provider_instance"), "tracks": 0, "duration": 0,
                                    "performers": []})
        a["tracks"] += 1
        a["duration"] += int(t.get("duration") or 0)
        for n in t.get("artists") or []:
            if n not in a["performers"] and not (sk and norm_key(surname(n, particles=False)) == sk):
                a["performers"].append(n)
    out = [a for k, a in albums.items() if k not in skip]
    out.sort(key=lambda a: (-a["tracks"], a["name"]))
    return out[:limit]
