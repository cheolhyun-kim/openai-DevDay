#!/bin/zsh
set -eu
cd "$(dirname "$0")"
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10 이상이 필요합니다"'
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
if [[ ! -f settings.py ]]; then
  cp settings.example.py settings.py
fi
mkdir -p input
echo '설치 완료. settings.py의 ANTHROPIC_API_KEY에 키를 넣으세요.'
echo '그다음 start_web.command를 더블클릭하면 브라우저에서 프로그램이 열립니다.'
