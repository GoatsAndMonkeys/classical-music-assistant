"""Tier 1: classical tags read straight from the file with mutagen (PLAN.md §5.2).

M0 found that MA's ffprobe-based `async_parse_tags` drops ID3 MVNM/MVIN and MP4 ©wrk/©mvn/©mvi/©mvc (see
DEVIATIONS.md), so the plugin reads tags with mutagen, which MA already ships, for every format. Keys are
normalized to Picard's names: `work`, `movementname`, `movement`, `movementtotal`, `musicbrainz_workid`,
`musicbrainz_trackid` (recording id), `composer`, `conductor`, `performer` (`Name (instrument)`), `ensemble`,
`lyricist`, `arranger`, plus `recordingdate` for explicit recording dates.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .mb import is_mbid
from .text import is_ensemble

# MP3 frames -> normalized keys (TIT1 is decided separately: it is the work only in some conventions)
_ID3 = {"TIT2": "title", "TALB": "album", "TPE1": "artist", "TPE2": "albumartist", "TCOM": "composer",
        "TPE3": "conductor", "TEXT": "lyricist", "TPE4": "arranger", "TDRC": "date", "TDOR": "originaldate",
        "TRCK": "tracknumber", "TPOS": "discnumber", "MVNM": "movementname", "GRP1": "grouping"}
_MP4 = {"\xa9nam": "title", "\xa9alb": "album", "\xa9ART": "artist", "aART": "albumartist", "\xa9wrt": "composer",
        "\xa9day": "date", "\xa9wrk": "work", "\xa9mvn": "movementname", "\xa9grp": "grouping"}
# free-form / TXXX / Vorbis spellings -> normalized
_ALIASES = {
    "musicbrainz work id": "musicbrainz_workid", "musicbrainz_workid": "musicbrainz_workid",
    "musicbrainz track id": "musicbrainz_trackid", "musicbrainz_trackid": "musicbrainz_trackid",
    "musicbrainz recording id": "musicbrainz_trackid", "musicbrainz_recordingid": "musicbrainz_trackid",
    "musicbrainz album id": "musicbrainz_albumid", "musicbrainz_albumid": "musicbrainz_albumid",
    "disctotal": "disctotal", "totaldiscs": "disctotal", "disc total": "disctotal",
    "work": "work", "movementname": "movementname", "movement": "movement", "movementnumber": "movement",
    "movementtotal": "movementtotal", "movementcount": "movementtotal", "composer": "composer",
    "conductor": "conductor", "performer": "performer", "ensemble": "ensemble", "orchestra": "ensemble",
    "choir": "ensemble", "lyricist": "lyricist", "librettist": "lyricist", "arranger": "arranger",
    "title": "title", "album": "album", "artist": "artist", "albumartist": "albumartist",
    "album artist": "albumartist", "tracknumber": "tracknumber", "discnumber": "discnumber", "date": "date",
    "originaldate": "originaldate", "original date": "originaldate", "originalyear": "originaldate",
    "recordingdate": "recordingdate", "recording_date": "recordingdate", "recording date": "recordingdate",
    "recorded": "recordingdate", "recordeddate": "recordingdate", "date recorded": "recordingdate",
    "grouping": "grouping", "showmovement": "showmovement",
    # the work's composition date, only under a name that says so: ORIGINALDATE / TDOR is the first
    # release's date (Picard), never the work's, so it is not read as one (DEVIATIONS.md, "Work dates")
    "compositiondate": "compositiondate", "composition date": "compositiondate",
    "composition_date": "compositiondate", "compositionyear": "compositiondate",
    "composition year": "compositiondate", "composeddate": "compositiondate", "datecomposed": "compositiondate",
    "date composed": "compositiondate", "yearcomposed": "compositiondate", "year composed": "compositiondate",
}
# marks a credit whose parenthesis a tag frame declares an instrument (ID3 TMCL), never a part sung
_INSTRUMENT = "\x01"
_PERFORMER = re.compile(r"^(?P<name>.+?)\s*\((?P<role>[^()]+)\)\s*$")
_GROUP_ROLES = {"orchestra", "ensemble", "choir", "chorus", "quartet", "band"}
# 'Ain Anger (bass (Pater Profundus))', 'Gustáv Beláček (Duke Bluebeard (bass))': a role and a detail
# (a character, the piece or concert, a section), nested either way round
_NESTED = re.compile(r"^(?P<name>[^()]+?)\s*\((?P<a>[^()]+?)\s*\((?P<b>[^()]+)\)\s*\)\s*$")
_ROLE_WORDS = frozenset(["soprano", "mezzo-soprano", "mezzo", "alto", "contralto", "countertenor", "tenor", "baritone", "bass", "bass-baritone", "treble", "boy-soprano", "voice", "vocals", "singer", "narrator", "speaker", "reciter", "actor", "piano", "fortepiano", "pianoforte", "harpsichord", "organ", "celesta", "keyboard", "keyboards", "violin", "viola", "cello", "violoncello", "double-bass", "contrabass", "bass-guitar", "harp", "guitar", "lute", "theorbo", "mandolin", "flute", "piccolo", "alto-flute", "recorder", "oboe", "english-horn", "cor-anglais", "clarinet", "bass-clarinet", "basset-horn", "saxophone", "bassoon", "contrabassoon", "horn", "french-horn", "trumpet", "cornet", "trombone", "bass-trombone", "tuba", "euphonium", "timpani", "percussion", "drums", "cymbals", "vibraphone", "marimba", "xylophone", "glockenspiel", "accordion", "bandoneon", "conductor", "chorus", "choir", "orchestra", "ensemble", "band", "quartet", "concertmaster", "leader", "chorus-master", "choirmaster", "announcer", "host", "presenter", "commentator", "soloist", "performer", "continuo"])
# words that make a parenthesis a performing role rather than a character sung ('Brünnhilde', '2nd Norn'):
# the role words themselves plus the broadcast, section and solo vocabulary found in real credits
_NON_CHARACTER = frozenset({w for r in _ROLE_WORDS for w in r.split("-")} - {"master", "boy"} | {
    "soloist", "soloists", "solo", "solos", "viol", "gamba", "schola", "orchestrator", "arranger", "organist",
    "interview", "interviewer", "intermission", "interval", "broadcaster", "broadcast", "editor", "author",
    "commentator", "guest", "honoree", "dramaturg", "direction", "preparation", "electronics",
    "harmonica", "hosts", "quartet", "trio", "quintet", "erhu", "pipa", "guzheng", "sitar", "sarod", "tabla",
    "oud", "koto", "shakuhachi", "shamisen", "kora", "ney", "kanun", "qanun", "duduk", "cimbalom", "dulcimer",
    "zither", "kantele", "balalaika", "bouzouki", "banjo", "ukulele", "bagpipes", "theremin", "ondes", "martenot",
    "synthesizer", "synthesiser", "harmonium", "clavichord", "virginal", "virginals", "spinet", "serpent",
    "ophicleide", "sackbut", "shawm", "crumhorn", "cornetto", "dulcian", "vihuela", "baryton", "arpeggione",
    "flugelhorn", "hurdy", "gurdy", "whistle", "tambourine", "triangle", "castanets", "gong", "tam", "bells",
    "chimes", "santoor", "santur", "steelpan", "cajon", "sampler", "tape", "fiddle", "nyckelharpa", "hardanger", "psaltery"})
# a parenthesis that is only one of these names a role, though the word can begin a part ('The Composer', 'Norn I')
_ROLE_ALONE = frozenset(["composer", "i", "ii", "iii", "iv", "boy"])
_VOICES = frozenset(["soprano", "mezzo-soprano", "mezzo", "alto", "contralto", "countertenor", "tenor", "baritone",
                     "bass", "bass-baritone", "treble", "boy-soprano", "voice", "narrator", "speaker"])
_ROLE_PREFIX = re.compile(r"^(?:principal|solo|first|second|third|1st|2nd|3rd|assistant|associate|co|guest|"
                          r"acting)[\s-]+", re.IGNORECASE)


@dataclass(slots=True)
class Person:
    name: str
    role: str        # conductor | orchestra | chorus | ensemble | soprano | piano | performer ...
    kind: str        # person | group
    detail: str | None = None   # what a nested credit adds: 'Pater Profundus', 'Symphony of Psalms'


@dataclass(slots=True)
class TagInfo:
    title: str | None = None
    album: str | None = None
    artist: str | None = None
    albumartist: str | None = None
    composers: list[str] = field(default_factory=list)
    work: str | None = None
    movement_name: str | None = None
    movement_no: int | None = None
    movement_total: int | None = None
    work_mbid: str | None = None
    recording_mbid: str | None = None
    disc: int | None = None
    disc_total: int | None = None      # TOTALDISCS / DISCTOTAL, or the '/2' of a 'n/2' disc number
    release_mbid: str | None = None    # MUSICBRAINZ_ALBUMID (the MB release)
    track: int | None = None
    recorded: str | None = None        # only an explicit recording-date tag
    release_date: str | None = None    # originaldate, else date
    performers: list[Person] = field(default_factory=list)
    lyricists: list[str] = field(default_factory=list)
    arrangers: list[str] = field(default_factory=list)
    composition_date: str | None = None   # an explicit COMPOSITIONDATE-style tag, as written

    @property
    def has_classical_tags(self) -> bool:
        return bool(self.work or self.work_mbid or self.movement_name or self.composers)

    def as_dict(self) -> dict:
        """asdict, without the credit `detail` field when it is empty (hashes and stored evidence of tags
        without nested credits stay what they were before the field existed)."""
        d = asdict(self)
        for k in ("disc_total", "release_mbid", "composition_date"):  # later fields: absent when empty, so old hashes stay
            if d[k] is None:
                del d[k]
        d["performers"] = [{k: v for k, v in p.items() if not (k == "detail" and v is None)}
                           for p in d["performers"]]
        return d

    def tag_hash(self) -> str:
        return hashlib.sha1(json.dumps(self.as_dict(), sort_keys=True, default=str).encode()).hexdigest()


def _vals(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for x in v:
            out += _vals(x)
        return out
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    s = str(v).strip()
    return [s] if s else []


def _int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"\s*(\d+)", str(s))
    return int(m.group(1)) if m else None


def read_raw(path: str | Path) -> dict[str, list[str]]:
    """{normalized key: [values]} for any format mutagen knows; unknown keys are kept lower-cased."""
    import mutagen  # shipped with MA

    f = mutagen.File(str(path))
    if f is None or f.tags is None:
        return {}
    out: dict[str, list[str]] = {}

    def put(key: str, values) -> None:
        k = _ALIASES.get(key.strip().lower(), key.strip().lower())
        for v in _vals(values):
            out.setdefault(k, []).append(v)

    tags = f.tags
    cls = type(tags).__name__
    if cls == "ID3":
        has_grp1 = any(k.startswith("GRP1") for k in tags)
        has_mv = any(k.startswith(("MVNM", "MVIN")) for k in tags)
        for key, frame in tags.items():
            fid = key.split(":")[0]
            if fid == "TXXX":
                put(frame.desc, frame.text)
            elif fid == "UFID" and frame.owner == "http://musicbrainz.org":
                put("musicbrainz_trackid", frame.data)
            elif fid == "MVIN":
                n, _, tot = str(frame.text[0]).partition("/")
                put("movement", n)
                if tot:
                    put("movementtotal", tot)
            elif fid == "TIT1":
                # iTunes ≥12.5 writes the work to TIT1 when it also writes GRP1 or movement frames
                put("work" if (has_grp1 or has_mv) else "grouping", frame.text)
            elif fid in ("TMCL", "TIPL"):
                for role, name in getattr(frame, "people", []):
                    if fid == "TMCL":
                        put("performer", f"{_INSTRUMENT}{name} ({role})")   # TMCL names the instrument
                    elif role.lower() in ("conductor", "arranger", "lyricist"):
                        put(role, name)
            elif fid in _ID3:
                put(_ID3[fid], [str(x) for x in frame.text])
    elif cls in ("MP4Tags",):
        for key, v in tags.items():
            if key in _MP4:
                put(_MP4[key], v)
            elif key == "\xa9mvi":
                put("movement", [str(x) for x in v])
            elif key == "\xa9mvc":
                put("movementtotal", [str(x) for x in v])
            elif key == "trkn":
                put("tracknumber", [str(x[0]) for x in v])
            elif key == "disk":
                put("discnumber", [str(x[0]) for x in v])
                put("disctotal", [str(x[1]) for x in v if len(x) > 1 and x[1]])
            elif key.startswith("----:"):
                put(key.split(":", 2)[2], [bytes(x) for x in v])
    else:  # Vorbis comments (FLAC, Ogg, Opus) and APEv2 behave like a {key: [str]} mapping
        for key, v in tags.items() if hasattr(tags, "items") else []:
            put(str(key), [str(x) for x in (v if isinstance(v, list) else [v])])
    return out


def is_role(text: str) -> bool:
    """'bass', 'principal trumpet', 'announcer / host': a performing role, not a character or a piece."""
    parts = [p for p in re.split(r"\s*(?:/|&|,|\band\b)\s*", text.strip().lower()) if p]
    return bool(parts) and all(re.sub(r"\s+", "-", _ROLE_PREFIX.sub("", p)) in _ROLE_WORDS for p in parts)


def _is_character(text: str) -> bool:
    """'Gutrune', '2nd Norn', 'Voice of Apollo', 'Roberto, Earl of Leicester': a part sung or spoken, which
    names no voice, instrument or broadcast role. A part is a name, so it is capitalised ('erhu' is an
    instrument the vocabulary lacks); 'X of Y' phrases do not count ('Leader of the Players')."""
    first = re.search(r"(?<!\w)[^\W\d_]", text)   # a word's first letter: '2nd Norn' -> N
    if not first or not first.group().isupper() or text.strip().lower() in _ROLE_ALONE:
        return False
    if text == text.upper() and len(re.findall(r"[^\W\d_]+", text)) < 2:
        return False   # 'WOTAN' or 'SANTUR': all capitals say nothing, so one word stays a role
    t = re.sub(r"([^\W\d_][\w-]*)\s+of\s+[^/,;]+",
               lambda o: " " if o.group(1) in ("voice", "spirit", "ghost") else o.group(1), text.lower())
    return not any(w in _NON_CHARACTER for w in re.findall(r"[^\W\d_]+", t.replace("\u2011", "-")))


def _voice_and_part(text: str) -> tuple[str, str] | None:
    """(voice, part) for a credit's parenthesis that names the part sung: 'Gutrune' -> ('voice', 'Gutrune'),
    'tenor, Evangelist' -> ('tenor', 'Evangelist'); None for a performing role ('principal viola')."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) > 1:
        for i in (0, len(parts) - 1):
            rest = ", ".join(parts[:i] + parts[i + 1:])
            if parts[i].lower() in _VOICES and _is_character(rest):
                return parts[i].lower(), rest
    return ("voice", text.strip()) if _is_character(text) else None


def split_values(value: str) -> list[str]:
    """Split a multi-value tag at NUL and ';', but not at a ';' inside a parenthesis that closes:
    'Martha Mödl (Gutrune; 3rd Norn)' is one credit; 'A (Wotan; B (violin)' is two."""
    out: list[str] = []
    for chunk in value.split("\x00"):
        inside, stack = set(), []
        for i, ch in enumerate(chunk):
            if ch == "(":
                stack.append(i)
            elif ch == ")" and stack:
                inside.update(range(stack.pop(), i))
        cuts = [i for i, ch in enumerate(chunk) if ch == ";" and i not in inside]
        out += [chunk[x + 1:y].strip() for x, y in zip([-1, *cuts], [*cuts, len(chunk)])]
    return [x for x in out if x]


def _person(text: str, default_role: str, parts: bool = True) -> Person | None:
    s = text.strip()
    if not s:
        return None
    detail = None
    n = _NESTED.match(s)
    m = None if n else _PERFORMER.match(s)
    if n:
        a, b = n.group("a").strip(), n.group("b").strip()
        # the role is whichever half names one; the other half is detail (a character, a concert)
        role_text, detail = (b, a) if is_role(b) and not is_role(a) else (a, b)
        name, role = n.group("name").strip(), role_text.lower()
    else:
        name, role = (m.group("name").strip(), m.group("role").strip().lower()) if m else (s, default_role)
        if parts and m and role not in _GROUP_ROLES and (vp := _voice_and_part(m.group("role"))):
            role, detail = vp   # 'Martha Mödl (Gutrune)': she sings Gutrune; it is not her instrument
    if role in _GROUP_ROLES or (role in ("performer", "ensemble") and is_ensemble(name, role)):
        group_role = "chorus" if role in ("choir", "chorus") or re.search(r"chor|choir|coro", name, re.IGNORECASE) else (
            "orchestra" if role == "orchestra" or re.search(r"orch|philharm|symphon|sinfoni|kapelle", name, re.IGNORECASE)
            else "ensemble")
        return Person(name, group_role, "group", detail)
    return Person(name, role, "person", detail)


def normalize_person(p: Person) -> Person:
    """A credit stored before nested roles were parsed ('Ain Anger (bass (Pater Profundus))' as one name)
    is parsed again on load, so old evidence reads the same as a fresh tag read."""
    if p.detail is not None or "(" not in p.name:
        return p
    q = _person(p.name, p.role if p.role in ("conductor", "performer", "ensemble") else "performer")
    return q if q is not None and q.name != p.name else p


def parse(raw: dict[str, list[str]]) -> TagInfo:
    def first(k: str) -> str | None:
        v = raw.get(k)
        return v[0] if v else None

    def many(k: str) -> list[str]:
        out: list[str] = []
        for v in raw.get(k, []):
            out += split_values(v)
        return list(dict.fromkeys(out))

    info = TagInfo(
        title=first("title"), album=first("album"), artist=first("artist"), albumartist=first("albumartist"),
        composers=many("composer"), work=first("work"), movement_name=first("movementname"),
        movement_no=_int(first("movement")), movement_total=_int(first("movementtotal")),
        disc=_int(first("discnumber")), track=_int(first("tracknumber")), recorded=first("recordingdate"),
        release_date=first("originaldate") or first("date"), lyricists=many("lyricist"), arrangers=many("arranger"),
        composition_date=(first("compositiondate") or "").strip() or None,
    )
    wid = (first("musicbrainz_workid") or "").strip().lower()
    info.work_mbid = wid if is_mbid(wid) else None
    disc = first("discnumber") or ""
    total = _int(first("disctotal")) or (_int(disc.partition("/")[2]) if "/" in disc else None)
    info.disc_total = total if total and total > 0 else None
    rel = (first("musicbrainz_albumid") or "").strip().lower()
    info.release_mbid = rel if is_mbid(rel) else None
    rid = (first("musicbrainz_trackid") or "").strip().lower()
    info.recording_mbid = rid if is_mbid(rid) else None
    for c in many("conductor"):
        info.performers.append(normalize_person(Person(c, "conductor", "person")))
    for e in many("ensemble"):
        p = _person(f"{e} (ensemble)" if "(" not in e else e, "ensemble")
        if p:
            info.performers.append(p)
    for t in many("performer"):
        p = _person(t.removeprefix(_INSTRUMENT), "performer", parts=not t.startswith(_INSTRUMENT))
        if p and not any(q.name == p.name for q in info.performers):
            info.performers.append(p)
    return info


def read_tags(path: str | Path) -> TagInfo:
    return parse(read_raw(path))


def year_of(date: str | None) -> int | None:
    m = re.match(r"\s*(\d{4})", date or "")
    return int(m.group(1)) if m else None
