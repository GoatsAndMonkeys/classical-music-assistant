#!/usr/bin/env bash
# Deploy the current commit to the M1 test Music Assistant (~/cma-test) and check the index did not regress.
# See docs/deploy.md. Usage:
#   scripts/deploy_m1.sh [--dry-run] [--auto-rollback] [--skip-tests] [--drain-timeout SECS]
#                        [--warn-only CAT,...] [--no-wait]
#   scripts/deploy_m1.sh --rollback-help DIR     print the rollback steps recorded for an earlier deploy
#
# Steps: local checks (clean tree, pytest, ruff) -> remote preflight (read-only) -> [dry run stops here, after a
# read-only snapshot of the live index] -> backup index.db (SQLite backup API) + snapshot of that backup -> keep a
# rollback bundle -> bundle + scp -> fast-forward -> uv reinstall -> restart -> sync_now.py (+ check the WMfvx38m
# track sync is running) -> wait for the CMA worker queue to drain -> snapshot after -> compare_snapshots.py.
#
# Only touches the M1 (ssh with HostKeyAlias, scp; no rsync) and only under ~/cma-test. Never Lotus, never the NAS.
# Tokens are never printed: remote helpers print chosen fields only, and nothing here runs with `set -x`.
set -euo pipefail

# Host details stay out of the repo: set them in the environment or in scripts/deploy_m1.local.env (gitignored).
LOCAL_ENV="$(dirname "$0")/deploy_m1.local.env"
# shellcheck source=/dev/null
[ -f "$LOCAL_ENV" ] && . "$LOCAL_ENV"
HOST=${CMA_M1_HOST:?set CMA_M1_HOST (user@host) in the environment or $LOCAL_ENV}
ALIAS=${CMA_M1_ALIAS:-${HOST#*@}}
RDIR=${CMA_M1_DIR:-cma-test}                    # relative to the remote home
PROVIDER=WMfvx38m
SSH_OPTS=(-o "HostKeyAlias=$ALIAS" -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30)

DRY=0; AUTO_RB=0; SKIP_TESTS=0; WAIT=1; DRAIN_TIMEOUT=${CMA_DRAIN_TIMEOUT:-7200}; WARN_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --auto-rollback) AUTO_RB=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --no-wait) WAIT=0 ;;
    --drain-timeout) DRAIN_TIMEOUT=$2; shift ;;
    --warn-only) WARN_ONLY=$2; shift ;;
    --rollback-help) cat "$2/ROLLBACK.txt"; exit 0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

REPO=$(cd "$(dirname "$0")/.." && git rev-parse --show-toplevel)
cd "$REPO"
SHA=$(git rev-parse HEAD); SHORT=${SHA:0:7}
TS=$(date +%Y%m%d-%H%M%S)
STATE=${CMA_DEPLOY_STATE:-$HOME/.cache/cma-deploy}/$TS-$SHORT
mkdir -p "$STATE"
LOG=$STATE/deploy.log
exec > >(tee -a "$LOG") 2>&1

say()  { printf '\n== %s  %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '\nABORT: %s\n' "$*" >&2; exit 1; }
rsh()  { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }
# run a local Python file on the M1 with the MA venv, from ~/cma-test (stdin carries the script)
rpy()  { local f=$1; shift; rsh "cd $RDIR && venv/bin/python - $*" < "$f"; }

PY=${CMA_PYTHON:-}
if [ -z "$PY" ]; then
  for c in "$REPO/.venv/bin/python" "$(git rev-parse --path-format=absolute --git-common-dir)/../.venv/bin/python"; do
    [ -x "$c" ] && { PY=$c; break; }
  done
fi
[ -n "$PY" ] || PY=python3

# --- remote helpers (written to the state dir, sent over stdin; they print chosen fields only, never tokens)
cat > "$STATE/status.py" <<'EOF'
import asyncio, json, sys
sys.path.insert(0, ".")
from ma_api import session
async def run(api):
    st = await api.call("classical/status")
    w = st.get("worker") or {}
    print(json.dumps({"queued": len(w.get("queued") or []), "current": w.get("current"), "running": w.get("running"),
                      "last_scan": w.get("last_scan"), "errors": w.get("errors"), "albums": st.get("albums"),
                      "recordings": st.get("recordings"), "schema": st.get("schema_version")}))
asyncio.run(session(run))
EOF
cat > "$STATE/synctasks.py" <<'EOF'
import asyncio, json, sys
sys.path.insert(0, ".")
from ma_api import session
async def run(api):
    for t in await api.call("tasks/list") or []:
        s = json.dumps(t, default=str)
        if "sync" in s.lower():
            print(json.dumps({k: t.get(k) for k in ("id", "status")}, default=str))
asyncio.run(session(run))
EOF

say "deploy $SHORT to $HOST:~/$RDIR (state: $STATE)"
[ $DRY = 1 ] && echo "DRY RUN: nothing on the M1 will be changed"

# --- 1. local checks
say "local checks"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "working tree has uncommitted changes (commit or stash)"
if [ $SKIP_TESTS = 0 ]; then
  if ! "$PY" -m pytest -q -x > "$STATE/pytest.txt" 2>&1; then tail -30 "$STATE/pytest.txt"; die "pytest failed"; fi
  tail -1 "$STATE/pytest.txt"
  RUFF=$(dirname "$PY")/ruff; [ -x "$RUFF" ] || RUFF=ruff
  "$RUFF" check -q classical_core provider tests scripts || die "ruff failed"
  echo "pytest + ruff ok"
else
  echo "tests skipped (--skip-tests)"
fi

# --- 2. remote preflight (read-only)
say "remote preflight"
OLD=$(rsh "git -C $RDIR/classical-music-assistant rev-parse HEAD") || die "cannot reach the M1 repo"
OLDS=${OLD:0:7}
echo "M1 is at $OLDS; deploying $SHORT"
[ -z "$(rsh "git -C $RDIR/classical-music-assistant status --porcelain --untracked-files=no")" ] \
  || die "the M1 checkout has local changes; sort that out by hand"
git cat-file -e "$OLD^{commit}" 2>/dev/null || die "M1 commit $OLDS is not in this repo; cannot check fast-forward"
git merge-base --is-ancestor "$OLD" "$SHA" || die "$SHORT is not a fast-forward of the M1's $OLDS"
[ "$OLD" != "$SHA" ] || echo "note: M1 already has $SHORT (will reinstall, restart and re-check)"
git log --oneline "$OLD..$SHA" | sed 's/^/  /'
STATUS=$(rpy "$STATE/status.py") || die "classical/status failed (is MA up on the M1?)"
echo "classical/status: $STATUS"
rpy "$STATE/synctasks.py" | grep "$PROVIDER" | sed 's/^/sync task: /' || true

# rollback material (local): the M1's current commit as a bundle
git branch -f "deploy-rollback-$OLDS" "$OLD" >/dev/null 2>&1 || true
git bundle create -q "$STATE/rollback-$OLDS.bundle" "deploy-rollback-$OLDS"
git branch -D "deploy-rollback-$OLDS" >/dev/null 2>&1 || true
git bundle create -q "$STATE/cma-$SHORT.bundle" HEAD
BAK=data/classical/index.db.bak-$TS-deploy-$SHORT
cat > "$STATE/ROLLBACK.txt" <<EOF
Rollback of deploy $SHORT (M1 was at $OLDS), on $HOST (ssh -o HostKeyAlias=$ALIAS):
  cd ~/$RDIR
  ./ctl.sh stop
  git -C classical-music-assistant reset --hard $OLD
  UV_CACHE_DIR=\$PWD/.uv-cache ~/.local/bin/uv pip install --python venv/bin/python --no-deps --reinstall ./classical-music-assistant
  cp $BAK data/classical/index.db && rm -f data/classical/index.db-wal data/classical/index.db-shm
  ./ctl.sh start            # then wait for port 8195
  venv/bin/python sync_now.py   # restarts do not resume the library sync
Local copies: $STATE/rollback-$OLDS.bundle (the old commit), before.json (index before the deploy).
EOF

if [ $DRY = 1 ]; then
  say "snapshot of the live index (read-only)"
  rpy scripts/snapshot.py data/classical/index.db > "$STATE/before.json" || die "snapshot failed"
  "$PY" - "$STATE/before.json" <<'EOF'
import json, sys
print(json.dumps(json.load(open(sys.argv[1]))["counts"], indent=1))
EOF
  say "DRY RUN done. Would now, on the M1: back up index.db to ~/$RDIR/$BAK and snapshot it, scp the bundle, fast-forward $OLDS..$SHORT, uv reinstall, restart, sync_now.py, wait for the queue (<= ${DRAIN_TIMEOUT}s), snapshot, compare."
  echo "state: $STATE"
  exit 0
fi

# --- 3. backup + before snapshot (one consistent copy: the backup is what the snapshot reads)
say "backup index.db -> ~/$RDIR/$BAK and snapshot it"
rsh "cd $RDIR && venv/bin/python - data/classical/index.db --copy-to $BAK --summary" < scripts/snapshot.py > "$STATE/before.json"

# --- 4. bundle, scp, fast-forward, reinstall
say "bundle + scp + fast-forward"
rsh "cd $RDIR && if [ -f cma.bundle ]; then mv cma.bundle cma.bundle.prev-$OLDS; fi"
scp -q "${SSH_OPTS[@]}" "$STATE/cma-$SHORT.bundle" "$HOST:$RDIR/cma.bundle"
rsh "cd $RDIR/classical-music-assistant && git fetch -q ../cma.bundle HEAD && git merge -q --ff-only FETCH_HEAD && git rev-parse HEAD" \
  | grep -qx "$SHA" || die "fast-forward did not land on $SHORT (M1 untouched apart from the bundle file; see $STATE/ROLLBACK.txt)"
say "uv reinstall"
rsh "cd $RDIR && UV_CACHE_DIR=\$PWD/.uv-cache ~/.local/bin/uv pip install -q --python venv/bin/python --no-deps --reinstall ./classical-music-assistant" \
  || die "uv reinstall failed; see $STATE/ROLLBACK.txt"

# --- 5. restart and re-trigger the library sync
say "restart"
LOGLINES=$(rsh "wc -l < $RDIR/logs/ma.log" | tr -d ' ')
RESTART_AT=$(date +%s)
rsh "cd $RDIR && ./ctl.sh restart"
for i in $(seq 1 60); do
  sleep 5
  if STATUS=$(rpy "$STATE/status.py" 2>/dev/null); then break; fi
  STATUS=""
done
[ -n "$STATUS" ] || die "MA did not come back within 5 minutes; check ./ctl.sh log on the M1, then $STATE/ROLLBACK.txt"
echo "up after $(( $(date +%s) - RESTART_AT ))s: $STATUS"
TB=$(rsh "tail -n +$((LOGLINES + 1)) $RDIR/logs/ma.log | grep -c Traceback || true")
[ "${TB:-0}" = 0 ] || echo "WARNING: $TB traceback(s) in ma.log since the restart (./ctl.sh log 200 on the M1)"

say "re-trigger the $PROVIDER track sync"
SYNC_OK=0
for attempt in 1 2 3; do
  rsh "cd $RDIR && venv/bin/python sync_now.py" | grep -i "sync started" || true
  for i in 1 2 3 4 5 6; do
    sleep 5
    T=$(rpy "$STATE/synctasks.py" 2>/dev/null || true)
    if echo "$T" | grep "${PROVIDER}_track" | grep -q '"running"'; then SYNC_OK=1; break 2; fi
  done
done
if [ $SYNC_OK = 1 ]; then echo "$PROVIDER track sync is running"
else echo "WARNING: the ${PROVIDER} track sync is not running: $(echo "$T" | grep "$PROVIDER" | tr '\n' ' ') (run: cd ~/$RDIR && venv/bin/python sync_now.py)"; fi

# --- 6. wait for the CMA worker queue to drain
DRAINED=0
if [ $WAIT = 1 ]; then
  say "waiting for the CMA worker to go idle (3 idle polls in a row, timeout ${DRAIN_TIMEOUT}s)"
  sleep 60
  idle=0; t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt "$DRAIN_TIMEOUT" ]; do
    S=$(rpy "$STATE/status.py" 2>/dev/null || echo '{}')
    echo "  $(date +%H:%M:%S) $S"
    if echo "$S" | grep -q '"queued": 0, "current": null'; then idle=$((idle + 1)); else idle=0; fi
    [ $idle -ge 3 ] && { DRAINED=1; break; }
    sleep 20
  done
  [ $DRAINED = 1 ] || echo "WARNING: the worker did not drain within ${DRAIN_TIMEOUT}s; comparing anyway"
fi

# --- 7. snapshot after, compare
say "snapshot after"
rsh "d=\$(mktemp -d /tmp/cma-snap.XXXXXX) && cd $RDIR && venv/bin/python - data/classical/index.db --copy-to \$d/i.db --summary; rc=\$?; rm -rf \$d; exit \$rc" \
  < scripts/snapshot.py > "$STATE/after.json"
say "compare"
set +e
"$PY" scripts/compare_snapshots.py "$STATE/before.json" "$STATE/after.json" ${WARN_ONLY:+--warn-only "$WARN_ONLY"} \
  | tee "$STATE/compare.txt"
RC=${PIPESTATUS[0]}
set -e
if [ "$RC" = 0 ]; then
  say "deploy $SHORT OK ($([ $DRAINED = 1 ] && echo drained || echo 'not drained')). Backup: ~/$RDIR/$BAK  State: $STATE"
  [ $SYNC_OK = 1 ] || exit 3
  exit 0
fi

say "REGRESSIONS found (compare exit $RC)"
cat "$STATE/ROLLBACK.txt"
if [ $AUTO_RB = 1 ] && [ "$RC" = 1 ]; then
  say "--auto-rollback: rolling back to $OLDS"
  rsh "cd $RDIR && ./ctl.sh stop; git -C classical-music-assistant reset -q --hard $OLD \
    && UV_CACHE_DIR=\$PWD/.uv-cache ~/.local/bin/uv pip install -q --python venv/bin/python --no-deps --reinstall ./classical-music-assistant \
    && for f in '' -wal -shm; do if [ -f data/classical/index.db\$f ]; then mv data/classical/index.db\$f data/classical/index.db.regressed-$TS-$SHORT\$f; fi; done \
    && cp $BAK data/classical/index.db \
    && ./ctl.sh start"
  for i in $(seq 1 60); do sleep 5; rpy "$STATE/status.py" >/dev/null 2>&1 && break; done
  rsh "cd $RDIR && venv/bin/python sync_now.py" | grep -i "sync started" || echo "WARNING: re-run sync_now.py by hand"
  echo "rolled back to $OLDS; the regressed index is kept as ~/$RDIR/data/classical/index.db.regressed-$TS-$SHORT"
else
  echo "Not rolled back. Run the steps above, or re-run with --auto-rollback next time."
fi
exit 1
