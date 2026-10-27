"""scripts/snapshot.py and scripts/compare_snapshots.py on indexes built from the fixture library (docs/deploy.md)."""

import asyncio
import importlib.util
import json
import sqlite3
import time

import pytest

from classical_core.index import Index
from classical_core.store import SqliteMBCache, connect, migrate
from tests.helpers import ROOT, fake_library, offline_mb


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


snapshot = _load("snapshot")
compare_snapshots = _load("compare_snapshots")


def build(path, albums):
    con = connect(path)
    migrate(con)
    idx = Index(con, offline_mb(cache=SqliteMBCache(con)))
    for a in albums:
        asyncio.run(idx.index_album(a))
    con.commit()
    return con


def copy(con, path):
    """A consistent copy of a live index (what deploy_m1.sh takes with the backup API)."""
    con.commit()
    dst = sqlite3.connect(path)
    con.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.close()
    return path


def snap(path):
    return json.loads(json.dumps(snapshot.snapshot(snapshot.connect(str(path)))))


@pytest.fixture(scope="module")
def albums(library):
    return fake_library(library)


@pytest.fixture
def base(tmp_path, albums):
    con = build(tmp_path / "index.db", albums)
    yield con, snap(copy(con, tmp_path / "before.db"))
    con.close()


def regressions(before, after, **kw):
    res = compare_snapshots.compare(before, after, **kw)
    return res, {k for k in res["findings"] if k in compare_snapshots.REGRESSIONS}


def test_snapshot_counts_and_keys(base):
    _, s = base
    c = s["counts"]
    assert c["albums"] == c["albums_indexed"] == 10
    assert c["composers_in_use"] >= 3 and c["recordings"] >= c["recordings_visible"] > 0
    assert "Ludwig van Beethoven" in s["composers"]
    for comp in s["composers"].values():          # every key is album:track ids, never a derived id
        for key in comp["works"]:
            alb, tracks = key.split(":", 1)
            assert alb in s["albums"] and tracks and not tracks.startswith(("r_", "#"))
    assert c["review_items"] == sum(s["review_by_reason"].values())


def test_snapshot_does_not_touch_the_db(tmp_path, base):
    con, _ = base
    p = copy(con, tmp_path / "ro.db")
    before = p.read_bytes()
    snap(p)
    assert p.read_bytes() == before
    assert not (tmp_path / "ro.db-wal").exists() and not (tmp_path / "ro.db-shm").exists()


def test_same_index_and_rederive_are_clean(tmp_path, base, albums):
    con, before = base
    res, bad = regressions(before, snap(copy(con, tmp_path / "same.db")))
    assert not bad and not res["findings"]
    # a fresh index of the same library: every derived id is new, nothing is a regression
    con2 = build(tmp_path / "fresh.db", albums)
    res, bad = regressions(before, snap(copy(con2, tmp_path / "fresh-copy.db")))
    assert not bad, res["findings"]


def test_library_import_is_growth_not_regression(tmp_path, albums):
    con = build(tmp_path / "index.db", albums[:7])
    before = snap(copy(con, tmp_path / "b.db"))
    idx = Index(con, offline_mb(cache=SqliteMBCache(con)))
    for a in albums[7:]:
        asyncio.run(idx.index_album(a))
    after = snap(copy(con, tmp_path / "a.db"))
    res, bad = regressions(before, after)
    assert not bad, res["findings"]
    assert res["existing_albums"] == 7 and res["growth"]["new_albums"] == 3
    assert res["growth"]["new_album_tracks_in_recordings"] > 0


def _collapse(con, names, into="Николай Карлович Метнер"):
    """What the bad deploy did: MB names in Cyrillic folded to an empty key, so every such composer resolved to
    the first one seen. The re-derive made new work rows credited to that composer and moved the recordings."""
    cid = "c_medtner"
    con.execute("INSERT INTO composer(id, name, sort_name) VALUES (?,?,?)", (cid, into, into))
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM composer WHERE name IN ({','.join('?' * len(names))})", names)]
    assert len(ids) == len(names)
    for wid, title in con.execute(f"SELECT DISTINCT w.id, w.title FROM work w JOIN work_composer wc ON wc.work_id=w.id "
                                  f"WHERE wc.composer_id IN ({','.join('?' * len(ids))})", ids).fetchall():
        new = wid + "_x"
        con.execute("INSERT INTO work(id, title, sort_title, created_at) VALUES (?,?,?,?)",
                    (new, title, title, time.time()))
        con.execute("INSERT INTO work_composer(work_id, composer_id) VALUES (?,?)", (new, cid))
        con.execute("UPDATE recording SET work_id=? WHERE work_id=?", (new, wid))
    con.commit()


def test_medtner_collapse_is_caught(tmp_path, base):
    con, before = base
    victims = ["Antonín Dvořák", "Johannes Brahms", "Wolfgang Amadeus Mozart"]
    assert all(v in before["composers"] for v in victims)
    _collapse(con, victims)
    after = snap(copy(con, tmp_path / "after.db"))
    res, bad = regressions(before, after)
    assert {"composer_merge", "composer_gone", "composer_changed", "works_lost"} <= bad
    [merge] = res["findings"]["composer_merge"]
    assert merge["into"] == "Николай Карлович Метнер" and set(merge["from"]) == set(victims)
    assert {g["composer"] for g in res["findings"]["composer_gone"]} == set(victims)
    assert res["findings"]["non_latin_composer"] == [{"composer": "Николай Карлович Метнер"}]
    assert "recordings_lost" not in bad        # the tracks are still there, just under the wrong composer


def test_cli_exit_codes_and_warn_only(tmp_path, base, capsys):
    con, before = base
    _collapse(con, ["Antonín Dvořák", "Johannes Brahms"])
    after = snap(copy(con, tmp_path / "after.db"))
    b, a = tmp_path / "b.json", tmp_path / "a.json"
    b.write_text(json.dumps(before))
    a.write_text(json.dumps(after))
    assert compare_snapshots.main([str(b), str(b)]) == 0
    assert "RESULT: OK" in capsys.readouterr().out
    assert compare_snapshots.main([str(b), str(a)]) == 1
    out = capsys.readouterr().out
    assert "REGRESSION: composer_merge" in out and "Antonín Dvořák" in out
    cats = "composer_merge,composer_gone,composer_changed,works_lost"
    assert compare_snapshots.main([str(b), str(a), "--warn-only", cats]) == 0
    capsys.readouterr()
    assert compare_snapshots.main([str(b), str(a), "--json"]) == 1
    assert "composer_merge" in json.loads(capsys.readouterr().out)["regressions"]
    assert compare_snapshots.main([str(b), str(tmp_path / "missing.json")]) == 2
    assert snapshot.main([str(tmp_path / "after.db"), "-o", str(tmp_path / "s.json")]) == 0
    assert json.loads((tmp_path / "s.json").read_text())["counts"] == after["counts"]


def test_lost_recordings_joins_accolades_reviews(tmp_path, base):
    con, _ = base
    rid, alb, wid = con.execute("SELECT id, album_library_id, work_id FROM recording WHERE joined_to IS NULL "
                                "ORDER BY id LIMIT 1").fetchone()
    other, alb2 = con.execute("SELECT id, album_library_id FROM recording WHERE album_library_id<>? AND "
                              "joined_to IS NULL ORDER BY id LIMIT 1", (alb,)).fetchone()
    third = con.execute("SELECT id FROM recording WHERE id NOT IN (?,?) AND joined_to IS NULL ORDER BY id LIMIT 1",
                        (rid, other)).fetchone()[0]
    acc = ("INSERT INTO accolade(id, target, recording_id, album_library_id, work_id, kind, award, year, source, "
           "source_ref, grade, fetched_at) VALUES (?, 'recording', ?, ?, ?, 'award', 'Gramophone', 1990, 'dataset', ?, "
           "'this_release', 0)")
    con.execute(acc, ("a1", rid, alb, wid, "e1"))
    con.execute(acc, ("a2", other, alb2, None, "e2"))
    con.execute("UPDATE recording SET joined_to=? WHERE id=?", (other, third))
    con.executemany("INSERT INTO review_item(id, reason, detail_json) VALUES (?, 'odd thing', '{}')",
                    [(f"ri{i}",) for i in range(4)])
    before = snap(copy(con, tmp_path / "b.db"))
    assert before["counts"]["joins"] == 1 and before["counts"]["accolades"] == 2

    con.execute("DELETE FROM recording_slot WHERE recording_id=? OR home_id=?", (rid, rid))   # recording lost
    con.execute("DELETE FROM recording WHERE id=?", (rid,))
    con.execute("DELETE FROM accolade WHERE id='a1'")                                       # accolade lost
    con.execute("UPDATE accolade SET recording_id=? WHERE id='a2'", (third,))               # accolade moved
    con.execute("UPDATE recording SET joined_to=NULL WHERE id=?", (third,))                 # join removed
    con.executemany("INSERT INTO review_item(id, reason, detail_json) VALUES (?, 'odd thing', '{}')",
                    [(f"rj{i}",) for i in range(20)])
    con.commit()
    res, bad = regressions(before, snap(copy(con, tmp_path / "a.db")))
    assert {"recordings_lost", "accolades_lost", "accolades_moved", "joins_removed"} <= bad
    assert res["findings"]["review_jump"] == [{"reason": "odd thing", "before": 4, "after": 24}]
    res, bad = regressions(before, snap(copy(con, tmp_path / "a.db")), review_min=50)
    assert "review_jump" not in res["findings"]


def test_copy_to_takes_a_consistent_backup(tmp_path, base):
    _, before = base
    live = tmp_path / "index.db"                      # still open, WAL mode, with uncommitted-to-file pages
    out, bk = tmp_path / "s.json", tmp_path / "index.db.bak-test"
    assert snapshot.main([str(live), "--copy-to", str(bk), "-o", str(out)]) == 0
    assert json.loads(out.read_text())["counts"] == before["counts"]
    assert not (tmp_path / "index.db.bak-test-wal").exists()
    with pytest.raises(SystemExit):
        snapshot.main([str(live), "--copy-to", str(bk)])   # never overwrites a backup
