#!/usr/bin/env python3
"""Snapshot a Classical index.db as JSON, for comparing an index before and after a deploy (docs/deploy.md).

    python scripts/snapshot.py path/to/index.db [-o out.json] [--copy-to backup.db]

Offline and read-only: the database is opened with mode=ro (or immutable=1 when no -wal/-shm file is beside it,
so a backup copy gets none). Standard library only, so it runs under any Python 3.10+ (the M1's
MA venv included) without the package installed.

Derived ids (recording, and work ids after a re-derive) change between runs, so everything is keyed by what
stays put: MA's album id and library track ids. A recording is "<album>:<track>,<track>..." over the tracks it
was derived from (its home slots, so a cross-disc join does not change it); a track is "<album>:<track>".
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

SNAPSHOT_VERSION = 1


def connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit(f"no such database: {path}")
    live = os.path.exists(path + "-wal") or os.path.exists(path + "-shm")
    mode = "mode=ro" if live else "immutable=1"
    con = sqlite3.connect(f"file:{path}?{mode}", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def copy_db(src: str, dest: str) -> None:
    """A consistent copy of a live index (WAL included) with SQLite's backup API; the source is only read."""
    if os.path.exists(dest):
        raise SystemExit(f"refusing to overwrite {dest}")
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
    finally:
        d.close()
        s.close()


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def snapshot(con: sqlite3.Connection) -> dict:
    tables = _tables(con)
    rcols = _cols(con, "recording")
    scols = _cols(con, "recording_slot")
    acols = _cols(con, "album_info")
    q = con.execute
    one = lambda sql, args=(): q(sql, args).fetchone()[0]

    meta = {r["key"]: r["value"] for r in q("SELECT key, value FROM meta")} if "meta" in tables else {}

    # composers per work, in credit order (a name, or the id when a row has no name)
    work_comp: dict[str, list[str]] = {}
    for r in q("SELECT wc.work_id, c.id, c.name FROM work_composer wc JOIN composer c ON c.id=wc.composer_id "
               "ORDER BY wc.work_id, wc.ord, c.name"):
        work_comp.setdefault(r["work_id"], []).append((r["name"] or "").strip() or r["id"])
    comp_ids: dict[str, set[str]] = {}
    for r in q("SELECT c.id, c.name FROM composer c"):
        comp_ids.setdefault((r["name"] or "").strip() or r["id"], set()).add(r["id"])
    work_title = {r["id"]: r["title"] for r in q("SELECT id, title FROM work")}

    # tracks of each recording, from the slots it was derived into (home_id survives cross-disc joins)
    home = "COALESCE(s.home_id, s.recording_id)" if "home_id" in scols else "s.recording_id"
    rec_tracks: dict[str, list[str]] = {}
    for r in q(f"SELECT {home} rid, o.library_track_id t FROM recording_slot s "
               "JOIN occurrence o ON o.id=s.occurrence_id ORDER BY 1, o.disc, o.position, o.library_track_id"):
        rec_tracks.setdefault(r["rid"], []).append(r["t"])

    joined = "joined_to" in rcols
    recs = {}
    for r in q(f"SELECT id, work_id, album_library_id, {'joined_to' if joined else 'NULL'} joined_to FROM recording"):
        tracks = rec_tracks.get(r["id"], [])
        key = f"{r['album_library_id']}:{','.join(tracks)}" if tracks else f"{r['album_library_id']}:#{r['work_id']}"
        recs[r["id"]] = {"key": key, "album": r["album_library_id"], "work": r["work_id"], "tracks": tracks,
                         "joined_to": r["joined_to"]}

    albums: dict[str, dict] = {}
    box_col = "box_id" if "box_id" in acols else "NULL"
    for r in q(f"SELECT album_library_id, name, artist, indexed_at, {box_col} box_id FROM album_info"):
        albums[r["album_library_id"]] = {"name": r["name"], "artist": r["artist"],
                                         "indexed": r["indexed_at"] is not None, "composers": {}}

    composers: dict[str, dict] = {}
    tracks_out: dict[str, dict] = {}
    for rec in recs.values():
        title = work_title.get(rec["work"], "?")
        names = work_comp.get(rec["work"]) or ["?"]
        alb = albums.setdefault(rec["album"], {"name": None, "artist": None, "indexed": False, "composers": {}})
        for n in names:
            alb["composers"].setdefault(n, set()).add(title)
            c = composers.setdefault(n, {"ids": sorted(comp_ids.get(n, ())), "works": {}})
            c["works"][rec["key"]] = title
        for t in rec["tracks"]:
            tracks_out[f"{rec['album']}:{t}"] = {"rec": rec["key"], "work": title, "composers": sorted(names)}
    for alb in albums.values():
        alb["composers"] = {n: sorted(ts) for n, ts in sorted(alb["composers"].items())}

    joins = sorted([rec["key"], recs[rec["joined_to"]]["key"] if rec["joined_to"] in recs else rec["joined_to"]]
                   for rec in recs.values() if rec["joined_to"])

    accolades = []
    if "accolade" in tables:
        for r in q("SELECT * FROM accolade"):
            rec = recs.get(r["recording_id"]) if r["recording_id"] else None
            accolades.append({
                "album": r["album_library_id"], "source": r["source"], "source_ref": r["source_ref"],
                "kind": r["kind"], "award": r["award"], "category": r["category"], "year": r["year"],
                "publication": r["publication"], "reviewer": r["reviewer"], "target": r["target"],
                "grade": r["grade"], "recording": rec["key"] if rec else None,
                "work": work_title.get(r["work_id"]) if r["work_id"] else (work_title.get(rec["work"]) if rec else None),
            })
        accolades.sort(key=lambda a: json.dumps(a, sort_keys=True, default=str))

    review = {r[0]: r[1] for r in q("SELECT reason, COUNT(*) FROM review_item GROUP BY reason ORDER BY reason")}
    used_works = "SELECT DISTINCT work_id FROM recording"
    wcols = _cols(con, "work")
    dated = (one(f"SELECT COUNT(*) FROM work WHERE id IN ({used_works}) AND "
                 "(composed_start IS NOT NULL" + (" OR premiered IS NOT NULL)" if "premiered" in wcols else ")")))
    mb_linked = one(f"SELECT COUNT(DISTINCT entity_id) FROM alias WHERE entity='work' AND kind='mbid' "
                    f"AND entity_id IN ({used_works})")
    counts = {
        "albums": one("SELECT COUNT(*) FROM album_info"),
        "albums_indexed": sum(1 for a in albums.values() if a["indexed"]),
        "composers_in_use": len(composers),
        "works_in_use": one(f"SELECT COUNT(*) FROM ({used_works})"),
        "recordings": len(recs),
        "recordings_visible": sum(1 for r in recs.values() if not r["joined_to"]),
        "tracks_in_recordings": len(tracks_out),
        "joins": len(joins),
        "boxes": one(f"SELECT COUNT(*) FROM (SELECT {box_col} b FROM album_info WHERE b IS NOT NULL "
                     "GROUP BY b HAVING COUNT(*) > 1)"),
        "accolades": len(accolades),
        "accolades_shown": sum(1 for a in accolades if a["grade"] != "uncertain"),
        "dated_works": dated,
        "mb_linked_works": mb_linked,
        "review_items": sum(review.values()),
    }
    return {
        "snapshot_version": SNAPSHOT_VERSION, "taken_at": time.time(), "schema_version": meta.get("schema_version"),
        "counts": counts, "review_by_reason": review,
        "composers": dict(sorted(composers.items())),
        "albums": dict(sorted(albums.items())),
        "tracks": dict(sorted(tracks_out.items())),
        "joins": joins, "accolades": accolades,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    ap.add_argument("--summary", action="store_true", help="print the counts to stderr too")
    ap.add_argument("--copy-to", metavar="PATH", help="first copy the db here (backup API) and snapshot the copy")
    a = ap.parse_args(argv)
    if a.copy_to:
        copy_db(a.db, a.copy_to)
    snap = snapshot(connect(a.copy_to or a.db))
    snap["db"] = os.path.basename(a.db)
    text = json.dumps(snap, ensure_ascii=False, indent=None, sort_keys=False, default=str)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text + "\n")
    if a.summary:
        print(json.dumps(snap["counts"], indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
