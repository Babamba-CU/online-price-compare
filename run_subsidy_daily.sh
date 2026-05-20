#!/usr/bin/env bash
# 공시지원금 일단위 파이프라인.
# 1) 3사 공식 사이트 크롤 (Playwright)
# 2) seongji_data 와 별개로 subsidy_data.js 빌드
# 3) (옵션) Supabase 동기화 — env 셋팅된 경우만
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"
LOG_DIR="$HERE/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/subsidy-$TS.log"

{
  echo "==== subsidy daily run @ $(date -u +%FT%TZ) ===="
  "$PY" subsidy_crawler.py --carriers SKT KT LGU+
  "$PY" subsidy_build.py

  if [[ -n "${SUPABASE_URL:-}" && \
        ( -n "${SUPABASE_SERVICE_ROLE_KEY:-}" || -n "${SUPABASE_ANON_KEY:-}" ) ]]; then
    echo "-- syncing to supabase --"
    "$PY" subsidy_supabase_sync.py --since "$(date -v-1d +%F 2>/dev/null || date -d 'yesterday' +%F)"
  else
    echo "-- supabase env not set, skipping sync --"
  fi
} | tee "$LOG"
