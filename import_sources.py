"""
주요 성지점 온라인 URL 엑셀 → seongji_sources.json 좌표 마스터 생성기.

사용법:
    python3 import_sources.py "/path/to/주요 성지점 온라인 URL 운영 현황_YYYYMMDD.xlsx"

- 첫 번째 시트(최신 스냅샷)의 (본부, 지역, 판매점명, URL) 컬럼을 파싱
- pf.kakao.com 핸들을 추출해 type='kakao' 로, 그 외(youtube/naver 등)는 type 별로 기록
- 출력: seongji_sources.json  [{handle, name, region, hq, url, type}]
- 엑셀이 갱신되면 재실행해서 덮어쓰면 됨 (openpyxl 불필요 — 표준 라이브러리만 사용)
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

OUT_PATH = Path(__file__).parent / "seongji_sources.json"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
T_TAG = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
KAKAO_RE = re.compile(r"https://pf\.kakao\.com/(_[A-Za-z]+)")


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(t.text or "" for t in si.iter(T_TAG))
        for si in root.findall("m:si", NS)
    ]


def _cell_value(c: ET.Element, ss: list[str]) -> str:
    v = c.find("m:v", NS)
    if v is None or v.text is None:
        return ""
    if c.get("t") == "s":
        return ss[int(v.text)]
    return v.text


def parse_xlsx(path: Path) -> list[dict]:
    z = zipfile.ZipFile(path)
    ss = _shared_strings(z)
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))  # 첫 시트 = 최신 스냅샷
    sources: list[dict] = []
    seen_urls: set[str] = set()

    for row in sheet.find("m:sheetData", NS).findall("m:row", NS):
        vals = [_cell_value(c, ss) for c in row.findall("m:c", NS)]
        url = next((v for v in vals if isinstance(v, str) and v.startswith("http")), None)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        idx = vals.index(url)
        # 컬럼 구조: 순번 | 본부 | 지역 | 판매점명 | URL
        name = vals[idx - 1].strip() if idx >= 1 else ""
        region = vals[idx - 2].strip() if idx >= 2 else ""
        hq = vals[idx - 3].strip() if idx >= 3 else ""

        m = KAKAO_RE.match(url)
        if m:
            src_type, handle = "kakao", m.group(1)
        else:
            host = urlparse(url).netloc
            src_type = (
                "youtube" if "youtu" in host
                else "naver_cafe" if "cafe.naver" in host
                else "naver" if "naver" in host
                else "other"
            )
            handle = None
        sources.append({
            "handle": handle,
            "name": name,
            "region": region,
            "hq": hq,
            "url": url,
            "type": src_type,
        })

    # 카카오 핸들 중복 제거 (같은 채널의 게시글/홈 링크 병존)
    deduped: list[dict] = []
    seen_handles: set[str] = set()
    for s in sources:
        if s["type"] == "kakao":
            if s["handle"] in seen_handles:
                continue
            seen_handles.add(s["handle"])
        deduped.append(s)
    return deduped


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    path = Path(sys.argv[1])
    sources = parse_xlsx(path)
    OUT_PATH.write_text(
        json.dumps(sources, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    kakao = sum(1 for s in sources if s["type"] == "kakao")
    print(f"wrote {OUT_PATH}  (총 {len(sources)}개, kakao {kakao}개, "
          f"기타 {len(sources) - kakao}개)")


if __name__ == "__main__":
    main()
