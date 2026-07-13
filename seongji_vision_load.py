"""
seongji_vision_data.json (Claude 세션이 시세표 이미지를 판독한 결과) → seongji_db 적재.

컨테이너 일일 갱신(app.py refresh_data)에서 호출된다.
파일이 없거나 비어 있으면 조용히 건너뜀 (graceful skip).

JSON 스키마:
{
  "extracted_at": "...",
  "items": [
    {handle, post_id, image_url, snapshot_date, model_name, storage_gb,
     carrier, subscription_type, contract_type, cash_price,
     plan_name, plan_fee, estimated, add_condition, confidence,
     name, region, posted_at, title}
  ]
}
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from seongji_db import aggregate_daily, connect, init_db, insert_prices, log_run, upsert_post

DATA_PATH = Path(__file__).parent / "seongji_vision_data.json"
PRICE_SANITY = (-500_000, 3_000_000)
MIN_CONFIDENCE = 0.6
# 신선도(사용자 확정 2026-07-13, 구현 수정): posted_at 은 신뢰 불가 —
# 매장들이 옛 게시글의 이미지를 제자리 교체해 게시일이 2023~2025로 남는다(실측).
# 대신 '자동화 판독분(reader 태그)만 적재'한다: 배치는 항상 채널의 현재 노출
# 이미지를 받으므로 판독 시점이 곧 신선도. 6월 레거시 판독분(무태그)은 제외.
INCLUDE_LEGACY = os.getenv("VISION_INCLUDE_LEGACY", "") == "1"
# 비휴대폰 제외(사용자 확정): 워치/태블릿/버즈 등 — 저가·키즈폰은 유지
NON_PHONE_RE = re.compile(r"(?i)watch|워치|buds|버즈|\btab\b|태블릿|ipad|아이패드|플립\s*워치")

# 사용자 확정 제외 규칙 (2026-06-13):
#  - 결합(인터넷+TV)·제휴카드 조건 포함가 → 순수 단말 시세가 아니므로 제외
#  - 온누리상품권 반영 '체감가' → 실결제액 왜곡이므로 제외
EXCLUDE_CONDITION_RE = re.compile(
    r"결합|인터넷\s*\+?\s*TV|제휴\s*카드|온누리|체감가")


def _log(msg: str) -> None:
    print(f"[seongji_vision] {msg}", file=sys.stderr, flush=True)


def load() -> dict:
    if not DATA_PATH.exists():
        _log("seongji_vision_data.json 없음 — 건너뜀")
        return {"skipped": True}
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _log(f"JSON 파싱 실패 — 건너뜀: {e!r}")
        return {"skipped": True}
    items = data.get("items", [])
    if not items:
        _log("판독 항목 없음 — 건너뜀")
        return {"skipped": True}

    init_db()
    started = datetime.utcnow()
    n_posts = n_prices = 0
    snapshots: set[str] = set()

    # 신선도 필터: 자동화 판독분(reader 태그)만 적재 — 레거시(6월 수동 판독)는
    # 이미지 교체 여부를 알 수 없어 제외. 비휴대폰(워치 등)도 제외.
    fresh, legacy, nonphone = [], 0, 0
    for it in items:
        if NON_PHONE_RE.search(it.get("model_name") or ""):
            nonphone += 1
            continue
        if not it.get("reader") and not INCLUDE_LEGACY:
            legacy += 1
            continue
        fresh.append(it)
    if legacy or nonphone:
        _log(f"신선도 필터: 레거시 {legacy}행 제외(reader 태그 없음) · 비휴대폰 {nonphone}행 제외")
    items = fresh

    # 게시글 단위로 그룹화 (한 게시글 이미지에서 여러 행)
    by_post: dict[str, list[dict]] = {}
    for it in items:
        by_post.setdefault(f"{it['handle']}/{it['post_id']}", []).append(it)

    with connect() as conn:
        for key, rows in by_post.items():
            first = rows[0]
            post_id = upsert_post(conn, {
                "source": "kakao_ocr",
                "source_post_id": key,
                "url": f"https://pf.kakao.com/{key}",
                "title": (first.get("title") or "시세표 이미지")[:80],
                "author": first.get("name"),
                "posted_at": first.get("posted_at"),
                "raw_text": f"vision 판독 {len(rows)}행 (extracted_at={data.get('extracted_at')})",
            })
            n_posts += 1
            price_rows = []
            for it in rows:
                cash = it.get("cash_price")
                conf = it.get("confidence", 0.7)
                if cash is None or conf < MIN_CONFIDENCE:
                    continue
                if not (PRICE_SANITY[0] <= cash <= PRICE_SANITY[1]):
                    continue
                # 결합/제휴카드/온누리 조건부 가격도 적재한다(전체 커버리지).
                # 단순 단말 시세와 구분되도록 add_condition 에 사유를 남겨
                # 대시보드에서 기본 필터(조건부 제외)로 토글 가능하게 한다.
                # 배치 다운로드는 각 채널의 '최신' 시세표 이미지를 받은 시점 캡처이므로
                # 모두 오늘 스냅샷으로 재스탬프한다(Vision 이 추정한 표기일은 게시글
                # 생성연도 혼동 등으로 부정확). 원 표기일은 add_condition 에 보존.
                orig = it.get("snapshot_date") or ""
                snap = date.today().isoformat()
                if orig and orig != snap:
                    it = {**it, "add_condition":
                          f"{it.get('add_condition') or ''} 시세표기준일 {orig}".strip()}
                snapshots.add(snap)
                price_rows.append({
                    "snapshot_date": snap,
                    "model_name": it["model_name"],
                    "model_raw": it.get("model_raw") or it["model_name"],
                    "carrier": it.get("carrier"),
                    "subscription_type": it.get("subscription_type"),
                    "contract_type": it.get("contract_type"),
                    "storage_gb": it.get("storage_gb"),
                    "cash_price": cash,
                    "plan_name": it.get("plan_name"),
                    "plan_duration_mo": it.get("duration_mo"),
                    "add_condition": it.get("add_condition") or ("월청구추정" if it.get("estimated") else None),
                    "region": it.get("region"),
                    "confidence": conf,
                    "raw_text": f"vision: {it.get('image_url', '')[:150]}",
                })
            # insert_prices 는 단일 snapshot 전제 — 날짜별로 나눠 적재
            for snap in {r["snapshot_date"] for r in price_rows}:
                n_prices += insert_prices(
                    conn, post_id, [r for r in price_rows if r["snapshot_date"] == snap])

        for snap in snapshots:
            aggregate_daily(conn, date.fromisoformat(snap))
        log_run(conn, "kakao_ocr", started, datetime.utcnow(),
                n_posts, n_prices, 0, "ok", None)

    _log(f"게시글 {n_posts}건 · 가격 {n_prices}건 적재 (스냅샷 {sorted(snapshots)})")
    return {"posts": n_posts, "prices": n_prices}


if __name__ == "__main__":
    print(load(), file=sys.stderr)
