import argparse
from pathlib import Path

from .data import empty_output, init_manifest, prepare, probe, read_json, register_existing, write_json


def threshold_value(value):
    import math
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("threshold must be finite and nonnegative")
    return value


def fps_value(value):
    value = threshold_value(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("capture FPS must be positive")
    return value


def demo(output):
    """Exercise real tracking on known motion; never call an external AI."""
    from devday.demo import PLAN, videos
    from .runner import run
    from .report import render
    out = empty_output(output)
    a, b = videos(out / "input", frames=720)
    write_json(out / "roi.json", PLAN)
    sources = [{"id": "s0", "path": str(a), "speed": "synthetic", "condition": "normal", "group_id": "synthetic-session-01", "capture_fps": 120, "frame_preservation_verified": True},
               {"id": "s1", "path": str(b), "speed": "synthetic", "condition": "known_motion_increase", "group_id": "synthetic-session-01", "capture_fps": 120, "frame_preservation_verified": True}]
    clips = [{"id": "c0", "recording_id": "s0", "start_frame": 0, "end_frame": 360},
             {"id": "c1", "recording_id": "s0", "start_frame": 360, "end_frame": 720},
             {"id": "c2", "recording_id": "s1", "start_frame": 0, "end_frame": 360}]
    cases = [{"id": "case01", "reference_clip": "c0", "candidate_clip": "c1", "split": "dev", "expected": "unchanged", "roi_plan_path": "roi.json"},
             {"id": "case02", "reference_clip": "c0", "candidate_clip": "c2", "split": "dev", "expected": "changed", "roi_plan_path": "roi.json"}]
    write_json(out / "manifest.json", {"schema_version": "devday.dataset/1", "recordings": sources, "clips": clips, "cases": cases})
    dataset = prepare(out / "manifest.json", out / "dataset")
    options = {"mode": "measurement", "bands": [["rotation", 20, 27]], "align_candidate": False,
               "conditions": {"same_speed_confirmed": True, "fixed_camera_confirmed": True}}
    metadata, rows = run(dataset, out / "baseline", "synthetic-measurement", options=options, repeats=2)
    render(out / "baseline" / "report", metadata, rows)
    return out / "baseline" / "report" / "report.html"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reproducible video benchmark; no API calls until run with full/fixed-roi mode")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a 12-condition manifest template; no video data moved")
    init.add_argument("--out", required=True)
    inspect = sub.add_parser("probe", help="Read container/frame information; cannot infer true capture FPS")
    inspect.add_argument("videos", nargs="+")
    inspect.add_argument("--capture-fps", type=fps_value)
    crop = sub.add_parser("prepare", help="Produce frame-preserving lossless clips")
    crop.add_argument("--manifest", required=True)
    crop.add_argument("--out", required=True)
    register = sub.add_parser("register", help="Register already-cut complete files; preserve original bytes and VFR timestamps")
    register.add_argument("--manifest", required=True)
    register.add_argument("--out", required=True)
    execute = sub.add_parser("run", help="Run repeated trials; default mode measurement (offline)")
    execute.add_argument("--dataset", required=True)
    execute.add_argument("--out", required=True)
    execute.add_argument("--revision", required=True)
    execute.add_argument("--adapter", default="devday.benchmark.adapters:devday")
    execute.add_argument("--options", help="JSON file with adapter options; credentials via environment only")
    execute.add_argument("--repeats", type=int, default=5)
    execute.add_argument("--seed", type=int, default=20261009)
    execute.add_argument("--split", choices=["dev", "test"], default="dev")
    execute.add_argument("--timeout", type=float, default=600)
    execute.add_argument("--opencv-threads", type=int, help="Optional fixed OpenCV thread count; recorded separately from analysis thresholds")
    execute.add_argument("--threshold", type=threshold_value, help="Optional pre-frozen score threshold; otherwise service final verdict")
    summary = sub.add_parser("summarize", help="Regenerate report without rerunning analysis")
    summary.add_argument("run_dir")
    summary.add_argument("--out", required=True)
    summary.add_argument("--threshold", type=threshold_value)
    compare = sub.add_parser("compare", help="Paired comparison of exactly matching data/trials")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--out", required=True)
    compare.add_argument("--threshold", type=threshold_value)
    smoke = sub.add_parser("demo", help="Offline synthetic measurement benchmark and charts")
    smoke.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_manifest(args.out)
            print(Path(args.out).resolve())
        elif args.command == "probe":
            for video in args.videos:
                import json
                print(json.dumps({"video": video, **probe(video, args.capture_fps)}, ensure_ascii=False, indent=2))
        elif args.command == "prepare":
            print(prepare(args.manifest, args.out))
        elif args.command == "register":
            print(register_existing(args.manifest, args.out))
        elif args.command == "demo":
            print(demo(args.out))
        else:
            from .runner import run, load_run
            from .report import render, render_comparison
            if args.command == "run":
                options = read_json(args.options) if args.options else {"mode": "measurement"}
                metadata, rows = run(args.dataset, args.out, args.revision, adapter=args.adapter, options=options,
                                     repeats=args.repeats, seed=args.seed, split=args.split, timeout=args.timeout, opencv_threads=args.opencv_threads)
                render(Path(args.out) / "report", metadata, rows, args.threshold)
                print(Path(args.out).resolve() / "report" / "report.html")
            elif args.command == "summarize":
                metadata, rows = load_run(args.run_dir)
                render(empty_output(args.out), metadata, rows, args.threshold)
                print(Path(args.out).resolve() / "report.html")
            else:
                from .metrics import compare_runs
                a_meta, a_rows = load_run(args.left)
                b_meta, b_rows = load_run(args.right)
                comparison = compare_runs(a_meta, a_rows, b_meta, b_rows, args.threshold)
                render_comparison(empty_output(args.out), comparison)
                print(Path(args.out).resolve() / "report.html")
        return 0
    except Exception as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
