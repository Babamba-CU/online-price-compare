#!/usr/bin/env bash
# 일일 루틴 3단계 — 판독 결과 병합 → 전체 재빌드 → 데이터 커밋·push.
# push 되면 deploy-lightsail.yml 이 자동 배포한다(데이터 파일 경로 트리거, ~4분).
# 클로드 앱 예약 작업(ROUTINE.md)이 판독을 마친 뒤 호출한다. 사용: bash routine_finalize.sh
set -euo pipefail

REPO="/Users/1108526/Documents/대시보드/온라인 단가비교"

# shellcheck disable=SC1091
source "$HOME/venvs/online-price/bin/activate"   # 사내 CA env 자동 로드
export GH_CONFIG_DIR="$HOME/.ghconfig"           # git push 자격증명(gh credential helper)
cd "$REPO"

echo "[routine] 1/3 판독 결과 병합 + 스킵리스트 갱신" >&2
python vision_session_merge.py

echo "[routine] 2/3 재수집·재빌드 (카카오 텍스트 + vision 적재 + 데이터 JS 생성)" >&2
python daily_collect.py finalize

echo "[routine] 3/3 데이터 커밋·push" >&2
DATA="seongji_vision_data.json vision_skiplist.json seongji_data.js seongji_kakao_history.json"
# shellcheck disable=SC2086
git add -- $DATA
if git diff --cached --quiet; then
  echo "[routine] 변경 없음 — 커밋 생략" >&2
  exit 0
fi
git commit -q -m "일일 카카오 시세표 루틴 수집 $(date +%F) [data-only]"

# 원격이 앞서 있으면(그사이 다른 push) 리베이스한다. 생성 데이터 파일 4개는 '이번 빌드 산출물'이
# 항상 정답이므로, 3-way 병합으로 JSON hunk 가 섞이지 않게 리베이스 뒤 그대로 복원해 amend 한다.
# (2026-09-07 실측: CI 데이터 커밋과 겹쳐 push 거부 → 이 절차로 해결)
git fetch origin -q
if [ "$(git rev-list --count HEAD..origin/main)" != "0" ]; then
  echo "[routine] 원격에 새 커밋 있음 — 리베이스 후 데이터 파일 복원" >&2
  KEEP="$(mktemp -d)"
  for f in $DATA; do git show "HEAD:$f" > "$KEEP/$f"; done
  git rebase -X theirs origin/main
  for f in $DATA; do cp "$KEEP/$f" "$f"; done
  # shellcheck disable=SC2086
  git add -- $DATA && git commit -q --amend --no-edit
  rm -rf "$KEEP"
fi
git push origin main
echo "[routine] push 완료 → Lightsail 자동 배포 진행(~4분). 확인: gh run list --workflow=deploy-lightsail.yml --limit 1" >&2
