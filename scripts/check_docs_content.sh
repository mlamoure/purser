#!/usr/bin/env bash
# Gates on the HAND-AUTHORED docs pages (the generated ones have their own
# drift gates in ci.yml):
#   1. retired-terms: language for removed/renamed features must not appear
#      in docs prose (scripts/retired-terms.txt).
#   2. env-var coverage: every PURSER_* variable the app understands — the
#      Settings fields in purser/config.py (env_prefix PURSER_) plus literal
#      PURSER_* references in code — must be documented in
#      docs/src/content/docs/reference/configuration.md.
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0

# 1) Retired terms. Add --exclude=<page> here if a page ever legitimately
#    discusses a retired feature's history.
if hits=$(grep -rniIE -f <(grep -vE '^\s*(#|$)' scripts/retired-terms.txt) \
    docs/src/content/docs); then
  echo "Retired terms in docs — rewrite the page (or exclude it here):"
  echo "$hits"
  fail=1
fi

# 2) Env-var coverage. Internal-only vars can be exempted via IGNORE_RE.
IGNORE_RE='^PURSER_TEST_'
missing=$(comm -23 \
  <({ grep -E '^    [a-z][a-z0-9_]*:' purser/config.py \
        | sed -E 's/^    ([a-z0-9_]+):.*/\1/' \
        | tr '[:lower:]' '[:upper:]' | sed 's/^/PURSER_/'; \
      grep -rhoE --include='*.py' 'PURSER_[A-Z0-9_]+' purser/; } \
      | grep -vE "$IGNORE_RE" | sort -u) \
  <(grep -ohE 'PURSER_[A-Z0-9_]+' \
      docs/src/content/docs/reference/configuration.md | sort -u))
if [ -n "$missing" ]; then
  echo "PURSER_* vars in code but not documented in reference/configuration.md:"
  echo "$missing"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "docs content check OK — no retired terms, env-var coverage complete"
fi
exit "$fail"
