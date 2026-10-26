"""The classical index (PLAN.md §4–§6): evidence in, works/recordings out, plus the browse queries.

`Index` is MA-free. The plugin glue turns MA library albums into `AlbumIn` and calls `index_album()`
from its single worker task, which owns the writer connection. Browse and command handlers call the
read methods, always passing the caller's visible provider instances (`vis`, None = all).

Flow for one album:
1. occurrences from the album's track listing (reusing ids by (album, track));
2. Tier 1: tags re-read per provider mapping when its file fingerprint changed; evidence replaced per
   origin only when the normalized tag hash changed; read errors retract nothing;
3. Tier 2: MusicBrainz recordings and works (bounded parent walk), evidence replaced per request URL on a
   complete response; failures keep the previous evidence;
4. derivation from live evidence: per-occurrence work candidates (tags > MB > title), the album grouping
   (§5.1), work identity (§5.5 merge rules) and the derived rows, all in one transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from . import catalogue as cat
from .composers import find_in_credit, lookup
from .grouping import Slot, group_album
from .ids import new_id
from .mb import MBClient, MBError, RecordingInfo, WorkInfo, is_mbid, parse_recording, parse_work
from .sethster_canonical import form_key
from .tags import Person, TagInfo, read_tags, year_of
from .text import fold, norm_key, sort_name
from .titles import parse_movement, parse_title, shared_prefix_runs

log = logging.getLogger("classical.index")

FOLDER_CAP = 500
MAX_PARENT_DEPTH = 3


# ---- inputs from the glue ------------------------------------------------------------------------------


@dataclass(slots=True)
class MappingIn:
    provider_instance: str
    item_id: str
    fingerprint: str | None = None
    path: str | None = None          # absolute path for local files; None skips Tier 1 (remote, CUE)
    available: bool = True           # False: provider configured but not loaded


@dataclass(slots=True)
class TrackIn:
    library_track_id: str
    disc: int | None
    position: int | None
    title: str
    artist: str = ""
    duration: int = 0
    mbid: str | None = None
    mappings: list[MappingIn] = field(default_factory=list)


@dataclass(slots=True)
class AlbumIn:
    library_album_id: str
    name: str
    artist: str = ""
    year: int | None = None
    image: dict | None = None
    tracks: list[TrackIn] = field(default_factory=list)


@dataclass(slots=True)
class AlbumStats:
    occurrences: int = 0
    recordings: int = 0
    tag_reads: int = 0
    read_errors: int = 0
    mb_requests: int = 0
    review_items: int = 0


# ---- per-occurrence work candidate ---------------------------------------------------------------------


@dataclass(slots=True)
class Cand:
    key: str
    title: str
    tier: str                                    # tags | mb | title
    composers: list[tuple[str, str | None]]      # (name, mbid)
    mbid: str | None = None
    mb_type: str | None = None
    key_sig: str | None = None
    catalogues: list[cat.Catalogue] = field(default_factory=list)
    parent_key: str | None = None
    part_key: str | None = None
    movement_no: int | None = None
    movement_name: str | None = None
    part_aliases: list[str] = field(default_factory=list)
    mb_parts: int | None = None
    movement_total: int | None = None


@dataclass(slots=True)
class OccFacts:
    occ_id: str
    track: dict
    tags: TagInfo | None
    tag_origin: str | None
    mb: RecordingInfo | None
    cand: Cand | None = None
    extra_works: list[Cand] = field(default_factory=list)   # multi-work tracks: every linked top work
    credits: list[Person] = field(default_factory=list)
    recorded: str | None = None
    release_year: int | None = None
    partial: bool = False
    live: bool = False
    medley: bool = False


def _tag_rows(info: TagInfo) -> list[tuple[str, str, str]]:
    """(field, value, edge) rows for one tag read."""
    rows: list[tuple[str, str, str]] = []
    for k, v in asdict(info).items():
        if v is None or v == []:
            continue
        if isinstance(v, list):
            for i, x in enumerate(v):
                rows.append((k, json.dumps(x, sort_keys=True), f"{k}:{i}"))
        else:
            rows.append((k, json.dumps(v), f"{k}:0"))
    return rows


def _tags_from_rows(rows: Iterable[sqlite3.Row]) -> TagInfo:
    data: dict[str, Any] = {}
    lists: dict[str, list[tuple[int, Any]]] = {}
    for r in rows:
        f, edge = r["field"], r["edge"]
        val = json.loads(r["value"])
        if f in ("composers", "performers", "lyricists", "arrangers"):
            lists.setdefault(f, []).append((int(edge.split(":")[1]), val))
        else:
            data[f] = val
    info = TagInfo(**{k: v for k, v in data.items() if k in TagInfo.__dataclass_fields__})
    for f, items in lists.items():
        vals = [v for _, v in sorted(items)]
        if f == "performers":
            vals = [Person(**v) for v in vals]
        setattr(info, f, vals)
    return info


# ---- the index -----------------------------------------------------------------------------------------


class Index:
    def __init__(self, con: sqlite3.Connection, mb: MBClient | None = None,
                 tag_reader: Callable[[str], TagInfo] = read_tags) -> None:
        self.con = con
        self.mb = mb
        self.tag_reader = tag_reader

    # -- small helpers --

    def _q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        return self.con.execute(sql, args).fetchone()

    def _alias(self, entity: str, kind: str, value: str) -> str | None:
        r = self._one("SELECT entity_id FROM alias WHERE entity=? AND kind=? AND value=?", (entity, kind, value))
        return self.resolve(entity, r[0]) if r else None

    def _add_alias(self, entity: str, entity_id: str, kind: str, value: str) -> None:
        self.con.execute("INSERT OR IGNORE INTO alias(entity, entity_id, kind, value) VALUES (?,?,?,?)",
                         (entity, entity_id, kind, value))

    def resolve(self, entity: str, entity_id: str) -> str:
        """Follow merge redirects (bounded)."""
        for _ in range(10):
            r = self._one("SELECT new_id FROM redirect WHERE entity=? AND old_id=?", (entity, entity_id))
            if not r:
                break
            entity_id = r[0]
        return entity_id

    # -- evidence --

    def replace_origin(self, origin: str, source: str, rows: list[tuple[str, str, str, str, str]]) -> bool:
        """Atomically make `rows` (entity, entity_id, field, value, edge) the live assertions of `origin`.
        Missing ones are retracted. Returns True when anything changed."""
        now = time.time()
        live = self._q("SELECT id, entity, entity_id, field, value, edge FROM evidence "
                       "WHERE source_ref=? AND retracted_at IS NULL", (origin,))
        have = {(r["entity"], r["entity_id"], r["field"], r["value"], r["edge"]): r["id"] for r in live}
        want = set(rows)
        if set(have) == want:
            return False
        own_tx = not self.con.in_transaction
        if own_tx:
            self.con.execute("BEGIN IMMEDIATE")
        try:
            gone = [have[k] for k in have.keys() - want]
            self.con.executemany("UPDATE evidence SET retracted_at=? WHERE id=?", [(now, i) for i in gone])
            self.con.executemany(
                "INSERT INTO evidence(entity, entity_id, field, value, source, source_ref, edge, observed_at) "
                "VALUES (?,?,?,?,?,?,?,?)", [(*k[:4], source, origin, k[4], now) for k in want - have.keys()])
            if own_tx:
                self.con.execute("COMMIT")
        except BaseException:
            if own_tx:
                self.con.execute("ROLLBACK")
            raise
        return True

    def retract_origin(self, origin: str) -> None:
        self.con.execute("UPDATE evidence SET retracted_at=? WHERE source_ref=? AND retracted_at IS NULL",
                         (time.time(), origin))

    def _live(self, entity: str, entity_id: str) -> list[sqlite3.Row]:
        return self._q("SELECT field, value, edge, source_ref FROM evidence WHERE entity=? AND entity_id=? "
                       "AND retracted_at IS NULL ORDER BY field, edge", (entity, entity_id))

    # -- Tier 1 --

    async def refresh_mapping(self, track: TrackIn, m: MappingIn, force: bool = False) -> str:
        """Returns 'read', 'unchanged', 'skipped', 'error' or 'unavailable'."""
        origin = f"tags:{m.provider_instance}/{m.item_id}"
        row = self._one("SELECT * FROM mapping_state WHERE provider_instance=? AND item_id=?",
                        (m.provider_instance, m.item_id))
        now = time.time()
        if not m.available:
            self._upsert_mapping(m, track, row, status="unavailable")
            return "unavailable"
        if m.path is None:
            self._upsert_mapping(m, track, row, status="ok")
            return "skipped"
        if (not force and row is not None and row["status"] == "ok" and row["file_fingerprint"] == m.fingerprint
                and m.fingerprint is not None):
            return "unchanged"
        try:
            info = await asyncio.to_thread(self.tag_reader, m.path)
        except Exception as e:  # noqa: BLE001 - any read failure is a read_error; it retracts nothing
            self._upsert_mapping(m, track, row, status="read_error", error=str(e)[:500], retry_after=now + 3600)
            return "error"
        h = info.tag_hash()
        if force or row is None or row["tag_hash"] != h:
            rows = [("mapping", f"{m.provider_instance}/{m.item_id}", f, v, e) for f, v, e in _tag_rows(info)]
            self.replace_origin(origin, "tags", rows)
        self._upsert_mapping(m, track, row, status="ok", tag_hash=h, mbid=info.recording_mbid, indexed_at=now)
        return "read"

    def _upsert_mapping(self, m: MappingIn, track: TrackIn, row: sqlite3.Row | None, status: str,
                        **kw: Any) -> None:
        vals = {"file_fingerprint": m.fingerprint if status == "ok" else (row["file_fingerprint"] if row else None),
                "tag_hash": row["tag_hash"] if row else None, "mbid": row["mbid"] if row else None,
                "indexed_at": row["indexed_at"] if row else None, "error": None, "retry_after": None}
        vals.update(kw)
        self.con.execute(
            "INSERT INTO mapping_state(provider_instance, item_id, library_track_id, file_fingerprint, tag_hash, "
            "mbid, indexed_at, status, error, retry_after) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider_instance, item_id) DO UPDATE SET library_track_id=excluded.library_track_id, "
            "file_fingerprint=excluded.file_fingerprint, tag_hash=excluded.tag_hash, mbid=excluded.mbid, "
            "indexed_at=excluded.indexed_at, status=excluded.status, error=excluded.error, "
            "retry_after=excluded.retry_after",
            (m.provider_instance, m.item_id, track.library_track_id, vals["file_fingerprint"], vals["tag_hash"],
             vals["mbid"], vals["indexed_at"], status, vals["error"], vals["retry_after"]))

    def mark_gone(self, provider_instance: str, item_id: str) -> None:
        """A mapping that is truly gone (§5.6) retracts everything from it."""
        self.retract_origin(f"tags:{provider_instance}/{item_id}")
        self.con.execute("UPDATE mapping_state SET status='gone' WHERE provider_instance=? AND item_id=?",
                         (provider_instance, item_id))

    # -- Tier 2 --

    async def refresh_mb_recording(self, mbid: str, stats: AlbumStats) -> None:
        if self.mb is None:
            return
        url = self.mb.url("recording", mbid, "work-rels+artist-rels")
        try:
            data = await self.mb.recording(mbid)
        except MBError as e:
            log.debug("MB recording %s: %s", mbid, e)
            return
        stats.mb_requests += 1
        rec = parse_recording(data)
        rows = [("mb_recording", mbid, "title", json.dumps(rec.title), "title")]
        rows += [("mb_recording", mbid, "work_link", json.dumps(asdict(w), sort_keys=True), w.edge) for w in rec.works]
        rows += [("mb_recording", mbid, "credit", json.dumps(asdict(c), sort_keys=True), c.edge) for c in rec.credits]
        self.replace_origin(url, "mb", rows)
        for w in rec.works:
            await self.refresh_mb_work_chain(w.work_mbid, stats)

    async def refresh_mb_work_chain(self, mbid: str, stats: AlbumStats) -> None:
        seen: set[str] = set()
        todo = [(mbid, 0)]
        while todo:
            wid, depth = todo.pop(0)
            if wid in seen:
                continue
            seen.add(wid)
            info = await self.refresh_mb_work(wid, stats)
            if info is not None and depth < MAX_PARENT_DEPTH:
                todo += [(p.mbid, depth + 1) for p in info.parents]

    async def refresh_mb_work(self, mbid: str, stats: AlbumStats) -> WorkInfo | None:
        if self.mb is None:
            return None
        url = self.mb.url("work", mbid, "work-rels+artist-rels+series-rels+aliases")
        try:
            data = await self.mb.work(mbid)
        except MBError as e:
            log.debug("MB work %s: %s", mbid, e)
            return self._mb_work(mbid)
        stats.mb_requests += 1
        w = parse_work(data)
        e = ("mb_work", mbid)
        rows = [(*e, "title", json.dumps(w.title), "title")]
        for f in ("work_type", "key", "title_en"):
            if getattr(w, f):
                rows.append((*e, f, json.dumps(getattr(w, f)), f))
        for f in ("composers", "credits", "parents", "parts", "arrangement_of", "revision_of"):
            rows += [(*e, f, json.dumps(asdict(x), sort_keys=True), x.edge) for x in getattr(w, f)]
        rows += [(*e, "catalogues", json.dumps(c), f"cat:{c[0]}:{c[1]}") for c in w.catalogues]
        self.replace_origin(url, "mb", rows)
        return w

    def _mb_recording(self, mbid: str) -> RecordingInfo | None:
        rows = self._live("mb_recording", mbid)
        if not rows:
            return None
        from .mb import Credit, WorkLink

        rec = RecordingInfo(mbid=mbid, title="", length_ms=None)
        for r in rows:
            v = json.loads(r["value"])
            if r["field"] == "title":
                rec.title = v
            elif r["field"] == "work_link":
                rec.works.append(WorkLink(**v))
            elif r["field"] == "credit":
                rec.credits.append(Credit(**v))
        rec.works.sort(key=lambda w: w.ord)
        rec.credits.sort(key=lambda c: c.ord)
        return rec

    def _mb_work(self, mbid: str) -> WorkInfo | None:
        rows = self._live("mb_work", mbid)
        if not rows:
            return None
        from .mb import Credit, WorkRef

        w = WorkInfo(mbid=mbid, title="", work_type=None, key=None)
        for r in rows:
            f, v = r["field"], json.loads(r["value"])
            if f in ("title", "work_type", "key", "title_en"):
                setattr(w, f, v)
            elif f in ("composers", "credits"):
                getattr(w, f).append(Credit(**v))
            elif f in ("parents", "parts", "arrangement_of", "revision_of"):
                getattr(w, f).append(WorkRef(**v))
            elif f == "catalogues":
                w.catalogues.append(tuple(v))
        w.parts.sort(key=lambda p: (p.ordering is None, p.ordering or 0))
        w.composers.sort(key=lambda c: c.ord)
        return w

    def mb_chain(self, mbid: str) -> tuple[list[str], str | None]:
        """[work, parent, grandparent, ...] up to the depth limit, plus an abstain reason if the walk had to
        stop at an ambiguity (two parents, a cycle, or the depth limit with parents left)."""
        chain = [mbid]
        seen = {mbid}
        cur = self._mb_work(mbid)
        while cur is not None:
            parents = {p.mbid for p in cur.parents}
            if not parents:
                return chain, None
            if len(parents) > 1:
                return chain, "work has two parents"
            if len(chain) > MAX_PARENT_DEPTH:
                return chain, "parent depth limit reached"
            nxt = next(iter(parents))
            if nxt in seen:
                return chain, "cycle in work hierarchy"
            chain.append(nxt)
            seen.add(nxt)
            cur = self._mb_work(nxt)
        return chain, None

    # -- album indexing --

    def album_signature(self, album_id: str) -> str | None:
        r = self._one("SELECT signature FROM album_info WHERE album_library_id=?", (album_id,))
        return r[0] if r else None

    def _retry_due(self, album: AlbumIn) -> bool:
        keys = [(m.provider_instance, m.item_id) for t in album.tracks for m in t.mappings]
        now = time.time()
        for pi, item in keys:
            r = self._one("SELECT status, retry_after FROM mapping_state WHERE provider_instance=? AND item_id=?",
                          (pi, item))
            if r and r["status"] == "read_error" and (r["retry_after"] or 0) <= now:
                return True
        return False

    async def index_album(self, album: AlbumIn, force: bool = False) -> AlbumStats | None:
        """Index one album. Returns None when nothing changed since the last run (and not forced)."""
        sig = album_signature(album)
        if not force and self.album_signature(album.library_album_id) == sig and not self._retry_due(album):
            return None
        stats = AlbumStats()
        self.con.execute(
            "INSERT INTO album_info(album_library_id, name, artist, year, image_json, indexed_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(album_library_id) DO UPDATE SET name=excluded.name, artist=excluded.artist, "
            "year=excluded.year, image_json=excluded.image_json, indexed_at=excluded.indexed_at",
            (album.library_album_id, album.name, album.artist, album.year,
             json.dumps(album.image) if album.image else None, time.time()))
        tracks = sorted(album.tracks, key=lambda t: (t.disc or 1, t.position or 0))
        for t in tracks:
            self.con.execute("INSERT OR REPLACE INTO track_info(library_track_id, title, artist, duration) "
                             "VALUES (?,?,?,?)", (t.library_track_id, t.title, t.artist, t.duration))
            for m in t.mappings:
                res = await self.refresh_mapping(t, m, force=force)
                stats.tag_reads += res == "read"
                stats.read_errors += res == "error"
        # Tier 2 entry points: recording MBIDs (MA's or the tag's) and work MBID tags
        rec_ids: set[str] = set()
        work_ids: set[str] = set()
        for t in tracks:
            tags = self.track_tags(t.library_track_id)[0]
            for mbid in (t.mbid, tags.recording_mbid if tags else None):
                if is_mbid(mbid):
                    rec_ids.add(mbid.lower())
            if tags and tags.work_mbid:
                work_ids.add(tags.work_mbid)
        for rid in sorted(rec_ids):
            await self.refresh_mb_recording(rid, stats)
        for wid in sorted(work_ids):
            await self.refresh_mb_work_chain(wid, stats)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self._sync_occurrences(album.library_album_id, tracks)
            self._derive_album(album, tracks, stats)
            self.con.execute("UPDATE album_info SET signature=? WHERE album_library_id=?",
                             (sig, album.library_album_id))
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return stats

    def remove_album(self, album_id: str) -> None:
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self._clear_derived(album_id)
            self.con.execute("UPDATE occurrence SET present=0 WHERE album_library_id=?", (album_id,))
            self.con.execute("DELETE FROM album_info WHERE album_library_id=?", (album_id,))
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise

    def _sync_occurrences(self, album_id: str, tracks: list[TrackIn]) -> None:
        existing = {r["library_track_id"]: r["id"] for r in
                    self._q("SELECT id, library_track_id FROM occurrence WHERE album_library_id=?", (album_id,))}
        keep = set()
        for t in tracks:
            oid = existing.get(t.library_track_id)
            if oid is None:
                oid = new_id("o")
                self.con.execute("INSERT INTO occurrence(id, album_library_id, disc, position, library_track_id) "
                                 "VALUES (?,?,?,?,?)", (oid, album_id, t.disc, t.position, t.library_track_id))
            else:
                self.con.execute("UPDATE occurrence SET disc=?, position=?, present=1 WHERE id=?",
                                 (t.disc, t.position, oid))
            keep.add(oid)
        for oid in existing.values():
            if oid not in keep:
                self.con.execute("UPDATE occurrence SET present=0 WHERE id=?", (oid,))

    def track_tags(self, library_track_id: str) -> tuple[TagInfo | None, str | None, bool]:
        """Live tag evidence for a library track: the lowest (provider_instance, item_id) mapping with live
        evidence wins; the flag says whether other live mappings disagree (→ review)."""
        maps = self._q("SELECT provider_instance, item_id, tag_hash FROM mapping_state WHERE library_track_id=? "
                       "AND status != 'gone' ORDER BY provider_instance, item_id", (library_track_id,))
        chosen: TagInfo | None = None
        origin = None
        hashes = set()
        for m in maps:
            rows = self._live("mapping", f"{m['provider_instance']}/{m['item_id']}")
            if not rows:
                continue
            hashes.add(m["tag_hash"])
            if chosen is None:
                chosen = _tags_from_rows(rows)
                origin = f"tags:{m['provider_instance']}/{m['item_id']}"
        return chosen, origin, len(hashes) > 1

    def _clear_derived(self, album_id: str) -> None:
        occ = "(SELECT id FROM occurrence WHERE album_library_id=?)"
        self.con.execute(f"DELETE FROM recording_slot WHERE occurrence_id IN {occ}", (album_id,))
        self.con.execute("DELETE FROM recording WHERE album_library_id=?", (album_id,))
        self.con.execute(f"DELETE FROM work_link WHERE occurrence_id IN {occ}", (album_id,))
        self.con.execute(f"DELETE FROM track_credit WHERE occurrence_id IN {occ}", (album_id,))
        self.con.execute("DELETE FROM review_item WHERE album_library_id=?", (album_id,))

    # -- derivation --

    def _derive_album(self, album: AlbumIn, tracks: list[TrackIn], stats: AlbumStats) -> None:
        aid = album.library_album_id
        old_rec = {r["occurrence_id"]: r["recording_id"] for r in self._q(
            "SELECT s.occurrence_id, s.recording_id FROM recording_slot s JOIN occurrence o ON o.id=s.occurrence_id "
            "WHERE o.album_library_id=?", (aid,))}
        self._clear_derived(aid)
        occ_ids = {r["library_track_id"]: r["id"] for r in
                   self._q("SELECT id, library_track_id FROM occurrence WHERE album_library_id=? AND present=1", (aid,))}
        reviews: list[tuple[str, str | None, str | None, dict]] = []
        facts: list[OccFacts] = []
        positions = [(t.disc or 1, t.position) for t in tracks]
        ambiguous = any(p[1] is None for p in positions) or len(set(positions)) != len(positions)
        self.con.execute("UPDATE album_info SET ambiguous=? WHERE album_library_id=?", (int(ambiguous), aid))
        for t in tracks:
            tags, origin, conflict = self.track_tags(t.library_track_id)
            if conflict:
                reviews.append(("conflicting tags on duplicate files", "occurrence", occ_ids[t.library_track_id],
                                {"track": t.title}))
            rec_mbid = next((m.lower() for m in (tags.recording_mbid if tags else None, t.mbid) if is_mbid(m)), None)
            mb = self._mb_recording(rec_mbid) if rec_mbid else None
            facts.append(OccFacts(occ_ids[t.library_track_id], {"title": t.title, "artist": t.artist,
                                  "duration": t.duration, "disc": t.disc, "position": t.position}, tags, origin, mb))

        self._assign_candidates(facts, album, reviews)

        slots = []
        for f in facts:
            c = f.cand
            slots.append(Slot(
                occ_id=f.occ_id, disc=f.track["disc"] or 1, position=f.track["position"],
                work_key=c.key if c else None, parent_key=c.parent_key if c else None,
                part_key=c.part_key if c else None, movement_no=c.movement_no if c else None,
                conductors={norm_key(p.name) for p in f.credits if p.role == "conductor"},
                orchestras={norm_key(p.name) for p in f.credits if p.role == "orchestra"},
                choirs={norm_key(p.name) for p in f.credits if p.role == "chorus"},
                recorded=f.recorded, multi=bool(f.extra_works)))
        result = group_album(slots, ambiguous=ambiguous)
        for reason, idx in result.notes:
            reviews.append((reason, "occurrence", slots[idx[0]].occ_id if idx else None,
                            {"tracks": [facts[i].track["title"] for i in idx]}))

        used_ids: set[str] = set()
        work_cache: dict[str, str] = {}
        for run in result.runs:
            fs = [facts[i] for i in run.slots]
            c = fs[0].cand
            assert c is not None
            parts = [x.cand.movement_name for x in fs if x.cand and x.cand.movement_name]
            wid = work_cache.get(c.key) or self._work_for(c, aid, reviews, parts)
            work_cache[c.key] = wid
            rid = old_rec.get(fs[0].occ_id)
            if rid is None or rid in used_ids:
                rid = new_id("r")
            used_ids.add(rid)
            recorded = sorted(x.recorded for x in fs if x.recorded)
            rel = next((x.release_year for x in fs if x.release_year), None) or album.year
            total = c.mb_parts or c.movement_total
            nums = [x.cand.movement_no for x in fs if x.cand and x.cand.movement_no is not None]
            complete = bool(total and len(fs) == total and (not nums or sorted(set(nums)) == list(range(1, total + 1))))
            n_fields = sum(bool(v) for v in (recorded, rel, any(x.credits for x in fs), c.mbid, c.catalogues))
            self.con.execute(
                "INSERT INTO recording(id, work_id, album_library_id, recorded_start, recorded_end, "
                "recorded_precision, release_year, tier, complete, evidence_fields) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, wid, aid, recorded[0] if recorded else None, recorded[-1] if recorded else None,
                 ("day" if len(recorded[0]) >= 10 else "month" if len(recorded[0]) >= 7 else "year") if recorded
                 else None, rel, c.tier, int(complete), n_fields))
            for ord_, x in enumerate(fs):
                xc = x.cand
                self.con.execute("INSERT INTO recording_slot(recording_id, occurrence_id, movement_no, movement_name, "
                                 "ord) VALUES (?,?,?,?,?)",
                                 (rid, x.occ_id, xc.movement_no if xc else None,
                                  (xc.movement_name if xc else None) or x.track["title"], ord_))
                self.con.execute("INSERT OR IGNORE INTO work_link(occurrence_id, work_id, ord, partial, live, medley, "
                                 "source, source_ref) VALUES (?,?,?,?,?,?,?,?)",
                                 (x.occ_id, wid, 0, int(x.partial), int(x.live), int(x.medley), c.tier, x.tag_origin))
            stats.recordings += 1
        # multi-work tracks: linked to each work, never grouped, never queued twice
        for f in facts:
            if not f.extra_works:
                continue
            for ord_, wc in enumerate(f.extra_works):
                wid = work_cache.get(wc.key) or self._work_for(wc, aid, reviews)
                work_cache[wc.key] = wid
                self.con.execute("INSERT OR IGNORE INTO work_link(occurrence_id, work_id, ord, partial, live, medley, "
                                 "source, source_ref) VALUES (?,?,?,?,?,?,?,?)",
                                 (f.occ_id, wid, ord_, int(f.partial), int(f.live), int(f.medley), wc.tier, None))
        for f in facts:
            for ord_, p in enumerate(f.credits):
                pid = self._person_for(p)
                self.con.execute("INSERT INTO track_credit(occurrence_id, person_id, role, instrument, ord) "
                                 "VALUES (?,?,?,?,?)", (f.occ_id, pid, p.role,
                                                        p.role if p.kind == "person" and p.role not in
                                                        ("conductor", "performer") else None, ord_))
        for reason, entity, entity_id, detail in reviews:
            self.con.execute("INSERT INTO review_item(id, reason, album_library_id, entity, entity_id, detail_json) "
                             "VALUES (?,?,?,?,?,?)", (new_id("v"), reason, aid, entity, entity_id,
                                                      json.dumps(detail, ensure_ascii=False)))
        stats.occurrences = len(facts)
        stats.review_items = len(reviews)

    def _assign_candidates(self, facts: list[OccFacts], album: AlbumIn,
                           reviews: list[tuple[str, str | None, str | None, dict]]) -> None:
        # MB chains for every occurrence that has one (from a recording link or a work-MBID tag)
        chains: list[list[str] | None] = []
        for f in facts:
            t = f.tags
            chain = None
            if t and t.work_mbid and self._mb_work(t.work_mbid):
                chain, why = self.mb_chain(t.work_mbid)
                if why:
                    reviews.append((f"MusicBrainz: {why}", "occurrence", f.occ_id, {"work": t.work_mbid}))
                    chain = chain[:1]
            elif f.mb and f.mb.works:
                tops = {}
                for link in f.mb.works:
                    ch, why = self.mb_chain(link.work_mbid)
                    if why:
                        reviews.append((f"MusicBrainz: {why}", "occurrence", f.occ_id, {"work": link.work_mbid}))
                        ch = ch[:1]
                    tops.setdefault(ch[-1], ch)
                f.partial = any(w.partial for w in f.mb.works)
                f.live = any(w.live for w in f.mb.works)
                f.medley = any(w.medley for w in f.mb.works)
                if len(tops) > 1:
                    f.extra_works = [self._mb_cand(ch, ch[-1], f) for ch in tops.values()]
                else:
                    chain = next(iter(tops.values()))
            chains.append(chain)

        # grouping boundary: maximal adjacent sequences sharing an ancestor; boundary = their lowest common one
        boundary: list[str | None] = [None] * len(facts)
        i = 0
        while i < len(facts):
            if chains[i] is None:
                i += 1
                continue
            common = set(chains[i])
            j = i + 1
            while j < len(facts) and chains[j] is not None and common & set(chains[j]):
                common &= set(chains[j])
                j += 1
            low = next(w for w in chains[i] if w in common)
            for k in range(i, j):
                boundary[k] = low
            i = j

        # shared-prefix runs for Tier 3
        titles = [f.track["title"] or "" for f in facts]
        prefix_of: dict[int, str] = {}
        for s, e, prefix in shared_prefix_runs(titles):
            for k in range(s, e):
                prefix_of[k] = prefix

        for i, f in enumerate(facts):
            t = f.tags
            f.credits = list(t.performers) if t else []
            f.recorded = t.recorded if t else None
            f.release_year = year_of(t.release_date) if t else None
            mb_cand = self._mb_cand(chains[i], boundary[i], f) if chains[i] and boundary[i] else None
            if f.mb:
                if not f.credits:
                    f.credits = [Person(c.name, c.role, c.kind) for c in f.mb.credits]
                if not f.recorded:
                    f.recorded = f.mb.recorded[0]
            if f.extra_works:
                continue
            tag_cand = self._tag_cand(f, mb_cand) if t and t.work else None
            if tag_cand is not None:
                f.cand = tag_cand
                if mb_cand and tag_cand.mbid is None:
                    reviews.append(("work tag disagrees with MusicBrainz", "occurrence", f.occ_id,
                                    {"tag": t.work, "mb": mb_cand.title}))
            elif mb_cand is not None:
                if t and t.movement_name:
                    mb_cand.movement_name = t.movement_name
                f.cand = mb_cand
            else:
                f.cand = self._title_cand(f, album, prefix_of.get(i))

    def _mb_cand(self, chain: list[str], top: str, f: OccFacts) -> Cand:
        w = self._mb_work(top)
        title = (w.title if w else "") or (f.mb.title if f.mb else "")
        composers = [(c.name, c.mbid) for c in (w.composers if w else [])]
        cats: list[cat.Catalogue] = []
        for _series, num in (w.catalogues if w else []):
            cats += cat.parse_catalogues(num)
        cats.sort(key=lambda c: (c.namespace != "op", c.namespace))
        idx = chain.index(top)
        leaf = chain[0]
        parent = chain[1] if len(chain) > 1 and idx >= 1 else None
        movement_no = None
        movement_name = None
        if parent:
            pw = self._mb_work(parent)
            if pw:
                for p in pw.parts:
                    if p.mbid == leaf:
                        movement_no = p.ordering
                lw = self._mb_work(leaf)
                if lw:
                    rest = lw.title[len(pw.title):].lstrip(" :,-–—") if lw.title.startswith(pw.title) else lw.title
                    n, name = parse_movement(rest)
                    movement_name = name or rest
                    movement_no = movement_no or n
        # prefer the track's own movement wording ("II. Largo") for display
        own = parse_movement(f.track["title"] or "")
        if own[0] is not None and parent:
            movement_name = own[1]
        topw = w
        return Cand(
            key=f"mb:{top}", title=title, tier="mb", composers=composers, mbid=top,
            mb_type=topw.work_type if topw else None, key_sig=topw.key if topw else None, catalogues=cats,
            parent_key=f"mb:{parent}" if parent else None, part_key=f"mb:{leaf}" if parent else None,
            movement_no=movement_no, movement_name=movement_name,
            mb_parts=len(topw.parts) if topw and topw.parts and idx == 1 else None)

    def _tag_cand(self, f: OccFacts, mb_cand: Cand | None) -> Cand:
        t = f.tags
        assert t is not None and t.work
        composers = [(n, None) for n in t.composers]
        if not composers and mb_cand:
            composers = mb_cand.composers
        cats = cat.parse_catalogues(t.work)
        mbid = None
        # attach the MB identity unless catalogues contradict (the tag wins, §5.3)
        if mb_cand and not cat.catalogues_conflict(cats, mb_cand.catalogues):
            mbid = mb_cand.mbid
        ckey = norm_key(composers[0][0]) if composers else ""
        key = f"mb:{mbid}" if mbid else f"tag:{ckey}|{norm_key(t.work)}"
        part = t.movement_name or (str(t.movement_no) if t.movement_no else None)
        return Cand(
            key=key, title=t.work, tier="tags", composers=composers, mbid=mbid,
            mb_type=mb_cand.mb_type if mb_cand and mbid else None, key_sig=cat.parse_key(t.work),
            catalogues=cats or (mb_cand.catalogues if mb_cand and mbid else []),
            parent_key=key, part_key=f"tagpart:{norm_key(part)}" if part else None, movement_no=t.movement_no,
            movement_name=t.movement_name, movement_total=t.movement_total,
            mb_parts=mb_cand.mb_parts if mb_cand and mbid else None)

    def _title_cand(self, f: OccFacts, album: AlbumIn, prefix: str | None) -> Cand | None:
        title = f.track["title"] or ""
        t = f.tags
        parsed = parse_title(title)
        work = movement = None
        number = None
        hint = None
        if prefix:
            work = prefix
            number, movement = parse_movement(title[len(prefix):].lstrip(" :,-–—/"))
        elif parsed:
            work, number, movement, hint = parsed.work, parsed.movement_no, parsed.movement_name, parsed.composer_hint
        composers: list[tuple[str, str | None]] = [(n, None) for n in (t.composers if t else [])]
        if not composers:
            known = (find_in_credit(f.track["artist"]) or find_in_credit(album.artist)
                     or (lookup(hint) if hint else None))
            if known:
                composers = [(known.name, None)]
        if not composers:
            return None  # no composer evidence: not treated as classical (keeps false positives down)
        if work is None:
            # a single-track work: needs a catalogue number or a recognisable form in its title
            if not (cat.parse_catalogues(title) or cat.work_type(title) != "Other"):
                return None
            work = title
        key = f"title:{norm_key(composers[0][0])}|{norm_key(work)}"
        return Cand(key=key, title=work, tier="title", composers=composers, key_sig=cat.parse_key(work),
                    catalogues=cat.parse_catalogues(work), parent_key=key,
                    part_key=f"titlepart:{number}:{norm_key(movement)}" if (movement or number) else None,
                    movement_no=number, movement_name=movement)

    # -- identity --

    def _composer_for(self, name: str, mbid: str | None) -> str:
        known = lookup(name)
        canonical = known.name if known else name
        cid = self._alias("composer", "mbid", mbid) if mbid else None
        cid = cid or self._alias("composer", "name", norm_key(canonical)) or self._alias("composer", "name",
                                                                                         norm_key(name))
        if cid is None:
            cid = new_id("c")
            self.con.execute("INSERT INTO composer(id, name, sort_name, born, died) VALUES (?,?,?,?,?)",
                             (cid, canonical, sort_name(canonical),
                              str(known.born) if known and known.born else None,
                              str(known.died) if known and known.died else None))
        self._add_alias("composer", cid, "name", norm_key(canonical))
        self._add_alias("composer", cid, "name", norm_key(name))
        if mbid:
            self._add_alias("composer", cid, "mbid", mbid)
        return cid

    def _person_for(self, p: Person) -> str:
        pid = self._alias("person", "name", norm_key(p.name))
        if pid is None:
            pid = new_id("p")
            kind = "person" if p.kind == "person" else {"orchestra": "orchestra", "chorus": "choir"}.get(p.role, "other")
            self.con.execute("INSERT INTO person(id, name, sort_name, kind) VALUES (?,?,?,?)",
                             (pid, p.name, sort_name(p.name, "group" if p.kind == "group" else "person"), kind))
            self._add_alias("person", pid, "name", norm_key(p.name))
        return pid

    def _work_for(self, c: Cand, album_id: str, reviews: list, part_names: list[str] | None = None) -> str:
        cids = [self._composer_for(n, m) for n, m in c.composers]
        primary = c.catalogues[0] if c.catalogues else None
        wid = None
        if c.mbid:
            wid = self._alias("work", "mbid", c.mbid)                        # merge rule (1)
        elif c.tier == "tags" and cids and primary:
            akey = f"{cids[0]}|{primary.key}"
            cand = self._alias("work", "tagcat", akey)                         # merge rule (2)
            if cand:
                w = self._one("SELECT * FROM work WHERE id=?", (cand,))
                has_mb = self._one("SELECT 1 FROM alias WHERE entity='work' AND entity_id=? AND kind='mbid'", (cand,))
                fk_a, fk_b = form_key(w["title"]), form_key(c.title)
                if has_mb or (w["key"] and c.key_sig and w["key"] != c.key_sig) or (fk_a and fk_b and fk_a != fk_b):
                    reviews.append(("merge candidate with a conflict", "work", cand, {"title": c.title}))
                else:
                    wid = cand
            if wid is None:
                for r in self._q("SELECT entity_id, value FROM alias WHERE entity='work' AND kind='tagcat' "
                                 "AND value LIKE ?", (f"{cids[0]}|{primary.namespace}:%",)):
                    other = cat.Catalogue(primary.namespace, tuple(r["value"].split(":", 1)[1].split("/")))
                    if other.is_prefix_of(primary) or primary.is_prefix_of(other):
                        reviews.append(("collection-level catalogue match", "work", r["entity_id"],
                                        {"title": c.title, "catalogue": primary.display}))
        if wid is None and not c.mbid and not (c.tier == "tags" and cids and primary):
            wid = self._alias("work", "local", f"{album_id}|{c.key}")
        if wid is None:
            wid = new_id("w")
            self.con.execute("INSERT INTO work(id, title, sort_title, work_type, identity_tier, created_at) "
                             "VALUES (?,?,?,?,?,?)", (wid, c.title, norm_key(c.title), "Other", c.tier, time.time()))
            # cross-album candidates by title are listed, never merged (§5.5)
            for r in self._q("SELECT w.id FROM work w JOIN work_composer wc ON wc.work_id=w.id "
                             "WHERE w.sort_title=? AND wc.composer_id=? AND w.id<>?",
                             (norm_key(c.title), cids[0] if cids else "", wid)):
                reviews.append(("possible same work on another album", "work", wid, {"other": r[0], "title": c.title}))
        if c.mbid:
            self._add_alias("work", wid, "mbid", c.mbid)
        if c.tier == "tags" and cids and primary and not c.mbid:
            self._add_alias("work", wid, "tagcat", f"{cids[0]}|{primary.key}")
        if not c.mbid and not (c.tier == "tags" and cids and primary):
            self._add_alias("work", wid, "local", f"{album_id}|{c.key}")
        for pa in c.part_aliases:
            self._add_alias("work", wid, "part_mbid", pa)
        for ord_, cid in enumerate(cids):
            self.con.execute("INSERT OR IGNORE INTO work_composer(work_id, composer_id, ord) VALUES (?,?,?)",
                             (wid, cid, ord_))
        # derived descriptive fields (never the display title: first spelling wins)
        wtype = cat.work_type(c.title, c.mb_type, part_names)
        cat_sort = None
        if primary:
            cat_sort = primary.namespace + ":" + "/".join(x.zfill(6) for x in primary.components)
        self.con.execute("UPDATE work SET work_type=?, key=COALESCE(?, key), catalogue_display=COALESCE(?, "
                         "catalogue_display), catalogue_sort=COALESCE(?, catalogue_sort) WHERE id=?",
                         (wtype, c.key_sig, primary.display if primary else None, cat_sort, wid))
        return wid

    # ---- reads (all filtered by visible provider instances) -------------------------------------------

    _VIS_OCC = ("o.present=1 AND EXISTS (SELECT 1 FROM mapping_state m WHERE m.library_track_id=o.library_track_id "
                "AND m.status IN ('ok','read_error') AND (:vis IS NULL OR m.provider_instance IN (SELECT value FROM json_each(:vis))) "
                "AND m.provider_instance NOT IN (SELECT provider_instance FROM provider_seen WHERE state<>'configured'))")
    _VIS_REC = ("NOT EXISTS (SELECT 1 FROM recording_slot s2 JOIN occurrence o ON o.id=s2.occurrence_id "
                "WHERE s2.recording_id=r.id AND NOT (" + _VIS_OCC + "))")

    @staticmethod
    def _vis(vis: Iterable[str] | None) -> dict:
        return {"vis": None if vis is None else json.dumps(sorted(vis))}

    def composers(self, vis=None) -> list[dict]:
        rows = self._q(
            "SELECT c.id, c.name, c.sort_name, c.born, c.died, COUNT(DISTINCT r.work_id) AS works "
            "FROM composer c JOIN work_composer wc ON wc.composer_id=c.id JOIN recording r ON r.work_id=wc.work_id "
            f"WHERE {self._VIS_REC} GROUP BY c.id ORDER BY c.sort_name", self._vis(vis))
        return [dict(r) for r in rows]

    def composer(self, cid: str) -> dict | None:
        r = self._one("SELECT * FROM composer WHERE id=?", (self.resolve("composer", cid),))
        return dict(r) if r else None

    def composer_types(self, cid: str, vis=None) -> list[tuple[str, int]]:
        rows = self._q(
            "SELECT w.work_type, COUNT(DISTINCT w.id) FROM work w JOIN work_composer wc ON wc.work_id=w.id "
            f"JOIN recording r ON r.work_id=w.id WHERE wc.composer_id=:cid AND {self._VIS_REC} GROUP BY w.work_type",
            {"cid": self.resolve("composer", cid), **self._vis(vis)})
        order = {t: i for i, t in enumerate(cat.WORK_TYPES)}
        return sorted(((r[0], r[1]) for r in rows), key=lambda x: order.get(x[0], 99))

    def works(self, cid: str | None = None, work_type: str | None = None, vis=None,
              limit: int | None = None, offset: int = 0) -> list[dict]:
        sql = ("SELECT w.id, w.title, w.work_type, w.key, w.catalogue_display, w.identity_tier, "
               "COUNT(DISTINCT r.id) AS recordings, "
               "(SELECT c.name FROM work_composer wc2 JOIN composer c ON c.id=wc2.composer_id "
               " WHERE wc2.work_id=w.id ORDER BY wc2.ord LIMIT 1) AS composer "
               "FROM work w JOIN recording r ON r.work_id=w.id "
               f"WHERE {self._VIS_REC} "
               "AND (:cid IS NULL OR EXISTS (SELECT 1 FROM work_composer wc WHERE wc.work_id=w.id AND wc.composer_id=:cid)) "
               "AND (:wt IS NULL OR w.work_type=:wt) GROUP BY w.id "
               "ORDER BY w.catalogue_sort IS NULL, w.catalogue_sort, w.sort_title")
        args = {"cid": self.resolve("composer", cid) if cid else None, "wt": work_type, **self._vis(vis)}
        if limit:
            sql += " LIMIT :lim OFFSET :off"
            args.update(lim=limit, off=offset)
        return [dict(r) for r in self._q(sql, args)]

    def count_works(self, cid: str | None, work_type: str | None, vis=None) -> int:
        return len(self.works(cid, work_type, vis))

    def work(self, wid: str) -> dict | None:
        r = self._one("SELECT * FROM work WHERE id=?", (self.resolve("work", wid),))
        if not r:
            return None
        d = dict(r)
        d["composers"] = [dict(x) for x in self._q(
            "SELECT c.id, c.name FROM work_composer wc JOIN composer c ON c.id=wc.composer_id WHERE wc.work_id=? "
            "ORDER BY wc.ord", (d["id"],))]
        return d

    def recordings(self, wid: str, vis=None) -> list[dict]:
        rows = self._q(
            "SELECT r.*, a.name AS album, a.artist AS album_artist, a.year AS album_year, a.image_json, "
            "(SELECT COALESCE(SUM(t.duration),0) FROM recording_slot s JOIN occurrence o ON o.id=s.occurrence_id "
            " JOIN track_info t ON t.library_track_id=o.library_track_id WHERE s.recording_id=r.id) AS duration, "
            "(SELECT COUNT(*) FROM recording_slot s WHERE s.recording_id=r.id) AS tracks, "
            "(SELECT t.artist FROM recording_slot s JOIN occurrence o ON o.id=s.occurrence_id "
            " JOIN track_info t ON t.library_track_id=o.library_track_id WHERE s.recording_id=r.id "
            " ORDER BY s.ord LIMIT 1) AS track_artist, "
            "(SELECT MIN(COALESCE(o.disc,1)*10000+COALESCE(o.position,0)) FROM recording_slot s "
            " JOIN occurrence o ON o.id=s.occurrence_id WHERE s.recording_id=r.id) AS first_pos, "
            "(SELECT GROUP_CONCAT(c.name, ';') FROM work_composer wc JOIN composer c ON c.id=wc.composer_id "
            " WHERE wc.work_id=r.work_id) AS composer_names "
            "FROM recording r LEFT JOIN album_info a ON a.album_library_id=r.album_library_id "
            f"WHERE r.work_id=:wid AND {self._VIS_REC} "
            "ORDER BY r.recorded_start IS NULL, r.recorded_start, r.album_library_id, first_pos",
            {"wid": self.resolve("work", wid), **self._vis(vis)})
        out = []
        for r in rows:
            d = dict(r)
            d["credits"] = self.recording_credits(d["id"])
            d["label"] = recording_label(d)
            d["image"] = json.loads(d.pop("image_json")) if d.get("image_json") else None
            out.append(d)
        return out

    def recording(self, rid: str, vis=None) -> dict | None:
        r = self._one(f"SELECT r.* FROM recording r WHERE r.id=:rid AND {self._VIS_REC}", {"rid": rid, **self._vis(vis)})
        if not r:
            return None
        for d in self.recordings(r["work_id"], vis):
            if d["id"] == rid:
                return d
        return None

    def recording_credits(self, rid: str) -> list[dict]:
        rows = self._q(
            "SELECT p.id, p.name, p.kind, tc.role, MIN(s.ord * 100 + tc.ord) AS o FROM recording_slot s "
            "JOIN track_credit tc ON tc.occurrence_id=s.occurrence_id JOIN person p ON p.id=tc.person_id "
            "WHERE s.recording_id=? GROUP BY p.id, tc.role ORDER BY o", (rid,))
        return [dict(r) for r in rows]

    def recording_tracks(self, rid: str, vis=None) -> list[dict]:
        rows = self._q(
            "SELECT s.ord, s.movement_no, s.movement_name, o.library_track_id, o.id AS occurrence_id, t.title, "
            f"t.duration FROM recording_slot s JOIN occurrence o ON o.id=s.occurrence_id "
            "LEFT JOIN track_info t ON t.library_track_id=o.library_track_id "
            f"WHERE s.recording_id=:rid AND {self._VIS_OCC} ORDER BY s.ord", {"rid": rid, **self._vis(vis)})
        return [dict(r) for r in rows]

    def contained_tracks(self, wid: str, vis=None) -> list[dict]:
        """Tracks that perform this work but sit in no recording (multi-work tracks, §4)."""
        rows = self._q(
            "SELECT o.library_track_id, o.id AS occurrence_id, t.title FROM work_link l "
            "JOIN occurrence o ON o.id=l.occurrence_id LEFT JOIN track_info t ON t.library_track_id=o.library_track_id "
            "WHERE l.work_id=:wid AND NOT EXISTS (SELECT 1 FROM recording_slot s WHERE s.occurrence_id=o.id) "
            f"AND {self._VIS_OCC}", {"wid": self.resolve("work", wid), **self._vis(vis)})
        return [dict(r) for r in rows]

    def occurrence_track(self, oid: str) -> str | None:
        r = self._one("SELECT library_track_id FROM occurrence WHERE id=? AND present=1", (oid,))
        return r[0] if r else None

    def album_ids(self) -> list[str]:
        return [r[0] for r in self._q("SELECT album_library_id FROM album_info ORDER BY album_library_id")]

    def albums_of_track(self, library_track_id: str) -> list[str]:
        return [r[0] for r in self._q("SELECT DISTINCT album_library_id FROM occurrence WHERE library_track_id=?",
                                      (library_track_id,))]

    def recently_added_works(self, vis=None, limit: int = 20) -> list[dict]:
        works = self.works(vis=vis)
        works.sort(key=lambda w: self._one("SELECT created_at FROM work WHERE id=?", (w["id"],))[0], reverse=True)
        return works[:limit]

    # -- provider presence (§5.6) --

    def provider_instances(self) -> list[str]:
        return [r[0] for r in self._q("SELECT provider_instance FROM mapping_state UNION "
                                      "SELECT provider_instance FROM provider_seen")]

    def provider_state(self, pi: str) -> str | None:
        r = self._one("SELECT state FROM provider_seen WHERE provider_instance=?", (pi,))
        return r[0] if r else None

    def set_provider_state(self, pi: str, state: str) -> bool:
        """Record a provider instance's presence; returns True when it changed."""
        if self.provider_state(pi) == state:
            return False
        self.con.execute("INSERT INTO provider_seen(provider_instance, state, last_change) VALUES (?,?,?) "
                         "ON CONFLICT(provider_instance) DO UPDATE SET state=excluded.state, "
                         "last_change=excluded.last_change", (pi, state, time.time()))
        return True

    def retract_provider(self, pi: str) -> list[str]:
        """Final removal of a provider instance: every mapping of it is gone and its evidence retracted.
        Returns the albums to re-derive (their signature is cleared so the next index pass runs)."""
        albums = [r[0] for r in self._q(
            "SELECT DISTINCT o.album_library_id FROM mapping_state m JOIN occurrence o "
            "ON o.library_track_id=m.library_track_id WHERE m.provider_instance=? AND m.status<>'gone'", (pi,))]
        self.con.execute("BEGIN IMMEDIATE")
        try:
            for r in self._q("SELECT item_id FROM mapping_state WHERE provider_instance=? AND status<>'gone'", (pi,)):
                self.mark_gone(pi, r[0])
            self.con.execute("UPDATE album_info SET signature=NULL WHERE album_library_id IN "
                             "(SELECT value FROM json_each(?))", (json.dumps(albums),))
            self.set_provider_state(pi, "removed")
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return albums

    def review(self, vis=None) -> list[dict]:
        rows = self._q(
            "SELECT v.* FROM review_item v WHERE v.album_library_id IS NULL OR EXISTS (SELECT 1 FROM occurrence o "
            f"WHERE o.album_library_id=v.album_library_id AND {self._VIS_OCC}) ORDER BY v.reason, v.id", self._vis(vis))
        return [dict(r) | {"detail": json.loads(r["detail_json"])} for r in rows]

    def best_recording(self, wid: str, vis=None) -> tuple[dict, str] | None:
        """Fixed ranking (§6): complete set, confirmed, most evidence fields, lowest id."""
        recs = self.recordings(wid, vis)
        if not recs:
            return None
        recs.sort(key=lambda r: (-r["complete"], r["status"] != "confirmed", -r["evidence_fields"], r["id"]))
        r = recs[0]
        reason = ("complete movement set" if r["complete"] else "most evidence") if len(recs) > 1 else "only recording"
        return r, reason

    def search(self, query: str, kind: str = "work", vis=None, limit: int = 25) -> list[dict]:
        q = fold(query)
        words = [w for w in re.split(r"\s+", norm_key(q)) if w]
        if kind == "composer":
            return [c for c in self.composers(vis) if all(w in norm_key(c["name"]) for w in words)][:limit]
        if kind == "recording":
            out = []
            for w in self.works(vis=vis):
                for r in self.recordings(w["id"], vis):
                    hay = norm_key(f"{w['title']} {w['composer'] or ''} {r['label']} {r.get('album') or ''}")
                    if all(x in hay for x in words):
                        out.append({**r, "work_title": w["title"], "composer": w["composer"]})
            return out[:limit]
        works = self.works(vis=vis)
        return [w for w in works if all(x in norm_key(f"{w['title']} {w['composer'] or ''} "
                                                      f"{w['catalogue_display'] or ''}") for x in words)][:limit]

    def status(self) -> dict:
        def n(sql: str) -> int:
            return self._one(sql)[0]

        return {
            "schema_version": int(self._one("SELECT value FROM meta WHERE key='schema_version'")[0]),
            "albums": n("SELECT COUNT(*) FROM album_info"), "occurrences": n("SELECT COUNT(*) FROM occurrence WHERE present=1"),
            "composers": n("SELECT COUNT(DISTINCT wc.composer_id) FROM work_composer wc JOIN recording r ON r.work_id=wc.work_id"),
            "works": n("SELECT COUNT(DISTINCT work_id) FROM recording"), "recordings": n("SELECT COUNT(*) FROM recording"),
            "review_items": n("SELECT COUNT(*) FROM review_item"),
            "mappings": {r[0]: r[1] for r in self._q("SELECT status, COUNT(*) FROM mapping_state GROUP BY status")},
            "evidence_live": n("SELECT COUNT(*) FROM evidence WHERE retracted_at IS NULL"),
        }


def album_signature(album: AlbumIn) -> str:
    """What an album's indexing depends on from MA's side; unchanged signature = nothing to do."""
    import hashlib

    data = [album.name, album.artist, album.year, album.image,
            sorted([t.library_track_id, t.disc, t.position, t.title, t.artist, t.duration, t.mbid,
                    sorted([m.provider_instance, m.item_id, m.fingerprint, m.path is not None, m.available]
                           for m in t.mappings)] for t in album.tracks)]
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _fmt_duration(sec: int) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def recording_label(r: dict) -> str:
    """'Herbert von Karajan / Berliner Philharmoniker (1962) [32:00]' (§6). With only release evidence the
    year reads '(rel. 2010)'; the soloist or ensemble stands in when there is no conductor."""
    credits = r.get("credits") or []
    conductor = next((c["name"] for c in credits if c["role"] == "conductor"), None)
    group = next((c["name"] for c in credits if c["role"] in ("orchestra", "chorus", "ensemble")), None)
    soloist = next((c["name"] for c in credits if c["role"] not in ("conductor", "orchestra", "chorus", "ensemble")),
                   None)
    names = [n for n in (conductor, group) if n] if conductor else [n for n in (soloist, group) if n]
    who = " / ".join(names)
    if not who:
        # no performer credits: the track or album artist, unless that is just the composer again
        composers = {norm_key(c) for c in (r.get("composer_names") or "").split(";") if c}
        for cand in (r.get("track_artist"), r.get("album_artist")):
            if cand and norm_key(cand) not in composers and norm_key(cand) not in ("various artists", "various"):
                who = cand
                break
        who = who or r.get("album") or "Unknown performers"
    if r.get("recorded_start"):
        when = f" ({r['recorded_start'][:4]})"
    elif r.get("release_year"):
        when = f" (rel. {r['release_year']})"
    else:
        when = ""
    dur = f" [{_fmt_duration(r['duration'])}]" if r.get("duration") else ""
    return f"{who}{when}{dur}"
