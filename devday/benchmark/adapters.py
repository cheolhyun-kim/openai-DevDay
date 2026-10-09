"""Only this file knows application-specific result/evidence fields."""
import math
from pathlib import Path

from .data import digest, file_hash, read_json, write_json


def measurement_signature(video, config, bands, opencv_threads):
    """Validate a frozen baseline against input, configuration and measurement code."""
    package = Path(__file__).resolve().parents[1]
    dependencies = ("contracts.py", "workflow.py", "video.py", "measurement.py", "registration.py", "vendor/fanvib/pipeline.py")
    return digest({"video_sha256": file_hash(video), "config": config, "bands": bands,
                   "opencv_threads": opencv_threads,
                   "software": {name: file_hash(package / name) for name in dependencies}})


def extract(output_dir):
    out = Path(output_dir)
    result = read_json(out / "result.json") if (out / "result.json").exists() else {}
    evidence = read_json(out / "evidence.json")
    if evidence.get("schema_version") not in ("devday.evidence/1", "devday.evidence/2"):
        raise ValueError("Unknown evidence schema; update adapter instead of silently scoring")
    diagnosis = read_json(out / "diagnosis.json") if (out / "diagnosis.json").exists() else result.get("diagnosis", {})
    rows = evidence.get("region_comparisons", [])
    eligible = [r for r in rows if r.get("eligible_for_interpretation")]
    ratios = [r["ratio"] for r in eligible if isinstance(r.get("ratio"), (int, float)) and math.isfinite(r["ratio"]) and r["ratio"] > 0]
    measurements = evidence.get("measurements", {})
    calls = result.get("model_calls", [])
    return {"schema_version": "devday.observation/1", "assessment": diagnosis.get("assessment"),
            "decision": result.get("decision", {}).get("label"),
            "rule_decision": evidence.get("rule_prefilter", {}).get("suggested_decision"),
            "score": max((abs(math.log2(v)) for v in ratios), default=None),
            "score_definition": "max eligible target abs(log2(candidate_rms/reference_rms))",
            "eligible_rows": len(eligible), "total_rows": len(rows),
            "measurement_available": bool(eligible),
            "quality": {side: value.get("quality", {}) for side, value in measurements.items()},
            "measurement_errors": {side: value["error"] for side, value in measurements.items() if value.get("status") == "failed"},
            "reference_measurement_reused": result.get("reference_measurement_reused", False),
            "inspection_roi_ids": [c["roi_id"] for c in diagnosis.get("inspection_candidates", [])],
            "model_call_count": len(calls), "model_calls": calls,
            "provider_mode": result.get("mode", "unknown"),
            "report_html": str(out / "report.html") if (out / "report.html").exists() else None}


def measurement_only(request, roi, options):
    """Reuse the application's measurement/registration/rules, without an AI verdict."""
    from devday.contracts import ROIPlan
    from devday.workflow import check_plan, align_plan
    from devday.video import prepare_video
    from devday.measurement import measure, validate_bands, build_evidence
    out = Path(request["output_dir"])
    out.mkdir(parents=True)
    fps = {"reference": request["capture_fps"], "candidate": request["candidate_capture_fps"]}
    bands = validate_bands(options.pop("bands", None), list(fps.values()))
    count = options.pop("frame_count", 3)
    align = options.pop("align_candidate", True)
    rules = options.pop("rule_thresholds", None)
    conditions = options.pop("conditions", {})
    reference_cache = options.pop("reference_cache_path", None)
    # These options govern AI stages, which this mode does not execute.
    for key in ("model", "roi_repair_attempts", "diagnosis_repair_attempts"):
        options.pop(key, None)
    if options:
        raise ValueError(f"Unsupported measurement options: {sorted(options)}")
    videos = {"reference": request["reference_video"], "candidate": request["candidate_video"]}
    metadata = {s: prepare_video(p, fps[s], out / "frames" / s, count) for s, p in videos.items()}
    plan = ROIPlan.model_validate_json(Path(roi).read_text(encoding="utf-8-sig"))
    check_plan(plan, metadata)
    plan, alignment, transfer = align_plan(plan, metadata, align)
    configs = check_plan(plan, metadata)
    write_json(out / "frames.json", metadata)
    write_json(out / "roi_plan.json", plan.model_dump())
    write_json(out / "configs.json", configs)
    write_json(out / "alignment.json", {"registration": alignment, "roi_transfer": transfer})
    measurements = {}
    for side, path in videos.items():
        if side == "reference" and reference_cache:
            import cv2
            cache = read_json(reference_cache)
            expected = measurement_signature(path, configs[side], bands, cv2.getNumThreads())
            if cache.get("schema_version") != "devday.frozen_reference/1" or cache.get("signature") != expected:
                raise ValueError("Frozen reference input/config/code/thread signature mismatch")
            measurements[side] = cache["measurement"]
            continue
        try:
            measurements[side], _ = measure(path, configs[side], out / "measurements" / side, bands)
        except ValueError as exc:
            measurements[side] = {"status": "failed", "error": str(exc), "regions": {}, "relative": {}}
    api_meta = {s: {k: v for k, v in m.items() if k != "frames"} for s, m in metadata.items()}
    evidence = build_evidence(plan, api_meta, measurements, bands, conditions, alignment=alignment, rules=rules)
    if reference_cache:
        evidence["frozen_reference"] = {"cache_path": str(Path(reference_cache).resolve()),
                                         "signature": cache["signature"], "provenance": cache.get("provenance")}
    write_json(out / "evidence.json", evidence)
    write_json(out / "result.json", {"mode": "measurement_only", "model_calls": [],
                                     "reference_measurement_reused": bool(reference_cache),
                                     "decision": {"label": evidence["rule_prefilter"]["suggested_decision"]}})


def devday(request):
    from devday.workflow import run_pipeline
    options = dict(request.get("options", {}))
    mode = options.pop("mode", "measurement")
    provider_kind = options.pop("provider_kind", "openai")
    if mode not in ("measurement", "fixed-roi", "full", "synthetic"):
        raise ValueError("mode must be measurement/fixed-roi/full/synthetic")
    option_roi = options.pop("roi_plan_path", None)
    roi = request.get("roi_plan_path") or option_roi
    if mode in ("measurement", "fixed-roi") and not roi:
        raise ValueError("measurement/fixed-roi requires reviewed roi_plan_path")
    if mode == "full" and roi:
        raise ValueError("full must propose fresh ROIs; use fixed-roi for saved plans")
    if mode == "measurement":
        measurement_only(request, roi, options)
        return extract(request["output_dir"])
    if mode == "synthetic":
        from devday.demo import SyntheticProvider
        provider = SyntheticProvider()
    else:
        from devday.providers import make_provider
        provider = make_provider(provider_kind, options.pop("model", None))
    run_pipeline(request["reference_video"], request["candidate_video"],
                 capture_fps=request["capture_fps"], candidate_capture_fps=request["candidate_capture_fps"],
                 output_dir=request["output_dir"], roi_plan_path=roi, provider=provider,
                 **options)
    return extract(request["output_dir"])
