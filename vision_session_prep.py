"""
루틴 판독 준비 — 다운로드된 시세표 이미지를 Claude 앱 세션이 직접 읽기 좋게 정리한다.
(API 판독기 vision_api_reader 를 대체하는 '클로드 앱 루틴' 경로의 2단계 입력 생성)

  · 세로 2,300px 초과 이미지는 원본 해상도로 겹침(150px) 분할해 조각 파일 생성
    — vision_api_reader._image_blocks 와 같은 규칙. 축소 판독으로 '현금가/적용가'
    행 라벨이 뭉개져 값이 섞이던 문제(2026-07 실측)를 세션 판독에서도 막는다.
  · /tmp/sise_batch/session_manifest.json : [{idx, handle, name, region, title, context, pieces:[파일...]}]
  · /tmp/sise_batch/RULES.md              : 판독 규칙(vision_api_reader.PROMPT 단일 소스) + 결과 JSON 스키마
  · /tmp/sise_batch/results/              : 세션이 이미지별 결과를 <idx>.json 으로 저장하는 곳(비워 둠)

사용: python vision_session_prep.py   (routine_prepare.sh 가 호출)
"""
from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

BATCH = Path("/tmp/sise_batch")
MAX_H, OVERLAP = 2300, 150      # vision_api_reader._image_blocks 와 동일


def _log(msg: str) -> None:
    print(f"[session-prep] {msg}", file=sys.stderr, flush=True)


def _split(path: Path) -> list[str]:
    """세로로 긴 이미지를 원본 해상도 조각 파일로. 짧으면 원본 1장."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(path.read_bytes()))
        w, h = img.size
    except Exception as e:  # noqa: BLE001 — 손상/미지원은 원본 그대로
        _log(f"{path.name}: 열기 실패({e!r}) — 원본 그대로")
        return [str(path)]
    if h <= int(MAX_H * 1.15):
        return [str(path)]
    pieces, y, n = [], 0, 0
    while y < h:
        n += 1
        piece = img.crop((0, y, w, min(y + MAX_H, h))).convert("RGB")
        out = path.with_name(f"{path.stem}.p{n}.jpg")
        piece.save(out, format="JPEG", quality=88)
        pieces.append(str(out))
        if y + MAX_H >= h:
            break
        y += MAX_H - OVERLAP
    _log(f"{path.name}: 세로 {h}px → {len(pieces)}조각(원본 해상도)")
    return pieces


def _rules_md() -> str:
    from vision_api_reader import PROMPT, SCHEMA   # 판독 규칙·스키마의 단일 소스
    rules = PROMPT.replace(
        "게시글 텍스트 컨텍스트(시세표 보는 법 등):\n{context}",
        "게시글 텍스트 컨텍스트(시세표 보는 법 등)는 session_manifest.json 의 각 항목 `context` 에 있다.",
    )
    return f"""# 시세표 이미지 판독 규칙 (클로드 앱 루틴용)

아래 규칙은 API 판독기(vision_api_reader.PROMPT)와 **같은 원문**이다. 이미지 1장(=manifest 1항목)을
읽고 결과를 `/tmp/sise_batch/results/<idx>.json` 에 아래 스키마의 JSON 객체 **하나**로 저장한다.

- 항목의 `pieces` 가 2개 이상이면: 세로로 긴 시세표를 위→아래 순서로 나눈 조각이다. 열 헤더는 첫
  조각에 있고 조각 경계에 겹침(150px)이 있으니 **중복 행은 한 번만** 추출한다.
- 시세표가 아니면(매장 사진·행사 포스터·조건 안내문) `{{"is_price_table": false, "board_date": null, "rows": []}}`.
- 값 단위·부호·열 대응·제외 대상 등은 아래 규칙을 그대로 따른다. 임의 추정 금지, 확신 없는 셀은 confidence 0.5 미만.

---

{rules}

---

## 결과 JSON 스키마 (반드시 준수 — 키 누락/추가 금지, nullable 은 null)

```json
{json.dumps(SCHEMA, ensure_ascii=False, indent=2)}
```

예시:
```json
{{"is_price_table": true, "board_date": "2026-09-07",
 "rows": [{{"model_name": "Galaxy Z Fold 8", "storage_gb": 256, "carrier": "SKT", "subscription_type": "MNP",
           "contract_type": "공시", "cash_price": 230000, "plan_name": "5GX프라임", "plan_fee": 89000,
           "estimated": false, "add_condition": null, "confidence": 0.9}}]}}
```
"""


def main() -> int:
    mf = BATCH / "manifest.json"
    if not mf.exists():
        _log("manifest.json 없음 — 다운로드된 이미지가 없다")
        (BATCH / "session_manifest.json").write_text("[]", encoding="utf-8")
        return 0
    manifest = json.loads(mf.read_text(encoding="utf-8"))

    results = BATCH / "results"
    shutil.rmtree(results, ignore_errors=True)   # 이전 실행 결과가 섞이지 않게 비운다
    results.mkdir(parents=True)

    session = []
    for idx, e in enumerate(manifest):
        session.append({
            "idx": idx,
            "handle": e.get("handle"),
            "name": e.get("name"),
            "region": e.get("region"),
            "title": e.get("title"),
            "context": (e.get("context") or "")[:600],
            "pieces": _split(Path(e["file"])),
        })
    (BATCH / "session_manifest.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")
    (BATCH / "RULES.md").write_text(_rules_md(), encoding="utf-8")
    n_pieces = sum(len(s["pieces"]) for s in session)
    _log(f"판독 준비 완료: 이미지 {len(session)}장({n_pieces}조각) → session_manifest.json, RULES.md, results/")
    print(f"READY images={len(session)} pieces={n_pieces}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
