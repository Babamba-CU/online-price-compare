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
# 성지 약어(S26U, ZF7, 아17프맥 등) 포함 — 구체적 패턴이 먼저 오도록 순서 유지.
MODEL_PATTERNS: list[tuple[re.Pattern, str, Optional[int]]] = [
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\s*pro\s*max\b|아\s*17\s*(?:프로\s*맥스|프맥)|17\s*프로\s*맥스|17\s*프맥"), "iPhone 17 Pro Max", 256),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\s*pro\b|아\s*17\s*프로|17\s*프로(?!\s*맥)"), "iPhone 17 Pro",     256),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\s*plus\b"),            "iPhone 17 Plus",    128),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*17\b|\b아17\b"),           "iPhone 17",         128),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*16\s*pro\s*max\b"),       "iPhone 16 Pro Max", 256),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*16\s*pro\b"),             "iPhone 16 Pro",     128),
    (re.compile(r"(?i)\b(iphone|아이폰)\s*16\b"),                   "iPhone 16",         128),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s26\s*ultra\b|\bS26\s*(?:U|울트라|울트)\b|S26\s*울트라"), "Galaxy S26 Ultra", 256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s26\s*\+|s26\s*plus|S26\s*플러스|\bS26\+"), "Galaxy S26+",       256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s26\b|\bS26\b"),           "Galaxy S26",        256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s25\s*ultra\b|\bS25\s*(?:U|울트라)\b"), "Galaxy S25 Ultra",  256),
    # S25 파생(Edge/FE/+)은 기본 S25 보다 먼저 — 더 구체적인 표기 우선
    (re.compile(r"(?i)\bs25\s*edge\b"),                            "Galaxy S25 Edge",   256),
    (re.compile(r"(?i)\bs25\s*fe\b"),                              "Galaxy S25 FE",     256),
    (re.compile(r"(?i)\bs25\s*\+|\bs25\s*plus\b|s25\s*플러스"),       "Galaxy S25+",       256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*s25\b|\bS25\b"),           "Galaxy S25",        256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*fold\s*7\b|폴드\s*7|\bZF\s*7\b|\bZ폴드7|\bfold\s*7\b"),  "Galaxy Z Fold 7",   256),
    # 플립7 FE 는 기본 플립7 보다 먼저
    (re.compile(r"(?i)\b(?:z\s*)?flip\s*7\s*fe\b|플립\s*7\s*fe"),     "Galaxy Z Flip 7 FE", 256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*flip\s*7\b|플립\s*7|\bZ플립7|\bflip\s*7\b"),  "Galaxy Z Flip 7",   256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*fold\s*6\b|폴드\s*6|\bZF\s*6\b|\bfold\s*6\b"),  "Galaxy Z Fold 6",   256),
    (re.compile(r"(?i)\b(galaxy|갤럭시)\s*z\s*flip\s*6\b|플립\s*6|\bflip\s*6\b"),  "Galaxy Z Flip 6",   256),
]

# 256GB / 256G / 256기가 모두 인식 ("256G👉" 처럼 GB 없이 G 만 붙는 성지 표기 대응)
STORAGE_RE = re.compile(r"(\d{3,4})\s*(?:GB|기가|G)(?![A-Za-z])", re.IGNORECASE)
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
    """본문에서 검출된 모델들 [(normalized, raw_match, default_storage)].

    패턴은 구체적인 것부터 평가하며, 이미 매칭된 스팬과 겹치는 매칭은 버린다
    ("S26 울트라"가 S26 Ultra 와 S26 둘 다로 잡히는 중복 방지).
    """
    hits: list[tuple[str, str, Optional[int]]] = []
    seen: set[str] = set()
    spans: list[tuple[int, int]] = []
    for pat, norm, default_storage in MODEL_PATTERNS:
        if norm in seen:
            continue
        for m in pat.finditer(text):
            s, e = m.span()
            if any(s < pe and ps < e for ps, pe in spans):
                continue   # 더 구체적인 패턴이 이미 차지한 영역
            hits.append((norm, m.group(0), default_storage))
            seen.add(norm)
            spans.append((s, e))
            break
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


# ------------------------------------------------------------------
# 라인 단위 시세표 파서 (카카오 채널 게시글용)
# ------------------------------------------------------------------
# 성지 시세표는 "S26U👉54", "플립7 30 (번이)" 처럼 한 줄에 모델+가격이 붙고,
# 통신사/가입유형은 섹션 헤더 줄("[ SK ] 번호이동")에서 내려오는 구조가 많다.
# 줄마다 모델+가격을 짝지어 추출하고, 헤더에서 갱신된 컨텍스트를 상속한다.
# 주의: "👉 211,000" 같은 콤마 원단위 숫자(상품권 금액표 등)는 만원으로 오인하지
# 않도록 콤마를 차단한다. 원단위는 현완/현금가 키워드가 붙은 경우만 채택.
LINE_PRICE_RES: list[tuple[re.Pattern, int]] = [
    # (패턴, 곱셈단위) — group(1) * 단위 = 원
    # 기종👉 263,000  (구매가, 원 단위·콤마 포함). 모델이 있는 줄에서만 평가되므로
    # "온누리상품권 337,000원 환급"(모델 없는 줄)과 충돌하지 않는다. → 가장 먼저 시도.
    (re.compile(r"👉🏻?\s*(-?\d{1,3}(?:,\d{3})+)(?!\d)"), 1),
    (re.compile(r"👉🏻?\s*(-?\d{1,3}(?:\.\d)?)(?![\d,])"), 10000),   # S26U👉54 (만원 약어)
    (re.compile(r"(?:현완|현금가|현금완납|일시불)\s*[:：]?\s*(-?\d{1,3}(?:\.\d)?)\s*만"), 10000),
    (re.compile(r"(?:현완|현금가|현금완납|일시불)\s*[:：]?\s*(\d{1,3}(?:,\d{3})+)\s*원"), 1),
    (re.compile(r"(-?\d{1,3}(?:\.\d)?)\s*만\s*원?(?!\s*원\s*대)"), 10000),  # 54만 / 54만원
]

# ------------------------------------------------------------------
# 월청구금액 → 현금완납가 추정
# ------------------------------------------------------------------
# 공식(사용자 확정): 현금완납가 ≈ (월청구액 − 요금제 정가) × 약정개월수(미표기 시 24)
# 요금제 정가 사전 — "NNN요금제" 표기 + 주요 요금제명
PLAN_FEE_MAP = {
    "5GX프리미엄": 109_000, "5GX 프리미엄": 109_000,
    "초이스스페셜": 110_000, "초이스 스페셜": 110_000, "5G 초이스 스페셜": 110_000,
    "5G프리미어슈퍼": 115_000, "5G 프리미어 슈퍼": 115_000,
}
PLAN_NNN_RE = re.compile(r"(\d{2,3})\s*요금제")          # 109요금제 → 109,000원
MONTHLY_BILL_RES: list[tuple[re.Pattern, int]] = [
    (re.compile(r"월\s*청구\s*(?:금액|액)?\s*[:：]?\s*(\d{1,3}(?:,\d{3})+)\s*원"), 1),
    (re.compile(r"월\s*납부\s*[:：]?\s*(\d{1,3}(?:,\d{3})+)\s*원"), 1),
    (re.compile(r"월\s*(?:청구|납부)\s*[:：]?\s*(\d{1,2}(?:\.\d)?)\s*만"), 10000),
]
DURATION_CTX_RE = re.compile(r"(\d{2})\s*개월")
DEFAULT_DURATION_MO = 24


def _plan_fee_from_text(text: str) -> tuple[Optional[str], Optional[int]]:
    """텍스트에서 (요금제명, 월정액 정가) 추출. 'NNN요금제' 또는 알려진 요금제명."""
    m = PLAN_NNN_RE.search(text)
    if m:
        return m.group(0), int(m.group(1)) * 1000
    for name, fee in PLAN_FEE_MAP.items():
        if name in text:
            return name, fee
    return None, None


def estimate_cash_from_monthly(line: str, ctx: str = "") -> Optional[dict]:
    """월청구액 표기 줄에서 현금완납가 추정.

    반환: {cash_price, plan_name, plan_fee, duration_mo} 또는 None.
    요금제 정가를 알 수 없으면 추정하지 않는다 (임의 가정 금지).
    """
    monthly = None
    for r, unit in MONTHLY_BILL_RES:
        m = r.search(line)
        if m:
            monthly = int(float(m.group(1).replace(",", "")) * unit)
            break
    if monthly is None:
        return None
    plan_name, plan_fee = _plan_fee_from_text(line)
    if plan_fee is None:
        plan_name, plan_fee = _plan_fee_from_text(ctx)
    if plan_fee is None or monthly <= plan_fee:
        return None   # 요금제 미상 또는 단말 부담 0 이하 — 추정 불가
    dm = DURATION_CTX_RE.search(line) or DURATION_CTX_RE.search(ctx)
    duration = int(dm.group(1)) if dm else DEFAULT_DURATION_MO
    return {
        "cash_price": (monthly - plan_fee) * duration,
        "plan_name": plan_name,
        "plan_fee": plan_fee,
        "duration_mo": duration,
    }


def parse_seongji_lines(title: str, body: str) -> list[ParsedPrice]:
    """게시글을 줄 단위로 훑어 모델+가격 쌍을 추출 (시세표 텍스트용).

    - 통신사/가입유형/약정은 해당 줄에 없으면 직전 헤더 줄의 컨텍스트를 상속
    - 모델과 가격이 같은 줄에 있어야 채택 (전체 텍스트 1가격 방식보다 정밀)
    - confidence: 모델+가격 0.6, +통신사 0.1, +가입유형 0.1 (최대 0.8)
    """
    text = (title or "") + "\n" + (body or "")
    ctx_carrier: Optional[str] = None
    ctx_sub: Optional[str] = None
    ctx_contract: Optional[str] = None
    out: list[ParsedPrice] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 컨텍스트 헤더 갱신 (가격 유무와 무관)
        c = _first(line, CARRIER_MAP)
        s = _first(line, SUB_MAP)
        k = _first(line, CONTRACT_MAP)
        models = _extract_models(line)

        if not models:
            # 모델 없는 줄 — 섹션 헤더로 보고 컨텍스트만 갱신
            if c: ctx_carrier = c
            if s: ctx_sub = s
            if k: ctx_contract = k
            continue

        price = None
        plan_name = None
        monthly_est = None
        for r, unit in LINE_PRICE_RES:
            m = r.search(line)
            if m:
                try:
                    price = int(float(m.group(1).replace(",", "")) * unit)
                except ValueError:
                    continue
                break
        if price is None:
            # 직접 가격이 없으면 월청구액 기반 추정 시도 (요금제 정가를 알 때만)
            monthly_est = estimate_cash_from_monthly(line, ctx=text)
            if monthly_est is None:
                continue   # 모델만 있고 가격 단서 없는 줄은 스킵
            price = monthly_est["cash_price"]
            plan_name = monthly_est["plan_name"]

        carrier  = c or ctx_carrier
        sub      = s or ctx_sub
        contract = k or ctx_contract
        confidence = 0.6
        if carrier: confidence += 0.1
        if sub:     confidence += 0.1
        if monthly_est: confidence = 0.6   # 추정값은 보수적으로 고정

        storage = _extract_storage(line)
        for norm, raw, default_storage in models:
            out.append(ParsedPrice(
                model_name=norm,
                model_raw=raw,
                carrier=carrier,
                subscription_type=sub,
                contract_type=contract,
                storage_gb=storage or default_storage,
                cash_price=price,
                plan_name=plan_name,
                add_condition=(
                    f"월청구추정({monthly_est['duration_mo']}개월,"
                    f"{monthly_est['plan_fee']:,}원요금제)" if monthly_est else None),
                confidence=min(confidence, 0.8),
                raw_text=line[:200],
            ))
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
