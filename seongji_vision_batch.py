"""
시세표 이미지 수동 배치 다운로더 (로컬 전용 — 배포 이미지 제외).

카카오 채널 게시글(+내부 교차링크 1-hop)의 시세표 이미지를 내려받아
Claude Code 세션이 판독할 수 있도록 /tmp/sise_batch/ 에 모은다.

  - 가격 키워드(시세/특가/가격/단가/대란/좌표) 게시글의 이미지만 대상
  - 이미 판독된 이미지(seongji_vision_data.json 의 image_url)는 제외
  - 텍스트 파싱으로 가격이 안 나온 채널(이미지-only) 우선
  - 채널당 최대 2장, 전체 상한 MAX_IMAGES

사용법:
  python3 seongji_vision_batch.py [--channels 30] [--max-images 60]
출력:
  /tmp/sise_batch/manifest.json + 이미지 파일들
판독 후:
  Claude 세션이 manifest 를 순회·판독해 seongji_vision_data.json 에 누적.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
SOURCES_PATH = BASE / "seongji_sources.json"
VISION_DATA_PATH = BASE / "seongji_vision_data.json"
DB_PATH = BASE / "seongji_prices.db"
OUT_DIR = Path("/tmp/sise_batch")

POSTS_URL = "https://pf.kakao.com/rocket-web/web/profiles/{handle}/posts"
POST_URL = "https://pf.kakao.com/rocket-web/web/profiles/{handle}/posts/{post_id}"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
INTERNAL_LINK_RE = re.compile(r"https?://pf\.kakao\.com/(_[A-Za-z]+)/(\d+)")
PRICE_KEYWORDS = ("시세", "특가", "가격", "단가", "대란", "좌표", "현완", "성지")
RECENT_DAYS = 14
SLEEP = 0.5


def _get(url: str):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=15))


def _text(item: dict) -> str:
    return "\n".join(c.get("v", "") for c in item.get("contents", []) if c.get("t") == "text")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", type=int, default=30)
    ap.add_argument("--max-images", type=int, default=60)
    ap.add_argument("--per-channel", type=int, default=2)
    args = ap.parse_args()

    sources = [s for s in json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
               if s.get("type") == "kakao" and s.get("handle")]

    # 이미 판독된 이미지 URL
    done_urls: set[str] = set()
    if VISION_DATA_PATH.exists():
        vd = json.loads(VISION_DATA_PATH.read_text(encoding="utf-8"))
        done_urls = {it.get("image_url") for it in vd.get("items", [])}

    # 텍스트 파싱으로 이미 가격이 나온 채널(판매점명 기준)은 후순위
    text_ok_authors: set[str] = set()
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        text_ok_authors = {r[0] for r in conn.execute(
            "SELECT DISTINCT po.author FROM seongji_prices p "
            "JOIN seongji_posts po ON po.id=p.post_id WHERE po.source='kakao'")}
        conn.close()
    sources.sort(key=lambda s: (s.get("name") or "") in text_ok_authors)

    cutoff_ms = (datetime.now() - timedelta(days=RECENT_DAYS)).timestamp() * 1000
    OUT_DIR.mkdir(exist_ok=True)
    manifest: list[dict] = []
    n_img = 0

    for s in sources[: args.channels]:
        if n_img >= args.max_images:
            break
        handle, name, region = s["handle"], s.get("name") or "", s.get("region") or ""
        try:
            items = _get(POSTS_URL.format(handle=handle)).get("items", [])
        except Exception as e:  # noqa: BLE001
            print(f"  [skip] {handle}: {e!r}", file=sys.stderr)
            time.sleep(SLEEP)
            continue

        # 교차링크 게시글도 후보에 포함
        linked: list[dict] = []
        for it in items:
            if (it.get("published_at") or 0) < cutoff_ms:
                continue
            for c in it.get("contents", []):
                if c.get("t") != "link":
                    continue
                m = INTERNAL_LINK_RE.match(c.get("v") or "")
                if m and len(linked) < 2:
                    try:
                        linked.append(_get(POST_URL.format(handle=m.group(1), post_id=m.group(2))))
                        time.sleep(SLEEP)
                    except Exception:  # noqa: BLE001
                        pass

        per_channel = 0
        for it in sorted(items + linked, key=lambda x: -(x.get("published_at") or 0)):
            if per_channel >= args.per_channel or n_img >= args.max_images:
                break
            blob = (it.get("title") or "") + " " + _text(it)[:200]
            if not it.get("media") or not any(k in blob for k in PRICE_KEYWORDS):
                continue
            for m in it["media"][:2]:
                if per_channel >= args.per_channel or n_img >= args.max_images:
                    break
                url = (m.get("xlarge_url") or m.get("url") or "").replace("http://", "https://")
                if not url or url in done_urls:
                    continue
                fn = OUT_DIR / f"{handle}_{it.get('id')}_{m.get('id')}.jpg"
                try:
                    urllib.request.urlretrieve(url, fn)
                except Exception as e:  # noqa: BLE001
                    print(f"  [img-fail] {url}: {e!r}", file=sys.stderr)
                    continue
                manifest.append({
                    "file": str(fn),
                    "handle": handle,
                    "post_id": it.get("id"),
                    "image_url": url,
                    "name": name,
                    "region": region,
                    "posted_at": datetime.fromtimestamp(
                        it["published_at"] / 1000).isoformat() if it.get("published_at") else None,
                    "title": (it.get("title") or "")[:80],
                    "context": _text(it)[:600],   # "보는 법" 해석 컨텍스트
                    "width": m.get("width"), "height": m.get("height"),
                })
                done_urls.add(url)
                n_img += 1
                per_channel += 1
        time.sleep(SLEEP)

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"downloaded {n_img} images from {len({m['handle'] for m in manifest})} channels "
          f"→ {OUT_DIR}/manifest.json")


if __name__ == "__main__":
    main()
