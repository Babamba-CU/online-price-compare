"""
공시지원금 SQLite (subsidy_offers.db) → Supabase 일별 동기화.

사용법:
  export SUPABASE_URL="https://xxxx.supabase.co"
  export SUPABASE_SERVICE_ROLE_KEY="..."
  python subsidy_supabase_sync.py --since 2026-05-01

Supabase 측 준비:
  1) subsidy_schema.sql 의 DDL 을 Supabase SQL Editor 에서 한 번 실행
     - INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY 로 치환
     - 파일 하단 ALTER TABLE / CREATE POLICY 주석 해제
  2) 환경변수 (URL + service_role 키)
  3) 첫 동기화는 --since 를 충분히 과거로 잡고 1회만, 이후 매일 cron/Routine 에서
     기본 --since (전일) 로 호출

업서트 키:
  - subsidy_devices : (model_name)
  - subsidy_offers  : (snapshot_date, carrier, model_name, storage_gb)
  - subsidy_changes : 단순 INSERT (idempotent 보장 위해 별도 (snapshot_date,
                      carrier, model_name, storage_gb, field) 인덱스 추가 권장)
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta

try:
    from supabase import create_client
except ImportError as e:
    raise SystemExit("supabase 패키지가 필요합니다: pip install supabase") from e

from subsidy_db import connect


def sync(since: str, batch: int = 500) -> None:
    url = os.environ.get("SUPABASE_URL")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
           or os.environ.get("SUPABASE_ANON_KEY"))
    if not (url and key):
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 환경변수 필요")

    sb = create_client(url, key)

    with connect() as conn:
        devices = [dict(r) for r in conn.execute("SELECT * FROM subsidy_devices")]
        offers  = [dict(r) for r in conn.execute(
            "SELECT * FROM subsidy_offers WHERE snapshot_date >= ?", (since,)
        )]
        changes = [dict(r) for r in conn.execute(
            "SELECT * FROM subsidy_changes WHERE snapshot_date >= ?", (since,)
        )]
        runs = [dict(r) for r in conn.execute(
            "SELECT * FROM subsidy_crawl_runs WHERE date(started_at) >= date(?)",
            (since,)
        )]

    # JSON 컬럼들 — 이미 string 으로 들어있지만 dict 면 직렬화
    for d in devices:
        if isinstance(d.get("aliases"), dict):
            d["aliases"] = json.dumps(d["aliases"], ensure_ascii=False)

    # devices
    for i in range(0, len(devices), batch):
        sb.table("subsidy_devices").upsert(
            devices[i:i + batch], on_conflict="model_name"
        ).execute()
    print(f"devices upserted: {len(devices)}")

    # offers (id 컬럼은 supabase 자체 BIGSERIAL 이므로 제외)
    for i in range(0, len(offers), batch):
        chunk = [{k: v for k, v in o.items() if k != "id"} for o in offers[i:i + batch]]
        sb.table("subsidy_offers").upsert(
            chunk,
            on_conflict="snapshot_date,carrier,model_name,storage_gb",
        ).execute()
    print(f"offers upserted: {len(offers)}")

    # changes (idempotent: 중복 무시)
    for i in range(0, len(changes), batch):
        chunk = [{k: v for k, v in c.items() if k != "id"} for c in changes[i:i + batch]]
        try:
            sb.table("subsidy_changes").insert(chunk).execute()
        except Exception as e:                         # noqa: BLE001
            # 중복 키 등은 무시하고 계속
            print(f"  changes batch failed (ignored): {e}")
    print(f"changes inserted: {len(changes)}")

    # runs
    for i in range(0, len(runs), batch):
        chunk = [{k: v for k, v in r.items() if k != "id"} for r in runs[i:i + batch]]
        sb.table("subsidy_crawl_runs").insert(chunk).execute()
    print(f"runs inserted: {len(runs)}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=(date.today() - timedelta(days=1)).isoformat())
    p.add_argument("--batch", type=int, default=500)
    a = p.parse_args()
    sync(a.since, a.batch)


if __name__ == "__main__":
    main()
