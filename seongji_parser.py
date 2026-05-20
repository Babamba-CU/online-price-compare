"""
성지폰 게시글 텍스트 → 정규화 가격 dict 변환기.

게시글 본문/제목/표 텍스트에서 다음을 추출:
 - 통신사 (SKT / KT / LGU+ / 알뜰)
 - 가입유형 (신규 / MNP / 기변)
 - 약정유형 (공시 / 선약 / 자급)
 - 모델명 (정규화: Galaxy S26 Ultra, iPhone 17 Pro Max, ...)
 - 저장용량 (GB)
 - 현금완납가 (원)
 - 요금제 / 의무유지

휴리스틱 기반: 한국어 성지폰 게시글의 일반적 표기 규칙을 정규식으로 다룬다.
실제 사이트별 HTML 파서는 seongji_crawler.py 에서 호출.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Optional

# ------------------------------------------------------------------
# 정규화 사전
# ------------------------------------------------------------------
CARRIER_MAP = {
    "SKT": ["SKT", "SK", "에스케이", "skt"],
    "KT":  ["KT", "케이티", "kt", "olleh", "올레"],
    "LGU+": ["LG", "LGU", "유플", "LGU+", "LG U+", "엘지", "lgu+"],
    "알뜰":  ["알뜰", "MVNO", "mvno", "알뜰폰"],
}

SUB_MAP = {
    "신규": ["신규", "010신규", "010 신규", "010"],
    "MNP":  ["MNP", "번이", "번호이동", "이동", "mnp"],
    "기변": ["기변", "기기변경", "기존변경"],
}

CONTRACT_MAP = {
    "공시": ["공시", "공시지원금", "공시지원"],
    "선약": ["선약", "선택약정", "25%할인", "25%"],
    "자급": ["자급", "자급제"],
}

# 모델 정규화. (정규식 패턴, 정규화명, 기본 저장용량)
MODEL_PATTERNS: list[tuple[re.Pattern, str, Optional[int]]] = [
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\s*pro\s*max\b"),       "iPhone 17 Pro Max", 256),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\s*pro\b"),             "iPhone 17 Pro",     256),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\s*plus\b"),            "iPhone 17 Plus",    128),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\b"),                   "iPhone 17",         128),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*16\s*pro\s*max\b"),       "iPhone 16 Pro Max", 256),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*16\s*pro\b"),             "iPhone 16 Pro",     128),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*16\b"),                   "iPhone 16",         128),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s26\s*ultra\b"),          "Galaxy S26 Ultra",  256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s26\s*\+|s26\s*plus"),     "Galaxy S26+",       256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s26\b"),                   "Galaxy S26",        256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s25\s*ultra\b"),          "Galaxy S25 Ultra",  256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s25\b"),                   "Galaxy S25",        256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*fold\s*7\b|폴드\s*7"),  "Galaxy Z Fold 7",   256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*flip\s*7\b|플립\s*7"),  "Galaxy Z Flip 7",   256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*fold\s*6\b|폴드\s*6"),  "Galaxy Z Fold 6",   256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*flip\s*6\b|플립\s*6"),  "Galaxy Z Flip 6",   256),
]

STORAGE_RE = re.compile(r"(\d{3,4})\s*(?:GB|기가|gb)", re.IGNORECASE)
# 가격: "현금완납 5만", "현완 50만원", "현완가 -10만원" 등
# 만원 단위, 마이너스(차익) 포함
PRICE_RES = [
    re.compile(r"(?:현완|현금완납|현금가|현금)\s*(?:가)?\s*[:：]?\s*(-?\d+(?:\.\d+)?)\s*만"),
    re.compile(r"(-?\d+(?:\.\d+)?)\s*만원\s*(?:현완|현금완납|현금가|일시불)"),
    re.compile(r"(?:일시불|일완)\s*[:：]?\s*(-?\d+(?:\.\d+)?)\s*만"),
]
MONTHLY_RE = re.compile(r"월\s*(\d{1,3}(?:,\d{3})*)\s*원")
PLAN_RE   = re.compile(r"(5GX?[가-힣A-Za-z]+|요고[가-힣]+|초이스[가-힣]+|다이렉트[가-힣]*|월\s*\d{1,3}\s*요금제|[5-9]G\s*\d+)")
DURATION_RE = re.compile(r"(\d+)\s*개월\s*유지")


@dataclass
class ParsedPrice:
    model_name: str
    model_raw: str
    carrier: Optional[str] = None
    subscription_type: Optional[str] = None
    contract_type: Optional[str] = None
    storage_gb: Optional[int] = None
    cash_price: Optional[int] = None
    monthly_fee: Optional[int] = None
    plan_name: Optional[str] = None
    plan_duration_mo: Optional[int] = None
    add_condition: Optional[str] = None
    confidence: float = 0.5
    raw_text: Optional[str] = None


def _first(text: str, mapping: dict[str, list[str]]) -> Optional[str]:
    for normalized, aliases in mapping.items():
        for a in aliases:
            if a in text:
                return normalized
    return None


def _extract_models(text: str) -> list[tuple[str, str, Optional[int]]]:
    """본문에서 검출된 모델들 [(normalized, raw_match, default_storage)]."""
    hits: list[tuple[str, str, Optional[int]]] = []
    seen: set[str] = set()
    for pat, norm, default_storage in MODEL_PATTERNS:
        m = pat.search(text)
        if m and norm not in seen:
            hits.append((norm, m.group(0), default_storage))
            seen.add(norm)
    return hits


def _extract_cash_price(text: str) -> Optional[int]:
    for r in PRICE_RES:
        m = r.search(text)
        if m:
            try:
                man = float(m.group(1))
                return int(man * 10000)
            except ValueError:
                continue
    # 폴백: "59만" 처럼 짧게만 적혀있을 때 — 너무 광범위 → 신뢰도 낮음
    return None


def _extract_storage(text: str) -> Optional[int]:
    m = STORAGE_RE.search(text)
    if m:
        try:
            v = int(m.group(1))
            if v in (64, 128, 256, 512, 1024, 2048):
                return v
        except ValueError:
            pass
    return None


def parse_post_text(title: str, body: str) -> list[ParsedPrice]:
    """게시글 제목 + 본문에서 가격 레코드들을 뽑아낸다."""
    text = (title or "") + "\n" + (body or "")
    models = _extract_models(text)
    if not models:
        return []

    carrier   = _first(text, CARRIER_MAP)
    sub       = _first(text, SUB_MAP)
    contract  = _first(text, CONTRACT_MAP)
    storage   = _extract_storage(text)
    cash      = _extract_cash_price(text)

    monthly_m = MONTHLY_RE.search(text)
    monthly   = int(monthly_m.group(1).replace(",", "")) if monthly_m else None

    plan_m    = PLAN_RE.search(text)
    plan_name = plan_m.group(0) if plan_m else None

    dur_m     = DURATION_RE.search(text)
    duration  = int(dur_m.group(1)) if dur_m else None

    # 본문이 너무 짧으면 신뢰도 낮춤
    confidence = 0.3
    if carrier and sub: confidence += 0.2
    if cash:            confidence += 0.3
    if contract:        confidence += 0.1
    confidence = min(confidence, 0.95)

    out: list[ParsedPrice] = []
    for norm, raw, default_storage in models:
        out.append(
            ParsedPrice(
                model_name=norm,
                model_raw=raw,
                carrier=carrier,
                subscription_type=sub,
                contract_type=contract,
                storage_gb=storage or default_storage,
                cash_price=cash,
                monthly_fee=monthly,
                plan_name=plan_name,
                plan_duration_mo=duration,
                add_condition=None,
                confidence=confidence,
                raw_text=text[:500],
            )
        )
    return out


def to_db_rows(parsed: list[ParsedPrice], snapshot_date: str) -> list[dict]:
    rows: list[dict] = []
    for p in parsed:
        r = asdict(p)
        r["snapshot_date"] = snapshot_date
        rows.append(r)
    return rows


if __name__ == "__main__":
    sample = "갤럭시 S26 울트라 256 KT MNP 선약 현완 35만원 월 89,000원 5GX프라임 6개월유지"
    for p in parse_post_text("[성지] " + sample, ""):
        print(p)
