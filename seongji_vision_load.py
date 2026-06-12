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
import sys
from datetime import date, datetime
from pathlib import Path

from seongji_db import aggregate_daily, connect, init_db, insert_prices, log_run, upsert_post

DATA_PATH = Path(__file__).parent / "seongji_vision_data.json"
PRICE_SANITY = (-500_000, 3_000_000)
MIN_CONFIDENCE = 0.6


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
                # 신선도: 시세표 표기일이 7일 이내면 오늘 스냅샷으로 재스탬프
                # (매일 재생성되는 대시보드에서 계속 보이도록). 원 표기일은 조건에 기록.
                orig = it.get("snapshot_date") or date.today().isoformat()
                try:
                    age = (date.today() - date.fromisoformat(orig)).days
                except ValueError:
                    age = 999
                if 0 < age <= 7:
                    snap = date.today().isoformat()
                    it = {**it, "add_condition":
                          f"{it.get('add_condition') or ''} 시세표기준일 {orig}".strip()}
                else:
                    snap = orig
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
