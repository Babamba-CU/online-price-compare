"""
일일 수집 — 한 Opus 세션에 끝나는 효율화 버전.

이 스크립트는 '결정적(deterministic)' 단계만 자동 수행한다:
  1) 카카오/네이버 텍스트 수집 (빠름, 네트워크)
  2) 새 시세표 이미지 다운로드 (스킵리스트 제외, 회당 상한 --max-images)
이후 'Vision 이미지 판독'은 Claude 세션이 manifest 를 읽어 수행하고(한 번에 끝나게 상한),
  3) 판독 결과 병합 → 4) 빌드 는 merge_and_build() 로 마무리한다.

5am 루틴 프롬프트가 이 모듈을 호출한다:
  python3 daily_collect.py prepare   # 1)+2) 까지 (수집·다운로드)
  → (Claude 가 /tmp/sise_batch 의 새 이미지를 상한 내에서 판독, 핸들별 성공/실패 기록)
  python3 daily_collect.py finalize  # 3)+4) (병합·빌드) — Claude 가 결과 JSON 경로 전달

효율화 핵심:
  · 회당 이미지 상한(DEFAULT_CAP) → 분석이 한 세션에 끝남
  · 분석 2회+ 실패 채널은 vision_skiplist 가 자동 제외
  · 부분 완료 허용(오늘 못한 채널은 다음날) → '한 번에 완료 불가' 문제 해소
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
SKILL_VENV = Path("/Users/taeholee/Documents/대시보드/.claude/skills/community-search/.venv/bin/python")
PY = str(SKILL_VENV) if SKILL_VENV.exists() else sys.executable

DEFAULT_CAP = 40          # 회당 신규 이미지 상한 (한 Opus 세션 안전선)
PER_CHANNEL = 1
RECENT_DAYS = 30


def _run(args: list[str]) -> int:
    print(f"$ {' '.join(args)}", file=sys.stderr, flush=True)
    return subprocess.call(args)


def prepare(cap: int = DEFAULT_CAP) -> None:
    """텍스트 수집 + 신규 시세표 이미지 다운로드(상한·스킵리스트 적용)."""
    # 1) 텍스트 수집 (카카오/네이버) — 실패해도 계속
    _run([PY, str(BASE / "seongji_kakao.py")])
    _run([PY, str(BASE / "seongji_naver.py")])
    # 2) 신규 이미지 다운로드 (회당 상한)
    _run([PY, str(BASE / "seongji_vision_batch.py"),
          "--channels", "342", "--per-channel", str(PER_CHANNEL),
          "--recent-days", str(RECENT_DAYS), "--max-images", str(cap)])
    print("[daily] prepare 완료 — /tmp/sise_batch/manifest.json 판독 준비됨", file=sys.stderr)


def merge_and_build() -> None:
    """판독 결과는 Claude 가 seongji_vision_data.json 에 이미 병합했다는 전제.

    수집기(카카오/네이버/사이트 크롤러)는 requests·bs4 의존이라 venv 서브프로세스로
    실행(_run) — in-process import 는 시스템 python 에 requests 가 없으면 통째로
    스킵되는 문제가 있었음(2026-07-08 실측). seed/vision/build 는 stdlib 만 사용.
    """
    import seongji_db, seed_sample, seongji_vision_load, seongji_build
    seongji_db.DB_PATH.unlink(missing_ok=True)
    seed_sample.seed()
    _run([PY, str(BASE / "seongji_kakao.py")])                       # 카카오 텍스트
    _run([PY, str(BASE / "seongji_naver.py")])                       # 네이버 검색(키 없으면 skip)
    _run([PY, str(BASE / "seongji_crawler.py"), "--max-pages", "2"])  # 사이트(뽐뿌 등)
    seongji_vision_load.load()
    seongji_build.main()
    print("[daily] finalize(merge_and_build) 완료", file=sys.stderr)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if cmd == "prepare":
        prepare(int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_CAP)
    elif cmd == "finalize":
        merge_and_build()
    else:
        print("usage: daily_collect.py [prepare [cap] | finalize]", file=sys.stderr)
