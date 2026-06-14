"""
Vision 분석 스킵리스트 — 한 Opus 세션에 끝나는 일일 수집을 위한 효율화.

카카오 채널의 최신 이미지를 분석했는데 시세표가 아니거나(매장사진·행사포스터)
판독이 실패하면, 그 채널의 fail 카운트를 올린다. fail 이 THRESHOLD(2) 이상이면
이후 일일 수집에서 그 채널은 다운로드·분석 대상에서 제외 → 매일 같은 헛수고 방지.

채널이 나중에 진짜 시세표를 올려 한 번이라도 추출에 성공하면 카운트를 0으로 리셋.

파일: vision_skiplist.json  { "<handle>": {"fails": N, "last": "YYYY-MM-DD", "reason": "..."} }
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

PATH = Path(__file__).parent / "vision_skiplist.json"
THRESHOLD = 2          # fails >= THRESHOLD 면 영구(쿨다운까지) 스킵
COOLDOWN_DAYS = 14     # 스킵 후 N일 지나면 한 번 더 기회(채널이 시세표 시작했을 수 있음)


def load() -> dict:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save(data: dict) -> None:
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def is_skipped(handle: str, data: dict | None = None, today: str | None = None) -> bool:
    data = load() if data is None else data
    e = data.get(handle)
    if not e or e.get("fails", 0) < THRESHOLD:
        return False
    # 쿨다운 경과 시 재시도 허용
    last = e.get("last")
    if last and today:
        try:
            if (date.fromisoformat(today) - date.fromisoformat(last)).days >= COOLDOWN_DAYS:
                return False
        except ValueError:
            pass
    return True


def record(results: dict[str, bool], reason: str = "시세표 미검출/판독실패") -> dict:
    """results: {handle: had_items}. 성공(True)→리셋, 실패(False)→fails+1."""
    data = load()
    today = date.today().isoformat()
    for handle, ok in results.items():
        if ok:
            data.pop(handle, None)                 # 성공 → 스킵리스트에서 제거
        else:
            e = data.get(handle, {"fails": 0})
            e["fails"] = e.get("fails", 0) + 1
            e["last"] = today
            e["reason"] = reason
            data[handle] = e
    save(data)
    return data


def active_skips(today: str | None = None) -> set[str]:
    """현재 스킵 대상 핸들 집합."""
    today = today or date.today().isoformat()
    data = load()
    return {h for h in data if is_skipped(h, data, today)}


if __name__ == "__main__":
    data = load()
    skipped = active_skips()
    print(f"스킵리스트 {len(data)}개 채널, 현재 스킵 {len(skipped)}개 (THRESHOLD={THRESHOLD}, 쿨다운 {COOLDOWN_DAYS}일)")
