import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from devday.benchmark.adapters import extract, measurement_only, measurement_signature
from devday.benchmark.data import file_hash, prepare, probe, read_json, register_existing, regular_timestamps, validate_manifest, write_json
from devday.benchmark.metrics import compare_runs, prediction, summarize, validate_observation
from devday.benchmark.runner import load_run, run


def manifest():
    return {"schema_version": "devday.dataset/1", "recordings": [
        {"id": "normal", "path": "normal.avi", "speed": "weak", "condition": "normal", "group_id": "session01", "capture_fps": 120},
        {"id": "coin", "path": "coin.avi", "speed": "weak", "condition": "coin", "group_id": "session01", "capture_fps": 120}],
        "clips": [{"id": "c0", "recording_id": "normal", "start_frame": 0, "end_frame": 12},
                  {"id": "c1", "recording_id": "normal", "start_frame": 12, "end_frame": 24},
                  {"id": "c2", "recording_id": "coin", "start_frame": 0, "end_frame": 12}],
        "cases": [{"id": "k0", "reference_clip": "c0", "candidate_clip": "c1", "expected": "unchanged", "split": "dev"},
                  {"id": "k1", "reference_clip": "c0", "candidate_clip": "c2", "expected": "changed", "split": "dev"}]}


def observation(decision="normal", score=0.0):
    return {"schema_version": "devday.observation/1", "decision": decision, "score": score, "measurement_available": True}


def trial(case="k0", expected="unchanged", decision="normal", repeat=1, status="ok", group="session01"):
    return {"schema_version": "devday.benchmark/1", "case_id": case, "expected": expected, "repeat": repeat,
            "seed": repeat, "group_id": group, "speed": "weak", "condition": "normal",
            "status": status, "duration_s": 1, "observation": observation(decision), "error": None}


class DatasetTests(unittest.TestCase):
    def test_existing_clip_registration_preserves_exact_file_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = manifest()
            value["clips"] = [value["clips"][0], value["clips"][2]]
            value["cases"] = [value["cases"][1]]
            for name in ("normal", "coin"):
                writer = cv2.VideoWriter(str(root / f"{name}.avi"), cv2.VideoWriter_fourcc(*"FFV1"), 30, (32, 24))
                self.assertTrue(writer.isOpened())
                try:
                    for i in range(12):
                        writer.write(np.full((24, 32, 3), i * 8, np.uint8))
                finally:
                    writer.release()
            write_json(root / "manifest.json", value)
            registered = read_json(register_existing(root / "manifest.json", root / "registered"))
            for clip in registered["clips"]:
                self.assertEqual(file_hash(root / f"{clip['recording_id']}.avi"), file_hash(clip["path"]))
                self.assertEqual(clip["timing_backend"], "original_file_byte_copy")
            value["clips"][0]["end_frame"] = 10
            write_json(root / "partial.json", value)
            with self.assertRaisesRegex(ValueError, "whole-file"):
                register_existing(root / "partial.json", root / "rejected")

    def test_session_leakage_including_shared_reference_is_rejected(self):
        value = manifest()
        value["cases"][1]["split"] = "test"
        with self.assertRaisesRegex(ValueError, "leakage"):
            validate_manifest(value)

    def test_different_recordings_in_same_session_still_cannot_cross_splits(self):
        value = manifest()
        for clip_id, source in [("c3", "coin"), ("c4", "coin")]:
            start = 12 if clip_id == "c3" else 24
            value["clips"].append({"id": clip_id, "recording_id": source, "start_frame": start, "end_frame": start + 12})
        value["cases"][1].update(reference_clip="c3", candidate_clip="c4", split="test")
        with self.assertRaisesRegex(ValueError, "leakage"):
            validate_manifest(value)

    def test_overlap_fps_speed_and_self_comparison_rejected(self):
        for field in ("overlap", "fps", "speed", "self"):
            value = manifest()
            if field == "overlap":
                value["clips"][1]["start_frame"] = 8
            elif field == "fps":
                value["recordings"][0]["capture_fps"] = None
            elif field == "speed":
                value["recordings"][1]["speed"] = "strong"
            else:
                value["cases"][0]["candidate_clip"] = "c0"
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_manifest(value)

    def test_lossless_frame_index_crop_preserves_selected_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("normal", "coin"):
                writer = cv2.VideoWriter(str(root / f"{name}.avi"), cv2.VideoWriter_fourcc(*"FFV1"), 30, (32, 24))
                self.assertTrue(writer.isOpened())
                try:
                    for i in range(24):
                        writer.write(np.full((24, 32, 3), i * 8, np.uint8))
                finally:
                    writer.release()
            write_json(root / "manifest.json", manifest())
            with patch("devday.benchmark.data.shutil.which", return_value=None):
                path = prepare(root / "manifest.json", root / "prepared")
            value = read_json(path)
            crop = next(c for c in value["clips"] if c["id"] == "c1")
            self.assertEqual(probe(crop["path"])["frame_count"], 12)
            cap = cv2.VideoCapture(crop["path"])
            try:
                for i in range(12, 24):
                    ok, image = cap.read()
                    self.assertTrue(ok)
                    self.assertTrue(np.all(image == i * 8))
                self.assertFalse(cap.read()[0])
            finally:
                cap.release()
            self.assertEqual(file_hash(crop["path"]), crop["sha256"])

    def test_variable_and_unknown_timestamps_not_silently_regularized(self):
        self.assertTrue(regular_timestamps([i / 30 for i in range(12)]))
        self.assertFalse(regular_timestamps([0, .01, .02, .04, .05]))
        self.assertFalse(regular_timestamps([0, 0, 0]))


class MetricTests(unittest.TestCase):
    def test_failure_and_abstention_stay_in_end_to_end_denominator(self):
        rows = [trial("a", "changed", "abnormal"), trial("b", "changed", "insufficient"),
                trial("c", "changed", status="failed"), trial("d", "unchanged", "normal"),
                trial("e", "unchanged", "abnormal")]
        result = summarize(rows)
        self.assertAlmostEqual(result["effective_correctness"], 2 / 5)
        self.assertAlmostEqual(result["conditional_accuracy"], 2 / 3)
        self.assertAlmostEqual(result["end_to_end_changed_hit_rate"], 1 / 3)
        self.assertAlmostEqual(result["false_alarm_rate_all_unchanged"], 1 / 2)
        self.assertAlmostEqual(result["decision_coverage"], 3 / 5)

    def test_final_service_veto_overrides_ai_assessment(self):
        row = trial(decision="insufficient")
        row["observation"]["assessment"] = "suspected_abnormal"
        self.assertEqual(prediction(row), "abstain")

    def test_unknown_labels_do_not_create_accuracy_or_fake_ci(self):
        result = summarize([trial(expected="unknown") for _ in range(50)])
        self.assertIsNone(result["effective_correctness"])
        self.assertIsNone(result["macro_session_completion_ci95"])

    def test_identically_failing_runs_are_not_counted_as_stable_decisions(self):
        result = summarize([trial(repeat=1, status="failed"), trial(repeat=2, status="failed")])
        self.assertEqual(result["repeatability"][0]["pairwise_outcome_agreement"], 1)
        self.assertFalse(result["repeatability"][0]["all_trials_decided_same"])

    def test_version_comparison_rejects_changed_dataset_or_dropped_failure(self):
        meta = {"dataset_id": "d0", "split": "dev", "revision": "v1"}
        other = {**meta, "revision": "v2"}
        rows = [trial(), trial("bad", status="failed")]
        for new_meta, new_rows in [({**other, "dataset_id": "d1"}, rows), (other, rows[:1])]:
            with self.assertRaises(ValueError):
                compare_runs(meta, rows, new_meta, new_rows)
        fixed = copy.deepcopy(rows)
        fixed[1]["status"] = "ok"
        self.assertAlmostEqual(compare_runs(meta, rows, other, fixed)["effective_correctness_delta"], .5)

    def test_invalid_adapter_schema_and_nonfinite_score_rejected(self):
        for value in [{**observation(), "schema_version": "other"}, observation(score=float("nan")),
                      {**observation(), "measurement_available": "yes"}]:
            with self.assertRaises(ValueError):
                validate_observation(value)


class AdapterTests(unittest.TestCase):
    def test_worker_applies_and_records_explicit_thread_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "request.json", {"seed": 1, "execution": {"opencv_threads": 2}})
            (root / "thread_fixture.py").write_text(
                'import cv2\ndef evaluate(request):\n    return {"threads_seen": cv2.getNumThreads()}\n', encoding="utf-8")
            import subprocess
            process = subprocess.run([sys.executable, "-m", "devday.benchmark.worker", "--adapter", "thread_fixture:evaluate",
                "--request", str(root / "request.json"), "--observation", str(root / "observation.json")],
                env={**os.environ, "PYTHONPATH": str(root) + os.pathsep + str(Path(__file__).resolve().parents[1])},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            value = read_json(root / "observation.json")
            self.assertEqual(value["threads_seen"], 2)
            self.assertEqual(value["execution"]["opencv_threads"], 2)

    def test_frozen_reference_signature_rejects_input_config_and_thread_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "reference.avi"
            video.write_bytes(b"original reference")
            config = {"capture_fps": 240, "rois": []}
            bands = [("low", 1, 10)]
            original = measurement_signature(video, config, bands, 18)
            self.assertNotEqual(original, measurement_signature(video, {**config, "capture_fps": 120}, bands, 18))
            self.assertNotEqual(original, measurement_signature(video, config, [("low", 2, 10)], 18))
            self.assertNotEqual(original, measurement_signature(video, config, bands, 1))
            video.write_bytes(b"changed reference")
            self.assertNotEqual(original, measurement_signature(video, config, bands, 18))

    def test_failed_frozen_reference_is_preserved_and_candidate_measured_fresh(self):
        from devday.contracts import ROIPlan
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            geometry = {"shape": "rectangle", "bounds_xywh": [.1, .1, .1, .1], "points_xy": None}
            plan = ROIPlan.model_validate({"object_name": "fixture", "same_setup_assessment": "consistent",
                "regions": [{"id": role, "part_name": role, "role": role, "reference": geometry,
                             "candidate": geometry, "semantic_confidence": "high", "caution": "fixture"}
                            for role in ("target", "background")], "relative_pairs": [], "limitations": []})
            write_json(root / "roi.json", plan.model_dump())
            reference = root / "reference.avi"
            reference.write_bytes(b"reference")
            configs = {"reference": {"capture_fps": 240}, "candidate": {"capture_fps": 240}}
            bands = [("low", 1, 10)]
            failure = {"status": "failed", "error": "reference lacks background", "regions": {}, "relative": {}}
            cache = {"schema_version": "devday.frozen_reference/1", "measurement": failure,
                     "signature": measurement_signature(reference, configs["reference"], bands, cv2.getNumThreads())}
            write_json(root / "cache.json", cache)
            request = {"output_dir": str(root / "artifacts"), "reference_video": str(reference),
                       "candidate_video": str(root / "candidate.avi"), "capture_fps": 240, "candidate_capture_fps": 240}
            options = {"bands": bands, "reference_cache_path": str(root / "cache.json")}
            with patch("devday.video.prepare_video", return_value={"frames": []}), \
                 patch("devday.workflow.check_plan", return_value=configs), \
                 patch("devday.workflow.align_plan", return_value=(plan, {}, {})), \
                 patch("devday.measurement.measure", return_value=({"status": "ok", "regions": {}}, {})) as measure, \
                 patch("devday.measurement.build_evidence", return_value={"rule_prefilter": {"suggested_decision": "insufficient"}}) as evidence:
                measurement_only(request, root / "roi.json", dict(options))
                measure.assert_called_once()
                self.assertEqual(measure.call_args.args[0], request["candidate_video"])
                self.assertEqual(evidence.call_args.args[2]["reference"], failure)
                self.assertTrue(read_json(root / "artifacts" / "result.json")["reference_measurement_reused"])
                reference.write_bytes(b"modified reference")
                request["output_dir"] = str(root / "rejected")
                with self.assertRaisesRegex(ValueError, "signature mismatch"):
                    measurement_only(request, root / "roi.json", dict(options))
                self.assertEqual(measure.call_count, 1)

    def test_v2_final_decision_and_both_change_directions_are_extracted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "evidence.json", {"schema_version": "devday.evidence/2", "region_comparisons": [
                {"eligible_for_interpretation": True, "ratio": .25},
                {"eligible_for_interpretation": False, "ratio": 100}], "rule_prefilter": {"suggested_decision": "normal"}})
            write_json(root / "result.json", {"decision": {"label": "insufficient"}})
            value = extract(root)
            self.assertEqual(value["score"], 2)
            self.assertEqual(value["decision"], "insufficient")
            self.assertEqual(value["eligible_rows"], 1)

    def test_runner_keeps_crashes_and_timeouts_and_blinds_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = manifest()
            value["dataset_id"] = "fixture"
            for clip in value["clips"]:
                path = root / (clip["id"] + ".avi")
                path.write_bytes(b"fixture-video-not-used-by-adapter")
                clip.update(path=str(path), sha256=file_hash(path))
            write_json(root / "dataset.json", value)
            module = root / "fixture_benchmark_adapter.py"
            module.write_text('import time\ndef crash(request):\n    assert "expected" not in request and "condition" not in request\n    raise RuntimeError("expected fixture crash")\ndef slow(request):\n    time.sleep(10)\n', encoding="utf-8")
            sys.path.insert(0, str(root))
            try:
                with patch.dict(os.environ, {"PYTHONPATH": str(root)}):
                    for function, timeout in [("crash", 15), ("slow", .05)]:
                        out = root / function
                        _, rows = run(root / "dataset.json", out, "fixture", adapter=f"fixture_benchmark_adapter:{function}", repeats=1, timeout=timeout)
                        self.assertEqual(len(rows), 2)
                        self.assertTrue(all(r["status"] == "failed" for r in rows))
                        self.assertEqual(len(load_run(out)[1]), 2)
                        request = read_json(Path(rows[0]["run_dir"]) / "request.json")
                        self.assertNotIn("expected", request)
                        self.assertNotIn("condition", request)
                    (root / "crash" / "trials.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "incomplete"):
                        load_run(root / "crash")
            finally:
                sys.path.remove(str(root))
                sys.modules.pop("fixture_benchmark_adapter", None)


if __name__ == "__main__":
    unittest.main()
