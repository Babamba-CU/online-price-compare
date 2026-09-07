"""
사내망 빌드: PostgreSQL → seongji_data.js / subsidy_data.js

GitLab 으로 받은 화면 코드(index.html)와 별도 경로로 받은 사내 PostgreSQL 을 연결한다.
사내 컨테이너 기동 시(또는 cron) 이 스크립트가 PG 를 읽어 정적 데이터 JS 를 생성하면
index.html 이 그대로 서빙된다. 외부 인터넷·수집 로직 불필요(폐쇄망 OK).

DB 접속은 환경변수(CLAUDE.md 규칙):
  DB_HOST, DB_PORT(기본 5432), DB_USER, DB_PASSWORD, DB_NAME
출력: seongji_data.js, subsidy_data.js  (seongji_build/subsidy_build 와 동일 스키마)

의존성: psycopg2-binary  (requirements 에 포함)
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

DAYS = 30
BOX_WINDOW_DAYS = 14
HISTORY_DAYS = 30
KAKAO_SOURCES = ("kakao", "kakao_ocr")
# 2026-09-08: seongji_build 와 동일하게 네이버만 제외(카카오 판독분은 성지폰 탭에 포함)
NON_SITE = ("naver_cafe", "naver_web", "naver_blog")


def _conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        dbname=os.getenv("DB_NAME", "pricedb"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _s(v):
    """date/datetime → ISO 문자열 (JSON 직렬화 + SQLite 빌드 출력과 동일 형태)."""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    out = []
    for r in cur.fetchall():
        out.append({k: _s(v) for k, v in r.items()})
    return out


def _pct(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx); hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return int(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


# ─────────────────────────── 성지폰 ───────────────────────────
def build_seongji(cur) -> dict:
    """seongji_build 와 같은 '현재 시세' 정의를 쓴다(current_set/box_stats_from/kakao_summary_from 공유)."""
    import seongji_build as sb
    from kst import today_kst
    today = today_kst()
    latest = today.isoformat()
    cutoff = (today - timedelta(days=DAYS)).isoformat()

    daily = _rows(cur, """
        SELECT snapshot_date, model_name, carrier, subscription_type,
               sample_count, min_price, median_price, avg_price, max_price,
               min_source, min_url
        FROM seongji_daily_stats WHERE snapshot_date >= %s
        ORDER BY snapshot_date, model_name, carrier
    """, (cutoff,))
    for r in daily:
        r["snapshot_date"] = _s(r["snapshot_date"])

    ph = ",".join(["%s"] * len(NON_SITE))
    row_sql = f"""
        SELECT p.snapshot_date, p.model_name, p.carrier, p.subscription_type,
               p.contract_type, p.storage_gb, p.cash_price, p.monthly_fee,
               p.plan_name, p.plan_duration_mo, p.confidence, p.region, p.add_condition,
               po.source, po.url, po.title, po.posted_at, po.author
        FROM seongji_prices p JOIN seongji_posts po ON po.id = p.post_id
        WHERE p.snapshot_date >= %s AND p.snapshot_date <= %s AND p.cash_price IS NOT NULL
          AND po.source NOT IN ({ph})
        ORDER BY p.model_name, p.carrier, p.cash_price
    """
    win_from = (today - timedelta(days=sb.CURRENT_WINDOW_DAYS)).isoformat()
    obs = _rows(cur, row_sql, (win_from, latest, *NON_SITE))
    stale = False
    if not obs:
        cur.execute("SELECT MAX(snapshot_date) AS m FROM seongji_prices")
        row = cur.fetchone()
        if row and row["m"]:
            win_from = (date.fromisoformat(_s(row["m"])) - timedelta(days=sb.CURRENT_WINDOW_DAYS)).isoformat()
            obs = _rows(cur, row_sql, (win_from, latest, *NON_SITE))
            stale = True
    for r in obs:
        r["snapshot_date"] = _s(r["snapshot_date"])
        r["posted_at"] = _s(r["posted_at"]) if r.get("posted_at") is not None else None
    current = sb.current_set(obs, today)
    detail = current
    kakao = [r for r in current if r["source"] in KAKAO_SOURCES]
    box_stats = sb.box_stats_from(current, BOX_WINDOW_DAYS)
    models = sb.models_from(current)

    runs = _rows(cur, """
        SELECT source, MAX(finished_at) AS finished_at,
               SUM(fetched_posts) AS fetched, SUM(parsed_prices) AS parsed,
               SUM(errors) AS errors
        FROM seongji_crawl_runs WHERE started_at::date >= %s::date
        GROUP BY source
    """, (cutoff,))

    feed = []
    feed_sql = """
        SELECT po.source, po.url, po.title, po.author,
               COALESCE(po.posted_at, po.crawled_at) AS posted_at,
               p.model_name, p.carrier, p.subscription_type, p.cash_price,
               p.confidence, p.region
        FROM seongji_posts po
        LEFT JOIN seongji_prices p ON p.id = (
            SELECT p2.id FROM seongji_prices p2 WHERE p2.post_id = po.id
            ORDER BY p2.confidence DESC NULLS LAST, (p2.cash_price IS NULL), p2.id LIMIT 1)
        WHERE po.source IN ({ph})
        ORDER BY posted_at DESC NULLS LAST, po.id DESC LIMIT 25
    """
    for srcs in (("kakao", "kakao_ocr"), ("naver_cafe", "naver_web", "naver_blog")):
        feed += _rows(cur, feed_sql.format(ph=",".join(["%s"] * len(srcs))), srcs)
    feed.sort(key=lambda r: (r["posted_at"] or ""), reverse=True)

    ksum = sb.kakao_summary_from(kakao, sb.CURRENT_WINDOW_DAYS)
    ksum["stale"] = stale
    ksum["windowFrom"] = win_from
    import kakao_history
    kakao_changes = kakao_history.compute_changes(
        kakao_history.record(kakao, latest), latest)

    return {
        "generatedAt": latest, "latestSnapshot": latest,
        "days": DAYS, "boxWindowDays": BOX_WINDOW_DAYS,
        "currentWindowDays": sb.CURRENT_WINDOW_DAYS, "models": models,
        "carriers": ["SKT", "KT", "LGU+", "알뜰"],
        "subscriptionTypes": ["신규", "MNP", "기변"],
        "daily": daily, "boxStats": box_stats, "detail": detail,
        "runs": runs, "feed": feed, "kakaoStores": kakao, "kakaoSummary": ksum,
        "kakaoChanges": kakao_changes,
    }


def build_subsidy(cur) -> dict:
    today = date.today()
    cutoff = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    cur.execute("SELECT MAX(snapshot_date) AS m FROM subsidy_offers")
    row = cur.fetchone()
    latest = _s(row["m"]) if row and row["m"] else today.isoformat()

    offers_raw = _rows(cur, """
        SELECT o.snapshot_date, o.carrier, o.model_name, o.storage_gb,
               o.subscription_type, o.retail_price, o.plan_name, o.plan_monthly_fee,
               o.subsidy_public, o.subsidy_additional, o.subsidy_total,
               o.select_discount_24mo, o.contract_months, o.net_buy_price,
               o.monthly_device_fee, o.source_url, o.raw_payload,
               d.released_at, d.manufacturer, d.storage_options
        FROM subsidy_offers o LEFT JOIN subsidy_devices d ON d.model_name = o.model_name
        WHERE o.snapshot_date = %s
    """, (latest,))

    offers, seen, dupes, errors = [], set(), 0, 0
    for o in offers_raw:
        key = (o["snapshot_date"], o["carrier"], o["model_name"],
               o.get("storage_gb"), o.get("subscription_type"))
        if key in seen:
            dupes += 1; continue
        seen.add(key)
        retail = o.get("retail_price") or 0
        pub = o.get("subsidy_public") or 0
        add = o.get("subsidy_additional") or 0
        net = o.get("net_buy_price") or 0
        if min(retail, pub, add, net, o.get("plan_monthly_fee") or 0) < 0 \
           or (pub + add > retail and retail > 0):
            errors += 1; continue
        if isinstance(o.get("raw_payload"), str):
            try:
                o["raw_payload_obj"] = json.loads(o["raw_payload"])
            except Exception:
                o["raw_payload_obj"] = None
        offers.append(o)

    appearing = {(o["model_name"], o.get("storage_gb")) for o in offers}
    device_rows = _rows(cur, """
        SELECT model_name, manufacturer, released_at, storage_options
        FROM subsidy_devices
        ORDER BY COALESCE(released_at, DATE '1900-01-01') DESC, model_name
    """)
    device_variants = []
    for d in device_rows:
        try:
            opts = json.loads(d.get("storage_options") or "[]") or []
        except Exception:
            opts = []
        for s in opts:
            if (d["model_name"], s) in appearing:
                device_variants.append({
                    "model_name": d["model_name"], "manufacturer": d["manufacturer"],
                    "released_at": d["released_at"], "storage_gb": s,
                    "variant_key": f"{d['model_name']}__{s}",
                    "display_name": f"{d['model_name']} {s}GB",
                })

    changes = _rows(cur, """
        SELECT snapshot_date, carrier, model_name, storage_gb,
               field, old_value, new_value, diff
        FROM subsidy_changes WHERE snapshot_date >= %s
        ORDER BY snapshot_date DESC, carrier, model_name LIMIT 200
    """, ((today - timedelta(days=7)).isoformat(),))

    runs = _rows(cur, """
        SELECT carrier, MAX(finished_at) AS finished_at,
               SUM(upserted) AS upserted, SUM(changed) AS changed, SUM(errors) AS errors
        FROM subsidy_crawl_runs WHERE started_at::date >= %s::date GROUP BY carrier
    """, (cutoff,))

    return {
        "generatedAt": today.isoformat(), "latestSnapshot": latest,
        "historyDays": HISTORY_DAYS, "carriers": ["SKT", "KT", "LGU+"],
        "subscriptionTypes": ["010신규", "MNP", "기변"],
        "deviceVariants": device_variants, "offers": offers,
        "changes": changes, "runs": runs,
        "validation": {"duplicates_dropped": dupes, "invalid_dropped": errors},
    }


def _write(path: str, varname: str, payload: dict):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"// auto-generated by build_from_pg.py (사내 PostgreSQL → 화면)\n")
        f.write(f"window.{varname} = ")
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write(";\n")


def main():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            sj = build_seongji(cur)
            su = build_subsidy(cur)
    finally:
        conn.close()
    _write("seongji_data.js", "SEONGJI_DATA", sj)
    _write("subsidy_data.js", "SUBSIDY_DATA", su)
    print(f"seongji_data.js  daily={len(sj['daily'])} box={len(sj['boxStats'])} "
          f"detail={len(sj['detail'])} kakao={len(sj['kakaoStores'])}/{sj['kakaoSummary']['stores']}점 feed={len(sj['feed'])}",
          file=sys.stderr)
    print(f"subsidy_data.js  variants={len(su['deviceVariants'])} offers={len(su['offers'])}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
