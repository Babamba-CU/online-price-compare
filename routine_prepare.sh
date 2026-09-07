#!/usr/bin/env bash
# 일일 루틴 1단계 — repo 동기화 → 신규 시세표 이미지 다운로드 → 세션 판독 준비.
# 이 맥(클로드 앱 예약작업)과 클라우드 루틴(claude.ai/code routines) 양쪽에서 동작:
#   · 경로는 스크립트 위치 기준(하드코딩 없음)
#   · 이 맥이면 venv(사내 CA 자동 로드)·gh 설정, 클라우드면 시스템 python + pip 설치
# 사용: bash routine_prepare.sh [상한=40]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAP="${1:-40}"
BATCH="/tmp/sise_batch"
cd "$REPO"

if [ -f "$HOME/venvs/online-price/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$HOME/venvs/online-price/bin/activate"       # ~/certs/env.sh(사내 CA) 자동 로드
  export GH_CONFIG_DIR="$HOME/.ghconfig"                # gh 토큰 위치(~/.config 가 root 소유라 우회)
  PY=python
  echo "[routine] 환경: 로컬 맥 (venv)" >&2
else
  PY=python3
  echo "[routine] 환경: 클라우드/기타 — 의존성 설치" >&2
  $PY -m pip install --quiet --disable-pip-version-check pillow requests beautifulsoup4 >/dev/null 2>&1 \
    || $PY -m pip install --quiet --user pillow requests beautifulsoup4 >/dev/null 2>&1 \
    || echo "[routine] pip 설치 실패 — 이미 설치돼 있길 기대하고 계속" >&2
fi

echo "[routine] 1/3 동기화" >&2
git pull --ff-only origin main >/dev/null 2>&1 || echo "[routine] git pull 실패 — 현재 체크아웃 상태로 계속" >&2

echo "[routine] 2/3 신규 시세표 이미지 다운로드 (상한 $CAP, 스킵리스트 적용)" >&2
rm -rf "$BATCH" && mkdir -p "$BATCH"
# 이미 판독된 image_url 은 자동 제외 → 채널별 '새로 올라온' 시세표만 받는다
$PY seongji_vision_batch.py --channels 342 --per-channel 1 --recent-days 30 --max-images "$CAP" \
  || echo "[routine] 다운로드 부분 실패 — 받은 것만으로 계속" >&2

echo "[routine] 3/3 세션 판독 준비 (긴 이미지 분할, RULES.md, results/)" >&2
$PY vision_session_prep.py
echo "[routine] prepare 완료 → 다음: $BATCH/RULES.md 규칙으로 session_manifest.json 의 이미지를 읽고 results/<idx>.json 저장 후 routine_finalize.sh" >&2
