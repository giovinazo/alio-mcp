#!/bin/bash
# alio_core.py를 alio-mcp(정본) → alio-crawler로 동기화
#
# 사용법:
#   cd 07_프로그램/alio-mcp
#   ./sync_to_crawler.sh
#
# 양쪽이 NAS의 형제 폴더에 있다고 가정한다.
# 다른 위치라면 CRAWLER_DIR 환경변수로 오버라이드:
#   CRAWLER_DIR=/path/to/alio-crawler ./sync_to_crawler.sh

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SRC="$SCRIPT_DIR/alio_core.py"
CRAWLER_DIR="${CRAWLER_DIR:-$SCRIPT_DIR/../1. 알리오 크롤러}"
DEST="$CRAWLER_DIR/alio_core.py"

if [ ! -f "$SRC" ]; then
    echo "ERROR: 정본 파일 없음: $SRC" >&2
    exit 1
fi

if [ ! -d "$CRAWLER_DIR" ]; then
    echo "ERROR: 크롤러 폴더 없음: $CRAWLER_DIR" >&2
    echo "  CRAWLER_DIR 환경변수로 경로 지정 가능" >&2
    exit 1
fi

if [ -f "$DEST" ] && cmp -s "$SRC" "$DEST"; then
    echo "[sync] 변경 없음 — 양쪽 alio_core.py 이미 동일"
    exit 0
fi

cp "$SRC" "$DEST"
SIZE=$(wc -l < "$DEST" | tr -d ' ')
echo "[sync] 완료: $SRC"
echo "       → $DEST"
echo "       ${SIZE} 라인"
