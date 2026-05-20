#!/usr/bin/env bash
# 성지폰 단가 일단위 파이프라인.
# 1) 크롤 → SQLite 적재
# 2) 일별 통계 머터리얼 갱신 (크롤러 내부 처리)
# 3) seongji_data.js 빌드 (HTML 대시보드용)
# 4) (옵션) Supabase 동기화 — env 가 셋업된 경우만
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"
LOG_DIR="$HERE/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/run-$TS.log"

{
  echo "==== seongji daily run @ $(date -u +%FT%TZ) ===="
  "$PY" seongji_crawler.py --max-pages 2
  "$PY" seongji_build.py

  if [[ -n "${SUPABASE_URL:-}" && ( -n "${SUPABASE_SERVICE_ROLE_KEY:-}" || -n "${SUPABASE_ANON_KEY:-}" ) ]]; then
    echo "-- syncing to supabase --"
    "$PY" seongji_supabase_sync.py --since "$(date -v-7d +%F 2>/dev/null || date -d '7 days ago' +%F)"
  else
    echo "-- supabase env not set, skipping sync --"
  fi
} | tee "$LOG"
