"""웹 프로그램 실행: 브라우저에서 영상 두 개를 올리면 분석 결과를 보여줍니다.

    python web.py            # 이 컴퓨터에서만 접속 (http://127.0.0.1:8000)
    python web.py --lan      # 같은 와이파이의 휴대폰에서도 접속 가능
    python web.py --public   # 외부 어디서나 접속 가능한 https 주소(Cloudflare 터널) + 접속 코드
"""
import argparse
import atexit
import importlib.util
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LABELS = {'claude': 'Claude (Anthropic)', 'openai': 'OpenAI'}


def load_settings(root=ROOT):
    """settings.py is optional; environment variables are the fallback."""
    values = {}
    path = root / 'settings.py'
    if path.is_file():
        spec = importlib.util.spec_from_file_location('devday_user_settings', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        values = {k: getattr(module, k) for k in dir(module) if k.isupper()}
    kind = (values.get('PROVIDER') or os.environ.get('DEVDAY_PROVIDER') or 'claude').strip().lower()
    if kind not in LABELS:
        raise ValueError('settings.py의 PROVIDER는 "claude" 또는 "openai"여야 합니다.')
    key_name = 'ANTHROPIC_API_KEY' if kind == 'claude' else 'OPENAI_API_KEY'
    key = (values.get(key_name) or os.environ.get(key_name) or '').strip()
    if key: os.environ[key_name] = key
    model = values.get('CLAUDE_MODEL') if kind == 'claude' else values.get('MODEL')
    from devday.providers import AnthropicProvider
    model = model or (os.environ.get('DEVDAY_CLAUDE_MODEL', AnthropicProvider.DEFAULT_MODEL) if kind == 'claude' else os.environ.get('DEVDAY_MODEL', 'gpt-6-luna'))
    code = str(values.get('ACCESS_CODE') or os.environ.get('DEVDAY_ACCESS_CODE') or '').strip()
    return kind, model, key_name, bool(key), code


TUNNEL_URL = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')


def find_cloudflared():
    for candidate in (shutil.which('cloudflared'), '/opt/homebrew/bin/cloudflared', '/usr/local/bin/cloudflared'):
        if candidate and os.path.isfile(candidate):
            return candidate
    raise ValueError('외부 주소를 만들려면 cloudflared가 필요해요. 터미널에서 한 번만 실행하세요:  brew install cloudflared\n'
                     '(Homebrew가 없다면 https://brew.sh 의 설치 명령을 먼저 실행하세요.)')


def start_tunnel(port, timeout=60, command=None):
    """Cloudflare quick tunnel: no account, random https://*.trycloudflare.com URL, valid while running."""
    cmd = command or [find_cloudflared(), 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{port}']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    lines = queue.Queue()

    def drain():  # keep reading so the pipe never fills up
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=drain, daemon=True).start()
    deadline = time.time() + timeout; tail = []
    while time.time() < deadline:
        try:
            line = lines.get(timeout=.5)
        except queue.Empty:
            continue
        if line is None:
            break
        tail = (tail + [line.strip()])[-8:]
        match = TUNNEL_URL.search(line)
        if match:
            return proc, match.group(0)
    proc.terminate()
    raise ValueError('외부 주소를 만들지 못했어요. 인터넷 연결을 확인하고 다시 실행하세요.\n' + '\n'.join(tail))


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1)); return s.getsockname()[0]
    except OSError:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='흔들림 비교 진단 웹 프로그램')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--lan', action='store_true', help='같은 네트워크의 다른 기기(휴대폰)에서 접속 허용')
    parser.add_argument('--public', action='store_true', help='외부 어디서나 접속 가능한 https 주소 만들기 (접속 코드 필수)')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    kind, model, key_name, has_key, code = load_settings()
    if args.public and not code:
        code = f'{secrets.randbelow(10**6):06d}'  # one per run unless ACCESS_CODE is set in settings.py
    from devday.providers import make_provider
    from devday.web.app import create_app
    import uvicorn
    info = {'provider': kind, 'provider_label': LABELS[kind], 'model': model, 'key_missing': not has_key,
            'max_request_mb': 95 if args.public else None}  # Cloudflare free tunnels cap one upload at 100MB
    data_root = Path(os.environ.get('DEVDAY_DATA_DIR', ROOT)).expanduser()
    app = create_app(data_root, lambda: make_provider(kind, model), info, access_code=code or None)
    host = '0.0.0.0' if args.lan or os.environ.get('PORT') else '127.0.0.1'
    url = f'http://127.0.0.1:{args.port}'
    print(f'AI: {LABELS[kind]} · 모델 {model}')
    if not has_key:
        print(f'[주의] {key_name}가 비어 있어요. settings.py에 키를 넣어야 분석할 수 있어요.')
    print(f'브라우저에서 열기: {url}')
    if args.lan and lan_ip():
        print(f'휴대폰(같은 와이파이)에서 열기: http://{lan_ip()}:{args.port}  · 암호가 없으니 믿을 수 있는 네트워크에서만 쓰세요.')
    if code and not args.public:
        print(f'접속 코드: {code}')
    if args.public:
        proc, public = start_tunnel(args.port)
        print('\n' + '=' * 60)
        print(f'  외부 접속 주소:  {public}')
        print(f'  접속 코드:       {code}')
        print('  이 창과 Mac이 켜져 있는 동안만 접속돼요. 주소는 실행할 때마다 바뀌어요.')
        print('=' * 60 + '\n')
        (ROOT / 'public_url.txt').write_text(f'{public}\n접속 코드: {code}\n', encoding='utf-8')
    print('끝내려면 이 창에서 Ctrl+C를 누르세요.')
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url, new=2)).start()
    uvicorn.run(app, host=host, port=args.port, log_level='warning')


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        print('실행 중단:', exc); raise SystemExit(1)
