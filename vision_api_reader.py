"""
시세표 이미지 → Claude API(Vision) 자동 판독기 — 무인 일일 수집용.

기존 파이프라인에서 "Claude Code 세션이 이미지를 직접 읽는 단계"를
Anthropic API 호출로 치환한다. GitHub Actions 등 어디서든 무인 실행 가능.

  seongji_vision_batch.py  → /tmp/sise_batch/manifest.json + 이미지
  vision_api_reader.py     → 이미지별 API 판독 → seongji_vision_data.json 병합
                             + vision_skiplist 갱신 (성공→리셋, 실패→+1)

판독 규칙(사용자 확정, 기존 세션 판독과 동일):
  - 표값 단위: 만원이 관행 → 원 단위 정수로 변환해 반환, 음수 = 차비(페이백)
  - 결합(인터넷+TV)/제휴카드/온누리 체감가 행: add_condition 에 표기(적재 단계에서 제외)
  - 약정 미표기 = 24개월, 기본료 = 요금제 정가, 월청구 공식 (월청구−정가)×개월수
  - 가입유형 미표기는 null (분석 단계에서 MNP 추정 처리)

환경변수:
  ANTHROPIC_API_KEY       (필수)
  VISION_MODEL            기본 claude-sonnet-5    (사용자 확정 — Sonnet 기준 동작)
  VISION_ESCALATE_MODEL   기본 claude-sonnet-5    (기본과 같으면 에스컬레이션 비활성)
  VISION_MAX_IMAGES       기본 40
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date
from pathlib import Path

import anthropic

BASE = Path(__file__).parent
VISION_DATA_PATH = BASE / "seongji_vision_data.json"
MANIFEST_DEFAULT = "/tmp/sise_batch/manifest.json"

MODEL = os.getenv("VISION_MODEL", "claude-sonnet-5")
ESCALATE_MODEL = os.getenv("VISION_ESCALATE_MODEL", "claude-sonnet-5")
MAX_IMAGES = int(os.getenv("VISION_MAX_IMAGES", "40"))
ESCALATE_CONF = 0.7          # 평균 confidence 미만이면 상위 모델 재판독
PRICE_SANITY = (-500_000, 3_000_000)
VALID_STORAGE = {64, 128, 256, 512, 1024, 2048}

# 구조화 출력 스키마 — 시세표 1장 → 행 목록
# 주의: 구조화 출력 검증기는 유니온 타입 배열("type": ["string","null"])을 지원하지
# 않는다 — nullable 은 반드시 anyOf 로 표현해야 함 (2026-07-13 실측 400 오류로 확인).
def _nullable(inner: dict, description: str | None = None) -> dict:
    out = {"anyOf": [inner, {"type": "null"}]}
    if description:
        out["description"] = description
    return out


SCHEMA = {
    "type": "object",
    "properties": {
        "is_price_table": {
            "type": "boolean",
            "description": "이 이미지가 휴대폰 단가/시세표인지 (매장사진·행사포스터·조건안내는 false)",
        },
        "board_date": _nullable({"type": "string"},
                                "시세표에 표기된 기준일 YYYY-MM-DD (없으면 null)"),
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "model_name": {
                        "type": "string",
                        "description": "정규화 모델명 (예: Galaxy S26 Ultra, iPhone 17 Pro Max, Galaxy Z Flip 7)",
                    },
                    "storage_gb": _nullable({"type": "integer"}),
                    "carrier": _nullable({"type": "string", "enum": ["SKT", "KT", "LGU+", "알뜰"]}),
                    "subscription_type": _nullable({"type": "string", "enum": ["MNP", "기변", "신규"]}),
                    "contract_type": _nullable({"type": "string", "enum": ["공시", "선약", "자급"]}),
                    "cash_price": {
                        "type": "integer",
                        "description": "현금완납가, 원 단위 정수. 만원 표기는 ×10000. 음수 = 차비(페이백) 지급",
                    },
                    "plan_name": _nullable({"type": "string"}),
                    "plan_fee": _nullable({"type": "integer"}, "요금제 월정액 정가(원)"),
                    "estimated": {"type": "boolean", "description": "월청구 공식 등으로 추정한 값이면 true"},
                    "add_condition": _nullable(
                        {"type": "string"},
                        "부가 조건. 결합/제휴카드/온누리 체감가 행은 반드시 해당 키워드 포함"),
                    "confidence": {"type": "number", "description": "0~1"},
                },
                "required": ["model_name", "storage_gb", "carrier", "subscription_type",
                             "contract_type", "cash_price", "plan_name", "plan_fee",
                             "estimated", "add_condition", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_price_table", "board_date", "rows"],
    "additionalProperties": False,
}

PROMPT = """한국 휴대폰 성지 매장의 카카오채널 게시 이미지다. 시세표라면 표를 판독해 모든 행을 추출하라.

판독 규칙:
1. 표값은 대부분 '만원' 단위 — cash_price는 원 단위 정수로 변환(예: 54 → 540000, -10 → -100000). 콤마 원단위 표기(263,000)는 그대로.
2. 음수 = 차비(페이백) 지급. 그대로 음수로.
3. 열/섹션 헤더의 통신사(SK/KT/LG)·가입유형(번이=MNP/기변/신규)을 각 행에 상속하라. 명시가 없으면 null.
4. '결합', '인터넷+TV', '제휴카드', '온누리', '체감가' 조건이 붙은 값은 add_condition에 해당 키워드를 반드시 포함시켜라.
5. 월청구액 형식이면: 현금완납가 ≈ (월청구액 − 요금제 정가) × 약정개월수(미표기 24). estimated=true, add_condition='월청구추정'.
6. 'NNN요금제'는 정가 NNN,000원 (예: 109요금제 → plan_fee 109000).
7. 상품권/사은품/증정/캐시백/페스티벌 '혜택 금액'은 단가가 아니다 — rows에 넣지 마라.
8. 시세표가 아니면(매장 사진, 행사 포스터, 조건 안내문) is_price_table=false, rows=[].
9. 확신 없는 행은 confidence를 낮게(0.5 미만) 매겨라. 임의 추정 금지.

게시글 텍스트 컨텍스트(시세표 보는 법 등):
{context}
"""


def _log(msg: str) -> None:
    print(f"[vision-api] {msg}", file=sys.stderr, flush=True)


def read_image(client: anthropic.Anthropic, entry: dict, model: str) -> dict | None:
    """이미지 1장 판독. 반환: 스키마 준수 dict 또는 None(호출 실패)."""
    try:
        img_b64 = base64.standard_b64encode(Path(entry["file"]).read_bytes()).decode()
    except OSError as e:
        _log(f"{entry['file']} 읽기 실패: {e!r}")
        return None
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text",
                     "text": PROMPT.format(context=(entry.get("context") or "(없음)")[:600])},
                ],
            }],
        )
    except anthropic.RateLimitError:
        _log(f"{model} rate limit — SDK 재시도 소진, 이미지 스킵")
        return None
    except anthropic.APIStatusError as e:
        _log(f"{model} API 오류 {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError as e:
        _log(f"네트워크 오류: {e!r}")
        return None

    if resp.stop_reason == "max_tokens":
        _log(f"{entry['file']}: 출력 상한 도달 — 부분 결과 폐기")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        _log(f"{entry['file']}: JSON 파싱 실패")
        return None


def _sane(row: dict) -> bool:
    if not (PRICE_SANITY[0] <= row.get("cash_price", 0) <= PRICE_SANITY[1]):
        return False
    sg = row.get("storage_gb")
    if sg is not None and sg not in VALID_STORAGE:
        row["storage_gb"] = None    # 용량 오인식은 버리되 행은 유지
    return True


def to_items(entry: dict, result: dict) -> list[dict]:
    """판독 결과 → seongji_vision_data.json 아이템 (기존 세션 판독과 동일 스키마)."""
    today = date.today().isoformat()
    snap = result.get("board_date") or today
    items = []
    for row in result.get("rows", []):
        if not _sane(row) or row.get("confidence", 0) < 0.5:
            continue
        items.append({
            "handle": entry["handle"],
            "post_id": entry["post_id"],
            "image_url": entry["image_url"],
            "snapshot_date": snap,
            "model_name": row["model_name"],
            "storage_gb": row.get("storage_gb"),
            "carrier": row.get("carrier"),
            "subscription_type": row.get("subscription_type"),
            "contract_type": row.get("contract_type"),
            "cash_price": row["cash_price"],
            "plan_name": row.get("plan_name"),
            "plan_fee": row.get("plan_fee"),
            "estimated": bool(row.get("estimated")),
            "add_condition": row.get("add_condition"),
            "confidence": round(float(row.get("confidence", 0.6)), 2),
            "name": entry.get("name"),
            "region": entry.get("region"),
            "posted_at": entry.get("posted_at"),
            "title": entry.get("title"),
            "reader": MODEL if not row.get("_escalated") else ESCALATE_MODEL,
        })
    return items


def merge(new_items: list[dict]) -> int:
    """기존 vision_data 와 병합 — 같은 image_url 기존 항목은 교체."""
    data = {"items": []}
    if VISION_DATA_PATH.exists():
        data = json.loads(VISION_DATA_PATH.read_text(encoding="utf-8"))
    new_urls = {it["image_url"] for it in new_items}
    kept = [it for it in data.get("items", []) if it.get("image_url") not in new_urls]
    data["items"] = kept + new_items
    data["extracted_at"] = date.today().isoformat()
    VISION_DATA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(data["items"])


def main() -> int:
    ap = argparse.ArgumentParser(description="시세표 이미지 Claude API 판독")
    ap.add_argument("--manifest", default=MANIFEST_DEFAULT)
    ap.add_argument("--max-images", type=int, default=MAX_IMAGES)
    ap.add_argument("--dry-run", action="store_true", help="판독만 하고 파일 미변경")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        _log("ANTHROPIC_API_KEY 미설정 — 판독 건너뜀 (graceful skip)")
        return 0
    mf = Path(args.manifest)
    if not mf.exists():
        _log(f"{mf} 없음 — 판독할 이미지 없음")
        return 0

    manifest = json.loads(mf.read_text(encoding="utf-8"))[: args.max_images]
    client = anthropic.Anthropic()   # ANTHROPIC_API_KEY 사용, 429/5xx 자동 재시도

    all_items: list[dict] = []
    channel_ok: dict[str, bool] = {}
    n_esc = 0
    for i, entry in enumerate(manifest, 1):
        result = read_image(client, entry, MODEL)

        # 에스컬레이션: 시세표인데 행이 없거나 평균 신뢰도가 낮으면 상위 모델 1회
        if result and result.get("is_price_table"):
            rows = result.get("rows", [])
            avg_conf = (sum(r.get("confidence", 0) for r in rows) / len(rows)) if rows else 0
            if (not rows or avg_conf < ESCALATE_CONF) and ESCALATE_MODEL != MODEL:
                _log(f"[{i}/{len(manifest)}] {entry['handle']}: 저신뢰(avg {avg_conf:.2f}) → {ESCALATE_MODEL} 재판독")
                esc = read_image(client, entry, ESCALATE_MODEL)
                if esc and len(esc.get("rows", [])) >= len(rows):
                    for r in esc.get("rows", []):
                        r["_escalated"] = True
                    result = esc
                    n_esc += 1

        handle = entry["handle"]
        if result is None:
            # API 호출 실패는 채널 잘못이 아님 — 스킵리스트에 반영하지 않음
            _log(f"[{i}/{len(manifest)}] {handle}: 호출 실패 (skiplist 미반영)")
            continue
        items = to_items(entry, result) if result.get("is_price_table") else []
        channel_ok[handle] = channel_ok.get(handle, False) or bool(items)
        all_items.extend(items)
        _log(f"[{i}/{len(manifest)}] {handle}: "
             f"{'시세표 ' + str(len(items)) + '행' if items else '미검출'}")

    _log(f"판독 완료: 이미지 {len(manifest)}장 → {len(all_items)}행 "
         f"(에스컬레이션 {n_esc}회)")

    if args.dry_run:
        print(json.dumps(all_items[:5], ensure_ascii=False, indent=1))
        return 0

    total = merge(all_items)
    _log(f"seongji_vision_data.json 병합: 총 {total}행")
    try:
        import vision_skiplist
        vision_skiplist.record(channel_ok)
        _log(f"skiplist 갱신: {sum(1 for v in channel_ok.values() if not v)}건 실패 기록")
    except Exception as e:  # noqa: BLE001
        _log(f"skiplist 갱신 실패(계속): {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
