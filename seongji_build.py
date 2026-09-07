"""
SQLite (seongji_prices.db) → seongji_data.js 빌드 스크립트.
대시보드 HTML 이 정적 파일로 바로 읽을 수 있는 JS 데이터 모듈을 생성한다.

'현재 시세' 정의(2026-09-08 점검 후 확정):
  · 행의 snapshot_date 는 시세표 기준일(board_date, 없으면 판독일)이다 — 적재에서 재스탬프하지 않는다.
  · 현재 시세 집합 = 최근 CURRENT_WINDOW_DAYS 안의 관측 중, 매장×(기종·통신사·가입유형·용량)별
    **가장 최신 기준일** 행만(같은 날 여러 줄은 유지, 완전 중복만 제거). 옛 표가 최신 표에 밀려난다.
  · detail/kakaoStores 의 snapshot_date 는 빌드일(KST)이고 board_date/age_days/store_asof 가 실제 날짜.
  · 일별 통계(daily)는 실제 기준일별 관측 → 시계열이 진짜 날짜로 쌓인다.
  · 조건부 가격(결합·제휴카드·적용가)은 is_conditional 로 표시만 하고 통계에서 제외.
같은 함수(current_set / box_stats_from / kakao_summary_from)를 build_from_pg.py(사내 PG 모드)도 쓴다.
"""
from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from kst import today_kst
from model_normalize import sort_key as model_sort_key
from price_conditions import is_conditional
from seongji_db import connect, init_db, aggregate_daily

OUT_PATH = Path(__file__).parent / "seongji_data.js"
DAYS = 30
BOX_WINDOW_DAYS = 14   # 박스플롯은 현재 시세 집합 중 기준일이 최근 N 일인 행만 분포로 사용
CURRENT_WINDOW_DAYS = int(os.getenv("SEONGJI_CURRENT_DAYS", "30"))   # 이보다 오래된 표는 '현재'가 아님

# 카카오 성지 채널 계열 소스 — "카카오 성지" 탭에서만 매장별로 보여준다.
KAKAO_SOURCES = ("kakao", "kakao_ocr")
_KAKAO_IN = "(" + ",".join(f"'{s}'" for s in KAKAO_SOURCES) + ")"

# "성지폰 단가 비교" 뷰(전국 온라인 시세)는 실판독 데이터(사이트 크롤러 + 카카오 시세표
# 판독)를 모두 집계한다. 네이버(검색 피드)는 집계 차원/품질이 달라 제외.
_NON_SITE = ("naver_cafe", "naver_web", "naver_blog")
_NON_SITE_IN = "(" + ",".join(f"'{s}'" for s in _NON_SITE) + ")"

_ROW_COLS = """p.snapshot_date, p.model_name, p.carrier, p.subscription_type,
               p.contract_type, p.storage_gb, p.cash_price, p.monthly_fee,
               p.plan_name, p.plan_duration_mo, p.confidence, p.region,
               p.add_condition,
               po.source, po.url, po.title, po.posted_at, po.author"""


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


def _offer_key(r: dict) -> tuple:
    return (r.get("author"), r["model_name"], r.get("carrier"),
            r.get("subscription_type"), r.get("storage_gb"))


def current_set(rows: list[dict], today: date) -> list[dict]:
    """관측 행(snapshot_date=기준일) → 현재 시세 집합.

    매장×오퍼키(기종·통신사·가입유형·용량)별로 가장 최신 기준일의 행만 남긴다. 같은 날의
    여러 줄(현금가/적용가, 선약 등)은 모두 유지하고 완전 중복(같은 값)만 제거한다.
    반환 행에는 board_date / age_days / store_asof / is_conditional 을 붙이고
    snapshot_date 를 빌드일로 바꾼다(화면은 '오늘 기준 현재 시세'로 읽는다).
    """
    latest: dict[tuple, str] = {}
    for r in rows:
        k = _offer_key(r)
        if r["snapshot_date"] > latest.get(k, ""):
            latest[k] = r["snapshot_date"]
    out, seen = [], set()
    for r in rows:
        k = _offer_key(r)
        if r["snapshot_date"] != latest[k]:
            continue
        dk = k + (r.get("contract_type"), r.get("plan_name"), r.get("add_condition"), r.get("cash_price"))
        if dk in seen:
            continue
        seen.add(dk)
        out.append(dict(r))
    store_asof: dict[str, str] = {}
    for r in out:
        a = r.get("author") or ""
        store_asof[a] = max(store_asof.get(a, ""), r["snapshot_date"])
    today_iso = today.isoformat()
    for r in out:
        bd = r["snapshot_date"]
        r["board_date"] = bd
        try:
            r["age_days"] = (today - date.fromisoformat(bd)).days
        except ValueError:
            r["age_days"] = None
        r["store_asof"] = store_asof.get(r.get("author") or "")
        r["is_conditional"] = is_conditional(r.get("add_condition"))
        r["snapshot_date"] = today_iso
    return out


def box_stats_from(rows: list[dict], window_days: int = BOX_WINDOW_DAYS) -> list[dict]:
    """현재 시세 집합 → (기종×통신사×가입유형) 분포. 조건부 제외, 기준일 최근 window_days 만.
    차비(음수)·0원도 실제 거래가라 포함한다."""
    buckets: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    stores: dict[tuple[str, str, str], set] = defaultdict(set)
    for r in rows:
        if r.get("cash_price") is None or r.get("is_conditional"):
            continue
        if r.get("age_days") is None or r["age_days"] > window_days:
            continue
        k = (r["model_name"], r.get("carrier") or "?", r.get("subscription_type") or "?")
        buckets[k].append(r["cash_price"])
        stores[k].add(r.get("author") or r.get("url"))
    out = []
    for (model, carrier, sub), vals in buckets.items():
        vals.sort()
        out.append({
            "model_name": model, "carrier": carrier, "subscription_type": sub,
            "count": len(vals), "stores": len(stores[(model, carrier, sub)]),
            "min": vals[0], "p25": _pct(vals, 0.25), "median": _pct(vals, 0.50),
            "p70": _pct(vals, 0.70), "p75": _pct(vals, 0.75), "max": vals[-1],
            "avg": int(sum(vals) / len(vals)),
        })
    return out


def kakao_summary_from(rows: list[dict], window_days: int = CURRENT_WINDOW_DAYS) -> dict:
    stores: dict[str, dict] = {}
    for r in rows:
        a = r.get("author")
        if not a:
            continue
        st = stores.setdefault(a, {"author": a, "region": r.get("region"), "asof": r.get("store_asof"),
                                   "age_days": None, "rows": 0})
        st["rows"] += 1
        if r.get("board_date") == r.get("store_asof") and r.get("age_days") is not None:
            st["age_days"] = r["age_days"] if st["age_days"] is None else min(st["age_days"], r["age_days"])
    store_list = sorted(stores.values(), key=lambda x: (x["asof"] or "", x["author"]), reverse=True)
    return {
        "stores":      len(stores),
        "regions":     len({r["region"] for r in rows if r.get("region")}),
        "rows":        len(rows),
        "models":      len({r["model_name"] for r in rows}),
        "negative":    sum(1 for r in rows if (r.get("cash_price") or 0) < 0),
        "subMissing":  sum(1 for r in rows if r.get("subscription_type") not in ("MNP", "기변", "신규")),
        "conditional": sum(1 for r in rows if r.get("is_conditional")),
        "windowDays":  window_days,
        "storesFresh7": sum(1 for x in store_list if x["age_days"] is not None and x["age_days"] <= 7),
        "storeList":   store_list,
    }


def models_from(rows: list[dict]) -> list[str]:
    return sorted({r["model_name"] for r in rows}, key=model_sort_key)


def build() -> dict:
    init_db()
    today = today_kst()
    latest = today.isoformat()
    cutoff = (today - timedelta(days=DAYS)).isoformat()

    with connect() as conn:
        # 통계 머터리얼 — 존재하는 모든 기준일에 대해 재집계(적재가 이미 했지만 빌드 단독 실행 대비)
        for (d,) in conn.execute("SELECT DISTINCT snapshot_date FROM seongji_prices WHERE snapshot_date >= ?", (cutoff,)):
            aggregate_daily(conn, date.fromisoformat(d))

        # 1) daily stats (라인차트용) — 실제 기준일별 관측 통계
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

        # 2) 현재 시세 집합 — 최근 CURRENT_WINDOW_DAYS 관측 → 매장×오퍼 최신만
        def _window_rows(win_from: str) -> list[dict]:
            return [dict(r) for r in conn.execute(
                f"""
                SELECT {_ROW_COLS}
                FROM seongji_prices p
                JOIN seongji_posts  po ON po.id = p.post_id
                WHERE p.snapshot_date >= ?
                  AND p.snapshot_date <= ?
                  AND p.cash_price IS NOT NULL
                  AND po.source NOT IN {_NON_SITE_IN}
                ORDER BY p.model_name, p.carrier, p.cash_price
                """,
                (win_from, latest),
            )]
        win_from = (today - timedelta(days=CURRENT_WINDOW_DAYS)).isoformat()
        obs = _window_rows(win_from)
        stale = False
        if not obs:
            # 최근 창에 관측이 전혀 없으면(루틴 장기 중단 등) 마지막 관측일 기준 창으로 대체하고 표시
            last = conn.execute("SELECT MAX(snapshot_date) FROM seongji_prices").fetchone()[0]
            if last:
                win_from = (date.fromisoformat(last) - timedelta(days=CURRENT_WINDOW_DAYS)).isoformat()
                obs = _window_rows(win_from)
                stale = True
        current = current_set(obs, today)
        detail = current
        kakao_rows = [r for r in current if r["source"] in KAKAO_SOURCES]

        # 3) 박스플롯 통계 — 현재 시세 집합(조건부 제외, 기준일 최근 BOX_WINDOW_DAYS)
        box_stats = box_stats_from(current, BOX_WINDOW_DAYS)

        # 4) 모델 옵션 — 현재 시세 집합에서 도출(하드코딩 목록 없음). 최신 세대가 위.
        models = models_from(current)

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

        # 7) 카카오 성지 요약 + 전일 대비 변화(현재 시세 집합 기준, 조건부 제외는 history 가 처리)
        kakao_summary = kakao_summary_from(kakao_rows, CURRENT_WINDOW_DAYS)
        kakao_summary["stale"] = stale
        kakao_summary["windowFrom"] = win_from
        import kakao_history
        kakao_changes = kakao_history.compute_changes(
            kakao_history.record(kakao_rows, latest), latest)

    return {
        "generatedAt":     latest,
        "latestSnapshot":  latest,
        "days":            DAYS,
        "boxWindowDays":   BOX_WINDOW_DAYS,
        "currentWindowDays": CURRENT_WINDOW_DAYS,
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
        f"kakao={len(payload['kakaoStores'])} / {payload['kakaoSummary']['stores']}점, "
        f"창 {payload['kakaoSummary']['windowFrom']}~{payload['latestSnapshot']}"
        f"{' [STALE]' if payload['kakaoSummary'].get('stale') else ''})"
    )


if __name__ == "__main__":
    main()
