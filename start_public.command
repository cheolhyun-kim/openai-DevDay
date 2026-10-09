#!/bin/zsh
# 외부에서 접속 가능한 https 주소를 만듭니다. 이 창과 Mac이 켜져 있는 동안만 열려 있어요.
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'setup.command를 먼저 실행하세요.'
  exit 1
fi
# caffeinate: 분석·접속 중 Mac이 잠들지 않게 합니다.
caffeinate -dims .venv/bin/python web.py --public "$@"
