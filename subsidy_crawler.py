"""
통신 3사 공시지원금 크롤러 (가입유형별 3패스).

데이터 소스:
  - SKT  : https://shop.tworld.co.kr/wireless/product/subsidy/main
           가입유형 셀렉터 = 드롭다운 (기기변경 / 번호이동 / 신규가입)
  - KT   : https://shop.kt.com/smart/supportAmtList.do
           가입유형 셀렉터 없음 → 1회 크롤 후 3유형 복제
  - LGU+ : https://www.lguplus.com/mobile/financing-model
           가입유형 셀렉터 = 라디오 버튼 (기기변경 / 번호이동 / 신규가입)

크롤링 흐름:
  for carrier in ["SKT", "KT", "LGU+"]:
    if carrier == "KT":
      offers = crawl_kt()                        # 1회
      → 3 sub_type 으로 복제 후 upsert
    else:
      for sub_type in ["010신규", "MNP", "기변"]:
        offers = crawl_skt_or_lgu(sub_type)      # 셀렉터 토글 후 크롤
        → upsert (각 sub_type 으로)

DB 매핑:
  공통지원금  = SKT 공통 = KT 공시 = LGU+ 이통사 → subsidy_public
  추가지원금  = SKT 추가 = KT 추가 = LGU+ 유통망 → subsidy_additional
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, date, timezone
from typing import Optional

from subsidy_db import init_db, connect, upsert_offer, upsert_device, log_run
from model_codes import normalize, KNOWN_RETAIL_HINTS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("subsidy")

# 사이트별 디폴트 요금제 (스크래핑시 자동 검출 우선, 실패시 폴백)
DEFAULT_PLANS = {
    "SKT":  {"name": "5GX 프라임",        "monthly_fee": 89_000},
    "KT":   {"name": "스페셜",            "monthly_fee": 90_000},
    "LGU+": {"name": "5G 프리미어 에센셜","monthly_fee": 85_000},
}

SOURCE_URLS = {
    # SKT 공시지원금 안내 페이지 (T 다이렉트샵 /notice)
    # 페이지 구조 (사용자 확인 + 라이브 검증, 2026-05-21):
    #   상품명 / 공시일 / 출시 가격 / 공통지원금 / 추가지원금 / 구매가격 / 추천 할인 / 신청
    #   가입유형 드롭다운 (신규가입 / 번호이동 / 기기변경)
    "SKT":  "https://shop.tworld.co.kr/notice",
    "KT":   "https://shop.kt.com/smart/supportAmtList.do",
    "LGU+": "https://www.lguplus.com/mobile/financing-model",
}

# 가입유형 — DB 표기 vs 각 사이트 UI 라벨
SUBSCRIPTION_TYPES = ["010신규", "MNP", "기변"]
SITE_SUB_LABEL = {
    "010신규": "신규가입",
    "MNP":     "번호이동",
    "기변":    "기기변경",
}


@dataclass
class Offer:
    carrier:    str
    model_name: str
    storage_gb: int
    retail_price:       int
    subsidy_public:     int
    subsidy_additional: int
    plan_name:          str
    plan_monthly_fee:   int
    subscription_type:  str = "MNP"
    published_date:     Optional[str] = None
    source_url:         str = ""
    raw:                dict = field(default_factory=dict)


# ------------------------------------------------------------------
# Selector 폴백 체인 (스크린샷 기준 best-effort; 사이트 갱신시 보강 필요)
# ------------------------------------------------------------------
SKT_SUB_TRIGGER_SELECTORS = [
    "button[aria-label*='가입 유형']",
    "button[aria-haspopup='listbox']",
    "[aria-haspopup='listbox']",
    "div.dropdown-toggle:has-text('번호이동')",
    "div.dropdown-toggle:has-text('신규가입')",
    "div.dropdown-toggle:has-text('기기변경')",
    "div[class*='dropdown'] button",
]

def skt_option_selectors(label: str) -> list[str]:
    return [
        f"li[role='option']:has-text('{label}')",
        f"button[role='option']:has-text('{label}')",
        f"div[role='menuitem']:has-text('{label}')",
        f"button:has-text('{label}')",
        f"a:has-text('{label}')",
        f"li:has-text('{label}')",
    ]

def lgu_radio_selectors(label: str) -> list[str]:
    return [
        f"input[type='radio'][value='{label}']",
        f"label:has-text('{label}') input[type='radio']",
        f"label:has-text('{label}')",
        f"[role='radio']:has-text('{label}')",
        f"button:has-text('{label}')",
    ]


async def click_first_match(page, selectors: list[str], timeout=2_500) -> Optional[str]:
    """폴백 체인. 성공한 selector 문자열 반환, 모두 실패시 None."""
    for s in selectors:
        try:
            loc = page.locator(s).first
            if await loc.count() == 0:
                continue
            await loc.click(timeout=timeout)
            return s
        except Exception:
            continue
    return None


# ------------------------------------------------------------------
# 공통 보조
# ------------------------------------------------------------------
def parse_won(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"-?\d[\d,]*", text.replace("원", "").strip())
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


async def _new_page(playwright, headless=True):
    browser = await playwright.chromium.launch(headless=headless)
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36",
        locale="ko-KR",
    )
    page = await ctx.new_page()
    return browser, ctx, page


# ------------------------------------------------------------------
# SKT — 가입유형 드롭다운 토글 후 크롤
# ------------------------------------------------------------------
async def crawl_skt(playwright, sub_type: str) -> list[Offer]:
    """SKT 페이지의 가입유형 드롭다운을 sub_type 으로 토글한 뒤 크롤."""
    url = SOURCE_URLS["SKT"]
    label = SITE_SUB_LABEL[sub_type]
    browser, ctx, page = await _new_page(playwright)
    out: list[Offer] = []
    selector_status = {"trigger": None, "option": None}
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)

        # 1) 가입유형 드롭다운 트리거 클릭
        trigger_hit = await click_first_match(page, SKT_SUB_TRIGGER_SELECTORS)
        selector_status["trigger"] = trigger_hit
        if not trigger_hit:
            log.warning("SKT[%s] 가입유형 트리거 클릭 실패 — 기본 페이지 데이터로 진행", sub_type)
        else:
            await page.wait_for_timeout(400)
            # 2) 라벨 옵션 클릭
            opt_hit = await click_first_match(page, skt_option_selectors(label))
            selector_status["option"] = opt_hit
            if not opt_hit:
                log.warning("SKT[%s] 옵션 '%s' 클릭 실패", sub_type, label)
            else:
                await page.wait_for_load_state("networkidle", timeout=10_000)
                await page.wait_for_timeout(600)

        # 3) 무한 스크롤
        prev = -1; stable = 0
        for _ in range(20):
            h = await page.evaluate("document.body.scrollHeight")
            if h == prev:
                stable += 1
                if stable >= 3: break
            else:
                stable = 0; prev = h
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)

        # 4) 파싱
        html = await page.content()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        plan = dict(DEFAULT_PLANS["SKT"])
        plan_el = soup.find(string=re.compile(r"5GX|기본료|월정액"))
        if plan_el:
            mfee = re.search(r"(\d{2,3}[, ]?\d{3})\s*원", str(plan_el))
            if mfee:
                plan["monthly_fee"] = int(mfee.group(1).replace(",", "").replace(" ", ""))

        for marker in soup.find_all(string=re.compile(r"공통지원금이?\s*[\d,]+원? 유리")):
            row = marker.find_parent(["tr", "li", "div"])
            if not row: continue
            text = row.get_text(" ", strip=True)
            tup = normalize(model_text=text)
            if not tup: continue
            model, storage = tup
            m_st = re.search(r"(\d{3,4})\s*G[B]?\b", text)
            if m_st:
                storage = int(m_st.group(1))
            nums = [int(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+", text)]
            if len(nums) < 3: continue
            nums_sorted = sorted(set(nums), reverse=True)
            retail = nums_sorted[0]
            pub    = next((v for v in nums_sorted[1:] if 100_000 <= v <= 800_000), None)
            add    = next((v for v in nums_sorted if 10_000  <= v <= 200_000 and v != pub), None)
            if not (retail and pub is not None): continue
            out.append(Offer(
                carrier="SKT", model_name=model, storage_gb=storage,
                retail_price=retail,
                subsidy_public=pub,
                subsidy_additional=add or 0,
                plan_name=plan["name"], plan_monthly_fee=plan["monthly_fee"],
                subscription_type=sub_type,
                source_url=url,
                raw={"text": text[:300], "selector_status": selector_status,
                     "site_sub_label": label},
            ))
        log.info("SKT[%s] parsed=%d (trigger=%s option=%s)",
                 sub_type, len(out), selector_status["trigger"], selector_status["option"])
    except Exception as e:                                   # noqa: BLE001
        log.exception("SKT[%s] crawl failed: %s", sub_type, e)
    finally:
        await ctx.close()
        await browser.close()
    return out


# ------------------------------------------------------------------
# KT — 가입유형 미구분 (단일 크롤, run() 에서 3유형 복제)
# ------------------------------------------------------------------
async def crawl_kt(playwright, sub_type: str = "MNP") -> list[Offer]:
    """
    KT 페이지는 가입유형 셀렉터가 없음. sub_type 인자는 다른 어댑터와 시그니처 통일용.
    실제로는 한 번만 호출되고, run() 에서 동일 결과를 3 sub_type 으로 복제 저장.
    """
    url = SOURCE_URLS["KT"]
    browser, ctx, page = await _new_page(playwright)
    out: list[Offer] = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        try:
            await page.select_option('select:has-text("요금제")', label="스페셜")
            await page.click('button:has-text("검색")')
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass  # 디폴트가 이미 스페셜인 경우

        html = await page.content()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        plan = dict(DEFAULT_PLANS["KT"])
        plan_label = soup.find(string=re.compile(r"선택한 ?요금제"))
        if plan_label:
            mname = re.search(r":\s*([가-힣A-Za-z0-9 ]+)", plan_label)
            if mname:
                plan["name"] = mname.group(1).strip()

        for tr in soup.select("table tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 7: continue
            txts = [c.get_text(" ", strip=True) for c in cells]

            code_match = next((t for t in txts if re.match(r"SM-[SFAGNM]\d{3}", t)), None)
            tup = normalize(model_code=code_match) if code_match else None
            if not tup:
                tup = normalize(model_text=" ".join(txts))
            if not tup: continue
            model, storage = tup

            nums: list[int] = []
            for t in txts:
                if v := parse_won(t):
                    if v > 0: nums.append(v)
            if len(nums) < 3: continue
            retail = nums[0]
            pub    = nums[1] if len(nums) >= 2 else 0
            add    = nums[2] if len(nums) >= 3 else 0
            sale_expected = retail - pub - add
            sale_found = nums[3] if len(nums) >= 4 else None
            if sale_found is not None and abs(sale_found - sale_expected) > 1000:
                log.warning("KT 판매가 불일치: %s expected=%d found=%d",
                            code_match, sale_expected, sale_found)

            pub_date = next((t for t in txts
                             if re.match(r"\d{4}[-./]\d{2}[-./]\d{2}", t)), None)

            out.append(Offer(
                carrier="KT", model_name=model, storage_gb=storage,
                retail_price=retail,
                subsidy_public=pub,
                subsidy_additional=add,
                plan_name=plan["name"], plan_monthly_fee=plan["monthly_fee"],
                subscription_type=sub_type,
                published_date=pub_date,
                source_url=url,
                raw={"model_code": code_match, "cells": txts[:8],
                     "policy_note": "KT 가입유형 미구분 — 동일값 3유형 복제"},
            ))
        log.info("KT parsed=%d (가입유형 미구분, run() 에서 복제)", len(out))
    except Exception as e:                                   # noqa: BLE001
        log.exception("KT crawl failed: %s", e)
    finally:
        await ctx.close()
        await browser.close()
    return out


# ------------------------------------------------------------------
# LGU+ — 가입유형 라디오 클릭 후 크롤
# ------------------------------------------------------------------
async def crawl_lgu(playwright, sub_type: str) -> list[Offer]:
    """LGU+ financing-model 페이지의 가입유형 라디오를 sub_type 으로 토글한 뒤 크롤."""
    url = SOURCE_URLS["LGU+"]
    label = SITE_SUB_LABEL[sub_type]
    browser, ctx, page = await _new_page(playwright)
    out: list[Offer] = []
    selector_hit = None
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        try:
            await page.wait_for_selector("table, [data-model], li.product", timeout=8_000)
        except Exception:
            pass

        # 가입유형 라디오 클릭
        selector_hit = await click_first_match(page, lgu_radio_selectors(label))
        if not selector_hit:
            log.warning("LGU+[%s] 라디오 '%s' 클릭 실패 — 기본 페이지로 진행", sub_type, label)
        else:
            await page.wait_for_load_state("networkidle", timeout=10_000)
            await page.wait_for_timeout(500)

        # lazy load 로드
        for _ in range(8):
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(500)

        html = await page.content()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        plan = dict(DEFAULT_PLANS["LGU+"])

        for tr in soup.select("tr"):
            text = tr.get_text(" ", strip=True)
            if "24개월" not in text:
                continue
            code_match = re.search(r"SM-[SFAGNM]\d{3}[A-Z]*\d{3,4}", text)
            tup = None
            if code_match:
                tup = normalize(model_code=code_match.group(0))
            if not tup:
                tup = normalize(model_text=text)
            if not tup: continue
            model, storage = tup

            nums = [int(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+", text)]
            if len(nums) < 4: continue
            retail = nums[0]
            pub    = next((v for v in nums[1:] if 200_000 <= v <= 900_000), 0)
            add    = next((v for v in nums[1:] if 30_000  <= v <= 700_000 and v != pub), 0)
            total_expected = pub + add
            total_found = next((v for v in nums if abs(v - total_expected) <= 1000), None)
            if total_found is None:
                log.warning("LGU+[%s] 총액 검증 실패: %s pub=%d add=%d",
                            sub_type, code_match, pub, add)

            pub_date_m = re.search(r"\d{4}[-./]\d{2}[-./]\d{2}", text)
            pub_date = pub_date_m.group(0) if pub_date_m else None

            out.append(Offer(
                carrier="LGU+", model_name=model, storage_gb=storage,
                retail_price=retail,
                subsidy_public=pub,
                subsidy_additional=add,
                plan_name=plan["name"], plan_monthly_fee=plan["monthly_fee"],
                subscription_type=sub_type,
                published_date=pub_date,
                source_url=url,
                raw={"model_code": code_match.group(0) if code_match else None,
                     "row_text": text[:300],
                     "site_sub_label": label,
                     "selector_hit": selector_hit},
            ))
        log.info("LGU+[%s] parsed=%d (radio=%s)", sub_type, len(out), selector_hit)
    except Exception as e:                                   # noqa: BLE001
        log.exception("LGU+[%s] crawl failed: %s", sub_type, e)
    finally:
        await ctx.close()
        await browser.close()
    return out


# ------------------------------------------------------------------
# 정합성 검증 (적재 직전)
# ------------------------------------------------------------------
def sanity_check(offer: Offer) -> list[str]:
    errs: list[str] = []
    if offer.retail_price       <= 0: errs.append("retail_price 비정상")
    if offer.subsidy_public     <  0: errs.append("공통지원금 음수")
    if offer.subsidy_additional <  0: errs.append("추가지원금 음수")
    if offer.subsidy_public + offer.subsidy_additional > offer.retail_price:
        errs.append("지원금 합이 출고가 초과")
    hint = KNOWN_RETAIL_HINTS.get((offer.model_name, offer.storage_gb))
    if hint and abs(offer.retail_price - hint) > hint * 0.05:
        errs.append(f"출고가 hint({hint:,}) 와 5%+ 차이")
    return errs


def build_payload(o: Offer, sub_type: str, today_iso: str) -> dict:
    select_disc = (o.plan_monthly_fee or 0) * 24 * 25 // 100
    return {
        "snapshot_date":      today_iso,
        "carrier":            o.carrier,
        "model_name":         o.model_name,
        "storage_gb":         o.storage_gb,
        "subscription_type":  sub_type,
        "color":              None,
        "retail_price":       o.retail_price,
        "plan_name":          o.plan_name,
        "plan_monthly_fee":   o.plan_monthly_fee,
        "subsidy_public":     o.subsidy_public,
        "subsidy_additional": o.subsidy_additional,
        "select_discount_24mo": select_disc,
        "contract_months":    24,
        "source_url":         o.source_url,
        "source_html_hash":   hashlib.md5(
            json.dumps(o.raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "raw_payload":        {**o.raw, "published_date": o.published_date},
        "fetched_at":         datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------
# 메인 — 가입유형별 3패스 (KT는 1패스 + 복제)
# ------------------------------------------------------------------
CRAWLERS = {"SKT": crawl_skt, "KT": crawl_kt, "LGU+": crawl_lgu}


async def run(carriers: list[str]) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit(
            "playwright 가 필요합니다.\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    init_db()
    today = date.today().isoformat()

    async with async_playwright() as p:
        for carrier in carriers:
            if carrier not in CRAWLERS:
                log.warning("unknown carrier: %s", carrier); continue
            started = datetime.now(timezone.utc)
            all_offers: list[tuple[str, Offer]] = []     # (sub_type, offer)
            err: Optional[str] = None
            try:
                if carrier == "KT":
                    # 1패스 + 3유형 복제
                    offers = await crawl_kt(p)
                    for o in offers:
                        for st in SUBSCRIPTION_TYPES:
                            all_offers.append((st, replace(o, subscription_type=st)))
                else:
                    # SKT / LGU+: 가입유형 셀렉터 토글하며 3패스
                    seen_signatures = {}   # sub_type → set((model, storage, pub, add))
                    for st in SUBSCRIPTION_TYPES:
                        offers = await CRAWLERS[carrier](p, sub_type=st)
                        sig = {(o.model_name, o.storage_gb, o.subsidy_public, o.subsidy_additional)
                               for o in offers}
                        seen_signatures[st] = sig
                        for o in offers:
                            all_offers.append((st, o))
                    # 셀렉터 효과 진단 — 3유형 시그니처가 모두 같으면 토글 실패 의심
                    sigs = list(seen_signatures.values())
                    if sigs and all(sigs[0] == s for s in sigs[1:]) and sigs[0]:
                        log.warning("[%s] 가입유형 토글 효과 없음 — 3유형 모두 동일 시그니처. "
                                    "selector 폴백 체인 점검 필요.", carrier)
            except Exception as e:                              # noqa: BLE001
                err = str(e)
                log.exception("crawler crashed: %s", carrier)
            finished = datetime.now(timezone.utc)

            upserted = errors = 0
            with connect() as conn:
                for sub_type, o in all_offers:
                    sc = sanity_check(o)
                    if sc:
                        log.warning("[%s/%s] %s %dGB → 검증 실패: %s",
                                    carrier, sub_type, o.model_name, o.storage_gb, ", ".join(sc))
                        errors += 1
                        continue
                    payload = build_payload(o, sub_type, today)
                    upsert_offer(conn, payload)
                    upsert_device(conn, {"model_name": o.model_name})
                    upserted += 1
                log_run(
                    conn,
                    started_at=started.isoformat(),
                    finished_at=finished.isoformat(),
                    carrier=carrier,
                    fetched_models=len(all_offers),
                    upserted=upserted,
                    changed=0,
                    errors=errors if not err else errors + 1,
                    status="failed" if err else ("partial" if errors else "success"),
                    error_message=err,
                    source_url=all_offers[0][1].source_url if all_offers else None,
                )
            log.info("[%s] total_rows=%d upserted=%d errors=%d",
                     carrier, len(all_offers), upserted, errors)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--carriers", nargs="+", default=["SKT", "KT", "LGU+"])
    args = p.parse_args()
    asyncio.run(run(args.carriers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
