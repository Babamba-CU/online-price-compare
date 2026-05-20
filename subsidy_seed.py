"""
공시지원금 비교 대시보드 — 시드.

시드 정책:
  - MNP 값: 사용자 제공 스크린샷 기준 실값 (Galaxy S26 패밀리 등)
  - 010신규 / 기변: MNP 와 동일한 placeholder 로 적재
      → 실제 SKT / LGU+ 사이트는 가입유형 토글마다 다른 값을 표시하지만,
         크롤러가 실제 페이지를 토글하면서 채워야 하는 값이므로 시드 단계에서
         임의 추정치(멀티플라이어)를 두지 않음. 크롤러 실행 후 덮어쓰임.
  - KT: 사이트에 가입유형 셀렉터 미구분 — 3 sub_type 모두 동일값이 정상
      → 크롤러는 1회 크롤 후 결과를 3 sub_type 으로 복제

데이터 소스 (2026-05-07 ~ 2026-05-14 기준):
  - SKT  shop.tworld.co.kr 휴대폰 지원금 (요금제: 5GX 프라임)
  - KT   shop.kt.com 공시지원금 안내    (요금제: 스페셜)
  - LGU+ lguplus.com financing-model    (요금제: 5G 프리미어 에센셜 / 24개월 유지)

Galaxy S26 256GB (출고가 1,254,000) 기준 — 사용자 제공:
  SKT  : 공통 580,000 / 추가 87,000   → 구매가 587,000
  KT   : 공통 600,000 / 추가 0        → 판매가 654,000
  LGU+ : 공통 700,000 / 추가 105,000 → 구매가 449,000
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from subsidy_db import init_db, connect, upsert_device, upsert_offer, log_run


# (model_name, storage_gb, retail_price)
# 출고가는 3사 동일. 다를 경우 carrier-specific override 로 처리.
DEVICES: list[tuple[str, str, str, int, int]] = [
    # (model, manufacturer, released_at, storage_gb, retail_price)
    # Galaxy S26 family (사용자 제공 실데이터)
    ("Galaxy S26",         "삼성전자(주)", "2026-01-15", 256, 1_254_000),
    ("Galaxy S26",         "삼성전자(주)", "2026-01-15", 512, 1_507_000),
    ("Galaxy S26+",        "삼성전자(주)", "2026-01-15", 256, 1_452_000),
    ("Galaxy S26+",        "삼성전자(주)", "2026-01-15", 512, 1_705_000),
    ("Galaxy S26 Ultra",   "삼성전자(주)", "2026-01-15", 256, 1_797_400),
    ("Galaxy S26 Ultra",   "삼성전자(주)", "2026-01-15", 512, 2_050_400),
    # Galaxy Z (2025)
    ("Galaxy Z Fold 7",    "삼성전자(주)", "2025-07-25", 256, 2_399_000),
    ("Galaxy Z Fold 7",    "삼성전자(주)", "2025-07-25", 512, 2_519_000),
    ("Galaxy Z Flip 7",    "삼성전자(주)", "2025-07-25", 256, 1_499_000),
    ("Galaxy Z Flip 7",    "삼성전자(주)", "2025-07-25", 512, 1_619_000),
    ("Galaxy Z Flip 7 FE", "삼성전자(주)", "2025-08-06", 256, 1_199_000),
    # Galaxy S25
    ("Galaxy S25",         "삼성전자(주)", "2025-01-22", 256,   899_800),
    ("Galaxy S25",         "삼성전자(주)", "2025-01-22", 512, 1_022_000),
    ("Galaxy S25 Ultra",   "삼성전자(주)", "2025-01-22", 256, 1_698_400),
    ("Galaxy S25 Ultra",   "삼성전자(주)", "2025-01-22", 512, 1_820_500),
    # iPhone 17 (2025-09)
    ("iPhone 17",          "Apple",       "2025-09-20", 128, 1_250_000),
    ("iPhone 17",          "Apple",       "2025-09-20", 256, 1_390_000),
    ("iPhone 17 Plus",     "Apple",       "2025-09-20", 128, 1_350_000),
    ("iPhone 17 Plus",     "Apple",       "2025-09-20", 256, 1_490_000),
    ("iPhone 17 Pro",      "Apple",       "2025-09-20", 256, 1_550_000),
    ("iPhone 17 Pro",      "Apple",       "2025-09-20", 512, 1_790_000),
    ("iPhone 17 Pro Max",  "Apple",       "2025-09-20", 256, 1_900_000),
    ("iPhone 17 Pro Max",  "Apple",       "2025-09-20", 512, 2_140_000),
    ("iPhone 17 Pro Max",  "Apple",       "2025-09-20",1024, 2_620_000),
    # iPhone 16 (2024-09)
    ("iPhone 16",          "Apple",       "2024-09-20", 128,   650_000),
    ("iPhone 16",          "Apple",       "2024-09-20", 256,   790_000),
    ("iPhone 16 Pro",      "Apple",       "2024-09-20", 256,   900_000),
    ("iPhone 16 Pro Max",  "Apple",       "2024-09-20", 256, 1_100_000),
]

# 공시지원금 (단말 변동 무관, 통신사 정책)
# (model, storage) → {carrier: (subsidy_public, subsidy_additional)}
# 사용자 제공값 + 그 외는 카르티에별 정책 일관 추정
SUBSIDIES: dict[tuple[str, int], dict[str, tuple[int, int]]] = {
    # 사용자 제공값 그대로
    ("Galaxy S26",       256): {"SKT": (580_000, 87_000),  "KT": (600_000, 0), "LGU+": (700_000, 105_000)},
    ("Galaxy S26",       512): {"SKT": (580_000, 87_000),  "KT": (600_000, 0), "LGU+": (700_000, 105_000)},
    ("Galaxy S26+",      256): {"SKT": (580_000, 87_000),  "KT": (600_000, 0), "LGU+": (700_000, 105_000)},
    ("Galaxy S26+",      512): {"SKT": (580_000, 87_000),  "KT": (600_000, 0), "LGU+": (700_000, 105_000)},
    ("Galaxy S26 Ultra", 256): {"SKT": (580_000, 87_000),  "KT": (600_000, 0), "LGU+": (700_000, 105_000)},
    ("Galaxy S26 Ultra", 512): {"SKT": (580_000, 87_000),  "KT": (600_000, 0), "LGU+": (700_000, 105_000)},
    # Z Flip 7 FE (LG screenshot: 500/600)
    ("Galaxy Z Flip 7 FE", 256): {"SKT": (450_000, 67_000), "KT": (500_000, 0), "LGU+": (500_000, 600_000)},
    # Z Fold/Flip 7 (출시 6개월차)
    ("Galaxy Z Fold 7", 256): {"SKT": (550_000, 80_000), "KT": (580_000, 0), "LGU+": (650_000, 100_000)},
    ("Galaxy Z Fold 7", 512): {"SKT": (550_000, 80_000), "KT": (580_000, 0), "LGU+": (650_000, 100_000)},
    ("Galaxy Z Flip 7", 256): {"SKT": (520_000, 78_000), "KT": (550_000, 0), "LGU+": (620_000, 90_000)},
    ("Galaxy Z Flip 7", 512): {"SKT": (520_000, 78_000), "KT": (550_000, 0), "LGU+": (620_000, 90_000)},
    # Galaxy S25 (1년차 이상)
    ("Galaxy S25",         256): {"SKT": (450_000, 67_000), "KT": (500_000, 0), "LGU+": (520_000, 78_000)},
    ("Galaxy S25",         512): {"SKT": (450_000, 67_000), "KT": (500_000, 0), "LGU+": (520_000, 78_000)},
    ("Galaxy S25 Ultra",   256): {"SKT": (450_000, 67_000), "KT": (500_000, 0), "LGU+": (520_000, 78_000)},
    ("Galaxy S25 Ultra",   512): {"SKT": (450_000, 67_000), "KT": (500_000, 0), "LGU+": (520_000, 78_000)},
    # iPhone 17 (출시 6~8개월차)
    ("iPhone 17",          128): {"SKT": (200_000, 30_000), "KT": (220_000, 0), "LGU+": (250_000, 37_500)},
    ("iPhone 17",          256): {"SKT": (200_000, 30_000), "KT": (220_000, 0), "LGU+": (250_000, 37_500)},
    ("iPhone 17 Plus",     128): {"SKT": (220_000, 33_000), "KT": (240_000, 0), "LGU+": (260_000, 39_000)},
    ("iPhone 17 Plus",     256): {"SKT": (220_000, 33_000), "KT": (240_000, 0), "LGU+": (260_000, 39_000)},
    ("iPhone 17 Pro",      256): {"SKT": (250_000, 37_500), "KT": (260_000, 0), "LGU+": (280_000, 42_000)},
    ("iPhone 17 Pro",      512): {"SKT": (250_000, 37_500), "KT": (260_000, 0), "LGU+": (280_000, 42_000)},
    ("iPhone 17 Pro Max",  256): {"SKT": (280_000, 42_000), "KT": (300_000, 0), "LGU+": (320_000, 48_000)},
    ("iPhone 17 Pro Max",  512): {"SKT": (280_000, 42_000), "KT": (300_000, 0), "LGU+": (320_000, 48_000)},
    ("iPhone 17 Pro Max", 1024): {"SKT": (280_000, 42_000), "KT": (300_000, 0), "LGU+": (320_000, 48_000)},
    # iPhone 16 (1년 6개월차)
    ("iPhone 16",          128): {"SKT": (400_000, 60_000), "KT": (430_000, 0), "LGU+": (450_000, 67_500)},
    ("iPhone 16",          256): {"SKT": (400_000, 60_000), "KT": (430_000, 0), "LGU+": (450_000, 67_500)},
    ("iPhone 16 Pro",      256): {"SKT": (430_000, 64_500), "KT": (450_000, 0), "LGU+": (480_000, 72_000)},
    ("iPhone 16 Pro Max",  256): {"SKT": (450_000, 67_500), "KT": (470_000, 0), "LGU+": (500_000, 75_000)},
}

CARRIERS = ["SKT", "KT", "LGU+"]
SUBSCRIPTION_TYPES = ["010신규", "MNP", "기변"]

# 가입유형별 멀티플라이어는 사용하지 않는다.
# - 실제 SKT/LGU+ 의 가입유형별 값은 일률적 배수가 아니라 통신사가 단말마다
#   독립적으로 책정한 값이고, 사이트 셀렉터를 토글해야 얻을 수 있다.
# - 크롤러가 이 작업을 수행하며, 시드는 그 전 상태에서 placeholder 로 채워둔다.

# 각사 디폴트 요금제 (사이트 표기 기준)
PLAN_BY_CARRIER = {
    "SKT":  {"name": "5GX 프라임",      "monthly_fee":  89_000},
    "KT":   {"name": "스페셜",          "monthly_fee":  90_000},   # KT 5G 스페셜
    "LGU+": {"name": "5G 프리미어 에센셜","monthly_fee":  85_000},
}

# 공시일자 (스크린샷 기준)
PUBLISHED_DATE = {
    "SKT":  "2026-05-07",
    "KT":   "2026-05-14",
    "LGU+": "2026-05-01",
}

SOURCE_URL = {
    "SKT":  "https://shop.tworld.co.kr/notice",
    "KT":   "https://shop.kt.com/smart/supportAmtList.do",
    "LGU+": "https://www.lguplus.com/mobile/financing-model",
}


def seed(days: int = 14) -> None:
    init_db()
    today = date.today()

    # 단말 마스터 — (model, storage_gb) variant 별 1행
    with connect() as conn:
        # model 단위로 한 번만 upsert (storage_options 는 모든 용량 합집합)
        storages_by_model: dict[str, list[int]] = {}
        meta_by_model: dict[str, tuple[str, str]] = {}
        for (model, mfr, released, st, _) in DEVICES:
            storages_by_model.setdefault(model, []).append(st)
            meta_by_model[model] = (mfr, released)
        for model, storages in storages_by_model.items():
            mfr, rel = meta_by_model[model]
            upsert_device(conn, {
                "model_name":      model,
                "manufacturer":    mfr,
                "released_at":     rel,
                "storage_options": sorted(set(storages)),
                "aliases":         {},
            })

    n_offers = 0
    for day_offset in range(days, -1, -1):
        snap = (today - timedelta(days=day_offset)).isoformat()
        for (model, mfr, released, storage, retail) in DEVICES:
            sub_map = SUBSIDIES.get((model, storage))
            if not sub_map:
                continue

            for carrier in CARRIERS:
                pair = sub_map.get(carrier)
                if pair is None:
                    continue   # 해당 통신사 미판매 → 행 미생성
                pub_mnp, add_mnp = pair   # 사이트에 표시되는 MNP 기준 값

                plan = PLAN_BY_CARRIER[carrier]
                # 3 sub_type 모두 MNP 와 동일한 값으로 적재 (placeholder).
                # SKT/LGU+ 의 실제 가입유형별 값은 크롤러가 사이트 셀렉터를 토글해
                # 가져온 후 덮어쓴다. KT 는 사이트 자체가 미구분이라 영원히 동일.
                for sub_type in SUBSCRIPTION_TYPES:
                    is_placeholder = (carrier in ("SKT", "LGU+") and sub_type != "MNP")
                    offer = {
                        "snapshot_date":      snap,
                        "carrier":            carrier,
                        "model_name":         model,
                        "storage_gb":         storage,
                        "subscription_type":  sub_type,
                        "color":              None,
                        "retail_price":       retail,
                        "plan_name":          plan["name"],
                        "plan_monthly_fee":   plan["monthly_fee"],
                        "subsidy_public":     pub_mnp,
                        "subsidy_additional": add_mnp,
                        "select_discount_24mo": plan["monthly_fee"] * 24 * 25 // 100,
                        "contract_months":    24,
                        "source_url":         SOURCE_URL[carrier],
                        "source_html_hash":   hashlib.md5(
                            f"{carrier}{model}{storage}{sub_type}{snap}".encode()
                        ).hexdigest(),
                        "raw_payload":        {
                            "sample":            True,
                            "published_date":    PUBLISHED_DATE[carrier],
                            "subscription_type": sub_type,
                            "is_placeholder":    is_placeholder,
                            "policy_note": (
                                "KT 가입유형 미구분 — 사이트 정책상 3유형 동일값"
                                if carrier == "KT"
                                else "MNP 실값" if sub_type == "MNP"
                                else f"{carrier} {sub_type} placeholder — 실제 크롤러 실행시 덮어쓰임"
                            ),
                        },
                        "fetched_at":         datetime.now(timezone.utc).isoformat(),
                    }
                    with connect() as conn:
                        upsert_offer(conn, offer)
                        n_offers += 1

    with connect() as conn:
        for c in CARRIERS:
            log_run(
                conn,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                carrier=c,
                fetched_models=len(set((m, s) for m, _, _, s, _ in DEVICES)),
                upserted=n_offers // 3,
                changed=0,
                errors=0,
                status="success",
                source_url=SOURCE_URL[c],
            )

    print(f"seeded device_variants={len(DEVICES)} × 3 carriers × {days+1} days "
          f"= {n_offers} offers (사용자 제공 실값 + 보조 추정값)")


if __name__ == "__main__":
    seed()
