"""
카카오 성지 단가 롤링 히스토리 — 전일 대비 리베이트 변화 감지용.

카카오/비전 데이터는 매일 최신 스냅샷으로 재스탬프되어 DB 안에 '어제'가 없다.
그래서 빌드 때마다 (모델×통신사×가입유형) 집계와 (매장×모델×통신사) 최저가를
JSON 파일에 날짜별로 누적하고, 직전 날짜와 비교해 변화를 산출한다.

- 파일: seongji_kakao_history.json (repo 커밋 — 5am 로컬 루틴이 갱신해 푸시)
- 컨테이너에서는 앱 디렉터리가 읽기전용일 수 있어 저장 실패는 무시(읽기+당일 비교는 동작)
- 가입유형 미표기는 MNP로 정규화해 집계한다(성지 시세표 관행 — 미표기≈번호이동 기준).
  원본 행은 건드리지 않고 히스토리 집계 차원만 정규화.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

HISTORY_PATH = Path(__file__).parent / "seongji_kakao_history.json"
KEEP_DAYS = 30          # 롤링 보존 일수
MIN_GROUP_N = 2         # 집계 최소 관측수 (1건짜리 변동은 노이즈)
CHANGE_MIN_WON = 30_000  # 이 금액 미만 변동은 무시


def eff_sub(sub: str | None) -> str:
    """가입유형 미표기 → MNP 간주 (분석 차원 정규화)."""
    return sub if sub in ("MNP", "기변", "신규") else "MNP"


def _aggregate(rows: list[dict]) -> dict:
    """kakao 매장 행 → {'agg': [...], 'stores': [...]} 집계."""
    groups: dict[tuple, list[int]] = defaultdict(list)
    store_min: dict[tuple, int] = {}
    for r in rows:
        price = r.get("cash_price")
        model, carrier = r.get("model_name"), r.get("carrier")
        if price is None or not model or carrier not in ("SKT", "KT", "LGU+"):
            continue
        sub = eff_sub(r.get("subscription_type"))
        groups[(model, carrier, sub)].append(price)
        if r.get("author"):     # 매장명 없는 행은 매장 단위 추적 불가 — 집계만 반영
            sk = (r["author"], model, carrier)
            if sk not in store_min or price < store_min[sk]:
                store_min[sk] = price

    agg = [
        {"model": m, "carrier": c, "sub": s, "count": len(v),
         "median": int(statistics.median(v)), "min": min(v)}
        for (m, c, s), v in groups.items() if len(v) >= MIN_GROUP_N
    ]
    stores = [
        {"author": a, "model": m, "carrier": c, "min": p}
        for (a, m, c), p in store_min.items()
    ]
    return {"agg": agg, "stores": stores}


def load() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"days": {}}


def record(rows: list[dict], snapshot_date: str) -> dict:
    """오늘 집계를 히스토리에 반영. 저장 실패(읽기전용 FS)는 무시하고 메모리 결과 반환."""
    hist = load()
    hist["days"][snapshot_date] = _aggregate(rows)
    keep = sorted(hist["days"].keys())[-KEEP_DAYS:]
    hist["days"] = {d: hist["days"][d] for d in keep}
    # 매장 상세(stores)는 직전일 비교에만 쓰임 — 최근 2일만 보존해 파일 비대화 방지
    # (agg 는 KEEP_DAYS 유지 — 추세 스파크라인 등 후속 활용 대비)
    for d in keep[:-2]:
        hist["days"][d].pop("stores", None)
    try:
        HISTORY_PATH.write_text(
            json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass   # 컨테이너 읽기전용 — 커밋된 파일 + 오늘 메모리 집계로 비교는 가능
    return hist


def compute_changes(hist: dict, latest_date: str) -> dict:
    """직전 날짜 대비 변화 산출.

    반환: {
      base_date, latest_date,
      agg: [{model, carrier, sub, prev_median, median, diff, prev_min, min, min_diff, count}],
      stores: [{author, model, carrier, prev_min, min, diff}],
    }
    diff < 0 = 가격 인하 = 리베이트 추가(공세), diff > 0 = 리베이트 축소.
    """
    days = hist.get("days", {})
    prev_dates = sorted(d for d in days if d < latest_date)
    if latest_date not in days or not prev_dates:
        return {"base_date": None, "latest_date": latest_date, "agg": [], "stores": []}
    base = prev_dates[-1]

    def _idx(day: str, kind: str, keyf) -> dict:
        return {keyf(x): x for x in days[day].get(kind, [])}

    cur_a = _idx(latest_date, "agg", lambda x: (x["model"], x["carrier"], x["sub"]))
    prv_a = _idx(base, "agg", lambda x: (x["model"], x["carrier"], x["sub"]))
    agg_changes = []
    for k, cur in cur_a.items():
        prev = prv_a.get(k)
        if not prev:
            continue
        diff = cur["median"] - prev["median"]
        min_diff = cur["min"] - prev["min"]
        if abs(diff) < CHANGE_MIN_WON and abs(min_diff) < CHANGE_MIN_WON:
            continue
        agg_changes.append({
            "model": k[0], "carrier": k[1], "sub": k[2],
            "prev_median": prev["median"], "median": cur["median"], "diff": diff,
            "prev_min": prev["min"], "min": cur["min"], "min_diff": min_diff,
            "count": cur["count"],
        })
    agg_changes.sort(key=lambda x: min(x["diff"], x["min_diff"]))

    cur_s = _idx(latest_date, "stores", lambda x: (x["author"], x["model"], x["carrier"]))
    prv_s = _idx(base, "stores", lambda x: (x["author"], x["model"], x["carrier"]))
    store_changes = []
    for k, cur in cur_s.items():
        prev = prv_s.get(k)
        if not prev:
            continue
        diff = cur["min"] - prev["min"]
        if abs(diff) < CHANGE_MIN_WON:
            continue
        store_changes.append({
            "author": k[0], "model": k[1], "carrier": k[2],
            "prev_min": prev["min"], "min": cur["min"], "diff": diff,
        })
    store_changes.sort(key=lambda x: x["diff"])

    return {"base_date": base, "latest_date": latest_date,
            "agg": agg_changes[:40], "stores": store_changes[:40]}
