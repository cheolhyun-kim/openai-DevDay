import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from pydantic import ValidationError
from devday.contracts import ROIPlan,Diagnosis,to_config
from devday.demo import PLAN,SyntheticProvider,videos,run_demo
from devday.measurement import validate_bands,build_evidence,safe_ratio
from devday.workflow import validate_diagnosis,run_pipeline
from devday.providers import OpenAIProvider

class ContractsTests(unittest.TestCase):
    def test_out_of_frame_api_roi_rejected(self):
        p=copy.deepcopy(PLAN);p['regions'][0]['reference']['bounds_xywh']=[.9,.2,.2,.2]
        with self.assertRaises(ValidationError):ROIPlan.model_validate(p)

    def test_degenerate_polygon_rejected(self):
        p=copy.deepcopy(PLAN);p['regions'][0]['reference']={'shape':'polygon','bounds_xywh':None,'points_xy':[[.1,.1],[.2,.2],[.3,.3]]}
        with self.assertRaises(ValidationError):ROIPlan.model_validate(p)

    def test_duplicate_ids_rejected(self):
        p=copy.deepcopy(PLAN);p['regions'][1]['id']='head'
        with self.assertRaises(ValidationError):ROIPlan.model_validate(p)

    def test_rotated_upright_coordinate_conversion(self):
        plan=ROIPlan.model_validate(PLAN);cfg=to_config(plan,[1080,1920],240,'reference')
        self.assertAlmostEqual(cfg['rois'][0]['bounds'][0],105/320*1080)
        self.assertEqual(cfg['frame_size'],[1080,1920]);self.assertEqual(cfg['capture_fps'],240)

    def test_nyquist_and_zero_baseline(self):
        with self.assertRaises(ValueError):validate_bands([('too_high',22,25)],[30,240])
        self.assertIsNone(safe_ratio(.1,0));self.assertIsNone(safe_ratio(None,.1))
        with self.assertRaises(ValueError):validate_bands([('../escape',1,10)],[120,120])

    def test_sparse_comparisons_excluded(self):
        plan=ROIPlan.model_validate(PLAN)
        region={'retained_points':2,'bands':{'low':{'point_median_rms_px':.1,'full_survivor_third_rms_px':[]}}}
        m={'reference':{'regions':{'head':region}},'candidate':{'regions':{'head':region}}}
        e=build_evidence(plan,{},m,[('low',1,10)],{})
        self.assertFalse(e['region_comparisons'][0]['eligible_for_interpretation']);self.assertEqual(e['allowed_evidence_ids'],[])

    def test_unvalidated_fault_claim_rejected(self):
        base={'assessment':'suspected_abnormal','is_abnormal':True,'abnormality_suspected':True,'fault_confirmed':False,'summary':'x','decision_basis':[],'inspection_candidates':[],'limitations':[],'recommended_validation':[]}
        # abnormal without any cited number
        with self.assertRaises(ValidationError):Diagnosis.model_validate(base)
        cited=dict(base,decision_basis=[{'evidence_id':'head:low','metric':'ratio','value':2.0}])
        Diagnosis.model_validate(cited)
        # a camera comparison can never confirm a physical fault
        with self.assertRaises(ValidationError):Diagnosis.model_validate(dict(cited,fault_confirmed=True))
        # is_abnormal must follow the assessment
        with self.assertRaises(ValidationError):Diagnosis.model_validate(dict(cited,assessment='no_clear_difference',abnormality_suspected=False))
        with self.assertRaises(ValidationError):Diagnosis.model_validate(dict(cited,assessment='inconclusive',abnormality_suspected=False))

    def test_api_request_has_images_schema_and_no_storage(self):
        captured={}
        def parse(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id='fake-response',status='completed',usage=None,output_parsed=ROIPlan.model_validate(PLAN))
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'image.jpg';p.write_bytes(b'image-for-request-shape-test')
            api=OpenAIProvider(client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
            api.propose_rois({'reference':{'capture_fps':240}},[('reference',p)])
        self.assertIs(captured['text_format'],ROIPlan);self.assertFalse(captured['store'])
        self.assertEqual(captured['input'][0]['content'][2]['type'],'input_image')
        self.assertEqual(len(api.calls),1)

    def test_unknown_diagnostic_roi_rejected(self):
        d=Diagnosis(assessment='suspected_abnormal',is_abnormal=True,abnormality_suspected=True,fault_confirmed=False,summary='x',decision_basis=[{'evidence_id':'head:low','metric':'ratio','value':2.0}],inspection_candidates=[{'roi_id':'bearing','reason':'x','evidence_ids':['head:low'],'confidence':'low','alternative_explanations':[],'action':'x'}],limitations=[],recommended_validation=[])
        with self.assertRaises(ValueError):validate_diagnosis(d,{'allowed_evidence_ids':['head:low'],'region_comparisons':[{'roi_id':'head','evidence_id':'head:low','eligible_for_interpretation':True}],'relative_comparisons':[]})

class ReportCandidateTests(unittest.TestCase):
    def test_all_candidates_below_five_and_first_five_otherwise(self):
        from devday.render import display_candidates,reports,inspection_map
        from unittest.mock import patch
        import numpy as np
        for count in [0,1,4,5,6]:
            with self.subTest(count=count),tempfile.TemporaryDirectory() as tmp:
                items=[{'roi_id':f'part_{i}','reason':'x','evidence_ids':[f'part_{i}:low'],'confidence':'low','alternative_explanations':[],'action':'고정 상태를 확인해보세요.'} for i in range(count)]
                d=Diagnosis(assessment='suspected_abnormal' if count else 'inconclusive',is_abnormal=True if count else None,abnormality_suspected=bool(count),fault_confirmed=False,summary='x',decision_basis=[{'evidence_id':'part_0:low','metric':'ratio','value':2.0}] if count else [],inspection_candidates=items,limitations=[],recommended_validation=[])
                evidence={'allowed_evidence_ids':[f'part_{i}:low' for i in range(count)],'region_comparisons':[{'roi_id':f'part_{i}','evidence_id':f'part_{i}:low','eligible_for_interpretation':True,'difference_px':1,'ratio':2.0} for i in range(count)],'relative_comparisons':[],'roi_plan':{'regions':[{'id':f'part_{i}','part_name':f'후보부위{i}'} for i in range(count)]}}
                validate_diagnosis(d,evidence)
                self.assertEqual([x.roi_id for x in display_candidates(d)],[f'part_{i}' for i in range(min(count,5))])
                reports(tmp,d,evidence,[],'openai_live')
                html=(Path(tmp)/'report.html').read_text();plain=(Path(tmp)/'diagnosis.txt').read_text()
                self.assertEqual(html.count('<article class="check">'),min(count,5))
                for i in range(count):
                    self.assertEqual(f'후보부위{i}' in html,i<5)
                    self.assertEqual(f'후보부위{i}' in plain,i<5)
                config={'rois':[{'id':f'part_{i}'} for i in range(count)]}
                with patch('devday.render.cv2.imread',return_value=np.zeros((80,80,3),dtype=np.uint8)),patch('devday.render.roi_mask',return_value=np.ones((80,80),dtype=np.uint8)*255),patch('devday.render.cv2.putText') as labels:
                    inspection_map('unused',config,d,Path(tmp)/'map.png')
                self.assertEqual([c.args[1] for c in labels.call_args_list],[str(i+1) for i in range(min(count,5))])

class EndToEndTests(unittest.TestCase):
    def test_known_frequency_camera_correction_and_two_model_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=run_demo(Path(tmp)/'demo')
            out=Path(result['output_dir']);e=json.loads((out/'evidence.json').read_text())
            row=e['region_comparisons'][0]
            self.assertTrue(row['eligible_for_interpretation']);self.assertGreater(row['ratio'],3.5)
            peaks=e['measurements']['candidate']['regions']['head']['peaks']['translation']
            self.assertLess(abs(peaks[0]['hz']-23.4),.5)
            self.assertEqual(e['videos']['candidate']['playback_fps'],30)
            self.assertEqual(e['videos']['candidate']['capture_fps'],120)
            self.assertEqual([c['stage'] for c in result['model_calls']],['roi','diagnosis'])
            self.assertEqual(result['mode'],'offline_synthetic')
            self.assertTrue(result['diagnosis']['is_abnormal'])
            self.assertEqual(result['decision']['label'],'abnormal');self.assertTrue(result['decision']['agreement'])
            self.assertEqual(row['rule_flag'],'increase');self.assertGreater(row['candidate_snr'],2);self.assertGreater(row['z_score'],3)
            self.assertLess(abs(row['candidate_dominant_hz']-23.4),.5)
            self.assertTrue(json.loads((out/'alignment.json').read_text())['registration']['ok'])
            self.assertIn('판단 근거 수치',Path(result['report_html']).read_text())
            self.assertIn('실제 AI가 판독한 진단서는 아니에요',Path(result['report_html']).read_text())
            self.assertTrue((out/'visuals'/'heatmap_rotation.png').exists())
            self.assertTrue((out/'visuals'/'inspection_roi.png').exists())
            with self.assertRaises(ValueError):run_demo(Path(tmp)/'demo')

    def test_different_setup_skips_diagnosis_instead_of_forcing_abnormal(self):
        class Uncertain(SyntheticProvider):
            def propose_rois(self,*args,**kwargs):
                p=copy.deepcopy(PLAN);p['same_setup_assessment']='different';return ROIPlan.model_validate(p)
            def diagnose(self,*args,**kwargs):raise AssertionError('Must not diagnose unsupported comparisons')
        with tempfile.TemporaryDirectory() as tmp:
            a,b=videos(Path(tmp)/'input',frames=180)
            result=run_pipeline(a,b,capture_fps=120,output_dir=Path(tmp)/'run',provider=Uncertain(),bands=[('rotation',20,27)])
            self.assertEqual(result['diagnosis']['assessment'],'inconclusive')
            self.assertEqual(result['diagnosis']['inspection_candidates'],[])

class SDKTransportTests(unittest.TestCase):
    def test_real_sdk_serializes_strict_roi_schema_without_external_network(self):
        import httpx
        from openai import OpenAI
        captured=[]
        def handler(request):
            body=json.loads(request.content);captured.append(body)
            return httpx.Response(200,json={'id':'resp_fixture','object':'response','created_at':0,'status':'completed','error':None,'incomplete_details':None,'model':'gpt-6-astra','output':[{'type':'message','id':'msg_fixture','status':'completed','role':'assistant','content':[{'type':'output_text','text':json.dumps(PLAN),'annotations':[]}]}]})
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'frame.jpg';image.write_bytes(b'fixture')
            with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
                with OpenAI(api_key='test-key-not-real',http_client=transport,max_retries=0) as client:
                    result=OpenAIProvider(client=client).propose_rois({},[('reference',image)])
        self.assertIsInstance(result,ROIPlan)
        self.assertEqual(len(captured),1)
        self.assertEqual(captured[0]['text']['format']['type'],'json_schema')
        self.assertTrue(captured[0]['text']['format']['strict'])
        self.assertFalse(captured[0]['store'])

    def test_refusal_does_not_become_success(self):
        def parse(**kwargs):return SimpleNamespace(status='completed',output_parsed=None,usage=None,id='refused')
        api=OpenAIProvider(client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
        with self.assertRaises(RuntimeError):api.propose_rois({},[])


if __name__=='__main__':unittest.main()
