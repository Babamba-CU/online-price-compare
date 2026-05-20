"""
로컬 SQLite (seongji_prices.db) → Supabase 동기화.

사용법:
  export SUPABASE_URL="https://xxxx.supabase.co"
  export SUPABASE_SERVICE_ROLE_KEY="..."   # service role 또는 anon (단, RLS 정책 허용 필요)
  python seongji_supabase_sync.py [--since YYYY-MM-DD]

업서트 키:
  - seongji_posts        : (source, url)
  - seongji_prices       : (post_id, snapshot_date, model_name, carrier, subscription_type)
  - seongji_daily_stats  : (snapshot_date, model_name, carrier, subscription_type)

주의: Supabase 측에 schema.sql 의 DDL 을 먼저 한 번 적용해야 한다.
      Postgres 에서는 INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL 로 바꿔야 한다.
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

try:
    from supabase import create_client
except ImportError as e:
    raise SystemExit("supabase 패키지가 필요합니다: pip install supabase") from e

from seongji_db import connect


def sync(since: str, batch: int = 500) -> None:
    url  = os.environ.get("SUPABASE_URL")
    key  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not (url and key):
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 환경변수 필요")

    sb = create_client(url, key)

    with connect() as conn:
        posts = [
            dict(r) for r in conn.execute(
                """
                SELECT DISTINCT po.*
                FROM seongji_posts po
                JOIN seongji_prices p ON p.post_id = po.id
                WHERE p.snapshot_date >= ?
                """,
                (since,),
            )
        ]
        prices = [
            dict(r) for r in conn.execute(
                """
                SELECT p.*, po.source AS _src, po.url AS _url
                FROM seongji_prices p
                JOIN seongji_posts  po ON po.id = p.post_id
                WHERE p.snapshot_date >= ?
                """,
                (since,),
            )
        ]
        stats = [
            dict(r) for r in conn.execute(
                "SELECT * FROM seongji_daily_stats WHERE snapshot_date >= ?",
                (since,),
            )
        ]

    # 게시글
    for i in range(0, len(posts), batch):
        sb.table("seongji_posts").upsert(
            posts[i : i + batch], on_conflict="source,url"
        ).execute()
    print(f"posts upserted: {len(posts)}")

    # 가격 — post_id 매핑은 Supabase 측에서 source+url 로 조회해 다시 묶는 게 안전하나
    # 단순화를 위해 동일 id 를 그대로 사용 (양쪽 PK 가 다르면 별도 매핑 로직 추가 필요).
    for i in range(0, len(prices), batch):
        chunk = [{k: v for k, v in p.items() if not k.startswith("_")} for p in prices[i : i + batch]]
        sb.table("seongji_prices").upsert(
            chunk,
            on_conflict="post_id,snapshot_date,model_name,carrier,subscription_type",
        ).execute()
    print(f"prices upserted: {len(prices)}")

    for i in range(0, len(stats), batch):
        sb.table("seongji_daily_stats").upsert(
            stats[i : i + batch],
            on_conflict="snapshot_date,model_name,carrier,subscription_type",
        ).execute()
    print(f"daily_stats upserted: {len(stats)}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=(date.today() - timedelta(days=7)).isoformat())
    p.add_argument("--batch", type=int, default=500)
    a = p.parse_args()
    sync(a.since, a.batch)


if __name__ == "__main__":
    main()
