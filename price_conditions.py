"""
조건부 가격 판정 — 파이프라인(적재·집계·히스토리)과 화면(index.html COND_RE)이 같은 기준을 쓴다.

'결합(인터넷+TV)·제휴카드·온누리 체감가·행사 적용가(바페적용가/이벤트가/추가페이백 적용가)'는
순수 단말 시세가 아니라 조건부 가격이다. 행은 버리지 않고 is_conditional 로 표시해
기본 집계(일별 통계·박스플롯·전일 대비)에서 제외하고, 화면에서는 토글로 볼 수 있게 한다.
(2026-09-08 점검: 종전엔 적재의 제외 정규식이 죽은 코드라 결합가가 일반 최저가로 집계됐다.)
"""
from __future__ import annotations

import re

# index.html 의 COND_RE 와 동일 키워드 + '카드'(제휴카드 축약 표기)
COND_KEYWORDS = ("결합", "인터넷", "TV", "제휴", "카드", "온누리", "체감",
                 "페스티벌", "이벤트", "적용가", "추가페이백")
COND_RE = re.compile("|".join(re.escape(k) for k in COND_KEYWORDS), re.IGNORECASE)


def is_conditional(add_condition: str | None) -> bool:
    return bool(add_condition) and bool(COND_RE.search(add_condition))


def cond_sql(col: str = "p.add_condition") -> str:
    """SQL WHERE 조각 — '조건부가 아님'. SQLite 에 REGEXP 가 없어 LIKE 체인으로 표현."""
    likes = " AND ".join(f"{col} NOT LIKE '%{k}%'" for k in COND_KEYWORDS)
    return f"({col} IS NULL OR ({likes}))"


if __name__ == "__main__":
    for t in (None, "", "약정1년", "월청구추정", "바페적용가", "인터넷+TV 동시가입", "제휴카드 할인 포함가", "온누리 체감가"):
        print(repr(t), "→", is_conditional(t))
