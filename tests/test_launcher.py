import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('launcher',Path(__file__).resolve().parents[1]/'run_analysis.py')
launcher=importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

class LauncherTests(unittest.TestCase):
    def prepare(self,root):
        (root/'input').mkdir()
        for name in ['normal.mp4','candidate.mp4']:(root/'input'/name).write_bytes(b'test')
        (root/'settings.py').write_text('NORMAL_VIDEO="input/normal.mp4"\nCANDIDATE_VIDEO="input/candidate.mp4"\nOPENAI_API_KEY="test-local-key"\nCAPTURE_FPS=240\nCANDIDATE_CAPTURE_FPS=179.82\nFIXED_CAMERA=False\nMODEL=None\n')

    def test_settings_reach_pipeline_and_each_run_has_new_output(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=False):
            root=Path(tmp);self.prepare(root)
            with patch.object(launcher.webbrowser,'open',return_value=True) as browser,patch.object(launcher,'run_pipeline',return_value={'report_html':str(root/'report.html')}) as pipeline:
                launcher.main(root);launcher.main(root)
            self.assertEqual(browser.call_count,2)
            browser.assert_called_with((root/'report.html').resolve().as_uri(),new=2)
            first,second=pipeline.call_args_list
            self.assertEqual(first.args,((root/'input/normal.mp4').resolve(),(root/'input/candidate.mp4').resolve()))
            self.assertEqual(first.kwargs['capture_fps'],240)
            self.assertEqual(first.kwargs['candidate_capture_fps'],179.82)
            self.assertNotIn('same_speed_confirmed',first.kwargs['conditions'])
            self.assertFalse(first.kwargs['conditions']['fixed_camera_confirmed'])
            self.assertNotEqual(first.kwargs['output_dir'],second.kwargs['output_dir'])
            self.assertEqual(os.environ['OPENAI_API_KEY'],'test-local-key')
            self.assertNotIn('test-local-key',str(first))

    def test_missing_key_video_fps_and_same_input_fail_before_pipeline(self):
        for change in [('OPENAI_API_KEY="test-local-key"','OPENAI_API_KEY=""'),('CAPTURE_FPS=240','CAPTURE_FPS=None'),('CANDIDATE_VIDEO="input/candidate.mp4"','CANDIDATE_VIDEO="input/missing.mp4"'),('CANDIDATE_VIDEO="input/candidate.mp4"','CANDIDATE_VIDEO="input/normal.mp4"')]:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.prepare(root);p=root/'settings.py';p.write_text(p.read_text().replace(*change))
                with patch.object(launcher,'run_pipeline') as pipeline:
                    with self.assertRaises(ValueError):launcher.main(root)
                    pipeline.assert_not_called()

    def test_browser_failure_preserves_success_and_analysis_failure_does_not_open(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=False):
            root=Path(tmp);self.prepare(root)
            result={'report_html':str(root/'report.html')}
            with patch.object(launcher,'run_pipeline',return_value=result),patch.object(launcher.webbrowser,'open',side_effect=OSError('unavailable')):
                self.assertEqual(launcher.main(root),result)
            with patch.object(launcher,'run_pipeline',side_effect=RuntimeError('measurement failed')),patch.object(launcher.webbrowser,'open') as browser:
                with self.assertRaises(RuntimeError):launcher.main(root)
                browser.assert_not_called()
