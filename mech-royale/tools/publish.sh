#!/bin/sh
# Sync mech-royale/bot onto the mech-royale-submit branch (Dockerfile at its top) and push.
# Prints the commit to submit.
set -e
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WT=/tmp/mech-submit
cd "$REPO"
if [ ! -d "$WT" ]; then
  git fetch -q origin
  if git ls-remote --exit-code --heads origin mech-royale-submit >/dev/null 2>&1; then
    git worktree add -q "$WT" mech-royale-submit 2>/dev/null || git worktree add -q -B mech-royale-submit "$WT" origin/mech-royale-submit
  else
    git worktree add -q --detach "$WT"
    (cd "$WT" && git checkout -q --orphan mech-royale-submit && git rm -rfq . && git clean -fdxq)
  fi
fi
rm -rf "$WT/mech-royale"
mkdir -p "$WT/mech-royale"
cp -R "$REPO/mech-royale/bot" "$WT/mech-royale/bot"
cp "$REPO/mech-royale/Dockerfile.mech" "$WT/Dockerfile"
cp "$REPO/LICENSE" "$WT/LICENSE"
printf '.git\n' > "$WT/.dockerignore"
cat > "$WT/README.md" <<'MD'
# TTUN – Mech Royale submission

Submission branch for the Builders Mech Royale track. The source of truth is `mech-royale/` on `main`
(https://github.com/varchaskukreja/TTUN/tree/main/mech-royale); this branch only adds the top-level Dockerfile.
Uses the Builders starter kit's `mechfight.ts` wrapper. MIT License.
MD
cd "$WT"
git add -A
git commit -qm "${1:-Mech Royale bot}" || true
git push -q origin mech-royale-submit
git rev-parse HEAD
