"""
성지폰 단가 DB 헬퍼 (SQLite).
schema.sql 의 DDL 을 그대로 사용한다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Iterable, Optional

DB_PATH = Path(__file__).parent / "seongji_prices.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()


@contextmanager
def connect(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_post(conn: sqlite3.Connection, post: dict) -> int:
    """게시글 upsert. 반환: post_id."""
    cur = conn.execute(
        """
        INSERT INTO seongji_posts
            (source, source_post_id, url, title, author, posted_at,
             crawled_at, raw_text, view_count, comment_count)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source, url) DO UPDATE SET
            title=excluded.title,
            posted_at=COALESCE(excluded.posted_at, seongji_posts.posted_at),
            crawled_at=excluded.crawled_at,
            raw_text=excluded.raw_text,
            view_count=COALESCE(excluded.view_count, seongji_posts.view_count),
            comment_count=COALESCE(excluded.comment_count, seongji_posts.comment_count)
        RETURNING id
        """,
        (
            post.get("source"),
            post.get("source_post_id"),
            post.get("url"),
            post.get("title"),
            post.get("author"),
            post.get("posted_at"),
            post.get("crawled_at") or datetime.utcnow().isoformat(),
            post.get("raw_text"),
            post.get("view_count"),
            post.get("comment_count"),
        ),
    )
    row = cur.fetchone()
    return row[0]


def insert_prices(conn: sqlite3.Connection, post_id: int, prices: Iterable[dict]) -> int:
    """동일 (post_id, snapshot_date) 의 기존 prices 는 지우고 새로 삽입."""
    sample = list(prices)
    if not sample:
        return 0
    snap = sample[0]["snapshot_date"]
    conn.execute(
        "DELETE FROM seongji_prices WHERE post_id=? AND snapshot_date=?",
        (post_id, snap),
    )
    conn.executemany(
        """
        INSERT INTO seongji_prices
            (post_id, snapshot_date, carrier, subscription_type, contract_type,
             model_name, model_raw, storage_gb, cash_price, monthly_fee,
             plan_name, plan_duration_mo, add_condition, region, confidence, raw_text)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                post_id,
                p["snapshot_date"],
                p.get("carrier"),
                p.get("subscription_type"),
                p.get("contract_type"),
                p["model_name"],
                p.get("model_raw"),
                p.get("storage_gb"),
                p.get("cash_price"),
                p.get("monthly_fee"),
                p.get("plan_name"),
                p.get("plan_duration_mo"),
                p.get("add_condition"),
                p.get("region"),
                p.get("confidence", 0.5),
                p.get("raw_text"),
            )
            for p in sample
        ],
    )
    return len(sample)


def log_run(
    conn: sqlite3.Connection,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    fetched_posts: int,
    parsed_prices: int,
    errors: int,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO seongji_crawl_runs
            (started_at, finished_at, source, fetched_posts, parsed_prices,
             errors, error_message, status)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            started_at.isoformat(),
            finished_at.isoformat(),
            source,
            fetched_posts,
            parsed_prices,
            errors,
            error_message,
            status,
        ),
    )


def aggregate_daily(conn: sqlite3.Connection, snapshot: date) -> int:
    """일별 통계 머터리얼 테이블 갱신. 반환: 행 수."""
    conn.execute(
        "DELETE FROM seongji_daily_stats WHERE snapshot_date=?",
        (snapshot.isoformat(),),
    )
    cur = conn.execute(
        """
        WITH ranked AS (
            SELECT
                p.snapshot_date,
                p.model_name,
                COALESCE(p.carrier, '?')             AS carrier,
                COALESCE(p.subscription_type, '?')   AS sub,
                p.cash_price,
                po.source,
                po.url,
                ROW_NUMBER() OVER (
                    PARTITION BY p.snapshot_date, p.model_name,
                                 COALESCE(p.carrier,'?'),
                                 COALESCE(p.subscription_type,'?')
                    ORDER BY p.cash_price ASC
                ) AS rn
            FROM seongji_prices p
            JOIN seongji_posts  po ON po.id = p.post_id
            WHERE p.snapshot_date = ?
              AND p.cash_price IS NOT NULL
              AND p.cash_price > 0
        ),
        agg AS (
            SELECT
                snapshot_date,
                model_name,
                carrier,
                sub,
                COUNT(*)              AS n,
                MIN(cash_price)       AS mn,
                MAX(cash_price)       AS mx,
                AVG(cash_price)       AS av
            FROM ranked
            GROUP BY snapshot_date, model_name, carrier, sub
        )
        INSERT INTO seongji_daily_stats
            (snapshot_date, model_name, carrier, subscription_type, sample_count,
             min_price, p25_price, median_price, p75_price, max_price, avg_price,
             min_source, min_url, updated_at)
        SELECT
            a.snapshot_date, a.model_name, a.carrier, a.sub, a.n,
            a.mn, p25.cash_price, med.cash_price, p75.cash_price,
            a.mx, CAST(a.av AS INTEGER),
            r.source, r.url, ?
        FROM agg a
        LEFT JOIN ranked r
          ON r.snapshot_date = a.snapshot_date
         AND r.model_name    = a.model_name
         AND r.carrier       = a.carrier
         AND r.sub           = a.sub
         AND r.rn = 1
        -- 분위수: rank 기반(하위 중앙값). SQLite 에 percentile 함수가 없어 rn 조인으로 계산.
        -- 기존 코드는 NULL 을 넣어 전일 대비(median) 분석이 동작하지 않았음 (2026-07-08 수정).
        LEFT JOIN ranked med
          ON med.snapshot_date = a.snapshot_date AND med.model_name = a.model_name
         AND med.carrier = a.carrier AND med.sub = a.sub
         AND med.rn = (a.n + 1) / 2
        LEFT JOIN ranked p25
          ON p25.snapshot_date = a.snapshot_date AND p25.model_name = a.model_name
         AND p25.carrier = a.carrier AND p25.sub = a.sub
         AND p25.rn = MAX(1, (a.n + 3) / 4)
        LEFT JOIN ranked p75
          ON p75.snapshot_date = a.snapshot_date AND p75.model_name = a.model_name
         AND p75.carrier = a.carrier AND p75.sub = a.sub
         AND p75.rn = MAX(1, (a.n * 3 + 3) / 4)
        """,
        (snapshot.isoformat(), datetime.utcnow().isoformat()),
    )
    return cur.rowcount


if __name__ == "__main__":
    init_db()
    print(f"initialized {DB_PATH}")
