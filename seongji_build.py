"""
SQLite (seongji_prices.db) → seongji_data.js 빌드 스크립트.
대시보드 HTML 이 정적 파일로 바로 읽을 수 있는 JS 데이터 모듈을 생성한다.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from seongji_db import connect, init_db, aggregate_daily

OUT_PATH = Path(__file__).parent / "seongji_data.js"
DAYS = 30
BOX_WINDOW_DAYS = 14   # 박스플롯은 최근 N 일 관측치를 분포로 사용


def build() -> dict:
    init_db()
    today = date.today()
    cutoff = (today - timedelta(days=DAYS)).isoformat()

    with connect() as conn:
        # 통계 머터리얼이 비어있을 수 있으므로 오늘자 재집계 시도
        aggregate_daily(conn, today)

        # 1) daily stats (라인차트용)
        daily = [
            dict(r) for r in conn.execute(
                """
                SELECT snapshot_date, model_name, carrier, subscription_type,
                       sample_count, min_price, median_price, avg_price, max_price,
                       min_source, min_url
                FROM seongji_daily_stats
                WHERE snapshot_date >= ?
                ORDER BY snapshot_date, model_name, carrier
                """,
                (cutoff,),
            )
        ]

        # 2) 최신 일자의 모델/통신사별 상세 (테이블용)
        latest_row = conn.execute(
            "SELECT MAX(snapshot_date) FROM seongji_daily_stats"
        ).fetchone()
        latest = latest_row[0] if latest_row and latest_row[0] else today.isoformat()

        detail = [
            dict(r) for r in conn.execute(
                """
                SELECT p.snapshot_date, p.model_name, p.carrier, p.subscription_type,
                       p.contract_type, p.storage_gb, p.cash_price, p.monthly_fee,
                       p.plan_name, p.plan_duration_mo, p.confidence,
                       po.source, po.url, po.title, po.posted_at
                FROM seongji_prices p
                JOIN seongji_posts  po ON po.id = p.post_id
                WHERE p.snapshot_date = ?
                  AND p.cash_price IS NOT NULL
                ORDER BY p.model_name, p.carrier, p.cash_price
                """,
                (latest,),
            )
        ]

        # 3) 박스플롯 통계 — 최근 BOX_WINDOW_DAYS 의 모든 관측치 분포
        box_cutoff = (today - timedelta(days=BOX_WINDOW_DAYS)).isoformat()
        box_rows = list(conn.execute(
            """
            SELECT p.model_name,
                   COALESCE(p.carrier, '?')           AS carrier,
                   COALESCE(p.subscription_type, '?') AS sub,
                   p.cash_price
            FROM seongji_prices p
            WHERE p.snapshot_date >= ?
              AND p.cash_price IS NOT NULL
              AND p.cash_price > 0
            """,
            (box_cutoff,),
        ))

        # (model, carrier, sub) → [prices...]
        buckets: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for r in box_rows:
            buckets[(r[0], r[1], r[2])].append(r[3])

        def _pct(sorted_vals: list[int], q: float) -> int:
            """선형 보간 분위수. q ∈ [0,1]."""
            if not sorted_vals:
                return 0
            if len(sorted_vals) == 1:
                return sorted_vals[0]
            idx = q * (len(sorted_vals) - 1)
            lo = int(idx)
            hi = min(lo + 1, len(sorted_vals) - 1)
            frac = idx - lo
            return int(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)

        box_stats = []
        for (model, carrier, sub), vals in buckets.items():
            vals.sort()
            box_stats.append({
                "model_name":        model,
                "carrier":           carrier,
                "subscription_type": sub,
                "count":             len(vals),
                "min":               vals[0],
                "p25":               _pct(vals, 0.25),
                "median":            _pct(vals, 0.50),
                "p70":               _pct(vals, 0.70),   # 상위30% 경계
                "p75":               _pct(vals, 0.75),
                "max":               vals[-1],
                "avg":               int(sum(vals) / len(vals)),
            })

        # 4) 모델 옵션
        models = [r[0] for r in conn.execute(
            "SELECT DISTINCT model_name FROM seongji_prices ORDER BY model_name"
        )]

        # 5) 크롤링 런 로그
        runs = [
            dict(r) for r in conn.execute(
                """
                SELECT source, MAX(finished_at) AS finished_at,
                       SUM(fetched_posts) AS fetched, SUM(parsed_prices) AS parsed,
                       SUM(errors) AS errors
                FROM seongji_crawl_runs
                WHERE date(started_at) >= date(?)
                GROUP BY source
                """,
                (cutoff,),
            )
        ]

        # 6) 실시간 수집 피드 — Naver 출처 최근 30건 (게시글당 최고신뢰 가격 1건 첨부)
        feed = [
            dict(r) for r in conn.execute(
                """
                SELECT po.source, po.url, po.title,
                       COALESCE(po.posted_at, po.crawled_at) AS posted_at,
                       p.model_name, p.carrier, p.subscription_type,
                       p.cash_price, p.confidence
                FROM seongji_posts po
                LEFT JOIN seongji_prices p ON p.id = (
                    SELECT p2.id FROM seongji_prices p2
                    WHERE p2.post_id = po.id
                    ORDER BY p2.confidence DESC, p2.cash_price IS NULL, p2.id
                    LIMIT 1)
                WHERE po.source LIKE 'naver%'
                ORDER BY posted_at DESC, po.id DESC
                LIMIT 30
                """
            )
        ]

    return {
        "generatedAt":     date.today().isoformat(),
        "latestSnapshot":  latest,
        "days":            DAYS,
        "boxWindowDays":   BOX_WINDOW_DAYS,
        "models":          models,
        "carriers":        ["SKT", "KT", "LGU+", "알뜰"],
        "subscriptionTypes": ["신규", "MNP", "기변"],
        "daily":           daily,
        "boxStats":        box_stats,
        "detail":          detail,
        "runs":            runs,
        "feed":            feed,
    }


def main() -> None:
    payload = build()
    js = "// auto-generated by seongji_build.py — do not edit\n"
    js += "window.SEONGJI_DATA = "
    js += json.dumps(payload, ensure_ascii=False, indent=2)
    js += ";\n"
    OUT_PATH.write_text(js, encoding="utf-8")
    print(
        f"wrote {OUT_PATH}  "
        f"(daily={len(payload['daily'])}, box={len(payload['boxStats'])}, "
        f"detail={len(payload['detail'])}, feed={len(payload['feed'])})"
    )


if __name__ == "__main__":
    main()
