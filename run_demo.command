#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'setup.command를 먼저 실행하세요.'
  exit 1
fi
.venv/bin/python run_analysis.py
