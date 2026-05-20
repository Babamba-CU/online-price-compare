"""
공시지원금 DB 헬퍼.
SQLite 로컬 + Supabase 동기화 양쪽 지원.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Iterable, Optional

DB_PATH = Path(__file__).parent / "subsidy_offers.db"
SCHEMA_PATH = Path(__file__).parent / "subsidy_schema.sql"


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


def upsert_device(conn: sqlite3.Connection, device: dict) -> None:
    conn.execute(
        """
        INSERT INTO subsidy_devices
            (model_name, manufacturer, released_at, storage_options, aliases)
        VALUES (?,?,?,?,?)
        ON CONFLICT(model_name) DO UPDATE SET
            manufacturer    = COALESCE(excluded.manufacturer,    subsidy_devices.manufacturer),
            released_at     = COALESCE(excluded.released_at,     subsidy_devices.released_at),
            storage_options = COALESCE(excluded.storage_options, subsidy_devices.storage_options),
            aliases         = COALESCE(excluded.aliases,         subsidy_devices.aliases)
        """,
        (
            device["model_name"],
            device.get("manufacturer"),
            device.get("released_at"),
            json.dumps(device.get("storage_options"), ensure_ascii=False)
                if device.get("storage_options") else None,
            json.dumps(device.get("aliases"), ensure_ascii=False)
                if device.get("aliases") else None,
        ),
    )


def upsert_offer(conn: sqlite3.Connection, offer: dict) -> tuple[bool, list[dict]]:
    """
    공시지원금 일별 스냅샷 upsert.
    반환: (changed?, change-records[])
      - 같은 (snapshot_date, carrier, model_name, storage_gb) 가 이미 있으면 UPDATE
      - 전일 데이터와 비교해 가격/지원금 변동이 있으면 subsidy_changes 에 기록
    """
    # 파생 계산
    pub  = offer.get("subsidy_public")     or 0
    add  = offer.get("subsidy_additional") or 0
    total = pub + add
    offer["subsidy_total"] = total

    retail = offer.get("retail_price") or 0
    offer["net_buy_price"] = max(retail - total, 0)
    months = offer.get("contract_months") or 24
    offer["monthly_device_fee"] = offer["net_buy_price"] // months if months else None

    sub_type = offer.get("subscription_type") or "MNP"

    # 전일 동일 단말+가입유형 조회 (변동 감지)
    prev = conn.execute(
        """
        SELECT * FROM subsidy_offers
        WHERE carrier=? AND model_name=?
          AND COALESCE(storage_gb, -1) = COALESCE(?, -1)
          AND subscription_type=?
          AND snapshot_date < ?
        ORDER BY snapshot_date DESC LIMIT 1
        """,
        (offer["carrier"], offer["model_name"], offer.get("storage_gb"),
         sub_type, offer["snapshot_date"]),
    ).fetchone()

    # upsert
    conn.execute(
        """
        INSERT INTO subsidy_offers (
            snapshot_date, carrier, model_name, storage_gb, subscription_type, color,
            retail_price, plan_name, plan_monthly_fee,
            subsidy_public, subsidy_additional, subsidy_total,
            select_discount_24mo, contract_months,
            net_buy_price, monthly_device_fee,
            source_url, source_html_hash, raw_payload, fetched_at
        ) VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?, ?,?,?,?)
        ON CONFLICT(snapshot_date, carrier, model_name, storage_gb, subscription_type) DO UPDATE SET
            color              = excluded.color,
            retail_price       = excluded.retail_price,
            plan_name          = excluded.plan_name,
            plan_monthly_fee   = excluded.plan_monthly_fee,
            subsidy_public     = excluded.subsidy_public,
            subsidy_additional = excluded.subsidy_additional,
            subsidy_total      = excluded.subsidy_total,
            select_discount_24mo = excluded.select_discount_24mo,
            net_buy_price      = excluded.net_buy_price,
            monthly_device_fee = excluded.monthly_device_fee,
            source_url         = excluded.source_url,
            source_html_hash   = excluded.source_html_hash,
            raw_payload        = excluded.raw_payload,
            fetched_at         = excluded.fetched_at
        """,
        (
            offer["snapshot_date"], offer["carrier"], offer["model_name"],
            offer.get("storage_gb"), sub_type, offer.get("color"),
            offer.get("retail_price"),
            offer.get("plan_name"), offer.get("plan_monthly_fee"),
            offer.get("subsidy_public"), offer.get("subsidy_additional"),
            offer.get("subsidy_total"),
            offer.get("select_discount_24mo"), offer.get("contract_months", 24),
            offer.get("net_buy_price"), offer.get("monthly_device_fee"),
            offer["source_url"], offer.get("source_html_hash"),
            json.dumps(offer.get("raw_payload"), ensure_ascii=False)
                if offer.get("raw_payload") else None,
            offer.get("fetched_at") or datetime.utcnow().isoformat(),
        ),
    )

    # 변동 감지
    changes = []
    if prev:
        for field in ("retail_price", "subsidy_public", "subsidy_additional",
                       "plan_monthly_fee"):
            old_v = prev[field]
            new_v = offer.get(field)
            if old_v is None or new_v is None:
                continue
            if old_v != new_v:
                changes.append({
                    "snapshot_date": offer["snapshot_date"],
                    "carrier":       offer["carrier"],
                    "model_name":    offer["model_name"],
                    "storage_gb":    offer.get("storage_gb"),
                    "field":         field,
                    "old_value":     old_v,
                    "new_value":     new_v,
                    "diff":          new_v - old_v,
                    "detected_at":   datetime.utcnow().isoformat(),
                })
    for c in changes:
        conn.execute(
            """
            INSERT INTO subsidy_changes
              (snapshot_date, carrier, model_name, storage_gb,
               field, old_value, new_value, diff, detected_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (c["snapshot_date"], c["carrier"], c["model_name"], c["storage_gb"],
             c["field"], c["old_value"], c["new_value"], c["diff"], c["detected_at"]),
        )
    return (len(changes) > 0, changes)


def log_run(conn, **kwargs) -> None:
    conn.execute(
        """
        INSERT INTO subsidy_crawl_runs
          (started_at, finished_at, carrier,
           fetched_models, upserted, changed, errors,
           status, error_message, source_url)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (kwargs.get("started_at"), kwargs.get("finished_at"),
         kwargs["carrier"], kwargs.get("fetched_models", 0),
         kwargs.get("upserted", 0), kwargs.get("changed", 0),
         kwargs.get("errors", 0),
         kwargs.get("status", "success"), kwargs.get("error_message"),
         kwargs.get("source_url")),
    )


if __name__ == "__main__":
    init_db()
    print(f"initialized {DB_PATH}")
