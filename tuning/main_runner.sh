#!/usr/bin/env bash
# Runs the current GitHub main in the sandbox, one run at a time, and records the official result.
#
#   bash tuning/main_runner.sh [run_id_to_wait_for]
#   REF=origin/<branch> bash tuning/main_runner.sh      # test a branch instead of main
#   NOWAIT=1 bash tuning/main_runner.sh                 # start at once, next to other team runs
#
# Without an argument: waits until no team run is active, checks out origin/main into a clean worktree (never the
# working copy, which may hold uncommitted work), scans it for scenario values, starts one test run and waits for it.
# With a run id: only waits for that run. Either way the run record (official score + breakdown) is saved to
# docs/runs/official/<run>.json (local only) and one line is appended to docs/runs/main_runs.tsv.
set -u
A="$LOCALAPPDATA/atira/bin/atira.cmd"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
mkdir -p docs/runs/official

run="${1:-}"
commit="?"
ref="${REF:-origin/main}"
if [ -z "$run" ]; then
  [ -n "${NOWAIT:-}" ] || for _ in $(seq 1 90); do  # unless NOWAIT: wait for every active team run
    "$A" status 2>&1 | grep -qE " running| in review| in evaluation" || break
    sleep 60
  done
  git fetch -q origin
  commit=$(git rev-parse --short "$ref")
  dir="../QuoteCraft-packages/pkg-main-$commit"
  [ -d "$dir" ] || git worktree add -q --detach "$dir" "$ref"
  if [ -f docs/golden/scenario_terms.txt ] && grep -rnFf docs/golden/scenario_terms.txt "$dir/starter-kit" \
       --include=*.py --include=*.md --include=*.json --include=*.txt --include=*.html --include=*.csv | grep -v uv.lock | head -1 | grep -q .; then
    echo "ABORT: scenario values found in $commit"; exit 2
  fi
  run=$(cd "$dir" && "$A" test-run ./starter-kit 2>&1 | grep -o "run_[0-9a-f]\{16\}" | head -1)
  # Name the run after its branch and commit (only our team sees it): NAME=... overrides, else the ref's branch, else
  # the remote branches that contain the commit.
  branch="${ref#origin/}"
  if [ "$branch" = "$ref" ] && ! git show-ref -q "refs/remotes/origin/$ref"; then
    branch=$(git branch -r --contains "$commit" 2>/dev/null | grep -v HEAD | sed 's#origin/##; s/^ *//' | head -2 | paste -sd+ -)
  fi
  name="${NAME:-${branch:-detached} $commit}"
  [ -n "$run" ] && "$A" run name "$run" "$name" >/dev/null 2>&1
  echo "started $run on $ref $commit ($name)"
fi

for _ in $(seq 1 45); do
  "$A" status 2>&1 | grep "$run" | grep -qE "finished|failed|cancel" && break
  sleep 60
done
token=$("$A" token)
curl -s -m 60 -H "x-api-key: $token" "https://ehl-challenge.atira.ai/v1/runs/$run" -o "docs/runs/official/$run.json"
python -I - "$run" "$commit" <<'PYEOF'
import json, sys
run, commit = sys.argv[1], sys.argv[2]
d = json.load(open(f"docs/runs/official/{run}.json", encoding="utf-8"))
s = d.get("score") or {}
b = s.get("breakdown") or {}
st, fi = d.get("started_at"), d.get("finished_at")
minutes = round((fi - st) / 60000, 1) if st and fi else None
positions = {p.get("position"): p.get("points") for p in b.get("positions", [])}
line = "\t".join(str(x) for x in (run, commit, d.get("status"), s.get("quality"), s.get("eligible"), minutes,
                                   d.get("calls"), round((d.get("cost_micro_usd") or 0) / 1e6, 2),
                                   json.dumps(positions), (d.get("error") or "")[:80]))
open("docs/runs/main_runs.tsv", "a", encoding="utf-8").write(line + "\n")
print(line)
PYEOF
