# Deploying to the M1 test instance

The M1 test machine runs a second Music Assistant in
`~/cma-test` with this repo checked out at `~/cma-test/classical-music-assistant` and installed (not editable) into
`~/cma-test/venv`. `scripts/deploy_m1.sh` does the whole deploy and checks that the index did not regress.

It never touches Lotus or the NAS, only `~/cma-test` on the M1, and copies with `scp` / `git bundle` (no rsync).
Tokens are never printed.

The host is not in the repo. Put it in `scripts/deploy_m1.local.env` (gitignored) or the environment:

```sh
CMA_M1_HOST=user@host      # ssh target
CMA_M1_ALIAS=host-alias    # optional ssh HostKeyAlias (defaults to the host part of CMA_M1_HOST)
```

## Usage

```sh
scripts/deploy_m1.sh --dry-run          # everything that does not change the M1
scripts/deploy_m1.sh                    # deploy HEAD, compare, print rollback steps on a regression
scripts/deploy_m1.sh --auto-rollback    # ... and roll back by itself on a regression
```

Options: `--skip-tests`, `--no-wait` (compare right after the sync is re-triggered), `--drain-timeout SECS`
(default 7200), `--warn-only CAT,...` (pass to the comparison, for an intended change such as a composer fix),
`--rollback-help DIR`. Environment: `CMA_M1_HOST`, `CMA_M1_ALIAS`, `CMA_M1_DIR`, `CMA_PYTHON`, `CMA_DEPLOY_STATE`
(default `~/.cache/cma-deploy`; one folder per deploy with the log, bundles, `before.json`, `after.json`,
`compare.txt` and `ROLLBACK.txt`).

## What it does

1. **Local checks**: refuses if tracked files have uncommitted changes; runs `pytest -q -x` and `ruff check`.
2. **Remote preflight** (read-only): the M1 checkout is clean, HEAD is a fast-forward of the M1's commit,
   `classical/status` answers, the `WMfvx38m` sync tasks are listed. Writes local rollback material: a bundle of the
   M1's current commit and `ROLLBACK.txt`.
   *`--dry-run` stops here*, after a read-only snapshot of the live index (opened `mode=ro`), and prints the plan.
3. **Backup + before snapshot**: `snapshot.py --copy-to` takes `data/classical/index.db.bak-<ts>-deploy-<sha>`
   with SQLite's backup API (consistent, write-ahead log included, unlike a plain `cp`) and snapshots that copy.
4. **Code**: the old `cma.bundle` becomes `cma.bundle.prev-<old sha>`; the new bundle is scp'd, fetched and
   fast-forwarded (`--ff-only`), then `uv pip install --no-deps --reinstall` into the MA venv.
5. **Restart**: `./ctl.sh restart`, wait for `classical/status`, warn on tracebacks in `logs/ma.log` since then.
6. **Sync**: `sync_now.py` (a restart does not resume the full-library walk) and a check that the
   `WMfvx38m_track` task is `running` (retried three times; a deploy that cannot see it exits 3).
7. **Drain**: polls `classical/status` every 20 s until the worker has an empty queue and nothing current three
   polls in a row.
8. **After snapshot** of a fresh backup-API copy in a remote temp dir (deleted afterwards), then
   `compare_snapshots.py before.json after.json`. On a regression it prints the rollback steps, and with
   `--auto-rollback` it runs them: stop MA, reset to the old commit, reinstall, move the regressed index aside as
   `index.db.regressed-<ts>-<sha>` (with its -wal/-shm), restore the backup, start, `sync_now.py`.

Exit status: 0 ok, 1 regression (or a failed step), 3 ok but the sync was not seen running.

Rolling back loses only what the CMA indexed since the backup; the re-triggered sync indexes it again.

## The snapshot and the comparison

`scripts/snapshot.py index.db [-o out.json] [--copy-to backup.db]` is standard-library only (it runs under the
M1's MA venv by being piped over ssh) and read-only: `mode=ro` when a `-wal`/`-shm` is beside the file, otherwise
`immutable=1` so a backup copy is never written to. It records counts (composers in use, works, recordings,
review items by reason, joins, boxes, accolades, dated works, MB-linked works), and keyed by MA's album and track
ids, which survive a re-derive: every track's work and composers, each composer's works, each album's
composer to works mapping, accolade placements and cross-disc joins.

`scripts/compare_snapshots.py before.json after.json [--json] [--warn-only CAT,...] [--strict]` exits 1 on a
regression, 0 otherwise (2 on bad input). Albums only in `after` are growth from the library import and only
summarised. On albums indexed in both:

| category | severity | meaning |
|---|---|---|
| `composer_merge` | regression | tracks of two or more composers now credited to one |
| `composer_gone` | regression | a composer with tracks on existing albums has none now |
| `composer_changed` | regression | tracks of an existing album credited to a different composer |
| `recordings_lost` | regression | tracks that were in a recording are in none now |
| `works_lost` | regression | a composer's work on an album no longer under that composer |
| `accolades_lost` / `accolades_moved` | regression | a shown accolade gone, or on another recording or work |
| `joins_removed` / `joins_added` | regression | cross-disc joins between existing albums changed |
| `works_retitled`, `recordings_regrouped`, `accolade_grade` | warning | same tracks, new title or grouping |
| `review_jump` | warning | a review reason moved by more than `--review-pct` (25%) and `--review-min` (10) |
| `albums_removed`, `albums_unindexed`, `count_drop` | warning | |
| `non_latin_composer` | warning | a new composer name with no Latin letters |

An intended change (a composer fix that moves works on purpose) shows up as a regression too: read the list,
then rerun the comparison with `--warn-only composer_changed,composer_gone,works_lost` to confirm nothing else.

## The Medtner collapse

Commit 4a5f4f2 fixed MusicBrainz composer names in Cyrillic or Japanese whose name key folded to `''`, which
merged Glinka, Glazunov, Takemitsu, Gubaidulina and Pletnev into Medtner (`Николай Карлович Метнер`) on the M1
re-derive. Comparing the M1's pre-deploy backup with the collapsed index reports:

```
REGRESSION: composer_merge (1)
  {"into": "Николай Карлович Метнер", "from": {"Alexander Glazunov": 1, "Mikhail Glinka": 1,
   "Mikhail Pletnev": 1, "Sofia Gubaidulina": 1, "Toru Takemitsu": 2}, "tracks": 6}
REGRESSION: composer_changed (5) ...   REGRESSION: composer_gone (6) ...   REGRESSION: works_lost (7) ...
```

`tests/unit/test_deploy_guard.py` reproduces it on the fixture library.
