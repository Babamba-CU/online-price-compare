#!/usr/bin/env bash
# 일일 루틴 1단계 — repo 동기화 → 신규 시세표 이미지 다운로드 → 세션 판독 준비.
# 클로드 앱 예약 작업(ROUTINE.md)이 매일 호출한다. 사용: bash routine_prepare.sh [상한=40]
set -euo pipefail

REPO="/Users/1108526/Documents/대시보드/온라인 단가비교"
CAP="${1:-40}"
BATCH="/tmp/sise_batch"

# venv activate 가 사내 프록시 CA(~/certs/env.sh: SSL_CERT_FILE 등)를 자동 로드한다.
# shellcheck disable=SC1091
source "$HOME/venvs/online-price/bin/activate"
export GH_CONFIG_DIR="$HOME/.ghconfig"     # gh 토큰 위치(~/.config 가 root 소유라 우회)
cd "$REPO"

echo "[routine] 1/3 동기화" >&2
git pull --ff-only origin main >/dev/null 2>&1 || echo "[routine] git pull 실패 — 오프라인/충돌, 로컬 상태로 계속" >&2

echo "[routine] 2/3 신규 시세표 이미지 다운로드 (상한 $CAP, 스킵리스트 적용)" >&2
rm -rf "$BATCH" && mkdir -p "$BATCH"
# 이미 판독된 image_url 은 자동 제외 → 채널별 '새로 올라온' 시세표만 받는다
python seongji_vision_batch.py --channels 342 --per-channel 1 --recent-days 30 --max-images "$CAP" \
  || echo "[routine] 다운로드 부분 실패 — 받은 것만으로 계속" >&2

echo "[routine] 3/3 세션 판독 준비 (긴 이미지 분할, RULES.md, results/)" >&2
python vision_session_prep.py
echo "[routine] prepare 완료 → 다음: $BATCH/RULES.md 규칙으로 session_manifest.json 의 이미지를 읽고 results/<idx>.json 저장 후 routine_finalize.sh" >&2
