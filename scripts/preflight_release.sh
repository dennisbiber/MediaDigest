#!/usr/bin/env bash
# Pre-flight audit to run inside the production repo BEFORE the first push.
# Checks the working tree AND git history for secrets, audits what git would publish,
# and confirms the gitignore actually protects the sensitive files. Exits non-zero if
# anything looks unsafe, so you can wire it into a pre-push hook.
#
#   cd ../digest-engine-public && ./scripts/preflight_release.sh
#
# For an extra pass against YOUR specific private values (Tailscale IP, ntfy host, etc.),
# put one value per line in an untracked, gitignored file named .release-denylist and this
# will grep history for each — that file is never committed, so your secrets don't ship.
set -uo pipefail
cd "$(dirname "$0")/.."
SCAN="python3 scripts/check_secrets.py"
fail=0
note() { printf '%s\n' "$*"; }
bad()  { printf 'FAIL: %s\n' "$*"; fail=1; }

note "== 1. working tree =="
$SCAN . || bad "secrets in the working tree"

note ""; note "== 2. git history (every commit, all branches) =="
if [ -d .git ]; then
  hist="$(mktemp)"
  git log -p --all --no-color > "$hist" 2>/dev/null
  $SCAN "$hist" || bad "secrets found in git history — rewriting history is required (see below)"
  rm -f "$hist"
  # .env must never have been committed, even if later removed
  if git log --all --oneline -- 'deploy/.env' '*.env' 2>/dev/null | grep -q .; then
    bad "an .env file appears in git history"
  fi
else
  note "  (no .git yet — history clean by definition)"
fi

note ""; note "== 3. what git would publish =="
if [ -d .git ]; then
  tracked="$(git ls-files)"
  risky="$(printf '%s\n' "$tracked" | grep -iE '\.(sqlite|db|db-wal|db-shm|token|tar|gz|bz2|xz)$|(^|/)\.env$|admin\.token|webui\.db|digest\.db' || true)"
  if [ -n "$risky" ]; then bad "data/secret files are tracked:"; printf '    %s\n' $risky; else
    note "  no data or secret files tracked"; fi
else
  note "  (not a git repo yet)"
fi

note ""; note "== 4. gitignore actually protects the sensitive paths =="
if [ -d .git ]; then
  for p in deploy/.env music_catalog.sqlite admin.token data/digest.db; do
    if git check-ignore -q "$p"; then note "  ignored: $p"; else bad "NOT ignored: $p"; fi
  done
fi

note ""; note "== 5. your private values (optional .release-denylist) =="
if [ -f .release-denylist ] && [ -d .git ]; then
  git check-ignore -q .release-denylist || bad ".release-denylist is itself tracked — gitignore it!"
  hist="$(mktemp)"; git log -p --all --no-color > "$hist" 2>/dev/null
  while IFS= read -r val; do
    [ -z "$val" ] && continue
    if grep -qF "$val" "$hist" 2>/dev/null; then bad "private value present in history: $val"; fi
    if grep -rqF "$val" --exclude-dir=.git . 2>/dev/null; then bad "private value present in tree: $val"; fi
  done < .release-denylist
  rm -f "$hist"
else
  note "  (no .release-denylist; pattern scan above still ran)"
fi

note ""; note "== 6. deeper third-party scan (recommended) =="
if command -v gitleaks >/dev/null 2>&1; then
  note "  running gitleaks..."
  gitleaks detect --no-banner --redact || bad "gitleaks found secrets"
else
  note "  gitleaks not installed. For the gold-standard history scan, install it:"
  note "    https://github.com/gitleaks/gitleaks  (or: brew install gitleaks)"
  note "  then re-run this script."
fi

note ""
if [ "$fail" -eq 0 ]; then
  note "PASS — no secrets, no machine-specific files, gitignore holds. Safe to push."
else
  note "NOT SAFE TO PUSH — resolve the FAIL lines above first."
fi
exit "$fail"
