"""
사내망 PostgreSQL 적재용 데이터 팩 생성기.

이 환경(인터넷)에서 수집·적재된 SQLite 두 DB
(seongji_prices.db / subsidy_offers.db)를 PostgreSQL 호환 SQL 덤프로 변환해
dist/price-pack-YYYYMMDD/ 에 묶는다. 사내망에서 psql 한 번으로 적재.

설계 원칙 — 주기적(일별) 전달에 안전한 멱등 적재:
  · 차원/마스터 테이블(posts, devices)  → ON CONFLICT DO UPDATE (누적 UPSERT)
  · 일별 사실 테이블(prices, offers, stats) → 해당 snapshot_date 만 DELETE 후 INSERT
      (같은 날짜를 다시 보내면 그 날짜만 교체 — 이력은 보존)
  · 로그 테이블(crawl_runs, changes)    → 단순 INSERT(append)
  · post_id FK 는 (source,url) 자연키로 재해석 → SQLite/PG 간 id 불일치 무관

사용:
  python3 export_pack.py [--out dist] [--date YYYYMMDD] [--no-tar]
산출:
  dist/price-pack-YYYYMMDD/{seongji_pg.sql, subsidy_pg.sql, manifest.json, README.md}
  dist/price-pack-YYYYMMDD.tar.gz
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent
SEONGJI_DB = BASE / "seongji_prices.db"
SUBSIDY_DB = BASE / "subsidy_offers.db"
SCHEMA_VERSION = "1.0.0"


# ---- SQL 리터럴 변환 (PostgreSQL standard_conforming_strings 가정) ----
def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("'", "''")   # 작은따옴표 이스케이프
    return "'" + s + "'"


def cols_vals(row: sqlite3.Row, columns: list[str]) -> tuple[str, str]:
    return (", ".join(columns),
            ", ".join(lit(row[c]) for c in columns))


# ---- PostgreSQL DDL (SQLite schema.sql 의 PG 변환본) ----
DDL_SEONGJI = """\
-- 성지폰 + 카카오 단가 (PostgreSQL)
CREATE TABLE IF NOT EXISTS seongji_posts (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    source_post_id  TEXT,
    url             TEXT NOT NULL,
    title           TEXT,
    author          TEXT,
    posted_at       TIMESTAMP,
    crawled_at      TIMESTAMP NOT NULL,
    raw_text        TEXT,
    view_count      INTEGER,
    comment_count   INTEGER,
    UNIQUE(source, url)
);
CREATE INDEX IF NOT EXISTS idx_posts_source_date ON seongji_posts(source, posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_crawled     ON seongji_posts(crawled_at);

CREATE TABLE IF NOT EXISTS seongji_prices (
    id                  BIGSERIAL PRIMARY KEY,
    post_id             BIGINT NOT NULL REFERENCES seongji_posts(id) ON DELETE CASCADE,
    snapshot_date       DATE NOT NULL,
    carrier             TEXT,
    subscription_type   TEXT,
    contract_type       TEXT,
    model_name          TEXT NOT NULL,
    model_raw           TEXT,
    storage_gb          INTEGER,
    cash_price          INTEGER,
    monthly_fee         INTEGER,
    plan_name           TEXT,
    plan_duration_mo    INTEGER,
    add_condition       TEXT,
    region              TEXT,
    confidence          DOUBLE PRECISION,
    raw_text            TEXT
);
CREATE INDEX IF NOT EXISTS idx_prices_snapshot  ON seongji_prices(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_prices_model     ON seongji_prices(model_name, carrier);
CREATE INDEX IF NOT EXISTS idx_prices_model_sub ON seongji_prices(model_name, carrier, subscription_type, snapshot_date);

CREATE TABLE IF NOT EXISTS seongji_daily_stats (
    snapshot_date       DATE NOT NULL,
    model_name          TEXT NOT NULL,
    carrier             TEXT NOT NULL,
    subscription_type   TEXT NOT NULL,
    sample_count        INTEGER,
    min_price           INTEGER,
    p25_price           INTEGER,
    median_price        INTEGER,
    p75_price           INTEGER,
    max_price           INTEGER,
    avg_price           INTEGER,
    min_source          TEXT,
    min_url             TEXT,
    updated_at          TIMESTAMP,
    PRIMARY KEY (snapshot_date, model_name, carrier, subscription_type)
);

CREATE TABLE IF NOT EXISTS seongji_crawl_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    source          TEXT NOT NULL,
    fetched_posts   INTEGER DEFAULT 0,
    parsed_prices   INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    error_message   TEXT,
    status          TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON seongji_crawl_runs(started_at);
"""

DDL_SUBSIDY = """\
-- 3사 공시지원금 (PostgreSQL)
CREATE TABLE IF NOT EXISTS subsidy_devices (
    id              BIGSERIAL PRIMARY KEY,
    model_name      TEXT NOT NULL UNIQUE,
    manufacturer    TEXT,
    released_at     DATE,
    storage_options TEXT,
    aliases         TEXT
);
CREATE INDEX IF NOT EXISTS idx_devices_released ON subsidy_devices(released_at DESC);

CREATE TABLE IF NOT EXISTS subsidy_offers (
    id                   BIGSERIAL PRIMARY KEY,
    snapshot_date        DATE NOT NULL,
    carrier              TEXT NOT NULL,
    model_name           TEXT NOT NULL,
    storage_gb           INTEGER,
    subscription_type    TEXT NOT NULL DEFAULT 'MNP',
    color                TEXT,
    retail_price         INTEGER,
    plan_name            TEXT,
    plan_monthly_fee     INTEGER,
    subsidy_public       INTEGER,
    subsidy_additional   INTEGER,
    subsidy_total        INTEGER,
    select_discount_24mo INTEGER,
    contract_months      INTEGER DEFAULT 24,
    net_buy_price        INTEGER,
    monthly_device_fee   INTEGER,
    source_url           TEXT NOT NULL,
    source_html_hash     TEXT,
    raw_payload          TEXT,
    fetched_at           TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_date    ON subsidy_offers(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_offers_carrier ON subsidy_offers(carrier, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_offers_model   ON subsidy_offers(model_name, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_offers_lookup  ON subsidy_offers(snapshot_date, model_name, carrier, subscription_type);

CREATE TABLE IF NOT EXISTS subsidy_changes (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_date   DATE NOT NULL,
    carrier         TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    storage_gb      INTEGER,
    field           TEXT NOT NULL,
    old_value       INTEGER,
    new_value       INTEGER,
    diff            INTEGER,
    detected_at     TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_date ON subsidy_changes(snapshot_date DESC);

CREATE TABLE IF NOT EXISTS subsidy_crawl_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    carrier         TEXT NOT NULL,
    fetched_models  INTEGER DEFAULT 0,
    upserted        INTEGER DEFAULT 0,
    changed         INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    status          TEXT,
    error_message   TEXT,
    source_url      TEXT
);
"""


def _rows(db: Path, sql: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def build_seongji_sql() -> tuple[str, dict]:
    out = ["BEGIN;", DDL_SEONGJI, ""]
    counts = {}
    # 적재되는 스냅샷 날짜들
    snaps = [r[0] for r in _rows(SEONGJI_DB,
             "SELECT DISTINCT snapshot_date FROM seongji_prices ORDER BY 1")]
    snap_in = ", ".join(lit(s) for s in snaps) or "NULL"

    # 1) posts — (source,url) UPSERT
    pcols = ["source", "source_post_id", "url", "title", "author",
             "posted_at", "crawled_at", "raw_text", "view_count", "comment_count"]
    posts = _rows(SEONGJI_DB, "SELECT * FROM seongji_posts")
    counts["seongji_posts"] = len(posts)
    out.append("-- seongji_posts (UPSERT by source,url)")
    for r in posts:
        c, v = cols_vals(r, pcols)
        upd = ", ".join(f"{col}=EXCLUDED.{col}" for col in pcols if col not in ("source", "url"))
        out.append(f"INSERT INTO seongji_posts ({c}) VALUES ({v}) "
                   f"ON CONFLICT (source, url) DO UPDATE SET {upd};")

    # 2) prices — 해당 snapshot 만 교체. post_id 는 (source,url) 자연키로 재해석
    out.append(f"\n-- seongji_prices (replace snapshots: {snap_in})")
    out.append(f"DELETE FROM seongji_prices WHERE snapshot_date IN ({snap_in});")
    qcols = ["snapshot_date", "carrier", "subscription_type", "contract_type",
             "model_name", "model_raw", "storage_gb", "cash_price", "monthly_fee",
             "plan_name", "plan_duration_mo", "add_condition", "region",
             "confidence", "raw_text"]
    prices = _rows(SEONGJI_DB, """
        SELECT p.*, po.source AS _src, po.url AS _url
        FROM seongji_prices p JOIN seongji_posts po ON po.id = p.post_id
    """)
    counts["seongji_prices"] = len(prices)
    for r in prices:
        vals = ", ".join(lit(r[c]) for c in qcols)
        out.append(
            f"INSERT INTO seongji_prices (post_id, {', '.join(qcols)}) "
            f"SELECT id, {vals} FROM seongji_posts "
            f"WHERE source={lit(r['_src'])} AND url={lit(r['_url'])};")

    # 3) daily_stats — PK UPSERT
    scols = ["snapshot_date", "model_name", "carrier", "subscription_type",
             "sample_count", "min_price", "p25_price", "median_price", "p75_price",
             "max_price", "avg_price", "min_source", "min_url", "updated_at"]
    stats = _rows(SEONGJI_DB, "SELECT * FROM seongji_daily_stats")
    counts["seongji_daily_stats"] = len(stats)
    out.append("\n-- seongji_daily_stats (UPSERT by PK)")
    for r in stats:
        c, v = cols_vals(r, scols)
        upd = ", ".join(f"{col}=EXCLUDED.{col}" for col in scols[4:])
        out.append(f"INSERT INTO seongji_daily_stats ({c}) VALUES ({v}) "
                   f"ON CONFLICT (snapshot_date, model_name, carrier, subscription_type) "
                   f"DO UPDATE SET {upd};")

    # 4) crawl_runs — append
    rcols = ["started_at", "finished_at", "source", "fetched_posts",
             "parsed_prices", "errors", "error_message", "status"]
    runs = _rows(SEONGJI_DB, "SELECT * FROM seongji_crawl_runs")
    counts["seongji_crawl_runs"] = len(runs)
    out.append("\n-- seongji_crawl_runs (append)")
    for r in runs:
        c, v = cols_vals(r, rcols)
        out.append(f"INSERT INTO seongji_crawl_runs ({c}) VALUES ({v});")

    out.append("\nCOMMIT;")
    return "\n".join(out), counts


def build_subsidy_sql() -> tuple[str, dict]:
    out = ["BEGIN;", DDL_SUBSIDY, ""]
    counts = {}
    snaps = [r[0] for r in _rows(SUBSIDY_DB,
             "SELECT DISTINCT snapshot_date FROM subsidy_offers ORDER BY 1")]
    snap_in = ", ".join(lit(s) for s in snaps) or "NULL"

    # devices — model_name UPSERT
    dcols = ["model_name", "manufacturer", "released_at", "storage_options", "aliases"]
    devs = _rows(SUBSIDY_DB, "SELECT * FROM subsidy_devices")
    counts["subsidy_devices"] = len(devs)
    out.append("-- subsidy_devices (UPSERT by model_name)")
    for r in devs:
        c, v = cols_vals(r, dcols)
        upd = ", ".join(f"{col}=EXCLUDED.{col}" for col in dcols if col != "model_name")
        out.append(f"INSERT INTO subsidy_devices ({c}) VALUES ({v}) "
                   f"ON CONFLICT (model_name) DO UPDATE SET {upd};")

    # offers — snapshot 교체
    ocols = ["snapshot_date", "carrier", "model_name", "storage_gb", "subscription_type",
             "color", "retail_price", "plan_name", "plan_monthly_fee", "subsidy_public",
             "subsidy_additional", "subsidy_total", "select_discount_24mo",
             "contract_months", "net_buy_price", "monthly_device_fee", "source_url",
             "source_html_hash", "raw_payload", "fetched_at"]
    offers = _rows(SUBSIDY_DB, "SELECT * FROM subsidy_offers")
    counts["subsidy_offers"] = len(offers)
    out.append(f"\n-- subsidy_offers (replace snapshots: {snap_in})")
    out.append(f"DELETE FROM subsidy_offers WHERE snapshot_date IN ({snap_in});")
    for r in offers:
        c, v = cols_vals(r, ocols)
        out.append(f"INSERT INTO subsidy_offers ({c}) VALUES ({v});")

    # changes / runs — append
    chcols = ["snapshot_date", "carrier", "model_name", "storage_gb", "field",
              "old_value", "new_value", "diff", "detected_at"]
    chs = _rows(SUBSIDY_DB, "SELECT * FROM subsidy_changes")
    counts["subsidy_changes"] = len(chs)
    if chs:
        out.append("\n-- subsidy_changes (append)")
        for r in chs:
            c, v = cols_vals(r, chcols)
            out.append(f"INSERT INTO subsidy_changes ({c}) VALUES ({v});")

    rcols = ["started_at", "finished_at", "carrier", "fetched_models", "upserted",
             "changed", "errors", "status", "error_message", "source_url"]
    runs = _rows(SUBSIDY_DB, "SELECT * FROM subsidy_crawl_runs")
    counts["subsidy_crawl_runs"] = len(runs)
    out.append("\n-- subsidy_crawl_runs (append)")
    for r in runs:
        c, v = cols_vals(r, rcols)
        out.append(f"INSERT INTO subsidy_crawl_runs ({c}) VALUES ({v});")

    out.append("\nCOMMIT;")
    return "\n".join(out), counts


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


README = """# 성지폰·공시지원금 데이터 팩 (PostgreSQL)

이 환경(인터넷)에서 카카오 시세표(Vision 판독)·네이버·사이트 시세·3사 공시지원금을
수집·정규화한 결과를 PostgreSQL 적재용 SQL 덤프로 묶은 것입니다. 개인정보 없음(공개 시세).

## 구성
- `seongji_pg.sql`  : 성지폰+카카오 단가 (seongji_posts/prices/daily_stats/crawl_runs)
- `subsidy_pg.sql`  : 3사 공시지원금 (subsidy_devices/offers/changes/crawl_runs)
- `manifest.json`   : 스냅샷·테이블 행수·스키마 버전·sha256

## 적재 (psql)
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f seongji_pg.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f subsidy_pg.sql
```
- 멱등 설계: 같은 날짜 팩을 다시 적재하면 해당 `snapshot_date` 만 교체(이력 보존),
  마스터(posts/devices)는 자연키 UPSERT. 매일 새 팩만 적재하면 이력이 누적됩니다.
- 트랜잭션(BEGIN/COMMIT) 단위라 실패 시 롤백.

## 핵심 조회 예시
```sql
-- 카카오 매장별 3사 현금완납가 (조건부 제외 = 순수가)
SELECT region, author, model_name, carrier, subscription_type, cash_price
FROM seongji_prices p JOIN seongji_posts po ON po.id=p.post_id
WHERE po.source='kakao_ocr' AND snapshot_date=CURRENT_DATE
  AND (add_condition IS NULL OR add_condition !~ '결합|제휴|온누리|체감|지원금')
ORDER BY model_name, carrier, cash_price;

-- 성지가 vs 공시 실구매가 (model_name 동일 정규화로 JOIN)
SELECT s.model_name, s.carrier, MIN(s.cash_price) AS 성지최저,
       o.net_buy_price AS 공시실구매가
FROM seongji_prices s
JOIN subsidy_offers o ON o.model_name=s.model_name AND o.carrier=s.carrier
 AND o.subscription_type = CASE s.subscription_type WHEN '신규' THEN 'MNP' ELSE s.subscription_type END
WHERE s.snapshot_date=CURRENT_DATE AND o.snapshot_date=CURRENT_DATE
GROUP BY s.model_name, s.carrier, o.net_buy_price;
```

## 화면(대시보드)도 필요하면
정적 대시보드(index.html + *_data.js)는 별도로 동봉 가능합니다. 단, 사내망은 외부 CDN
차단이라 폰트(Pretendard/JetBrains Mono)·xlsx 라이브러리를 로컬 번들로 교체해야 합니다.
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--no-tar", action="store_true")
    args = ap.parse_args()

    pack = Path(args.out) / f"price-pack-{args.date}"
    pack.mkdir(parents=True, exist_ok=True)

    seongji_sql, sc = build_seongji_sql()
    subsidy_sql, uc = build_subsidy_sql()
    (pack / "seongji_pg.sql").write_text(seongji_sql, encoding="utf-8")
    (pack / "subsidy_pg.sql").write_text(subsidy_sql, encoding="utf-8")
    (pack / "README.md").write_text(README, encoding="utf-8")

    snaps_s = [r[0] for r in _rows(SEONGJI_DB,
               "SELECT DISTINCT snapshot_date FROM seongji_prices ORDER BY 1")]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "target": "postgresql",
        "generated_for": args.date,
        "snapshots": snaps_s,
        "tables": {**sc, **uc},
        "files": {
            "seongji_pg.sql": _sha256(pack / "seongji_pg.sql"),
            "subsidy_pg.sql": _sha256(pack / "subsidy_pg.sql"),
        },
    }
    (pack / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_tar:
        tar = Path(args.out) / f"price-pack-{args.date}.tar.gz"
        subprocess.run(["tar", "-czf", str(tar), "-C", str(args.out),
                        f"price-pack-{args.date}"], check=True)
        print(f"→ {tar}")
    print(f"pack: {pack}")
    print("tables:", manifest["tables"])
    print("snapshots:", snaps_s)


if __name__ == "__main__":
    main()
