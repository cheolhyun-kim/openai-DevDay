"""Web app: upload -> background job -> results payload, using the synthetic videos and fake provider."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from devday.contracts import ROIPlan, Diagnosis
from devday.demo import SyntheticProvider, videos, PLAN
from devday.web.app import create_app
from devday.providers import AnthropicProvider


def wait(client, job_id, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['state'] in ('done', 'failed'):
            return job
        time.sleep(.5)
    raise AssertionError('job did not finish')


class WebAppTests(unittest.TestCase):
    def test_upload_runs_pipeline_and_serves_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            normal, candidate = videos(Path(tmp) / 'v', frames=240)
            app = create_app(Path(tmp) / 'app', SyntheticProvider, {'provider': 'test'})
            with TestClient(app) as client:
                self.assertIn('흔들림 비교 진단', client.get('/').text)
                files = {'normal': ('normal.avi', normal.read_bytes(), 'video/x-msvideo'), 'candidate': ('candidate.avi', candidate.read_bytes(), 'video/x-msvideo')}
                r = client.post('/api/jobs', files=files, data={'capture_fps': '120', 'same_speed': 'true', 'fixed_camera': 'true'})
                self.assertEqual(r.status_code, 200, r.text)
                job_id = r.json()['id']
                self.assertTrue(any('30fps' in w for w in r.json()['warnings']))  # MJPG test file is 30 fps
                job = wait(client, job_id)
                self.assertEqual(job['state'], 'done', job.get('error'))
                res = job['results']
                self.assertEqual(res['decision']['label'], 'abnormal')
                self.assertTrue(res['cited'] and res['table'])
                self.assertTrue(any(i['path'] == 'visuals/inspection_roi.png' for i in res['images']))
                ev = json.loads((Path(tmp) / 'app' / 'web_jobs' / job_id / 'run' / 'evidence.json').read_text())
                self.assertTrue(any('120fps 슬로모션으로 간주' in w for w in ev['conditions']['input_warnings']))  # the AI sees it
                self.assertEqual(client.get(f'/api/jobs/{job_id}/files/visuals/inspection_roi.png').status_code, 200)
                self.assertEqual(client.get(f'/api/jobs/{job_id}/files/../job.json').status_code, 404)
                self.assertEqual(client.get(f'/jobs/{job_id}').status_code, 200)
                self.assertEqual(client.get('/api/jobs').json()[0]['label'], 'abnormal')
                self.assertEqual(client.get('/api/jobs/not-a-job').status_code, 404)

    def test_rejects_non_video_and_reports_provider_failure(self):
        def broken():
            raise RuntimeError('Set ANTHROPIC_API_KEY; use replay/demo for offline verification')
        with tempfile.TemporaryDirectory() as tmp:
            normal, candidate = videos(Path(tmp) / 'v', frames=60)
            with TestClient(create_app(Path(tmp) / 'app', broken)) as client:
                r = client.post('/api/jobs', files={'normal': ('a.txt', b'x'), 'candidate': ('b.mov', b'x')})
                self.assertEqual(r.status_code, 400)
                r = client.post('/api/jobs', files={'normal': ('a.mov', b'not a video'), 'candidate': ('b.mov', b'x')})
                self.assertEqual(r.status_code, 400)
                r = client.post('/api/jobs', files={'normal': ('n.avi', normal.read_bytes()), 'candidate': ('c.avi', candidate.read_bytes())}, data={'capture_fps': '120'})
                job = wait(client, r.json()['id'])
                self.assertEqual(job['state'], 'failed')
                self.assertTrue(job['error']['hints'])


class AnthropicProviderTests(unittest.TestCase):
    def test_request_shape_structured_output_and_validation(self):
        captured = {}
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id='msg_1', stop_reason='end_turn', usage=None,
                                   content=[SimpleNamespace(type='text', text=json.dumps(PLAN))])
        with tempfile.TemporaryDirectory() as tmp:
            import cv2, numpy as np
            img = Path(tmp) / 'f.jpg'; cv2.imwrite(str(img), np.zeros((1920, 1080, 3), np.uint8))
            p = AnthropicProvider(model='claude-sonnet-5-5', client=SimpleNamespace(messages=SimpleNamespace(create=create)))
            plan = p.propose_rois({'reference': {}}, [('reference frame=0', img)])
        self.assertIsInstance(plan, ROIPlan)
        self.assertEqual(captured['output_config']['format']['type'], 'json_schema')
        self.assertIn('regions', captured['output_config']['format']['schema']['properties'])
        blocks = captured['messages'][0]['content']
        self.assertEqual([b['type'] for b in blocks], ['text', 'text', 'image'])
        import base64, cv2, numpy as np
        decoded = cv2.imdecode(np.frombuffer(base64.b64decode(blocks[2]['source']['data']), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(max(decoded.shape[:2]), 1568)  # downscaled to Claude's native long side
        self.assertIn('ROIPlan', captured['system'])

    def test_refusal_and_invalid_json_raise(self):
        bad = lambda reason, text: SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(id='x', stop_reason=reason, usage=None, content=[SimpleNamespace(type='text', text=text)])))
        with self.assertRaises(RuntimeError):
            AnthropicProvider(client=bad('refusal', '{}')).diagnose({}, [])
        with self.assertRaises(ValueError):  # pydantic ValidationError -> workflow repair path
            AnthropicProvider(client=bad('end_turn', '{"assessment":"x"}')).diagnose({}, [])

    def test_real_sdk_serializes_request_without_network(self):
        import httpx2, anthropic
        sent = []
        answer = {'assessment': 'inconclusive', 'is_abnormal': None, 'abnormality_suspected': False, 'fault_confirmed': False, 'summary': 'x',
                  'decision_basis': [], 'inspection_candidates': [], 'limitations': [], 'recommended_validation': []}
        def handler(request):
            sent.append(json.loads(request.content))
            return httpx2.Response(200, json={'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-5-5',
                                              'content': [{'type': 'text', 'text': json.dumps(answer)}], 'stop_reason': 'end_turn', 'stop_sequence': None,
                                              'usage': {'input_tokens': 10, 'output_tokens': 5}})
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as http:
            client = anthropic.Anthropic(api_key='test-key-not-real', http_client=http, max_retries=0)
            d = AnthropicProvider(client=client).diagnose({'allowed_evidence_ids': []}, [])
        self.assertIsInstance(d, Diagnosis)
        self.assertEqual(sent[0]['model'], 'claude-sonnet-5-5')
        self.assertEqual(sent[0]['output_config']['format']['type'], 'json_schema')
        self.assertIn('decision_basis', sent[0]['output_config']['format']['schema']['properties'])


if __name__ == '__main__':
    unittest.main()


class AccessCodeTests(unittest.TestCase):
    def test_pages_and_api_require_code_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp) / 'app', SyntheticProvider, access_code='482913')
            with TestClient(app) as client:
                r = client.get('/', follow_redirects=False)
                self.assertEqual(r.status_code, 303); self.assertTrue(r.headers['location'].startswith('/login'))
                self.assertEqual(client.get('/api/jobs').status_code, 401)
                self.assertEqual(client.post('/api/jobs', files={'normal': ('a.mov', b'x'), 'candidate': ('b.mov', b'x')}).status_code, 401)
                self.assertEqual(client.get('/static/style.css').status_code, 200)
                self.assertEqual(client.post('/login', data={'code': '000000', 'next': '/'}, follow_redirects=False).status_code, 401)
                r = client.post('/login', data={'code': '482913', 'next': '//evil.example'}, follow_redirects=False)
                self.assertEqual(r.status_code, 303); self.assertEqual(r.headers['location'], '/')  # no open redirect
                self.assertEqual(client.get('/api/jobs').status_code, 200)
                self.assertIn('흔들림 비교 진단', client.get('/').text)

    def test_no_code_means_open_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(create_app(Path(tmp) / 'app', SyntheticProvider)) as client:
                self.assertEqual(client.get('/api/jobs').status_code, 200)


class TunnelTests(unittest.TestCase):
    def test_parses_quick_tunnel_url(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location('webmain', Path(__file__).resolve().parents[1] / 'web.py')
        web = importlib.util.module_from_spec(spec); spec.loader.exec_module(web)
        fake = [sys.executable, '-c', "import time;print('INF Requesting new quick Tunnel');print('INF |  https://brave-otter-12ab.trycloudflare.com  |',flush=True);time.sleep(5)"]
        proc, url = web.start_tunnel(0, timeout=10, command=fake)
        proc.terminate()
        self.assertEqual(url, 'https://brave-otter-12ab.trycloudflare.com')
        with self.assertRaises(ValueError):
            web.start_tunnel(0, timeout=3, command=[sys.executable, '-c', "print('ERR failed to connect')"])
