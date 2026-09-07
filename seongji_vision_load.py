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

from kst import today_kst
from model_normalize import normalize as normalize_model
from seongji_db import aggregate_daily, connect, init_db, insert_prices, log_run, upsert_post
from vision_api_reader import MIN_CONFIDENCE, PRICE_SANITY   # 판독·적재 단일 기준

DATA_PATH = Path(__file__).parent / "seongji_vision_data.json"
# 신선도(사용자 확정 2026-07-13): posted_at 은 신뢰 불가 — 매장들이 옛 게시글의 이미지를
# 제자리 교체해 게시일이 2023~2025로 남는다(실측). '자동화 판독분(reader 태그)만 적재'한다.
# 2026-09-08 변경: 행의 snapshot_date 는 판독기가 표에서 읽은 기준일(없으면 판독일)을 그대로
# 쓴다. 종전엔 전량을 적재일로 재스탬프해 2023년 표까지 '오늘 단가'로 둔갑했다. '현재 시세'는
# seongji_build 가 매장별 최신 관측만 골라 만든다(과거 관측은 일별 통계의 시계열로 남는다).
INCLUDE_LEGACY = os.getenv("VISION_INCLUDE_LEGACY", "") == "1"
# 비휴대폰 제외(사용자 확정): 워치/태블릿/버즈 등 — 저가·키즈폰은 유지
NON_PHONE_RE = re.compile(r"(?i)watch|워치|buds|버즈|\btab\b|태블릿|ipad|아이패드|플립\s*워치")
# 조건부(결합·제휴카드·온누리·적용가) 행은 버리지 않고 적재한다 — 집계 제외·화면 토글은
# price_conditions.is_conditional 로 판정(적재·빌드·히스토리·화면 공통 기준).


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
    today = today_kst().isoformat()
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
                try:
                    conf = float(it.get("confidence")) if it.get("confidence") is not None else 0.7
                except (TypeError, ValueError):
                    conf = 0.0
                if cash is None or conf < MIN_CONFIDENCE:
                    continue
                if not (PRICE_SANITY[0] <= cash <= PRICE_SANITY[1]):
                    continue
                # 기준일 = 판독기가 표에서 읽은 board_date(없으면 판독일). 재스탬프하지 않는다.
                snap = it.get("snapshot_date") or it.get("read_date") or today
                try:
                    date.fromisoformat(snap)
                except (TypeError, ValueError):
                    snap = today
                snapshots.add(snap)
                # 월청구 추정값 표시는 add_condition 이 비었을 때만 채운다(종전엔 기준일 접미어가
                # 먼저 붙어 이 폴백이 영원히 죽어 있었다).
                cond = it.get("add_condition") or ("월청구추정" if it.get("estimated") else None)
                # 기종명 정규화(패턴 기반, 신규 기종 자동 반영) — 원 표기는 model_raw 에 보존.
                price_rows.append({
                    "snapshot_date": snap,
                    "model_name": normalize_model(it["model_name"]),
                    "model_raw": it.get("model_raw") or it["model_name"],
                    "carrier": it.get("carrier"),
                    "subscription_type": it.get("subscription_type"),
                    "contract_type": it.get("contract_type"),
                    "storage_gb": it.get("storage_gb"),
                    "cash_price": cash,
                    "monthly_fee": it.get("plan_fee"),          # 요금제 월정액(원) — 종전엔 버려짐
                    "plan_name": it.get("plan_name"),
                    "plan_duration_mo": it.get("duration_mo"),  # 판독 스키마에 없음(호환용, 보통 None)
                    "add_condition": cond,
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
