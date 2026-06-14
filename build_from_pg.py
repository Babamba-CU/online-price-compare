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
NON_SITE = KAKAO_SOURCES + ("naver_cafe", "naver_web")


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
    today = date.today()
    cutoff = (today - timedelta(days=DAYS)).isoformat()

    daily = _rows(cur, """
        SELECT snapshot_date, model_name, carrier, subscription_type,
               sample_count, min_price, median_price, avg_price, max_price,
               min_source, min_url
        FROM seongji_daily_stats WHERE snapshot_date >= %s
        ORDER BY snapshot_date, model_name, carrier
    """, (cutoff,))

    cur.execute("SELECT MAX(snapshot_date) AS m FROM seongji_daily_stats")
    row = cur.fetchone()
    latest = _s(row["m"]) if row and row["m"] else today.isoformat()

    ph = ",".join(["%s"] * len(NON_SITE))
    detail = _rows(cur, f"""
        SELECT p.snapshot_date, p.model_name, p.carrier, p.subscription_type,
               p.contract_type, p.storage_gb, p.cash_price, p.monthly_fee,
               p.plan_name, p.plan_duration_mo, p.confidence, p.region, p.add_condition,
               po.source, po.url, po.title, po.posted_at, po.author
        FROM seongji_prices p JOIN seongji_posts po ON po.id = p.post_id
        WHERE p.snapshot_date = %s AND p.cash_price IS NOT NULL
          AND po.source NOT IN ({ph})
        ORDER BY p.model_name, p.carrier, p.cash_price
    """, (latest, *NON_SITE))

    box_cutoff = (today - timedelta(days=BOX_WINDOW_DAYS)).isoformat()
    box_rows = _rows(cur, f"""
        SELECT p.model_name, COALESCE(p.carrier,'?') AS carrier,
               COALESCE(p.subscription_type,'?') AS sub, p.cash_price
        FROM seongji_prices p JOIN seongji_posts po ON po.id = p.post_id
        WHERE p.snapshot_date >= %s AND p.cash_price IS NOT NULL AND p.cash_price > 0
          AND po.source NOT IN ({ph})
    """, (box_cutoff, *NON_SITE))
    buckets = defaultdict(list)
    for r in box_rows:
        buckets[(r["model_name"], r["carrier"], r["sub"])].append(r["cash_price"])
    box_stats = []
    for (model, carrier, sub), vals in buckets.items():
        vals.sort()
        box_stats.append({
            "model_name": model, "carrier": carrier, "subscription_type": sub,
            "count": len(vals), "min": vals[0], "p25": _pct(vals, .25),
            "median": _pct(vals, .5), "p70": _pct(vals, .7), "p75": _pct(vals, .75),
            "max": vals[-1], "avg": int(sum(vals) / len(vals)),
        })

    models = [r["model_name"] for r in _rows(cur, f"""
        SELECT DISTINCT p.model_name FROM seongji_prices p
        JOIN seongji_posts po ON po.id = p.post_id
        WHERE po.source NOT IN ({ph}) ORDER BY p.model_name
    """, NON_SITE)]

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
    for srcs in (("kakao", "kakao_ocr"), ("naver_cafe", "naver_web")):
        feed += _rows(cur, feed_sql.format(ph=",".join(["%s"] * len(srcs))), srcs)
    feed.sort(key=lambda r: (r["posted_at"] or ""), reverse=True)

    kakao = _rows(cur, f"""
        SELECT p.snapshot_date, p.model_name, p.carrier, p.subscription_type,
               p.storage_gb, p.cash_price, p.plan_name, p.plan_duration_mo,
               p.add_condition, p.confidence, p.region,
               po.source, po.url, po.title, po.posted_at, po.author
        FROM seongji_prices p JOIN seongji_posts po ON po.id = p.post_id
        WHERE p.snapshot_date = %s AND p.cash_price IS NOT NULL
          AND po.source IN ({",".join(["%s"] * len(KAKAO_SOURCES))})
        ORDER BY po.author, p.model_name, p.storage_gb, p.cash_price
    """, (latest, *KAKAO_SOURCES))
    ksum = {
        "stores": len({r["author"] for r in kakao if r["author"]}),
        "regions": len({r["region"] for r in kakao if r["region"]}),
        "rows": len(kakao),
        "models": len({r["model_name"] for r in kakao}),
        "negative": sum(1 for r in kakao if (r["cash_price"] or 0) < 0),
    }

    return {
        "generatedAt": today.isoformat(), "latestSnapshot": latest,
        "days": DAYS, "boxWindowDays": BOX_WINDOW_DAYS, "models": models,
        "carriers": ["SKT", "KT", "LGU+", "알뜰"],
        "subscriptionTypes": ["신규", "MNP", "기변"],
        "daily": daily, "boxStats": box_stats, "detail": detail,
        "runs": runs, "feed": feed, "kakaoStores": kakao, "kakaoSummary": ksum,
    }


# ─────────────────────────── 공시지원금 ───────────────────────────
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
