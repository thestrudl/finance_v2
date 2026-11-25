#!/bin/sh
# Build and start production, then record what runs in .env.deploy.
#
#   cd /data/production/finance_v2 && ./deploy.sh        # same as: docker compose up -d --build
#   ./deploy.sh finance                                  # extra arguments go to docker compose up
#
# The images are built here from this directory (finance-app:latest,
# renovation-app:latest; no registry), so the backup of this directory is the
# source of the next build. .env.deploy says which commit that was, for a
# restore (RESTORE.md). Outside a git checkout, set DEPLOYED_COMMIT yourself.
set -eu
cd "$(dirname "$0")"

if commit=$(git rev-parse HEAD 2>/dev/null); then
    branch=$(git rev-parse --abbrev-ref HEAD)
    # Tracked files changed since the commit (app/household.json is untracked and always local).
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then dirty=1; else dirty=0; fi
else
    commit=${DEPLOYED_COMMIT:-unknown}
    branch=unknown
    dirty=unknown
    echo "deploy.sh: not a git checkout; recording DEPLOYED_COMMIT=$commit" >&2
fi

docker compose up -d --build "$@"

# Only after a successful start, so the file never names a build that isn't running.
cat > .env.deploy.tmp <<EOF
# Written by deploy.sh after each deploy: the source the running images were built from.
# Not secret. Read by people restoring a backup (RESTORE.md), not by docker compose.
DEPLOYED_COMMIT=$commit
DEPLOYED_BRANCH=$branch
DEPLOYED_DIRTY=$dirty
DEPLOYED_AT=$(date -Iseconds)
EOF
mv .env.deploy.tmp .env.deploy
echo "Deployed $commit (branch $branch, uncommitted changes: $dirty); recorded in .env.deploy"
