"""
KST 날짜 헬퍼 — 파이프라인 전체가 같은 '오늘'을 쓰게 한다.

클라우드 루틴(Anthropic 샌드박스)과 GitHub Actions 는 UTC 라서 `date.today()` 가
05:05 KST 실행분을 전날로 라벨했다(2026-09-07 실측: 15:34Z 커밋이 09-07 로 표기).
한국은 서머타임이 없으므로 고정 오프셋 +9 로 충분하다(tzdata 의존 없음).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst() -> date:
    return now_kst().date()


def today_iso() -> str:
    return today_kst().isoformat()
