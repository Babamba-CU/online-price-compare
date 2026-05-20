"""
3사 사이트의 단말 표기를 통일된 (model_name, storage_gb) 튜플로 정규화.

3사 표기 차이:
  SKT  : "갤럭시 S26" + 별도 "256G" 컬럼              → (Galaxy S26, 256)
  KT   : 펫네임 "갤럭시 S26" + 모델명 "SM-S942NK512"  → (Galaxy S26, 512)
                                            "SM-S942NK"     → (Galaxy S26, 256)
  LGU+ : "갤럭시 S26 256GB" + 모델명 "SM-S942N256"    → (Galaxy S26, 256)
                            "SM-S942N512"             → (Galaxy S26, 512)

핵심 매핑은 삼성 모델 코드 (SM-Sxxx, SM-Fxxx) 의 4자리 prefix 다.
"""
from __future__ import annotations

import re
from typing import Optional

# 삼성 4자리 모델 prefix → 정규화 단말명
SAMSUNG_FAMILY: dict[str, str] = {
    # Galaxy S 시리즈 (2026)
    "S942": "Galaxy S26",
    "S947": "Galaxy S26+",
    "S948": "Galaxy S26 Ultra",
    # Galaxy S 시리즈 (2025)
    "S931": "Galaxy S25",
    "S937": "Galaxy S25+",
    "S938": "Galaxy S25 Ultra",
    # Galaxy S 시리즈 (2024)
    "S921": "Galaxy S24",
    "S926": "Galaxy S24+",
    "S928": "Galaxy S24 Ultra",
    # Galaxy Z (Fold/Flip)
    "F956": "Galaxy Z Fold 7",
    "F766": "Galaxy Z Flip 7",
    "F761": "Galaxy Z Flip 7 FE",
    "F946": "Galaxy Z Fold 6",
    "F741": "Galaxy Z Flip 6",
    # Galaxy A
    "A356": "Galaxy A35",
    "A556": "Galaxy A55",
    # 기타
    "G781": "Galaxy Quantum 5",
    "M156": "Galaxy Wide 7",
}

# 한글 펫네임/표기 → 정규화 단말명 (SKT 처럼 모델코드 없이 한글만 있는 경우)
KOREAN_NICKNAME: dict[str, str] = {
    "갤럭시 S26 울트라":    "Galaxy S26 Ultra",
    "갤럭시 S26 플러스":    "Galaxy S26+",
    "갤럭시 S26":            "Galaxy S26",
    "갤럭시 S25 울트라":    "Galaxy S25 Ultra",
    "갤럭시 S25 플러스":    "Galaxy S25+",
    "갤럭시 S25":            "Galaxy S25",
    "갤럭시 Z 폴드 7":      "Galaxy Z Fold 7",
    "갤럭시 Z Fold7":       "Galaxy Z Fold 7",
    "갤럭시 Z 플립 7 FE":   "Galaxy Z Flip 7 FE",
    "갤럭시 Z Flip7 FE":    "Galaxy Z Flip 7 FE",
    "갤럭시 Z 플립 7":      "Galaxy Z Flip 7",
    "갤럭시 Z Flip7":       "Galaxy Z Flip 7",
    "갤럭시 Z 폴드 6":      "Galaxy Z Fold 6",
    "갤럭시 Z 플립 6":      "Galaxy Z Flip 6",
}

# Apple 모델 (한글/영문 양쪽)
APPLE_NAMES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)(iPhone|아이폰)\s*17\s*Pro\s*Max"),      "iPhone 17 Pro Max"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*17\s*Pro"),            "iPhone 17 Pro"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*17\s*Plus"),           "iPhone 17 Plus"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*17"),                  "iPhone 17"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*16\s*Pro\s*Max"),      "iPhone 16 Pro Max"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*16\s*Pro"),            "iPhone 16 Pro"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*16\s*Plus"),           "iPhone 16 Plus"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*16"),                  "iPhone 16"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*SE\s*4"),              "iPhone SE 4"),
    (re.compile(r"(?i)(iPhone|아이폰)\s*15\s*Pro\s*Max"),      "iPhone 15 Pro Max"),
]


def parse_samsung_code(code: str) -> Optional[tuple[str, int]]:
    """
    삼성 모델 코드에서 (정규화 단말명, 용량GB) 추출.

    예시:
      SM-S942NK512  → ("Galaxy S26", 512)
      SM-S942NK     → ("Galaxy S26", 256)   (용량 미표기 → 기본 256)
      SM-S942N256   → ("Galaxy S26", 256)
      SM-S942N      → ("Galaxy S26", 256)
      SM-F761N256   → ("Galaxy Z Flip 7 FE", 256)
    """
    if not code:
        return None
    code = code.strip().upper().replace(" ", "")
    # SM-XYYYZ[storage] 형식
    m = re.match(r"SM[-_]?([SFAGNM])(\d{3})[A-Z]*(\d{3,4})?", code)
    if not m:
        return None
    prefix = (m.group(1) + m.group(2))                 # ex: S942, F761
    storage = int(m.group(3)) if m.group(3) else 256   # 미표기 = base 256
    family = SAMSUNG_FAMILY.get(prefix)
    if not family:
        return None
    return (family, storage)


def parse_korean_nickname(text: str, storage_hint: Optional[int] = None) -> Optional[tuple[str, int]]:
    """한글 펫네임 (SKT 스타일) + 별도 용량 hint 에서 정규화."""
    if not text:
        return None
    text = text.strip()
    # 가장 긴 매치 우선
    for kor, eng in sorted(KOREAN_NICKNAME.items(), key=lambda kv: -len(kv[0])):
        if kor in text:
            return (eng, storage_hint or 256)
    return None


def parse_apple(text: str, storage_hint: Optional[int] = None) -> Optional[tuple[str, int]]:
    if not text:
        return None
    for pat, name in APPLE_NAMES:
        if pat.search(text):
            return (name, storage_hint or 128)
    return None


def normalize(model_text: str = "",
              model_code: str = "",
              storage_hint: Optional[int] = None) -> Optional[tuple[str, int]]:
    """
    3사 어떤 입력이든 받아 (model_name, storage_gb) 반환.
    우선순위:
      1) 삼성 모델 코드 (SM-Sxxx) — 가장 정확
      2) 한글 펫네임 (갤럭시 S26 등) — storage_hint 결합
      3) Apple 정규식
    """
    if model_code:
        out = parse_samsung_code(model_code)
        if out:
            # 코드 끝에 용량이 있어도 hint 가 더 신뢰가능하면 우선
            if storage_hint and out[1] != storage_hint:
                return (out[0], storage_hint)
            return out
    if model_text:
        out = parse_korean_nickname(model_text, storage_hint)
        if out:
            return out
        out = parse_apple(model_text, storage_hint)
        if out:
            return out
    return None


# 정합성 점검용: 알려진 단말의 캐논 출고가 (사이트별 ±5,000원 허용)
# 사용자 제공 스크린샷 기준 — 변경되면 직접 갱신 필요
KNOWN_RETAIL_HINTS: dict[tuple[str, int], int] = {
    ("Galaxy S26",        256): 1_254_000,
    ("Galaxy S26",        512): 1_507_000,
    ("Galaxy S26+",       256): 1_452_000,
    ("Galaxy S26+",       512): 1_705_000,
    ("Galaxy S26 Ultra",  256): 1_797_400,
    ("Galaxy S26 Ultra",  512): 2_050_400,
    ("Galaxy Z Flip 7 FE", 256): 1_199_000,
}


if __name__ == "__main__":
    samples = [
        ("",                 "SM-S942NK"),
        ("",                 "SM-S942NK512"),
        ("",                 "SM-S942N256"),
        ("",                 "SM-S942N512"),
        ("",                 "SM-S948NK512"),
        ("",                 "SM-F761N256"),
        ("갤럭시 S26",        "", 256),
        ("갤럭시 S26 울트라",  "", 512),
        ("iPhone 17 Pro Max", "", 256),
        ("아이폰 16 Pro",     "", 128),
    ]
    for s in samples:
        if len(s) == 2:
            print(s, "→", normalize(model_code=s[1]))
        else:
            print(s, "→", normalize(model_text=s[0], storage_hint=s[2]))
