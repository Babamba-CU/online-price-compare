#!/usr/bin/env bash
#
# AWS Lightsail Container Service 배포 스크립트 — 온라인 단가비교(성지폰 단가 모니터링) 대시보드
# -----------------------------------------------------------------------------
# 사전 준비:
#   1) aws configure 로 자격증명 설정 (Lightsail 권한 필요)
#   2) Docker Desktop 실행 (docker info 가 정상 동작해야 함)
#   3) lightsailctl 플러그인 설치  (brew install aws/tap/lightsailctl)
#
# 사용법:  ./deploy.sh
#
# 환경변수로 조정 가능:
#   SERVICE_NAME (기본 seongji-price-monitor)
#   REGION       (기본 ap-northeast-2  서울)
#   POWER        (기본 nano  - nano/micro/small/medium/large)
#   SCALE        (기본 1)
#   IMAGE_LABEL  (기본 app)

set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-seongji-price-monitor}"
REGION="${REGION:-ap-northeast-2}"
POWER="${POWER:-nano}"
SCALE="${SCALE:-1}"
IMAGE_LABEL="${IMAGE_LABEL:-app}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- NAVER API 자격증명 (커밋 금지 — gitignored .env 또는 셸 환경변수) ----------
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi
if [ -z "${NAVER_CLIENT_ID:-}" ] || [ -z "${NAVER_CLIENT_SECRET:-}" ]; then
  echo "[경고] NAVER_CLIENT_ID/SECRET 미설정 — 컨테이너는 합성 데이터만 생성합니다."
  echo "       설정: $SCRIPT_DIR/.env 에 키 작성 (.env.example 참고)"
fi

echo "================================================="
echo " Lightsail 배포: $SERVICE_NAME ($REGION, $POWER x$SCALE)"
echo "================================================="

# --- 0. 사전 점검 ------------------------------------------------------------
command -v aws >/dev/null || { echo "[오류] aws CLI 가 필요합니다."; exit 1; }
command -v docker >/dev/null || { echo "[오류] docker 가 필요합니다."; exit 1; }
docker info >/dev/null 2>&1 || { echo "[오류] Docker 데몬이 실행 중이 아닙니다. Docker Desktop을 켜주세요."; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "[오류] AWS 자격증명이 없습니다. aws configure 를 먼저 실행하세요."; exit 1; }
command -v lightsailctl >/dev/null 2>&1 || {
  echo "[오류] lightsailctl 플러그인이 없습니다."
  echo "       설치: brew install aws/tap/lightsailctl"
  exit 1
}

# --- 1. 컨테이너 서비스 생성 (없으면) ----------------------------------------
echo "[1/5] 컨테이너 서비스 확인/생성..."
if ! aws lightsail get-container-services --service-name "$SERVICE_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "  → 서비스가 없어 새로 생성합니다."
  aws lightsail create-container-service \
    --service-name "$SERVICE_NAME" \
    --power "$POWER" \
    --scale "$SCALE" \
    --region "$REGION"
  echo "  → 서비스가 READY 상태가 될 때까지 대기(수 분 소요)..."
  while true; do
    STATE=$(aws lightsail get-container-services --service-name "$SERVICE_NAME" --region "$REGION" \
      --query 'containerServices[0].state' --output text)
    echo "     상태: $STATE"
    [ "$STATE" = "READY" ] && break
    [ "$STATE" = "DISABLED" ] && { echo "[오류] 서비스가 DISABLED 상태"; exit 1; }
    sleep 15
  done
else
  echo "  → 기존 서비스를 사용합니다."
fi

# --- 2. Docker 이미지 빌드 ---------------------------------------------------
echo "[2/5] Docker 이미지 빌드 (linux/amd64)..."
docker build --platform linux/amd64 -t "$SERVICE_NAME:latest" "$PROJECT_ROOT"

# --- 3. 이미지 Lightsail로 푸시 ---------------------------------------------
echo "[3/5] 이미지 푸시..."
PUSH_OUTPUT=$(aws lightsail push-container-image \
  --service-name "$SERVICE_NAME" \
  --label "$IMAGE_LABEL" \
  --image "$SERVICE_NAME:latest" \
  --region "$REGION" 2>&1)
echo "$PUSH_OUTPUT"

# 푸시 결과에서 생성된 이미지 참조(:service.label.N) 추출
IMAGE_REF=$(echo "$PUSH_OUTPUT" | grep -oE ':[a-zA-Z0-9._-]+\.'"$IMAGE_LABEL"'\.[0-9]+' | tail -n1)
if [ -z "$IMAGE_REF" ]; then
  echo "[오류] 푸시된 이미지 참조를 찾지 못했습니다. 위 출력을 확인하세요."
  exit 1
fi
echo "  → 이미지 참조: $IMAGE_REF"

# --- 4. 배포 명세 생성 (이미지 참조 치환) ------------------------------------
echo "[4/5] 배포 명세 생성..."
TMP_CONTAINERS=$(mktemp)
python3 - "$SCRIPT_DIR/containers.json" "$IMAGE_REF" \
  "${NAVER_CLIENT_ID:-}" "${NAVER_CLIENT_SECRET:-}" > "$TMP_CONTAINERS" <<'PYEOF'
import json, sys
src, image_ref, nid, nsec = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(src, encoding="utf-8") as f:
    data = json.load(f)
for name, spec in data.items():
    spec["image"] = image_ref
    env = spec.setdefault("environment", {})
    # 자격증명은 배포 명세(mktemp)에만 — 커밋되는 containers.json 은 {} 유지
    if nid and nsec:
        env["NAVER_CLIENT_ID"] = nid
        env["NAVER_CLIENT_SECRET"] = nsec
print(json.dumps(data, ensure_ascii=False))
PYEOF

# --- 5. 배포 생성 ------------------------------------------------------------
echo "[5/5] 배포 시작..."
aws lightsail create-container-service-deployment \
  --service-name "$SERVICE_NAME" \
  --region "$REGION" \
  --containers "file://$TMP_CONTAINERS" \
  --public-endpoint "file://$SCRIPT_DIR/public-endpoint.json"

rm -f "$TMP_CONTAINERS"

echo ""
echo "================================================="
echo " 배포 요청 완료. 상태/URL 확인:"
echo "   aws lightsail get-container-services --service-name $SERVICE_NAME --region $REGION \\"
echo "     --query 'containerServices[0].{state:state,url:url}'"
echo "================================================="
