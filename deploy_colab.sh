#!/usr/bin/env bash
# 사내 Polaris Colab 배포 — user-apps 저장소로 '슬림 배포 트리'를 push 한다.
#
# 배경(2026-09-08): Colab 은 GitLab `CDS/orbit/colab/user-apps/<앱>` 저장소를 소스로 빌드한다
# (포털에서 앱을 만들면 저장소가 자동 생성됨). 그 그룹은 push 규칙이 엄격하다:
#   · 커밋 author 이메일이 팀 멤버여야 함(taeholee@sk.com — gmail 은 거부)
#   · main 보호(force/shallow push 불가), 파일 10MiB 제한
# 그래서 main 을 그대로 push 하지 않고, 컨테이너에 필요 없는 큰 파일(판독 원본 JSON 11MiB 등)을
# 뺀 트리를 별도 커밋으로 만들어(author 고정) 로컬 브랜치 `colab-deploy` 에 쌓고 원격 main 으로 보낸다.
#
# 사용:
#   bash deploy_colab.sh --repo https://gitlab.tde.sktelecom.com/CDS/orbit/colab/user-apps/<앱>.git   # 최초 1회(원격 등록)
#   bash deploy_colab.sh ["커밋 메시지"]        # 이후: 현재 main 트리를 배포 커밋으로 push
#   bash deploy_colab.sh --dry-run              # push 없이 트리 구성·크기만 확인
# push 뒤에는 포털(polaris-colab.sktelecom.com)에서 '배포' 버튼 → cicd-builder 파이프라인이 빌드·배포한다.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

AUTHOR_NAME="${COLAB_AUTHOR_NAME:-이태호}"
AUTHOR_EMAIL="${COLAB_AUTHOR_EMAIL:-taeholee@sk.com}"
BRANCH="colab-deploy"
REMOTE="colab"
# 컨테이너(git 미러/PG 모드)에 필요 없는 항목 — 판독 원본(11MiB, 10MiB 규칙 위반), GitHub 워크플로, 반출 팩
EXCLUDE=(seongji_vision_data.json .github dist)

DRY=0; MSG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) shift; git remote get-url "$REMOTE" >/dev/null 2>&1 && git remote set-url "$REMOTE" "$1" || git remote add "$REMOTE" "$1"; echo "[colab] 원격 $REMOTE = $(git remote get-url "$REMOTE" | sed -E 's#//[^@]+@#//***@#')"; shift ;;
    --dry-run) DRY=1; shift ;;
    *) MSG="$1"; shift ;;
  esac
done
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "[colab] 원격 '$REMOTE' 가 없습니다. 포털에서 앱을 만든 뒤: bash deploy_colab.sh --repo <user-apps 저장소 URL>" >&2
  [ "$DRY" = 1 ] || exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "[colab] 작업 트리에 커밋되지 않은 변경이 있습니다 — 먼저 main 에 커밋하세요." >&2
  exit 1
fi

SRC="$(git rev-parse HEAD)"
# 1) 임시 인덱스에 HEAD 트리를 읽고 제외 항목을 뺀 트리 오브젝트 생성(작업 트리·현재 인덱스는 건드리지 않음)
TMP_INDEX="$(mktemp)"; trap 'rm -f "$TMP_INDEX"' EXIT
export GIT_INDEX_FILE="$TMP_INDEX"
git read-tree "$SRC"
for p in "${EXCLUDE[@]}"; do git rm -r -q --cached --ignore-unmatch "$p" >/dev/null 2>&1 || true; done
TREE="$(git write-tree)"
unset GIT_INDEX_FILE

# 2) 부모 결정: 원격 main(있으면) 을 우선 — 다른 곳에서 올린 커밋 위에 쌓아 non-FF 거부를 피한다
REMOTE_HEAD=""
if git remote get-url "$REMOTE" >/dev/null 2>&1 && git fetch -q "$REMOTE" main 2>/dev/null; then
  REMOTE_HEAD="$(git rev-parse FETCH_HEAD)"
fi
LOCAL="$(git rev-parse -q --verify "refs/heads/$BRANCH" 2>/dev/null || true)"
PARENT="$LOCAL"
if [ -n "$REMOTE_HEAD" ] && { [ -z "$LOCAL" ] || ! git merge-base --is-ancestor "$REMOTE_HEAD" "$LOCAL"; }; then
  PARENT="$REMOTE_HEAD"
fi
if [ -n "$PARENT" ] && [ "$(git rev-parse "$PARENT^{tree}")" = "$TREE" ]; then
  echo "[colab] 배포 트리가 원격과 같습니다 — 변경 없음."; exit 0
fi

# 3) 배포 커밋(author 고정) → 로컬 이력 브랜치 갱신
MSG="colab 배포 $(date +%F) ← main ${SRC:0:7}: ${MSG:-$(git log -1 --pretty=%s "$SRC")}"
COMMIT="$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
          GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
          git commit-tree "$TREE" ${PARENT:+-p "$PARENT"} -m "$MSG")"
git update-ref "refs/heads/$BRANCH" "$COMMIT"

# 4) 요약(10MiB 규칙 사전 점검)
FILES="$(git ls-tree -r -l "$TREE")"
echo "[colab] 배포 커밋 ${COMMIT:0:7} (author $AUTHOR_EMAIL) · 파일 $(printf '%s\n' "$FILES" | wc -l | tr -d ' ')개 · 합계 $(printf '%s\n' "$FILES" | awk '{s+=$4} END{printf "%.1fMiB", s/1048576}')"
printf '%s\n' "$FILES" | awk '$4>10*1048576{printf "  !! 10MiB 초과: %.1fMiB %s\n",$4/1048576,$5; bad=1} END{exit bad}' || { echo "[colab] 10MiB 초과 파일이 있어 push 가 거부됩니다 — EXCLUDE 에 추가하세요." >&2; exit 1; }
printf '%s\n' "$FILES" | sort -k4 -n -r | head -3 | awk '{printf "  큰 파일: %.1fMiB %s\n",$4/1048576,$5}'
if [ "$DRY" = 1 ]; then echo "[colab] dry-run — push 생략 (로컬 브랜치 $BRANCH 만 갱신)"; exit 0; fi

# 5) push (보호 브랜치: force 없이 FF 만)
git push "$REMOTE" "refs/heads/$BRANCH:refs/heads/main" 2>&1 | sed -E 's#//[^@]+@#//***@#'
echo "[colab] push 완료 → 포털(polaris-colab.sktelecom.com)에서 '배포' 버튼을 눌러 cicd-builder 파이프라인을 실행하세요."
