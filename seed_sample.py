"""
크롤러 없이 대시보드 동작을 검증하기 위한 샘플 데이터 시드.
실제 운영에서는 seongji_crawler.py 가 이 역할을 한다.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from seongji_db import init_db, connect, upsert_post, insert_prices, aggregate_daily


MODELS = [
    ("iPhone 17 Pro Max", 256, 1900000),
    ("iPhone 17 Pro",     256, 1550000),
    ("iPhone 17",         128, 1250000),
    ("Galaxy S26 Ultra",  256, 1800000),
    ("Galaxy S26+",       256, 1450000),
    ("Galaxy S26",        256, 1250000),
    ("Galaxy Z Fold 7",   256, 2400000),
    ("Galaxy Z Flip 7",   256, 1500000),
]
CARRIERS  = ["SKT", "KT", "LGU+", "알뜰"]
SUBS      = ["MNP", "신규", "기변"]
SOURCES   = ["ppomppu", "algosa", "ppasak", "sajangnim", "moyoplan", "modusj"]
CONTRACTS = ["공시", "선약"]


def seed(days: int = 14) -> None:
    init_db()
    rng = random.Random(42)
    today = date.today()
    n_posts = n_prices = 0

    with connect() as conn:
        # 가짜 게시글 1개당 1~3개 모델가 부여 X days
        for day_offset in range(days, -1, -1):
            snap = (today - timedelta(days=day_offset)).isoformat()
            for src in SOURCES:
                for i in range(rng.randint(4, 10)):
                    title = f"[{src}] 성지가 — {rng.choice(['역대급', '오늘만', '한정', '필독'])}"
                    url   = f"https://example.com/{src}/post-{snap}-{i}"
                    pid = upsert_post(conn, {
                        "source":     src,
                        "url":        url,
                        "title":      title,
                        "posted_at":  snap + "T10:00:00",
                        "crawled_at": datetime.now(timezone.utc).isoformat(),
                        "raw_text":   title,
                    })
                    n_posts += 1

                    rows = []
                    n_models = rng.randint(1, 3)
                    for model, storage, msrp in rng.sample(MODELS, n_models):
                        carrier = rng.choice(CARRIERS)
                        sub     = rng.choice(SUBS)
                        contract = rng.choice(CONTRACTS)
                        # 사이트별 가격 편차 + 날짜에 따른 점진 하락
                        site_bias = {
                            "ppomppu": -0.02, "algosa": 0.0, "ppasak": -0.04,
                            "sajangnim": -0.06, "moyoplan": 0.01, "modusj": -0.03,
                        }[src]
                        day_decay = -0.001 * (days - day_offset)
                        noise = rng.uniform(-0.05, 0.05)
                        ratio = max(0.05, 0.35 + site_bias + day_decay + noise)
                        cash = int(msrp * ratio / 10000) * 10000  # 만원 단위
                        plan = rng.choice(["5GX프라임", "요고69", "초이스플러스", "5G다이렉트55"])

                        rows.append({
                            "snapshot_date":     snap,
                            "carrier":           carrier,
                            "subscription_type": sub,
                            "contract_type":     contract,
                            "model_name":        model,
                            "model_raw":         model,
                            "storage_gb":        storage,
                            "cash_price":        cash,
                            "monthly_fee":       rng.randint(45, 110) * 1000,
                            "plan_name":         plan,
                            "plan_duration_mo":  rng.choice([3, 6]),
                            "add_condition":     None,
                            "region":            None,
                            "confidence":        rng.uniform(0.55, 0.92),
                            "raw_text":          title,
                        })
                    n_prices += insert_prices(conn, pid, rows)

        # 통계 머터리얼
        for day_offset in range(days, -1, -1):
            aggregate_daily(conn, today - timedelta(days=day_offset))

    print(f"seeded posts={n_posts} prices={n_prices}")


if __name__ == "__main__":
    seed()
