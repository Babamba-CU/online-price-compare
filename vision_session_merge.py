"""
루틴 판독 결과 병합 — Claude 앱 세션이 저장한 /tmp/sise_batch/results/<idx>.json 을
seongji_vision_data.json 에 병합하고 vision_skiplist 를 갱신한다.

API 판독기와 **같은 후처리 함수**(vision_api_reader.to_items / merge)를 재사용하므로
저장 스키마·신선도 태그(reader)·가격 sanity·신뢰도 컷이 API 경로와 동일하다.
reader 태그만 "claude-app-routine" 으로 남겨 어느 경로로 판독됐는지 구분한다.

  · 결과 파일이 없는 이미지(세션이 상한/시간 때문에 못 읽음)는 '미판독' — 스킵리스트에
    반영하지 않고 다음 날 다시 다운로드 대상이 된다(부분 완료 허용).
  · 판독은 했는데 시세표가 아니거나 행이 0인 채널만 실패로 기록(2회 누적 시 스킵).

사용: python vision_session_merge.py   (routine_finalize.sh 가 호출)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import vision_api_reader as reader   # to_items / merge 재사용
import vision_skiplist

BATCH = Path("/tmp/sise_batch")
READER_TAG = "claude-app-routine"
REQUIRED_ROW_KEYS = ("model_name", "cash_price")


def _log(msg: str) -> None:
    print(f"[session-merge] {msg}", file=sys.stderr, flush=True)


def _load_result(path: Path) -> dict | None:
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _log(f"{path.name}: JSON 파싱 실패 — {e!r}")
        return None
    if not isinstance(r, dict) or not isinstance(r.get("rows"), list):
        _log(f"{path.name}: 스키마 불일치(rows 배열 없음)")
        return None
    # 필수 키 빠진 행은 버린다(to_items 가 KeyError 로 죽지 않게)
    r["rows"] = [row for row in r["rows"]
                 if isinstance(row, dict) and all(k in row for k in REQUIRED_ROW_KEYS)
                 and isinstance(row["cash_price"], int)]
    return r


def main() -> int:
    mf = BATCH / "manifest.json"
    if not mf.exists():
        _log("manifest.json 없음 — 병합할 것 없음")
        return 0
    manifest = json.loads(mf.read_text(encoding="utf-8"))

    # reader 태그를 루틴 경로로 (to_items 는 호출 시점의 모듈 전역을 읽는다)
    reader.MODEL = READER_TAG
    reader.ESCALATE_MODEL = READER_TAG

    all_items: list[dict] = []
    channel_ok: dict[str, bool] = {}
    n_read = n_missing = n_bad = n_nontable = 0
    for idx, entry in enumerate(manifest):
        rf = BATCH / "results" / f"{idx}.json"
        if not rf.exists():
            n_missing += 1
            continue
        result = _load_result(rf)
        if result is None:
            n_bad += 1
            continue
        n_read += 1
        if not result.get("is_price_table"):
            n_nontable += 1
            items = []
        else:
            items = reader.to_items(entry, result)
        handle = entry["handle"]
        channel_ok[handle] = channel_ok.get(handle, False) or bool(items)
        all_items.extend(items)

    _log(f"판독 {n_read}장(시세표 아님 {n_nontable}) · 미판독 {n_missing}장 · 손상 {n_bad}장 → {len(all_items)}행")
    if all_items:
        total = reader.merge(all_items)
        _log(f"seongji_vision_data.json 병합: 총 {total}행")
    else:
        _log("새 행 없음 — seongji_vision_data.json 변경하지 않음")
    if channel_ok:
        vision_skiplist.record(channel_ok)
        _log(f"skiplist 갱신: 실패 {sum(1 for v in channel_ok.values() if not v)}건 / 성공 {sum(1 for v in channel_ok.values() if v)}건")
    print(f"MERGED rows={len(all_items)} read={n_read} missing={n_missing} bad={n_bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
