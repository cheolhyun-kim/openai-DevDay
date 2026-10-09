"""Local web app: upload two slow-motion videos -> API -> measurement -> API -> results page.

Jobs run one at a time in a background thread (the pipeline is CPU-heavy and uses matplotlib).
Every job lives in web_jobs/<id>/ with job.json (state, progress events) and run/ (pipeline output).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
from urllib.parse import quote
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..workflow import run_pipeline

STATIC = Path(__file__).parent / 'static'
VIDEO_EXT = {'.mov', '.mp4', '.m4v', '.avi'}
SERVE_EXT = {'.png', '.jpg', '.jpeg', '.json', '.html', '.txt'}
ARCHIVE_EXCLUDE = {'input', 'frames', 'frames.json'}
JOB_ID = re.compile(r'^\d{8}-\d{6}-[0-9a-f]{6}$')
EQUIPMENT_ID = re.compile(r'^eq_[0-9a-f]{16}$')
STAGES = [('frames', '대표 장면 추출'), ('roi_api', 'AI가 측정할 부위 고르기'), ('alignment', '두 영상 위치 맞추기'),
          ('measurement_reference', '정상 영상 흔들림 측정'), ('measurement_candidate', '점검 영상 흔들림 측정'),
          ('maps', '측정 지도 그리기'), ('diagnosis_api', 'AI 판정'), ('finished', '완료')]
METRIC_KO = {'reference_rms_px': '정상 영상 흔들림', 'candidate_rms_px': '점검 영상 흔들림', 'ratio': '흔들림 배율(점검/정상)',
             'z_score': '변화 크기(z)', 'reference_snr': '정상 영상 신호/배경잡음', 'candidate_snr': '점검 영상 신호/배경잡음',
             'snr_ratio': '신호/잡음 배율'}
FLAG_KO = {'increase': '증가 기준 충족', 'decrease': '감소', 'no_significant_change': '유의한 변화 없음',
           'below_noise_floor': '배경 잡음 수준', 'not_measurable': '측정 불가'}


def now():
    return datetime.now().isoformat(timespec='seconds')


class JobStore:
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def dir(self, job_id):
        if not JOB_ID.match(job_id or ''):
            raise HTTPException(404, '분석을 찾을 수 없어요.')
        d = self.root / job_id
        if not (d / 'job.json').is_file():
            raise HTTPException(404, '분석을 찾을 수 없어요.')
        return d

    def new_id(self):
        return datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]

    def read(self, job_id):
        return json.loads((self.root / job_id / 'job.json').read_text(encoding='utf-8'))

    def write(self, job_id, meta):
        path = self.root / job_id / 'job.json'; tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding='utf-8'); tmp.replace(path)

    def update(self, job_id, **changes):
        with self.lock:
            meta = self.read(job_id); meta.update(changes); self.write(job_id, meta); return meta

    def all(self):
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            if d.is_dir() and JOB_ID.match(d.name) and (d / 'job.json').is_file():
                try: out.append(self.read(d.name))
                except (OSError, ValueError): pass
        return out


def probe(path, capture_fps=240.0):
    """Cheap upload check: decodable, size, nominal fps; warn when it is not a slow-motion original.

    A low-fps file is still analysed: every decoded frame is treated as one capture frame at the
    entered capture FPS (correct for slow motion exported at playback speed, wrong for real-time video).
    """
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError('영상을 열 수 없어요. .mov 또는 .mp4 파일인지 확인해주세요.')
        ok, frame = cap.read()
        if not ok:
            raise ValueError('영상에서 장면을 읽을 수 없어요.')
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0); n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        h, w = frame.shape[:2]
    finally:
        cap.release()
    warnings = []
    if 0 < fps < 100:
        warnings.append(f'파일의 프레임 속도가 {fps:.0f}fps예요. {capture_fps:g}fps 슬로모션으로 간주해서 분석해요. '
                        '슬로모션을 내보내면서 프레임 속도가 바뀐 파일이면 흔들림 크기 비교는 대체로 맞지만 주파수는 20% 정도 어긋날 수 있어요. '
                        f'슬로모션이 아닌 일반 {fps:.0f}fps 촬영본이면 결과를 믿을 수 없어요. 가능하면 240fps 원본(에어드롭 "모든 사진 데이터")을 올려주세요.')
    return {'width': w, 'height': h, 'file_fps': round(fps, 2), 'frame_count': n}, warnings


def friendly_error(exc):
    text = f'{type(exc).__name__}: {exc}'
    for key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY'):
        value = os.environ.get(key)
        if value: text = text.replace(value, '[API 키 숨김]')
    hints = []
    low = text.lower()
    if 'api_key' in low or 'authentication' in low or '401' in low:
        hints.append('API 키를 확인해주세요 (settings.py — Claude는 ANTHROPIC_API_KEY, GPT는 OPENAI_API_KEY).')
    if 'background' in low:
        hints.append('배경(벽, 바닥, 콘센트 등)이 잘 보이게, 삼각대로 고정해서 다시 찍어주세요.')
    if 'credit' in low or 'billing' in low or '402' in low:
        hints.append('API 크레딧이 남아 있는지 Claude Console에서 확인해주세요.')
    return {'message': text[:1500], 'hints': hints}


def build_results(run_dir):
    """Flatten pipeline outputs into one payload for the results page."""
    run = Path(run_dir)
    load = lambda name: json.loads((run / name).read_text(encoding='utf-8')) if (run / name).is_file() else None
    evidence = load('evidence.json') or {}; decision = load('decision.json'); diagnosis = load('diagnosis.json')
    result = load('result.json') or {}; alignment = load('alignment.json') or {}
    names = {r['id']: r['part_name'] for r in evidence.get('roi_plan', {}).get('regions', [])}
    rows = {r['evidence_id']: r for r in evidence.get('region_comparisons', []) + evidence.get('relative_comparisons', [])}

    def label(row):
        if row is None: return ''
        if 'roi_id' in row: return names.get(row['roi_id'], row['roi_id'])
        return f"{names.get(row['a'], row['a'])} ↔ {names.get(row['b'], row['b'])} (상대 움직임)"

    def band(row):
        lo, hi = row['band_hz']; return f'{lo:g}–{hi:g} Hz'

    cited = []
    for c in (decision or {}).get('cited_numbers', []):
        row = rows.get(c['evidence_id'])
        cited.append({'part': label(row) or c['evidence_id'], 'band': band(row) if row else '', 'metric': METRIC_KO.get(c['metric'], c['metric']),
                      'unit': 'px' if c['metric'].endswith('_px') else '', 'value': c['value'], 'flag': FLAG_KO.get((row or {}).get('rule_flag'), '')})
    table = []
    for row in evidence.get('region_comparisons', []) + evidence.get('relative_comparisons', []):
        table.append({'id': row['evidence_id'], 'part': label(row), 'band': band(row), 'reference_px': row.get('reference_rms_px'),
                      'candidate_px': row.get('candidate_rms_px'), 'ratio': row.get('ratio'), 'z': row.get('z_score'),
                      'reference_snr': row.get('reference_snr'), 'candidate_snr': row.get('candidate_snr'),
                      'flag': row.get('rule_flag'), 'flag_ko': FLAG_KO.get(row.get('rule_flag'), ''), 'used': row.get('eligible_for_interpretation'),
                      'dominant_hz': [row.get('reference_dominant_hz'), row.get('candidate_dominant_hz')],
                      'points': [row.get('reference_points'), row.get('candidate_points')]})
    candidates = []
    for i, item in enumerate((diagnosis or {}).get('inspection_candidates', [])[:5], 1):
        candidates.append({'number': i, 'part': names.get(item['roi_id'], item['roi_id']), 'confidence': item['confidence'],
                           'reason': item['reason'], 'action': item['action'], 'alternatives': item.get('alternative_explanations', [])})
    measurements = evidence.get('measurements', {})
    tracking = {side: {k: (m.get('metadata') or {}).get(k) for k in ('decoded_frames', 'timestamp_resampled', 'missing_frame_fraction', 'timestamp_source', 'timestamp_warning')}
                for side, m in measurements.items()}
    reg = alignment.get('registration') or {}
    images = []
    for rel, title in [('visuals/inspection_roi.png', '확인해볼 위치'), ('measurements/reference/roi.png', '정상 영상 측정 부위'),
                       ('measurements/candidate/roi.png', '점검 영상 측정 부위')]:
        if (run / rel).is_file(): images.append({'path': rel, 'title': title})
    for p in sorted((run / 'visuals').glob('heatmap_*.png')):
        band_id = p.stem.replace('heatmap_', '')
        row = next((x for x in evidence.get('region_comparisons', []) if x['evidence_id'].endswith(':' + band_id)), {})
        hz = row.get('band_hz') or []
        label = '낮은 주파수' if band_id in ('low', 'lower') else '높은 주파수' if band_id in ('higher', 'high') else f'{band_id} 대역'
        span = f' · {hz[0]:g}–{hz[1]:g} Hz' if len(hz) == 2 else ''
        images.append({'path': f'visuals/{p.name}', 'title': f'흔들림 지도 · {label}{span}'})
    if (run / 'visuals/spectrum_comparison.png').is_file():
        images.append({'path': 'visuals/spectrum_comparison.png', 'title': '주파수별 흔들림 비교'})
    usage = [{'stage': c.get('stage'), 'model': c.get('model'), 'usage': c.get('usage')} for c in result.get('model_calls', [])]
    return {'decision': decision, 'summary': (diagnosis or {}).get('summary'), 'limitations': (diagnosis or {}).get('limitations', []),
            'recommended_validation': (diagnosis or {}).get('recommended_validation', []), 'cited': cited, 'table': table,
            'candidates': candidates, 'thresholds': (evidence.get('rule_prefilter') or {}).get('thresholds'),
            'background_floor': evidence.get('background_noise_floor_px'),
            'alignment': {k: reg.get(k) for k in ('ok', 'scale', 'rotation_deg', 'translation_px', 'inliers', 'inlier_fraction', 'reason')},
            'tracking': tracking, 'images': images, 'mode': result.get('mode'), 'model_calls': usage,
            'files': [f for f in ('report.html', 'evidence.json', 'diagnosis.json', 'decision.json') if (run / f).is_file()]}


LOGIN_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>접속 코드 · 흔들림 비교 진단</title><link rel="stylesheet" href="/static/style.css"></head><body><main style="max-width:440px">
<div class="eyebrow">흔들림 비교 진단</div><h1>접속 코드</h1><p class="lead">프로그램을 연 사람에게 받은 접속 코드를 입력하세요.</p>
<form method="post" action="/login" class="card"><input type="hidden" name="next" value="{next}">
<input name="code" type="password" autocomplete="current-password" autofocus required style="width:100%;padding:12px;font-size:17px;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--text)">
{error}<button class="primary" style="margin-top:14px;width:100%">들어가기</button></form></main></body></html>"""


def safe_next(value):
    return value if isinstance(value, str) and value.startswith('/') and not value.startswith('//') else '/'


def create_app(root, provider_factory, provider_info=None, max_upload_mb=2048, access_code=None):
    """provider_factory() -> a ModelProvider; called per job so a missing key fails that job, not the server.

    access_code: when set (always for a public URL), every page/API needs a cookie obtained at /login,
    so strangers who find the link cannot spend the API credits.
    """
    store = JobStore(Path(root) / 'web_jobs')
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='devday-job')
    app = FastAPI(title='DevDay 흔들림 진단', docs_url=None, redoc_url=None)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    if access_code:
        token = hmac.new(secrets.token_bytes(32), access_code.encode(), hashlib.sha256).hexdigest()

        @app.middleware('http')
        async def require_code(request: Request, call_next):
            path = request.url.path
            if path.startswith('/static/') or path == '/login' or hmac.compare_digest(request.cookies.get('devday_access', ''), token):
                return await call_next(request)
            if path.startswith('/api/'):
                return JSONResponse({'detail': '접속 코드가 필요해요. 페이지를 새로고침하세요.'}, status_code=401)
            return RedirectResponse('/login?next=' + quote(path), status_code=303)

        @app.get('/login', response_class=HTMLResponse)
        def login_page(next: str = '/'):
            return LOGIN_PAGE.format(next=quote(safe_next(next), safe='/'), error='')

        @app.post('/login')
        def login(request: Request, code: str = Form(''), next: str = Form('/')):
            if not hmac.compare_digest(code.strip().encode(), access_code.encode()):
                time.sleep(1.0)  # slow down guessing
                return HTMLResponse(LOGIN_PAGE.format(next=quote(safe_next(next), safe='/'), error='<div class="error">접속 코드가 맞지 않아요.</div>'), status_code=401)
            response = RedirectResponse(safe_next(next), status_code=303)
            secure = request.headers.get('x-forwarded-proto') == 'https' or request.url.scheme == 'https'
            response.set_cookie('devday_access', token, max_age=7 * 24 * 3600, httponly=True, samesite='lax', secure=secure)
            return response

    for meta in store.all():  # a server restart interrupts running/queued jobs
        if meta.get('state') in ('queued', 'running'):
            store.update(meta['id'], state='failed', error={'message': '서버가 다시 시작돼 분석이 중단됐어요. 다시 올려주세요.', 'hints': []})

    def run_job(job_id):
        meta = store.update(job_id, state='running', started_at=now())
        d = store.root / job_id

        def progress(event):
            with store.lock:
                m = store.read(job_id); m['events'].append({**event, 'at': now()}); m['stage'] = event['stage']; store.write(job_id, m)
        try:
            provider = provider_factory()
            if meta.get('equipment', {}).get('id'):
                equipment_path=equipment_dir(meta['equipment']['id'])
                equipment_meta=json.loads((equipment_path/'equipment.json').read_text(encoding='utf-8'))
                normal_name=Path(equipment_meta.get('normal',{}).get('stored','')).name
                normal_path=(equipment_path/normal_name).resolve()
                if equipment_path.resolve() not in normal_path.parents or not normal_path.is_file():
                    raise ValueError('The saved healthy reference video is missing.')
            else:
                normal_path = d / 'input' / meta['inputs']['normal']['stored']
            result = run_pipeline(normal_path, d / 'input' / meta['inputs']['candidate']['stored'],
                                  capture_fps=meta['capture_fps'], output_dir=d / 'run', provider=provider,
                                  conditions={'same_speed_confirmed': meta['same_speed'], 'fixed_camera_confirmed': meta['fixed_camera'], 'same_setup_declared': True,
                                              'input_warnings': meta.get('warnings', [])},
                                  on_progress=progress)
            store.update(job_id, state='done', finished_at=now(), decision=result.get('decision'), mode=result.get('mode'))
        except Exception as exc:  # report every failure on the page; never crash the server
            store.update(job_id, state='failed', finished_at=now(), error=friendly_error(exc))

    @app.get('/', response_class=HTMLResponse)
    def index():
        return (STATIC / 'index.html').read_text(encoding='utf-8')

    @app.get('/equipment', response_class=HTMLResponse)
    def equipment_page():
        return (STATIC / 'equipment.html').read_text(encoding='utf-8')

    @app.get('/diagnose', response_class=HTMLResponse)
    def diagnose_page():
        return (STATIC / 'diagnose.html').read_text(encoding='utf-8')

    @app.get('/history', response_class=HTMLResponse)
    def history_page():
        return (STATIC / 'history.html').read_text(encoding='utf-8')

    def equipment_dir(equipment_id):
        if not EQUIPMENT_ID.fullmatch(equipment_id or ''):
            raise HTTPException(404, 'Equipment was not found.')
        path=store.root/'equipment'/equipment_id
        if not (path/'equipment.json').is_file():
            raise HTTPException(404, 'Equipment was not found.')
        return path

    def save_video(upload,destination,limit_mb,capture_fps):
        suffix=Path(upload.filename or '').suffix.lower()
        if suffix not in VIDEO_EXT:
            raise HTTPException(400,'Upload a .mov, .mp4, .m4v, or .avi video.')
        size=0
        with open(destination,'wb') as output:
            while chunk:=upload.file.read(1<<20):
                size+=len(chunk)
                if size>limit_mb*1024*1024:
                    raise HTTPException(413,f'Video exceeds the {limit_mb} MB upload limit.')
                output.write(chunk)
        try:
            info,warnings=probe(destination,capture_fps)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc
        return {'filename':Path(upload.filename).name,'stored':Path(destination).name,'bytes':size,**info},warnings

    @app.get('/api/equipment')
    def list_equipment():
        root=store.root/'equipment'
        items=[]
        if root.is_dir():
            for path in sorted(root.glob('*/equipment.json'),reverse=True):
                try:
                    item=json.loads(path.read_text(encoding='utf-8'))
                    if EQUIPMENT_ID.fullmatch(item.get('id','')) and (path.parent/item.get('normal',{}).get('stored','')).is_file():items.append(item)
                except (OSError,ValueError,TypeError):
                    continue
        return items

    @app.post('/api/equipment')
    def register_equipment(name: str = Form(...), model: str = Form(''), normal: UploadFile = File(...), capture_fps: float = Form(240.0)):
        name=name.strip();model=model.strip()
        if not name or len(name)>80 or len(model)>120:
            raise HTTPException(400,'Enter an equipment name (up to 80 characters) and a model up to 120 characters.')
        if not (1<=capture_fps<=10000):
            raise HTTPException(400,'Capture FPS must be between 1 and 10000.')
        limit=min(max_upload_mb,int((provider_info or {}).get('max_request_mb') or max_upload_mb))
        equipment_id='eq_'+uuid.uuid4().hex[:16]
        folder=store.root/'equipment'/equipment_id;folder.mkdir(parents=True,exist_ok=False)
        try:
            suffix=Path(normal.filename or '').suffix.lower()
            reference,warnings=save_video(normal,folder/f'reference{suffix}',limit,capture_fps)
            item={'id':equipment_id,'name':name,'model':model,'capture_fps':capture_fps,'created_at':now(),'normal':reference,'warnings':warnings}
            (folder/'equipment.json').write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf-8')
            return item
        except Exception:
            shutil.rmtree(folder,ignore_errors=True)
            raise

    @app.post('/api/jobs/from-equipment')
    def create_equipment_job(equipment_id: str = Form(...), candidate: UploadFile = File(...),
                             same_speed: bool = Form(False), fixed_camera: bool = Form(False)):
        folder=equipment_dir(equipment_id)
        item=json.loads((folder/'equipment.json').read_text(encoding='utf-8'))
        normal=item.get('normal') or {}
        reference_name=Path(normal.get('stored','')).name
        reference_path=(folder/reference_name).resolve()
        if not reference_name or folder.resolve() not in reference_path.parents or not reference_path.is_file():
            raise HTTPException(400,'The saved healthy reference video is missing. Re-register this equipment.')
        capture_fps=float(item.get('capture_fps') or 240)
        if not (1<=capture_fps<=10000):
            raise HTTPException(400,'The registered capture FPS is invalid. Re-register this equipment.')
        limit=min(max_upload_mb,int((provider_info or {}).get('max_request_mb') or max_upload_mb))
        job_id=store.new_id();d=store.root/job_id;(d/'input').mkdir(parents=True)
        try:
            normal_input={**normal,'source':'registered-equipment'}
            candidate_suffix=Path(candidate.filename or '').suffix.lower()
            candidate_input,warnings=save_video(candidate,d/'input'/f'candidate{candidate_suffix}',limit,capture_fps)
            meta={'id':job_id,'created_at':now(),'state':'queued','stage':None,'events':[],
                  'inputs':{'normal':normal_input,'candidate':candidate_input},'equipment':{'id':item['id'],'name':item['name'],'model':item.get('model','')},
                  'capture_fps':capture_fps,'same_speed':same_speed,'fixed_camera':fixed_camera,
                  'warnings':warnings+item.get('warnings',[]),'provider':provider_info or {}}
            (d/'job.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
            executor.submit(run_job,job_id)
            return {'id':job_id,'url':f'/jobs/{job_id}','warnings':meta['warnings']}
        except Exception:
            shutil.rmtree(d,ignore_errors=True)
            raise

    def ensure_demo_job():
        """Make one read-only demo snapshot from an existing completed analysis; never call a model."""
        existing = next((m for m in store.all() if m.get('is_demo') and m.get('state') == 'done'), None)
        if existing:
            return existing['id']
        sources=[]
        for meta in store.all():
            if meta.get('is_demo') or meta.get('state') != 'done':
                continue
            run=store.root/meta['id']/'run'
            if not all((run/name).is_file() for name in ('result.json','evidence.json','diagnosis.json','decision.json','roi_plan.json','configs.json')):
                continue
            decision=json.loads((run/'decision.json').read_text(encoding='utf-8'))
            evidence=json.loads((run/'evidence.json').read_text(encoding='utf-8'))
            usable=sum(1 for row in evidence.get('region_comparisons',[]) if row.get('eligible_for_interpretation'))
            increases=sum(1 for row in evidence.get('region_comparisons',[]) if row.get('eligible_for_interpretation') and row.get('rule_flag')=='increase')
            has_signals=all((run/'measurements'/side/'signals.npz').is_file() for side in ('reference','candidate'))
            has_frames=any((run/'frames'/side).is_dir() for side in ('reference','candidate'))
            score=(has_signals, has_frames, decision.get('label')=='abnormal', increases, usable, meta.get('created_at',''))
            sources.append((score,meta))
        if not sources:
            raise HTTPException(404,'완료된 이전 분석 기록이 없어 데모를 열 수 없어요.')
        source=max(sources,key=lambda item:item[0])[1]
        source_dir=store.root/source['id']
        demo_id=store.new_id()
        while (store.root/demo_id).exists():demo_id=store.new_id()
        demo_dir=store.root/demo_id
        try:
            shutil.copytree(source_dir/'run',demo_dir/'run')
            run=demo_dir/'run'
            try:
                import numpy as np
                from ..contracts import Diagnosis
                from ..render import inspection_map, reports, spectrum_comparison
                plan=json.loads((run/'roi_plan.json').read_text(encoding='utf-8'))
                configs=json.loads((run/'configs.json').read_text(encoding='utf-8'))
                evidence=json.loads((run/'evidence.json').read_text(encoding='utf-8'))
                diagnosis=Diagnosis.model_validate(json.loads((run/'diagnosis.json').read_text(encoding='utf-8')))
                alignment=json.loads((run/'alignment.json').read_text(encoding='utf-8')).get('registration',{})
                band_map={}
                for row in evidence.get('region_comparisons',[]):
                    name=row['evidence_id'].rsplit(':',1)[-1]
                    lo,hi=row.get('band_hz') or (None,None)
                    if lo is not None:band_map[name]=(name,float(lo),float(hi))
                bands=list(band_map.values())
                spectrum=spectrum_comparison(run/'visuals',{side:run/'measurements'/side for side in ('reference','candidate')},
                    [r for r in plan.get('regions',[]) if r.get('role')=='target'],alignment,bands)
                maps={}
                for side in ('reference','candidate'):
                    track=run/'measurements'/side/'tracks.npz'
                    if not track.is_file():continue
                    with np.load(track,allow_pickle=False) as data:
                        keep=data['keep'].astype(bool)
                        maps[side]={'points':data['tr'][0,keep],'labels':data['labels'][keep]}
                candidate_frames=sorted((run/'frames'/'candidate').glob('*.jpg'))
                if candidate_frames:
                    inspection_map(candidate_frames[0],configs['candidate'],diagnosis,run/'visuals'/'inspection_roi.png',evidence=evidence,maps=maps)
                result=json.loads((run/'result.json').read_text(encoding='utf-8'))
                artifacts=sorted((run/'visuals').glob('heatmap_*.png'))
                if spectrum is not None:artifacts.append(spectrum)
                if (run/'visuals'/'inspection_roi.png').is_file():artifacts.append(run/'visuals'/'inspection_roi.png')
                reports(run,diagnosis,evidence,artifacts,result.get('mode','offline_synthetic'),decision=json.loads((run/'decision.json').read_text(encoding='utf-8')))
            except Exception:
                # Keep the previous saved result usable if a legacy job lacks inputs for a new visual.
                pass
            demo_meta={**source,'id':demo_id,'is_demo':True,'demo_source_job_id':source['id'],
                'inputs':{'normal':{**source.get('inputs',{}).get('normal',{}),'filename':'데모 정상 영상'},
                          'candidate':{**source.get('inputs',{}).get('candidate',{}),'filename':'데모 점검 영상'}}}
            (demo_dir/'job.json').write_text(json.dumps(demo_meta,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception:
            shutil.rmtree(demo_dir,ignore_errors=True)
            raise
        return demo_id

    @app.get('/demo')
    def demo():
        return RedirectResponse(f'/jobs/{ensure_demo_job()}',status_code=303)

    @app.get('/jobs/{job_id}', response_class=HTMLResponse)
    def job_page(job_id: str):
        store.dir(job_id)
        return (STATIC / 'job.html').read_text(encoding='utf-8')

    @app.get('/api/config')
    def config():
        return {'stages': [{'id': s, 'label': l} for s, l in STAGES], **(provider_info or {})}

    @app.get('/api/jobs')
    def jobs():
        return [{k: m.get(k) for k in ('id', 'created_at', 'state', 'stage', 'inputs', 'equipment')} | {'label': (m.get('decision') or {}).get('label')} for m in store.all() if not m.get('is_demo')][:30]

    @app.post('/api/jobs')
    def create_job(normal: UploadFile = File(...), candidate: UploadFile = File(...), capture_fps: float = Form(240.0),
                   same_speed: bool = Form(False), fixed_camera: bool = Form(False)):
        if not (1 <= capture_fps <= 10000):
            raise HTTPException(400, '촬영 FPS를 확인해주세요 (예: 240).')
        for f in (normal, candidate):
            if Path(f.filename or '').suffix.lower() not in VIDEO_EXT:
                raise HTTPException(400, f'{f.filename}: .mov / .mp4 영상만 올릴 수 있어요.')
        job_id = store.new_id(); d = store.root / job_id; (d / 'input').mkdir(parents=True)
        inputs, warnings = {}, []
        try:
            for role, f in (('normal', normal), ('candidate', candidate)):
                stored = role + Path(f.filename).suffix.lower(); dest = d / 'input' / stored; size = 0
                with open(dest, 'wb') as out:
                    while chunk := f.file.read(1 << 20):
                        size += len(chunk)
                        if size > max_upload_mb * 1024 * 1024:
                            raise HTTPException(413, f'영상 하나는 {max_upload_mb}MB까지 올릴 수 있어요.')
                        out.write(chunk)
                info, warn = probe(dest, capture_fps)
                inputs[role] = {'filename': f.filename, 'stored': stored, 'bytes': size, **info}
                warnings += [('정상 영상: ' if role == 'normal' else '점검 영상: ') + w for w in warn]
        except HTTPException:
            shutil.rmtree(d, ignore_errors=True); raise
        except ValueError as exc:
            shutil.rmtree(d, ignore_errors=True); raise HTTPException(400, str(exc))
        meta = {'id': job_id, 'created_at': now(), 'state': 'queued', 'stage': None, 'events': [], 'inputs': inputs,
                'capture_fps': capture_fps, 'same_speed': same_speed, 'fixed_camera': fixed_camera, 'warnings': warnings,
                'provider': provider_info or {}}
        (d / 'job.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        executor.submit(run_job, job_id)
        return {'id': job_id, 'url': f'/jobs/{job_id}', 'warnings': warnings}

    @app.get('/api/jobs/{job_id}')
    def job(job_id: str):
        d = store.dir(job_id); meta = store.read(job_id)
        meta['queue_position'] = sum(1 for m in store.all() if m.get('state') == 'queued' and m['id'] < job_id) if meta['state'] == 'queued' else None
        meta['is_demo'] = bool(meta.get('is_demo'))
        if meta['state'] == 'done':
            meta['results'] = build_results(d / 'run')
        return meta

    @app.get('/api/jobs/{job_id}/files/{path:path}')
    def job_file(job_id: str, path: str):
        run = (store.dir(job_id) / 'run').resolve(); target = (run / path).resolve()
        if run not in target.parents or target.suffix.lower() not in SERVE_EXT or not target.is_file():
            raise HTTPException(404, '파일을 찾을 수 없어요.')
        return FileResponse(target)

    @app.get('/api/jobs/{job_id}/archive')
    def job_archive(job_id: str):
        """Download the analysis record without the uploaded videos or extracted source frames."""
        d = store.dir(job_id)
        memory = __import__('io').BytesIO()
        with zipfile.ZipFile(memory, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for base in (d / 'job.json', d / 'run'):
                if not base.exists():
                    continue
                paths = [base] if base.is_file() else sorted(p for p in base.rglob('*') if p.is_file())
                for path in paths:
                    rel = path.relative_to(d)
                    if any(part in ARCHIVE_EXCLUDE for part in rel.parts):
                        continue
                    archive.write(path, arcname=Path(job_id) / rel)
        memory.seek(0)
        return StreamingResponse(memory, media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="devday-analysis-{job_id}.zip"'
        })

    app.state.store = store; app.state.executor = executor
    return app
