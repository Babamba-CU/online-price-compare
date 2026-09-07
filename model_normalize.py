"""
기종명 정규화 — Vision 판독/텍스트 파서가 뱉는 제각각 표기를 하나의 정규 표기로 통일.

설계 원칙(2026-09-07, 신규 기종 자동 반영 구조):
  · 화이트리스트가 아니라 **패턴 규칙**이다. "Galaxy {계열} {세대} {변형}" /
    "iPhone {세대} {변형}" 꼴이면 처음 보는 세대(폴드 8, S27, 아이폰 18…)도
    등록 없이 정규화된다 — 새 기종이 출시돼도 코드를 고칠 필요가 없다.
  · 모르는 이름은 **절대 버리지 않는다**. 패턴에 안 맞으면 공백/브랜드 표기만
    다듬어 그대로 통과시킨다(별도 기종으로 남음). 잘못 합치는 것보다 낫다.
  · 잘못 합칠 위험이 있는 애매한 꼬리표(예: "A17S"의 S, "A175")는 변형으로
    해석하지 않고 원형 유지.

실측 파편화 사례(seongji_vision_data.json 2026-07):
  Galaxy S25FE / S25 FE            → Galaxy S25 FE
  Galaxy Z Fold7 / Z Fold 7        → Galaxy Z Fold 7
  Galaxy S25U / S25 Ultra          → Galaxy S25 Ultra
  Galaxy S25 엣지 / 옛지 / Edge     → Galaxy S25 Edge
  iPhone 17E / 17 e / 17e          → iPhone 17e
  iPhone 16+                       → iPhone 16 Plus
  갤럭시퀀텀6 / Galaxy Quantum6    → Galaxy Quantum 6
  Galaxy Z Flip 7 (사전예약)       → Galaxy Z Flip 7
"""
from __future__ import annotations

import re

_PAREN_RE = re.compile(r"\s*[\(（\[][^)）\]]*[\)）\]]")   # (사전예약) [특가] 등 꼬리표 제거
_WS_RE = re.compile(r"\s+")

# ---- Galaxy ---------------------------------------------------------------
# 계열 표기(영/한, 붙여쓰기 포함) → 정규 계열명. 긴 표기가 먼저 매치되도록 정렬해 사용.
_GALAXY_FAMILY = {
    "z fold": "Z Fold", "zfold": "Z Fold", "fold": "Z Fold", "z 폴드": "Z Fold", "z폴드": "Z Fold", "폴드": "Z Fold",
    "z flip": "Z Flip", "zflip": "Z Flip", "flip": "Z Flip", "z 플립": "Z Flip", "z플립": "Z Flip", "플립": "Z Flip",
    "quantum": "Quantum", "퀀텀": "Quantum",
    "wide": "Wide", "와이드": "Wide",
    "jump": "Jump", "점프": "Jump",
    "buddy": "Buddy", "버디": "Buddy",
    "note": "Note", "노트": "Note",
    "s": "S", "a": "A", "m": "M",
}
_GALAXY_FAMILY_RE = "|".join(
    re.escape(k) for k in sorted(_GALAXY_FAMILY, key=len, reverse=True))
# 세대 번호 뒤에 붙는 변형 토큰 → 정규 표기. 여기 없는 토큰이 붙으면 정규화하지 않음(원형 유지).
_GALAXY_VARIANT = {
    "ultra": "Ultra", "u": "Ultra", "울트라": "Ultra", "울트": "Ultra",
    "+": "+", "plus": "+", "플러스": "+",
    "fe": "FE",
    "edge": "Edge", "엣지": "Edge", "옛지": "Edge",
    "wide": "Wide",
}
# 단문자 계열(S/A/M)은 삼성 표기대로 숫자를 붙여 쓴다("Galaxy S26"); 단어 계열은 띄운다("Galaxy Z Fold 7").
_LETTER_FAMILY = {"S", "A", "M"}
# "galaxy" 접두어는 선택 — 매장 약칭 "버디5"/"점프5"/"Buddy 5" 도 Galaxy 로 귀속.
# 단, 접두어 없는 단문자 계열("S26","A175")은 모델코드와 혼동되므로 정규화하지 않는다.
_GALAXY_RE = re.compile(
    rf"^(galaxy\s*)?({_GALAXY_FAMILY_RE})\s*(\d+)\s*([a-z+가-힣]*)\s*(.*)$", re.IGNORECASE)

# ---- iPhone ---------------------------------------------------------------
_IPHONE_VARIANT = {
    "pro": "Pro", "프로": "Pro",
    "max": "Max", "맥스": "Max",
    "plus": "Plus", "+": "Plus", "플러스": "Plus",
    "e": "e",
    "air": "Air", "에어": "Air",
    "mini": "mini", "미니": "mini",
}
_IPHONE_RE = re.compile(r"^iphone\s*(air|se)?\s*(\d+)?\s*([a-z+가-힣]*)\s*(.*)$", re.IGNORECASE)

# 규칙으로 못 푸는 확정 별칭 (소문자 키). Apple 공식명은 세대 없는 "iPhone Air".
_ALIASES = {
    "iphone 17 air": "iPhone Air",
}


def _tidy(s: str) -> str:
    s = _PAREN_RE.sub("", s or "")
    s = s.replace("＋", "+")
    s = re.sub(r"(?i)갤럭시\s*", "Galaxy ", s)
    s = re.sub(r"(?i)아이폰\s*", "iPhone ", s)
    return _WS_RE.sub(" ", s).strip()


def _split_tokens(attached: str, rest: str) -> list[str]:
    """숫자 뒤에 붙은 꼬리(attached)와 공백 뒤 나머지(rest)를 토큰 목록으로."""
    toks = []
    if attached:
        # "프로맥스"처럼 한 덩어리로 붙은 복합 변형 분리
        attached = attached.replace("프로맥스", "프로 맥스")
        toks += attached.split()
    if rest:
        toks += rest.replace("프로맥스", "프로 맥스").split()
    return toks


def normalize(name: str | None) -> str:
    """제각각 표기의 기종명 → 정규 표기. 모르는 이름은 다듬기만 하고 그대로 반환."""
    s = _tidy(name)
    if not s:
        return s
    key = s.lower()
    if key in _ALIASES:
        return _ALIASES[key]

    m = _GALAXY_RE.match(s)
    if m:
        prefix, fam_raw, num, attached, rest = m.groups()
        fam = _GALAXY_FAMILY[fam_raw.lower()]
        if not prefix and fam in _LETTER_FAMILY:
            return s                # "S26"/"A175" 처럼 접두어 없는 단문자 계열은 애매 → 원형 유지
        toks = _split_tokens(attached, rest)
        variants = []
        for t in toks:
            v = _GALAXY_VARIANT.get(t.lower())
            if v is None:
                return s            # 모르는 꼬리표(A17S 의 S 등) → 원형 유지
            if v not in variants:
                variants.append(v)
        sep = "" if fam in _LETTER_FAMILY else " "
        out = f"Galaxy {fam}{sep}{num}"
        for v in variants:
            out += v if v == "+" else f" {v}"
        return out

    m = _IPHONE_RE.match(s)
    if m:
        kind, num, attached, rest = m.groups()
        toks = _split_tokens(attached, rest)
        variants = []
        for t in toks:
            v = _IPHONE_VARIANT.get(t.lower())
            if v is None:
                return s
            if v not in variants:
                variants.append(v)
        if kind and kind.lower() == "air" and not num:
            return "iPhone Air"
        if kind and kind.lower() == "se":
            return "iPhone SE" + (f" {num}" if num else "")
        if not num:
            return s
        out = f"iPhone {num}"
        # 'e' 파생은 붙여쓰기(17e), 나머지는 띄어쓰기(17 Pro Max)
        for v in variants:
            out += v if v == "e" else f" {v}"
        return out

    return s


def sort_key(name: str) -> tuple:
    """드롭다운 정렬: 브랜드 그룹 → 최신 세대 우선 → 이름. 새 기종이 위로 온다."""
    n = normalize(name)
    low = n.lower()
    if re.match(r"galaxy s\d", low):
        brand = 0
    elif low.startswith("galaxy z "):
        brand = 1
    elif low.startswith("iphone"):
        brand = 2
    elif low.startswith("galaxy"):
        brand = 3
    else:
        brand = 4
    gen_m = re.search(r"\d+", n)
    gen = int(gen_m.group()) if gen_m else (17 if low == "iphone air" else 0)
    return (brand, -gen, n)


if __name__ == "__main__":
    import json
    import sys
    # 자체 검증: 인자로 준 JSON({raw: count}) 의 모든 이름을 정규화해 병합 결과 출력
    src = sys.argv[1] if len(sys.argv) > 1 else None
    raw = json.load(open(src, encoding="utf-8")) if src else {}
    merged: dict[str, list[str]] = {}
    for r in raw:
        merged.setdefault(normalize(r), []).append(r)
    for canon in sorted(merged, key=sort_key):
        srcs = merged[canon]
        mark = "  ← 병합" if len(srcs) > 1 or srcs[0] != canon else ""
        print(f"{canon:28s} {sum(raw[s] for s in srcs):5d}행  {srcs if mark else ''}{mark}")
    print(f"\n{len(raw)} raw → {len(merged)} canonical")
