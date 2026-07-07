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

# 카카오 성지 채널 계열 소스 — "카카오 성지" 탭에서만 매장별로 보여준다.
KAKAO_SOURCES = ("kakao", "kakao_ocr")
_KAKAO_IN = "(" + ",".join(f"'{s}'" for s in KAKAO_SOURCES) + ")"

# 기존 "성지폰 단가 비교" 뷰(전국 온라인 시세)는 사이트 크롤러 소스만 보여준다.
# 카카오(매장 좌표)·네이버(검색 피드)는 집계 차원/품질이 달라 제외.
_NON_SITE = KAKAO_SOURCES + ("naver_cafe", "naver_web", "naver_blog")
_NON_SITE_IN = "(" + ",".join(f"'{s}'" for s in _NON_SITE) + ")"


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
                f"""
                SELECT p.snapshot_date, p.model_name, p.carrier, p.subscription_type,
                       p.contract_type, p.storage_gb, p.cash_price, p.monthly_fee,
                       p.plan_name, p.plan_duration_mo, p.confidence, p.region,
                       p.add_condition,
                       po.source, po.url, po.title, po.posted_at, po.author
                FROM seongji_prices p
                JOIN seongji_posts  po ON po.id = p.post_id
                WHERE p.snapshot_date = ?
                  AND p.cash_price IS NOT NULL
                  AND po.source NOT IN {_NON_SITE_IN}
                ORDER BY p.model_name, p.carrier, p.cash_price
                """,
                (latest,),
            )
        ]

        # 3) 박스플롯 통계 — 최근 BOX_WINDOW_DAYS 의 모든 관측치 분포
        box_cutoff = (today - timedelta(days=BOX_WINDOW_DAYS)).isoformat()
        box_rows = list(conn.execute(
            f"""
            SELECT p.model_name,
                   COALESCE(p.carrier, '?')           AS carrier,
                   COALESCE(p.subscription_type, '?') AS sub,
                   p.cash_price
            FROM seongji_prices p
            JOIN seongji_posts po ON po.id = p.post_id
            WHERE p.snapshot_date >= ?
              AND p.cash_price IS NOT NULL
              AND p.cash_price > 0
              AND po.source NOT IN {_NON_SITE_IN}
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

        # 4) 모델 옵션 (사이트 시세 뷰 전용 — 카카오 전용 모델 제외)
        models = [r[0] for r in conn.execute(
            f"""
            SELECT DISTINCT p.model_name
            FROM seongji_prices p JOIN seongji_posts po ON po.id = p.post_id
            WHERE po.source NOT IN {_NON_SITE_IN}
            ORDER BY p.model_name
            """
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

        # 6) 실시간 수집 피드 — 소스별 쿼터(카카오 25 + 네이버 25)로 최근글 수집.
        # 네이버는 게시일이 없어 crawled_at(=수집 직후) 기준이라, 단일 정렬로 합치면
        # 갱신 직후 네이버가 피드를 독식함 → 소스별로 뽑은 뒤 병합 정렬.
        FEED_SQL = """
            SELECT po.source, po.url, po.title, po.author,
                   COALESCE(po.posted_at, po.crawled_at) AS posted_at,
                   p.model_name, p.carrier, p.subscription_type,
                   p.cash_price, p.confidence, p.region
            FROM seongji_posts po
            LEFT JOIN seongji_prices p ON p.id = (
                SELECT p2.id FROM seongji_prices p2
                WHERE p2.post_id = po.id
                ORDER BY p2.confidence DESC, p2.cash_price IS NULL, p2.id
                LIMIT 1)
            WHERE po.source IN ({placeholders})
            ORDER BY posted_at DESC, po.id DESC
            LIMIT 25
        """
        feed = []
        for srcs in (("kakao", "kakao_ocr"), ("naver_cafe", "naver_web", "naver_blog")):
            ph = ",".join("?" * len(srcs))
            feed += [dict(r) for r in conn.execute(
                FEED_SQL.format(placeholders=ph), srcs)]
        feed.sort(key=lambda r: (r["posted_at"] or "", ), reverse=True)

        # 7) 카카오 성지 — 매장(판매점)별 수집 단가 (별도 탭). 최신 스냅샷 한정.
        #    출고가/공시지원금은 생략하고 매장이 게시한 가격(현금완납가/실구매추정)만 보여준다.
        kakao_rows = [
            dict(r) for r in conn.execute(
                f"""
                SELECT p.snapshot_date, p.model_name, p.carrier, p.subscription_type,
                       p.storage_gb, p.cash_price, p.plan_name, p.plan_duration_mo,
                       p.add_condition, p.confidence, p.region,
                       po.source, po.url, po.title, po.posted_at, po.author
                FROM seongji_prices p
                JOIN seongji_posts  po ON po.id = p.post_id
                WHERE p.snapshot_date = ?
                  AND p.cash_price IS NOT NULL
                  AND po.source IN {_KAKAO_IN}
                ORDER BY po.author, p.model_name, p.storage_gb, p.cash_price
                """,
                (latest,),
            )
        ]
        kakao_summary = {
            "stores":   len({r["author"] for r in kakao_rows if r["author"]}),
            "regions":  len({r["region"] for r in kakao_rows if r["region"]}),
            "rows":     len(kakao_rows),
            "models":   len({r["model_name"] for r in kakao_rows}),
            "negative": sum(1 for r in kakao_rows if (r["cash_price"] or 0) < 0),
            "subMissing": sum(1 for r in kakao_rows if r["subscription_type"] not in ("MNP", "기변", "신규")),
        }

        # 7-1) 카카오 전일 대비 변화 — 롤링 히스토리에 오늘 집계 반영 후 직전일과 비교.
        #      diff<0 = 가격 인하 = 리베이트 추가(경쟁 공세 신호).
        import kakao_history
        kakao_changes = kakao_history.compute_changes(
            kakao_history.record(kakao_rows, latest), latest)

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
        "kakaoStores":     kakao_rows,
        "kakaoSummary":    kakao_summary,
        "kakaoChanges":    kakao_changes,
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
        f"detail={len(payload['detail'])}, feed={len(payload['feed'])}, "
        f"kakao={len(payload['kakaoStores'])} / {payload['kakaoSummary']['stores']}점)"
    )


if __name__ == "__main__":
    main()
