"""
SQLite (subsidy_offers.db) → subsidy_data.js 빌드 + 정합성 검증.

검증 규칙:
  1) 음수 가격/지원금 없음
  2) 공통지원금 + 추가지원금 ≤ 출고가 (지원금이 출고가를 초과하지 않음)
  3) net_buy_price ≥ 0
  4) 동일 (snapshot_date, carrier, model_name, storage_gb) 중복 없음
  5) 출고가가 각사 사이트별로 다를 수 있음 (각사 그대로 적재 — 검증 통과)
  6) 추가지원금 정책 (참고):
       SKT  → 사이트에 명시
       KT   → 대부분 0원
       LGU+ → 유통망지원금

검증 실패시 stderr 로 경고 출력하고 해당 offer 는 build 대상에서 제외.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from subsidy_db import connect, init_db

OUT = Path(__file__).parent / "subsidy_data.js"
HISTORY_DAYS = 30


def validate_offer(o: dict) -> list[str]:
    """offer 한 건에 대한 검증 메시지 반환. 빈 리스트면 통과."""
    errs: list[str] = []
    retail = o.get("retail_price") or 0
    pub    = o.get("subsidy_public") or 0
    add    = o.get("subsidy_additional") or 0
    net    = o.get("net_buy_price") or 0
    plan   = o.get("plan_monthly_fee") or 0

    if retail < 0:        errs.append(f"음수 출고가 ({retail})")
    if pub    < 0:        errs.append(f"음수 공통지원금 ({pub})")
    if add    < 0:        errs.append(f"음수 추가지원금 ({add})")
    if plan   < 0:        errs.append(f"음수 요금제 ({plan})")
    if pub + add > retail and retail > 0:
        errs.append(f"지원금 합({pub+add:,}) > 출고가({retail:,})")
    if net < 0:           errs.append(f"음수 구매가격 ({net})")
    return errs


def build() -> dict:
    init_db()
    today = date.today()
    cutoff = (today - timedelta(days=HISTORY_DAYS)).isoformat()

    with connect() as conn:
        row = conn.execute("SELECT MAX(snapshot_date) FROM subsidy_offers").fetchone()
        latest = row[0] if row and row[0] else today.isoformat()

        offers_raw = [
            dict(r) for r in conn.execute(
                """
                SELECT
                  o.snapshot_date, o.carrier, o.model_name, o.storage_gb,
                  o.subscription_type,
                  o.retail_price, o.plan_name, o.plan_monthly_fee,
                  o.subsidy_public, o.subsidy_additional, o.subsidy_total,
                  o.select_discount_24mo, o.contract_months,
                  o.net_buy_price, o.monthly_device_fee,
                  o.source_url, o.raw_payload,
                  d.released_at, d.manufacturer, d.storage_options
                FROM subsidy_offers o
                LEFT JOIN subsidy_devices d ON d.model_name = o.model_name
                WHERE o.snapshot_date = ?
                """,
                (latest,),
            )
        ]

        # 정합성 검증 + 중복 체크
        offers: list[dict] = []
        seen: set[tuple] = set()
        dupes = errors = 0
        for o in offers_raw:
            key = (o["snapshot_date"], o["carrier"], o["model_name"],
                   o.get("storage_gb"), o.get("subscription_type"))
            if key in seen:
                dupes += 1
                print(f"[warn] duplicate offer dropped: {key}", file=sys.stderr)
                continue
            seen.add(key)
            errs = validate_offer(o)
            if errs:
                errors += 1
                print(f"[warn] invalid offer {key} → {', '.join(errs)}", file=sys.stderr)
                continue
            # raw_payload(JSON 문자열) 풀어주기
            if isinstance(o.get("raw_payload"), str):
                try:
                    o["raw_payload_obj"] = json.loads(o["raw_payload"])
                except Exception:
                    o["raw_payload_obj"] = None
            offers.append(o)

        # 단말 variant 목록 (model + storage 각각 분리) — 출시일 내림차순
        # 1) 단말 마스터에서 storage_options 펼침
        device_rows = [
            dict(r) for r in conn.execute(
                """
                SELECT model_name, manufacturer, released_at, storage_options
                FROM subsidy_devices
                ORDER BY COALESCE(released_at, '1900-01-01') DESC, model_name
                """
            )
        ]

        # 실제 offer 에 등장한 (model, storage) 만 노출
        appearing = {(o["model_name"], o.get("storage_gb")) for o in offers}
        device_variants: list[dict] = []
        for d in device_rows:
            try:
                opts = json.loads(d.get("storage_options") or "[]") or []
            except Exception:
                opts = []
            for s in opts:
                if (d["model_name"], s) in appearing:
                    device_variants.append({
                        "model_name":   d["model_name"],
                        "manufacturer": d["manufacturer"],
                        "released_at":  d["released_at"],
                        "storage_gb":   s,
                        "variant_key":  f"{d['model_name']}__{s}",
                        "display_name": f"{d['model_name']} {s}GB",
                    })

        # 변동 이력 (최근 7일)
        changes = [
            dict(r) for r in conn.execute(
                """
                SELECT snapshot_date, carrier, model_name, storage_gb,
                       field, old_value, new_value, diff
                FROM subsidy_changes
                WHERE snapshot_date >= ?
                ORDER BY snapshot_date DESC, carrier, model_name
                LIMIT 200
                """,
                ((today - timedelta(days=7)).isoformat(),),
            )
        ]

        runs = [
            dict(r) for r in conn.execute(
                """
                SELECT carrier, MAX(finished_at) AS finished_at,
                       SUM(upserted) AS upserted, SUM(changed) AS changed,
                       SUM(errors)   AS errors
                FROM subsidy_crawl_runs
                WHERE date(started_at) >= date(?)
                GROUP BY carrier
                """,
                (cutoff,),
            )
        ]

    print(f"validation: dropped {dupes} duplicates, {errors} invalid offers", file=sys.stderr)

    return {
        "generatedAt":       date.today().isoformat(),
        "latestSnapshot":    latest,
        "historyDays":       HISTORY_DAYS,
        "carriers":          ["SKT", "KT", "LGU+"],
        "subscriptionTypes": ["010신규", "MNP", "기변"],
        "deviceVariants":    device_variants,
        "offers":            offers,
        "changes":           changes,
        "runs":              runs,
        "validation": {
            "duplicates_dropped": dupes,
            "invalid_dropped":    errors,
        },
    }


def main() -> None:
    payload = build()
    out = "// auto-generated by subsidy_build.py\n"
    out += "window.SUBSIDY_DATA = "
    out += json.dumps(payload, ensure_ascii=False, indent=2)
    out += ";\n"
    OUT.write_text(out, encoding="utf-8")
    print(f"wrote {OUT}  (variants={len(payload['deviceVariants'])}, "
          f"offers={len(payload['offers'])}, changes={len(payload['changes'])})")


if __name__ == "__main__":
    main()
