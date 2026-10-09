"""Load user settings and run the live API -> measurement -> API pipeline."""
import importlib.util
import math
import os
import webbrowser
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from devday import run_pipeline

ROOT = Path(__file__).resolve().parent

def load_settings(root=ROOT):
    path = root / 'settings.py'
    if not path.is_file():
        raise ValueError('settings.example.py를 settings.py로 복사하고 입력을 설정하세요.')
    spec = importlib.util.spec_from_file_location('devday_user_settings', path)
    settings = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(settings)
    key = settings.OPENAI_API_KEY.strip()
    if not key:
        raise ValueError('settings.py의 OPENAI_API_KEY에 API 키를 넣으세요.')
    paths = []
    for name in ['NORMAL_VIDEO', 'CANDIDATE_VIDEO']:
        value = getattr(settings, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'settings.py의 {name}에 영상 경로를 넣으세요.')
        path = Path(value).expanduser()
        if not path.is_absolute(): path = root / path
        path = path.resolve()
        if not path.is_file(): raise ValueError(f'{name}: 영상을 찾을 수 없습니다: {path}')
        paths.append(path)
    if paths[0].samefile(paths[1]):
        raise ValueError('정상 영상과 대상 영상은 서로 다른 파일로 지정하세요.')
    fps = settings.CAPTURE_FPS
    fps2 = settings.CANDIDATE_CAPTURE_FPS
    for value in [fps, fps if fps2 is None else fps2]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('settings.py에 실제 촬영 FPS를 양수로 입력하세요.')
    return settings, key, paths

def main(root=ROOT):
    settings, key, paths = load_settings(root)
    os.environ['OPENAI_API_KEY'] = key
    output = root / 'results' / ('analysis-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
    print('실제 영상 분석을 시작합니다. 대표 이미지와 측정값이 OpenAI API로 전송됩니다.', flush=True)
    def progress(event):
        print(f"[{event['stage']}] {event['status']}", flush=True)
    result = run_pipeline(paths[0], paths[1], capture_fps=settings.CAPTURE_FPS,
        candidate_capture_fps=settings.CANDIDATE_CAPTURE_FPS, output_dir=output,
        model=settings.MODEL, conditions={'fixed_camera_confirmed':bool(settings.FIXED_CAMERA),
        'same_setup_declared':True},
        on_progress=progress)
    print('완료. 진단서:', result['report_html'])
    try:
        opened = webbrowser.open(Path(result['report_html']).resolve().as_uri(), new=2)
        if not opened:
            print('브라우저를 자동으로 열지 못했습니다. 위 경로의 진단서를 직접 열어주세요.')
    except Exception:
        print('브라우저를 자동으로 열지 못했습니다. 분석 결과는 저장되어 있습니다. 위 경로의 진단서를 직접 열어주세요.')
    return result

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never print the user's key or a traceback containing settings values.
        message = str(exc)
        key = os.environ.get('OPENAI_API_KEY', '')
        if key: message = message.replace(key, '[API 키 숨김]')
        print('실행 중단:', message)
        raise SystemExit(1)
