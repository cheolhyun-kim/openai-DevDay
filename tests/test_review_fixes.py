"""Tests for the review fixes: noise floor/SNR, rule pre-filter, citations, guardrail, registration."""
import copy
from types import SimpleNamespace
import json
import tempfile
import unittest
from pathlib import Path
import cv2
import numpy as np
from devday.contracts import ROIPlan, Diagnosis
from devday.demo import PLAN, SyntheticProvider, videos
from devday.measurement import build_evidence, compare, DEFAULT_RULES
from devday.registration import register, transfer_plan
from devday.workflow import validate_diagnosis, final_decision, run_pipeline


def region(rms, windows, points=40):
    return {'retained_points': points, 'bands': {'low': {'region_rms_px': rms, 'window_rms_px': windows, 'point_median_rms_px': rms, 'full_survivor_third_rms_px': []}}}


def measurements(ref_rms, cand_rms, floor=.01, windows=None):
    windows = windows or [ref_rms * f for f in (.95, 1.0, 1.05, .98, 1.02, 1.0)]
    side = lambda v: {'regions': {'head': region(v, windows)}, 'relative': {}, 'background': {'low': {'floor_px': floor, 'per_region_px': {}}}}
    return {'reference': side(ref_rms), 'candidate': side(cand_rms)}


def diag(assessment, basis, candidates=()):
    flag = {'suspected_abnormal': True, 'no_clear_difference': False, 'inconclusive': None}[assessment]
    return Diagnosis(assessment=assessment, is_abnormal=flag, abnormality_suspected=flag is True, fault_confirmed=False, summary='x',
                     decision_basis=basis, inspection_candidates=list(candidates), limitations=[], recommended_validation=[])


class NoiseFloorAndRules(unittest.TestCase):
    def setUp(self):
        self.plan = ROIPlan.model_validate(PLAN)

    def test_below_noise_floor_is_not_eligible(self):
        e = build_evidence(self.plan, {}, measurements(.012, .015, floor=.01), [('low', 1, 10)], {})
        row = e['region_comparisons'][0]
        self.assertEqual(row['rule_flag'], 'below_noise_floor')
        self.assertFalse(row['eligible_for_interpretation'])
        self.assertEqual(e['rule_prefilter']['suggested_decision'], 'insufficient')

    def test_clear_increase_flags_abnormal(self):
        e = build_evidence(self.plan, {}, measurements(.05, .2), [('low', 1, 10)], {})
        row = e['region_comparisons'][0]
        self.assertEqual(row['rule_flag'], 'increase')
        self.assertAlmostEqual(row['ratio'], 4.0, places=2)
        self.assertAlmostEqual(row['candidate_snr'], 20.0, places=1)
        self.assertEqual(e['rule_prefilter']['suggested_decision'], 'abnormal')
        self.assertEqual(e['rule_prefilter']['increase_evidence_ids'], ['head:low'])

    def test_measurable_but_unchanged_is_normal(self):
        e = build_evidence(self.plan, {}, measurements(.05, .055), [('low', 1, 10)], {})
        self.assertEqual(e['region_comparisons'][0]['rule_flag'], 'no_significant_change')
        self.assertEqual(e['rule_prefilter']['suggested_decision'], 'normal')

    def test_global_noise_increase_is_not_flagged(self):
        # Candidate target doubles but so does its background: snr_ratio ~1 -> not an increase.
        m = measurements(.05, .1)
        m['candidate']['background']['low']['floor_px'] = .02
        e = build_evidence(self.plan, {}, m, [('low', 1, 10)], {})
        self.assertNotEqual(e['region_comparisons'][0]['rule_flag'], 'increase')

    def test_registration_scale_converts_candidate_pixels(self):
        e = build_evidence(self.plan, {}, measurements(.05, .1), [('low', 1, 10)], {}, alignment={'ok': True, 'scale': 2.0})
        self.assertAlmostEqual(e['region_comparisons'][0]['ratio'], 1.0, places=3)

    def test_z_score_uses_reference_window_spread(self):
        c = compare(1.0, 1.6, .1, .1, [.5, 1.5, .8, 1.2, 1.0, 1.0], DEFAULT_RULES)
        self.assertLess(c['z_score'], 3)
        self.assertEqual(c['rule_flag'], 'no_significant_change')


class CitationsAndGuardrail(unittest.TestCase):
    def setUp(self):
        self.e = build_evidence(ROIPlan.model_validate(PLAN), {}, measurements(.05, .2), [('low', 1, 10)], {})

    def test_exact_citation_accepted_and_agreement(self):
        d = validate_diagnosis(diag('suspected_abnormal', [{'evidence_id': 'head:low', 'metric': 'ratio', 'value': 4.0}]), self.e)
        decision = final_decision(d, self.e)
        self.assertEqual(decision['label'], 'abnormal'); self.assertTrue(decision['agreement'])

    def test_invented_number_rejected(self):
        with self.assertRaises(ValueError):
            validate_diagnosis(diag('suspected_abnormal', [{'evidence_id': 'head:low', 'metric': 'ratio', 'value': 7.5}]), self.e)

    def test_unknown_evidence_citation_rejected(self):
        with self.assertRaises(ValueError):
            validate_diagnosis(diag('suspected_abnormal', [{'evidence_id': 'bearing:low', 'metric': 'ratio', 'value': 4.0}]), self.e)

    def test_api_decision_is_final_when_rule_recommends_another_label(self):
        d = validate_diagnosis(diag('no_clear_difference', [{'evidence_id': 'head:low', 'metric': 'ratio', 'value': 4.0}]), self.e)
        decision = final_decision(d, self.e)
        self.assertEqual(decision['label'], 'normal'); self.assertFalse(decision['agreement'])

    def test_api_can_flag_a_decrease_without_rule_increase(self):
        e = build_evidence(ROIPlan.model_validate(PLAN), {}, measurements(.05, .055), [('low', 1, 10)], {})
        e['allowed_evidence_ids']=['head:low']
        d = validate_diagnosis(diag('suspected_abnormal', [{'evidence_id': 'head:low', 'metric': 'ratio', 'value': 1.1}]), e)
        self.assertEqual(final_decision(d, e)['label'], 'abnormal')

    def test_ratios_below_one_are_not_automatically_normal(self):
        for ratio in (.59, .73):
            with self.subTest(ratio=ratio):
                e = build_evidence(ROIPlan.model_validate(PLAN), {}, measurements(.05, .05 * ratio), [('low', 1, 10)], {})
                row = e['region_comparisons'][0]
                self.assertAlmostEqual(row['ratio'], ratio, places=2)
                d = validate_diagnosis(diag('suspected_abnormal', [{'evidence_id': 'head:low', 'metric': 'ratio', 'value': row['ratio']}]), e)
                self.assertEqual(final_decision(d, e)['label'], 'abnormal')

    def test_frequency_change_is_citable_and_suggested(self):
        m=measurements(.05,.055)
        for side,peak in [('reference',17.3),('candidate',13.6)]:
            m[side]['regions']['head']['bands']['low'].update({'dominant_hz':peak,'frequency_resolution_hz':.25,'peak_power_share':.8})
        e=build_evidence(ROIPlan.model_validate(PLAN),{},m,[('low',1,30)],{})
        row=e['region_comparisons'][0]
        self.assertEqual(row['frequency_shift_hz'],3.7)
        self.assertEqual(e['rule_prefilter']['frequency_change_evidence_ids'],['head:low'])
        d=validate_diagnosis(diag('suspected_abnormal',[{'evidence_id':'head:low','metric':'frequency_shift_hz','value':3.7}]),e)
        self.assertEqual(final_decision(d,e)['label'],'abnormal')


class Registration(unittest.TestCase):
    def test_recovers_known_similarity_and_transfers_rois(self):
        rng = np.random.default_rng(1)
        img = (rng.random((360, 480)) * 255).astype(np.uint8)
        img = cv2.GaussianBlur(img, (0, 0), 1.5)
        for _ in range(80):
            x, y = int(rng.integers(0, 440)), int(rng.integers(0, 320))
            cv2.rectangle(img, (x, y), (x + int(rng.integers(5, 30)), y + int(rng.integers(5, 30))), int(rng.integers(0, 255)), -1)
        M = cv2.getRotationMatrix2D((240, 180), 2.0, 1.1); M[:, 2] += [6, -4]
        moved = cv2.warpAffine(img, M, (480, 360), borderMode=cv2.BORDER_REFLECT)
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a.png', Path(tmp) / 'b.png'
            cv2.imwrite(str(a), img); cv2.imwrite(str(b), moved)
            r = register(a, b)
        self.assertTrue(r['ok'])
        self.assertAlmostEqual(r['scale'], 1.1, delta=.01)
        self.assertAlmostEqual(r['rotation_deg'], -2.0, delta=.3)  # OpenCV positive angle = counter-clockwise in image coords
        plan, report = transfer_plan(ROIPlan.model_validate(PLAN), r, [480, 360], [480, 360])
        self.assertTrue(report['applied'])
        head = next(x for x in plan.regions if x.id == 'head')
        self.assertEqual(head.candidate.shape, 'polygon')  # rotated -> polygon
        x, y = head.reference.bounds_xywh[:2]
        expected = M[:, :2] @ np.array([x * 480, y * 360]) + M[:, 2]
        self.assertTrue(np.allclose(np.array(head.candidate.points_xy[0]) * [480, 360], expected, atol=2.0))

    def test_failed_registration_keeps_model_geometry(self):
        plan = ROIPlan.model_validate(PLAN)
        same, report = transfer_plan(plan, {'ok': False, 'reason': 'x'}, [320, 240], [320, 240])
        self.assertIs(same, plan); self.assertFalse(report['applied'])


class DiagnosisRepair(unittest.TestCase):
    def test_wrong_number_gets_one_repair_call(self):
        class Sloppy(SyntheticProvider):
            def diagnose(self, evidence, images):
                d = super().diagnose(evidence, images)
                if 'previous_answer_rejected' not in evidence:
                    data = d.model_dump(); data['decision_basis'][0]['value'] = 999.0
                    return Diagnosis.model_validate(data)
                return d
        with tempfile.TemporaryDirectory() as tmp:
            a, b = videos(Path(tmp) / 'input', frames=180); provider = Sloppy()
            result = run_pipeline(a, b, capture_fps=120, output_dir=Path(tmp) / 'run', provider=provider, bands=[('rotation', 20, 27)])
            self.assertEqual([c['stage'] for c in provider.calls], ['roi', 'diagnosis', 'diagnosis'])
            self.assertTrue((Path(result['output_dir']) / 'diagnosis_rejection_0.json').exists())
            self.assertEqual(result['decision']['label'], 'abnormal')


class TrackingQuality(unittest.TestCase):
    def test_flickering_points_are_excluded(self):
        """Points under a patch whose brightness flickers (blades behind a guard) must not be measured."""
        from devday.vendor.fanvib.pipeline import analyze_video
        rng = np.random.default_rng(3)
        base = np.full((120, 160, 3), 40, np.uint8)
        for _ in range(120):
            x, y = int(rng.integers(2, 155)), int(rng.integers(2, 115)); cv2.rectangle(base, (x, y), (x + 2, y + 2), (230, 230, 230), -1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'v.avi'
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 30, (160, 120))
            for i in range(120):
                f = base.copy()
                gain = 1 + .5 * np.sin(2 * np.pi * 30 * i / 120)  # 30 Hz brightness flicker on the right half
                f[:, 80:] = np.clip(f[:, 80:].astype(float) * gain, 0, 255).astype(np.uint8)
                w.write(f)
            w.release()
            cfg = {'capture_fps': 120, 'nperseg': 64, 'use_container_timestamps': False,
                   'rois': [{'id': 'bg', 'role': 'background', 'shape': 'rectangle', 'bounds': [2, 2, 70, 116]},
                            {'id': 'flick', 'role': 'target', 'shape': 'rectangle', 'bounds': [90, 2, 66, 116]}]}
            r = analyze_video(path, cfg, Path(tmp) / 'out')
        self.assertGreater(r['regions']['flick']['flicker_excluded_points'], 0)
        self.assertEqual(r['regions']['flick']['retained_points'], 0)
        self.assertEqual(r['regions']['bg']['flicker_excluded_points'], 0)


if __name__ == '__main__':
    unittest.main()


class DiagnosisSDKTransport(unittest.TestCase):
    def test_real_sdk_serializes_strict_diagnosis_schema(self):
        import httpx
        from openai import OpenAI
        from devday.providers import OpenAIProvider
        answer = diag('inconclusive', []).model_dump()
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'resp_fixture', 'object': 'response', 'created_at': 0, 'status': 'completed', 'error': None, 'incomplete_details': None, 'model': 'gpt-6-luna', 'output': [{'type': 'message', 'id': 'msg', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': json.dumps(answer), 'annotations': []}]}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
            with OpenAI(api_key='test-key-not-real', http_client=transport, max_retries=0) as client:
                provider = OpenAIProvider(client=client)
                result = provider.diagnose({'allowed_evidence_ids': []}, [])
        self.assertIsInstance(result, Diagnosis)
        self.assertEqual(captured[0]['model'], 'gpt-6-luna')
        self.assertIn('decision_basis', captured[0]['text']['format']['schema']['properties'])
        self.assertTrue(captured[0]['text']['format']['strict'])


class SetupGate(unittest.TestCase):
    def plan(self, assessment):
        p = copy.deepcopy(PLAN); p['same_setup_assessment'] = assessment; return ROIPlan.model_validate(p)

    def test_uncertain_passes_only_with_verified_registration(self):
        good = {'ok': True, 'scale': .993, 'rotation_deg': -.2, 'inliers': 165}
        e = build_evidence(self.plan('uncertain'), {}, measurements(.05, .2), [('low', 1, 10)], {}, alignment=good)
        self.assertTrue(e['region_comparisons'][0]['eligible_for_interpretation'])
        self.assertEqual(e['setup']['basis'], 'model_uncertain_but_registration_verified')
        self.assertEqual(e['rule_prefilter']['suggested_decision'], 'abnormal')
        for bad in (None, {'ok': False}, dict(good, inliers=20), dict(good, scale=1.2), dict(good, rotation_deg=5)):
            e = build_evidence(self.plan('uncertain'), {}, measurements(.05, .2), [('low', 1, 10)], {}, alignment=bad)
            self.assertFalse(e['region_comparisons'][0]['eligible_for_interpretation'], bad)

    def test_different_always_blocks(self):
        good = {'ok': True, 'scale': 1.0, 'rotation_deg': 0, 'inliers': 500}
        e = build_evidence(self.plan('different'), {}, measurements(.05, .2), [('low', 1, 10)], {}, alignment=good)
        self.assertFalse(e['setup']['eligible']); self.assertEqual(e['allowed_evidence_ids'], [])


class LowConfidenceAndSparse(unittest.TestCase):
    def plan(self, conf):
        p = copy.deepcopy(PLAN); p['regions'][0]['semantic_confidence'] = conf; return ROIPlan.model_validate(p)

    def test_low_confidence_counts_and_flicker_needs_stronger_increase(self):
        e = build_evidence(self.plan('low'), {}, measurements(.05, .4), [('low', 1, 10)], {})
        self.assertTrue(e['region_comparisons'][0]['eligible_for_interpretation'])
        self.assertEqual(e['rule_prefilter']['suggested_decision'], 'abnormal')
        for cand, expected in ((.09, 'no_significant_change'), (.4, 'increase')):  # x1.8 vs x8 on a flicker-affected ROI
            m = measurements(.05, cand); m['candidate']['regions']['head']['flicker_excluded_points'] = 4
            row = build_evidence(self.plan('medium'), {}, m, [('low', 1, 10)], {})['region_comparisons'][0]
            self.assertTrue(row['flicker_strict_thresholds']); self.assertEqual(row['rule_flag'], expected)

    def test_sparse_increase_is_not_reported_as_normal(self):
        m = measurements(.05, .4)
        for side in m.values(): side['regions']['head']['retained_points'] = 5
        e = build_evidence(self.plan('medium'), {}, m, [('low', 1, 10)], {})
        self.assertEqual(e['region_comparisons'][0]['rule_flag'], 'increase')
        self.assertEqual(e['rule_prefilter']['suggested_decision'], 'insufficient')


class ROIPlanRobustness(unittest.TestCase):
    def test_invalid_relative_pairs_are_dropped_not_fatal(self):
        p = copy.deepcopy(PLAN)
        p['relative_pairs'] = [{'a': 'head', 'b': 'background_left'}, {'a': 'head', 'b': 'head'}, {'a': 'head', 'b': 'missing'}]
        plan = ROIPlan.model_validate(p)
        self.assertEqual(plan.relative_pairs, [])
        self.assertTrue(any('dropped invalid relative pairs' in l for l in plan.limitations))

    def test_missing_background_still_rejected_with_roles_listed(self):
        p = copy.deepcopy(PLAN); p['regions'][1]['role'] = 'target'
        with self.assertRaises(ValueError) as ctx:
            ROIPlan.model_validate(p)
        self.assertIn('background_left(target)', str(ctx.exception))

    def test_repair_request_shows_previous_answer(self):
        from devday.providers import AnthropicProvider
        bad = copy.deepcopy(PLAN); bad['regions'][1]['role'] = 'target'
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(id='x', stop_reason='end_turn', usage=None,
                                 content=[SimpleNamespace(type='text', text=json.dumps(bad))])))
        with self.assertRaises(ValueError) as ctx:
            AnthropicProvider(client=client).propose_rois({}, [])
        self.assertIn('--- previous answer ---', str(ctx.exception))
