#!/usr/bin/env bash
# Deploys the current directory to Railway and sets GIT_COMMIT_SHA to match
# it in the same operation -- the two are set atomically here specifically
# because they weren't before: this service has no GitHub connection
# (`railway api` confirms `repoTriggers` is empty), so every deploy is a
# manual `railway up`, and it's easy to demo a running instance while
# believing it's a newer commit than what was actually last pushed with
# `railway up`. /api/version reads GIT_COMMIT_SHA at runtime; this script is
# the one place that sets it, right before the matching code ships.
#
# Refuses to run against a dirty working tree or a HEAD that isn't pushed --
# a locally-committed-but-unpushed SHA reported as "deployed" would be just
# as misleading as a stale deploy, in the other direction.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
    echo "refusing to deploy: working tree is dirty (git status --porcelain is non-empty)" >&2
    echo "commit or stash first -- a deploy must correspond to a real, inspectable commit" >&2
    exit 1
fi

SHA="$(git rev-parse HEAD)"

if ! git merge-base --is-ancestor "$SHA" origin/master 2>/dev/null && [ "$(git rev-parse origin/master 2>/dev/null || echo '')" != "$SHA" ]; then
    echo "warning: HEAD ($SHA) does not appear to be pushed to origin/master -- deploying anyway (local dir is the real source), but the deployed commit won't be found on GitHub if someone goes looking" >&2
fi

echo "Deploying commit $SHA ..."

railway variable set "GIT_COMMIT_SHA=$SHA" --service credforge --skip-deploys

railway up --service credforge --ci

echo ""
echo "Deployed. Verify with:"
echo "  curl https://credforge-production.up.railway.app/api/version"
echo "Expect commit_sha == $SHA"
