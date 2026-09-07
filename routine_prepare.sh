#!/usr/bin/env bash
# 일일 루틴 1단계 — 신규 시세표 이미지 확보 → 세션 판독 준비.
# 이 맥(클로드 앱 예약작업)과 클라우드 루틴(claude.ai/code routines) 양쪽에서 동작:
#   · 로컬 맥(venv 있음): 사내 CA 로 pf.kakao.com 에서 직접 다운로드 → 분할·규칙파일 생성
#   · 클라우드(venv 없음): 샌드박스가 pf.kakao.com 을 차단(403)하므로, GitHub Actions
#     (sise-fetch.yml, 04:10 KST)가 미리 받아 발행한 고아 브랜치 `sise-batch` 를 받는다.
#     브랜치가 없으면 직접 다운로드를 시도한다(폴백).
# 사용: bash routine_prepare.sh [상한=40]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAP="${1:-40}"
BATCH="/tmp/sise_batch"
cd "$REPO"

download_and_prep() {
  echo "[routine] 신규 시세표 이미지 다운로드 (상한 $CAP, 스킵리스트 적용)" >&2
  rm -rf "$BATCH" && mkdir -p "$BATCH"
  # 이미 판독된 image_url 은 자동 제외 → 채널별 '새로 올라온' 시세표만 받는다
  $PY seongji_vision_batch.py --channels 342 --per-channel 1 --recent-days 30 --max-images "$CAP" \
    || echo "[routine] 다운로드 부분 실패 — 받은 것만으로 계속" >&2
  echo "[routine] 세션 판독 준비 (긴 이미지 분할, RULES.md, results/)" >&2
  $PY vision_session_prep.py
  echo "[routine] 공시지원금 스냅샷 갱신 (스마트초이스)" >&2
  $PY subsidy_smartchoice.py --no-db || echo "[routine] 공시지원금 수집 실패 — 기존 스냅샷 유지" >&2
}

if [ -f "$HOME/venvs/online-price/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$HOME/venvs/online-price/bin/activate"       # ~/certs/env.sh(사내 CA) 자동 로드
  export GH_CONFIG_DIR="$HOME/.ghconfig"                # gh 토큰 위치(~/.config 가 root 소유라 우회)
  PY=python
  echo "[routine] 환경: 로컬 맥 (venv)" >&2
  echo "[routine] 동기화" >&2
  git pull --ff-only origin main >/dev/null 2>&1 || echo "[routine] git pull 실패 — 현재 상태로 계속" >&2
  download_and_prep
else
  PY=python3
  echo "[routine] 환경: 클라우드/기타 — 의존성 설치" >&2
  $PY -m pip install --quiet --disable-pip-version-check pillow requests beautifulsoup4 >/dev/null 2>&1 \
    || $PY -m pip install --quiet --user pillow requests beautifulsoup4 >/dev/null 2>&1 \
    || echo "[routine] pip 설치 실패 — 이미 설치돼 있길 기대하고 계속" >&2
  echo "[routine] 동기화" >&2
  git pull --ff-only origin main >/dev/null 2>&1 || echo "[routine] git pull 실패 — 현재 체크아웃 상태로 계속" >&2
  if git fetch -q origin "+sise-batch:refs/remotes/origin/sise-batch" 2>/dev/null; then
    echo "[routine] 배치 브랜치 sise-batch 수신 (GitHub Actions 가 미리 받은 이미지)" >&2
    rm -rf "$BATCH" && mkdir -p "$BATCH/results"
    git archive --format=tar origin/sise-batch | tar -x -C "$BATCH"
    # 공시지원금 스냅샷(CI 가 스마트초이스에서 수집) — 샌드박스는 외부망이 막혀 배치로 받는다
    if [ -f "$BATCH/subsidy_snapshot.json" ]; then
      cp "$BATCH/subsidy_snapshot.json" "$REPO/subsidy_snapshot.json"
      echo "[routine] 공시지원금 스냅샷 수신 ($(python3 -c "import json;print(json.load(open('$REPO/subsidy_snapshot.json'))['snapshot_date'])" 2>/dev/null || echo '?'))" >&2
    fi
    echo "[routine] 배치 생성 시각(UTC): $(cat "$BATCH/BATCH_CREATED_UTC" 2>/dev/null || echo '?') · 이미지 $(ls "$BATCH"/*.jpg 2>/dev/null | wc -l | tr -d ' ')장" >&2
  else
    echo "[routine] 배치 브랜치 없음 — 직접 다운로드 시도(샌드박스에선 차단될 수 있음)" >&2
    download_and_prep
  fi
fi

# 준비 상태 요약 (세션이 이 줄로 N 을 판단한다)
$PY - <<'PYEOF'
import json, pathlib
b = pathlib.Path("/tmp/sise_batch")
s = json.loads((b / "session_manifest.json").read_text(encoding="utf-8")) if (b / "session_manifest.json").exists() else []
(b / "results").mkdir(parents=True, exist_ok=True)
print(f"READY images={len(s)} pieces={sum(len(x['pieces']) for x in s)}")
PYEOF
echo "[routine] prepare 완료 → 다음: $BATCH/RULES.md 규칙으로 session_manifest.json 의 이미지를 읽고 results/<idx>.json 저장 후 routine_finalize.sh" >&2
