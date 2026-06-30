#!/usr/bin/env bash
# One command to safely redeploy after pulling a new tarball.
#
# Why this exists: a plain `docker compose up -d` does NOT pick up code changes
# (the core runs a built image) and does NOT reliably pick up .env content changes.
# This script always does the complete, correct thing so you never have to remember
# which kind of change you made:
#   - rebuild the core image            (activates digestcore/ code changes)
#   - sync the OWUI pipeline copy        (the pipelines container loads a copy)
#   - force-recreate every container     (applies .env changes)
#   - reinstall the editable CLI         (keeps `digest` in step with the package)
#   - verify the core is healthy and print the config it ended up with
#
# Usage:  ./scripts/redeploy.sh            (core only)
#         ./scripts/redeploy.sh owui       (core + the OWUI front-end stack)
#         ./scripts/redeploy.sh owui ollama
set -euo pipefail

cd "$(dirname "$0")/.."          # repo root
ENV=deploy/.env
CORE=deploy/core/docker-compose.yml
[ -f "$ENV" ] || { echo "missing $ENV — copy deploy/.env.example and fill it in first."; exit 1; }

echo "==> syncing OWUI pipeline copy"
cp -f interfaces/owui/digest_pipeline.py deploy/frontends/owui/pipeline/digest_pipeline.py

echo "==> rebuilding + recreating core (image picks up code; --force-recreate picks up .env)"
docker compose -f "$CORE" --env-file "$ENV" build
docker compose -f "$CORE" --env-file "$ENV" up -d --force-recreate

# bring up any named front-end / engine stacks too (e.g. owui, ollama)
for name in "$@"; do
  for cat in frontends llm; do
    f="deploy/$cat/$name/docker-compose.yml"
    if [ -f "$f" ]; then
      echo "==> recreating $cat/$name"
      gpu="deploy/$cat/$name/docker-compose.gpu.yml"
      args=(-f "$f")
      if [ -f "$gpu" ] && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
        args+=(-f "$gpu")
      fi
      docker compose "${args[@]}" --env-file "$ENV" up -d --force-recreate
    fi
  done
done

echo "==> reinstalling the host CLI (editable)"
pip install -e . >/dev/null 2>&1 && echo "    digest CLI updated" || echo "    (skipped: pip install -e . failed; run it manually if you use the CLI)"

echo "==> waiting for the core to report healthy"
for i in $(seq 1 30); do
  if curl -fs "http://localhost:8787/healthz" >/dev/null 2>&1; then echo "    core healthy"; break; fi
  sleep 1
  [ "$i" = 30 ] && echo "    WARNING: core did not report healthy in 30s"
done

echo "==> effective core config"
docker exec digest-core printenv | grep '^DIGEST' | sort | sed 's/^/    /'
echo "==> timezone sanity (should be your local time)"
docker exec digest-core python3 -c "import datetime as dt,os; from zoneinfo import ZoneInfo; \
import sqlite3; r=sqlite3.connect('/data/digest.db').execute('SELECT tz FROM users LIMIT 1').fetchone(); \
tz=r[0] if r and r[0] else 'UTC'; print('   ', tz, '->', dt.datetime.now(ZoneInfo(tz)))" 2>/dev/null \
  || echo "    (could not read tz)"
echo "==> done"
