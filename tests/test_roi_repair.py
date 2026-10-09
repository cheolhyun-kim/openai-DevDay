import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch
from devday.demo import SyntheticProvider,videos
from devday.workflow import run_pipeline
from devday.measurement import measure as real_measure

class RepairTests(unittest.TestCase):
    def test_tracking_failure_reselects_and_remeasures_both_videos(self):
        class Provider(SyntheticProvider):
            def propose_rois(self,metadata,images,correction=None):
                self.correction=correction
                return super().propose_rois(metadata,images,correction)
        with tempfile.TemporaryDirectory() as tmp:
            a,b=videos(Path(tmp)/'input',frames=180);provider=Provider();calls=[]
            def measure(video,config,directory,bands):
                calls.append(str(video))
                if len(calls)<=2:raise ValueError('Fewer than six full-duration background features; use better ROI/video')
                return real_measure(video,config,directory,bands)
            with patch('devday.workflow.measure',side_effect=measure):
                result=run_pipeline(a,b,capture_fps=120,output_dir=Path(tmp)/'run',provider=provider,bands=[('rotation',20,27)])
            self.assertEqual(calls,[str(a),str(b),str(a),str(b)])
            self.assertEqual([c['stage'] for c in provider.calls],['roi','roi','diagnosis'])
            self.assertIn('full_video_background_tracking',provider.correction)
            self.assertTrue((Path(result['output_dir'])/'roi_attempts/tracking_0/roi_plan.json').exists())

    def test_repeated_failure_has_one_retry_and_skips_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b=videos(Path(tmp)/'input',frames=30);provider=SyntheticProvider()
            with patch('devday.workflow.measure',side_effect=ValueError('Background transform failed at frame 10; no identity fallback')) as measurement:
                result=run_pipeline(a,b,capture_fps=120,output_dir=Path(tmp)/'run',provider=provider,bands=[('rotation',20,27)])
            self.assertEqual(measurement.call_count,4)
            self.assertEqual([c['stage'] for c in provider.calls],['roi','roi'])
            self.assertEqual(result['diagnosis']['assessment'],'inconclusive')

    def test_disabled_retry_makes_no_extra_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b=videos(Path(tmp)/'input',frames=30);provider=SyntheticProvider()
            with patch('devday.workflow.measure',side_effect=ValueError('Fewer than six full-duration background features')) as measurement:
                run_pipeline(a,b,capture_fps=120,output_dir=Path(tmp)/'run',provider=provider,bands=[('rotation',20,27)],roi_repair_attempts=0)
            self.assertEqual(measurement.call_count,2)
            self.assertEqual(len(provider.calls),1)

    def test_format_repair_does_not_consume_tracking_repair(self):
        class FormatFailure(SyntheticProvider):
            def __init__(self):
                super().__init__();self.roi_requests=0;self.corrections=[]
            def propose_rois(self,metadata,images,correction=None):
                self.roi_requests+=1;self.corrections.append(correction)
                if self.roi_requests==1:
                    raise ValueError('Relative pairs must reference two distinct targets')
                return super().propose_rois(metadata,images,correction)
        with tempfile.TemporaryDirectory() as tmp:
            a,b=videos(Path(tmp)/'input',frames=180);provider=FormatFailure();calls=[]
            def measure(video,config,directory,bands):
                calls.append(str(video))
                if len(calls)<=2:raise ValueError('Fewer than six full-duration background features')
                return real_measure(video,config,directory,bands)
            with patch('devday.workflow.measure',side_effect=measure):
                result=run_pipeline(a,b,capture_fps=120,output_dir=Path(tmp)/'run',provider=provider,bands=[('rotation',20,27)])
            self.assertEqual(provider.roi_requests,3)
            self.assertIn('Relative pairs',provider.corrections[1])
            self.assertIn('full_video_background_tracking',provider.corrections[2])
            self.assertEqual(calls,[str(a),str(b),str(a),str(b)])
            self.assertEqual(provider.calls[-1]['stage'],'diagnosis')
            self.assertTrue((Path(result['output_dir'])/'roi_rejection_0.json').exists())

    def test_combined_failures_still_have_bounded_tracking_retry(self):
        class FormatFailure(SyntheticProvider):
            def __init__(self):super().__init__();self.requests=0
            def propose_rois(self,*args,**kwargs):
                self.requests+=1
                if self.requests==1:raise ValueError('invalid initial ROI')
                return super().propose_rois(*args,**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            a,b=videos(Path(tmp)/'input',frames=30);provider=FormatFailure()
            with patch('devday.workflow.measure',side_effect=ValueError('Fewer than six full-duration background features')) as measurement:
                result=run_pipeline(a,b,capture_fps=120,output_dir=Path(tmp)/'run',provider=provider,bands=[('rotation',20,27)])
            self.assertEqual(provider.requests,3)
            self.assertEqual(measurement.call_count,4)
            self.assertEqual(result['diagnosis']['assessment'],'inconclusive')
            self.assertNotIn('diagnosis',[c['stage'] for c in provider.calls])
