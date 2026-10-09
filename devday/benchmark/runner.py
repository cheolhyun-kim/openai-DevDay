"""UI-free trial orchestration using a replaceable module:function adapter."""
from datetime import datetime, timezone
import importlib
import importlib.metadata
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time

from . import SCHEMA
from .data import digest, empty_output, file_hash, read_json, validate_manifest, write_json
from .metrics import validate_observation


def reject_credentials(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if any(word in key.lower() for word in ("api_key", "password", "secret", "access_token")):
                raise ValueError("Supply credentials through environment variables, never benchmark options")
            reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            reject_credentials(child)


def fingerprint(adapter):
    package = Path(__file__).resolve().parents[1]
    source_hashes = {str(p.relative_to(package)): file_hash(p) for p in sorted(package.rglob("*")) if p.is_file() and p.suffix in (".py", ".txt", ".html", ".js", ".css")}
    module = importlib.import_module(adapter.split(":", 1)[0])
    adapter_path = getattr(module, "__file__", None)
    def git(*args):
        process = subprocess.run(["git", *args], cwd=package.parent, capture_output=True, text=True)
        return process.stdout.strip() if process.returncode == 0 else None
    versions = {}
    for name in ("numpy", "scipy", "opencv-python-headless", "opencv-python", "matplotlib", "pydantic", "openai", "anthropic"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"git_commit": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
            "source_digest": digest(source_hashes), "source_hashes": source_hashes,
            "adapter_sha256": file_hash(adapter_path) if adapter_path else None,
            "python": sys.version, "platform": platform.platform(),
            "package_versions": versions,
            "configured_models": {k: os.environ[k] for k in ("DEVDAY_MODEL", "DEVDAY_CLAUDE_MODEL") if k in os.environ}}


def run(dataset_path, output, revision, *, adapter="devday.benchmark.adapters:devday",
        options=None, repeats=5, seed=20261009, split="dev", timeout=600, opencv_threads=None):
    if repeats < 1 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("repeats and timeout must be positive")
    if opencv_threads is not None and (type(opencv_threads) is not int or opencv_threads < 1):
        raise ValueError("opencv_threads must be a positive integer")
    options = dict(options or {"mode": "measurement"})
    reject_credentials(options)
    dataset_path = Path(dataset_path).resolve()
    dataset = read_json(dataset_path)
    source_map, clip_map = validate_manifest(dataset)
    if not dataset.get("dataset_id"):
        raise ValueError("Use prepare first; runner requires an immutable prepared dataset")
    for clip in clip_map.values():
        path = (dataset_path.parent / clip["path"]).resolve()
        if file_hash(path) != clip.get("sha256"):
            raise ValueError(f"Clip content changed: {clip['id']}")
        clip["path"] = str(path)
    selected = [c for c in dataset["cases"] if c["split"] == split]
    if not selected:
        raise ValueError(f"No cases in split={split}")
    if options.get("roi_plan_path"):
        options["roi_plan_path"] = str(Path(options["roi_plan_path"]).resolve())
    # Paths and ROI choices may change between worktrees/variants, but the input
    # clips, source timing and experimental target labels must be identical.
    identity = {"declared_dataset_id": dataset["dataset_id"],
                "recordings": [{k: v for k, v in r.items() if k not in ("path", "probe")} for r in dataset["recordings"]],
                "clips": [{k: v for k, v in c.items() if k != "path"} for c in dataset["clips"]],
                "cases": [{k: v for k, v in c.items() if k != "roi_plan_path"} for c in dataset["cases"]]}
    out = empty_output(output)
    schedule = [(case, repeat) for case in selected for repeat in range(1, repeats + 1)]
    random.Random(seed).shuffle(schedule)
    metadata = {"schema_version": SCHEMA, "revision": revision, "dataset_id": digest(identity),
                "dataset_path": str(dataset_path), "split": split, "repeats": repeats, "seed": seed,
                "timeout_s": timeout, "adapter": adapter, "options": options,
                "opencv_threads": opencv_threads,
                "started_at_utc": datetime.now(timezone.utc).isoformat(), "fingerprint": fingerprint(adapter),
                "roi_plan_hashes": {}, "schedule": [{"case_id": c["id"], "repeat": r} for c, r in schedule]}
    if options.get("roi_plan_path"):
        metadata["roi_plan_hashes"]["default"] = file_hash(options["roi_plan_path"])
    for case in selected:
        if case.get("roi_plan_path"):
            case["roi_plan_path"] = str((dataset_path.parent / case["roi_plan_path"]).resolve())
            metadata["roi_plan_hashes"][case["id"]] = file_hash(case["roi_plan_path"])
    write_json(out / "metadata.json", metadata)
    write_json(out / "dataset.snapshot.json", dataset)
    trials = out / "trials.jsonl"
    trials.touch()
    project_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(project_root) + os.pathsep + environment.get("PYTHONPATH", "")
    rows = []
    for index, (case, repeat) in enumerate(schedule, 1):
        job = out / "runs" / f"r{index:05d}"
        job.mkdir(parents=True)
        a, b = clip_map[case["reference_clip"]], clip_map[case["candidate_clip"]]
        sa, sb = source_map[a["recording_id"]], source_map[b["recording_id"]]
        trial_seed = int(digest([seed, case["id"], repeat])[:8], 16)
        request = {"schema_version": "devday.request/1", "case_id": case["id"], "seed": trial_seed,
                   "reference_video": a["path"], "candidate_video": b["path"],
                   "capture_fps": sa["capture_fps"], "candidate_capture_fps": sb["capture_fps"],
                   "output_dir": str(job / "artifacts"), "options": options,
                   "execution": {"opencv_threads": opencv_threads},
                   "roi_plan_path": None if options.get("mode") == "full" else case.get("roi_plan_path")}
        # Ground truth, condition names and source filenames are not adapter inputs.
        write_json(job / "request.json", request)
        row = {"schema_version": SCHEMA, "case_id": case["id"], "repeat": repeat, "seed": trial_seed,
               "group_id": sb["group_id"], "speed": sb.get("speed", "unknown"),
               "condition": sb.get("condition", "unknown"), "expected": case["expected"],
               "status": "failed", "error": None, "observation": {}, "run_dir": str(job)}
        start = time.perf_counter()
        print(f"[{index}/{len(schedule)}] {case['id']} repeat={repeat}", flush=True)
        try:
            with (job / "worker.log").open("w", encoding="utf-8") as log:
                process = subprocess.run([sys.executable, "-m", "devday.benchmark.worker", "--adapter", adapter,
                                          "--request", str(job / "request.json"), "--observation", str(job / "observation.json")],
                                         cwd=project_root, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            if process.returncode:
                raise RuntimeError(f"Adapter exited {process.returncode}; see runs/{job.name}/worker.log")
            row["observation"] = validate_observation(read_json(job / "observation.json"))
            row["status"] = "ok"
        except subprocess.TimeoutExpired:
            row["error"] = f"Timeout after {timeout}s; worker terminated"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["duration_s"] = time.perf_counter() - start
        with trials.open("a", encoding="utf-8") as stream:
            import json
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        rows.append(row)
        print(f"  {row['status']}: {row['duration_s']:.1f}s", flush=True)
    metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    end_fingerprint = fingerprint(adapter)
    metadata["code_changed_during_run"] = any(end_fingerprint[key] != metadata["fingerprint"][key]
                                               for key in ("source_digest", "adapter_sha256"))
    write_json(out / "metadata.json", metadata)
    if metadata["code_changed_during_run"]:
        raise ValueError("Code changed during benchmark; trials retained but report/comparison invalid")
    return metadata, rows


def load_run(path):
    import json
    path = Path(path)
    metadata = read_json(path / "metadata.json")
    if metadata.get("schema_version") != SCHEMA:
        raise ValueError("Unknown benchmark run schema")
    if metadata.get("code_changed_during_run"):
        raise ValueError("Code changed during benchmark; rerun with a fixed revision")
    rows = [json.loads(line) for line in (path / "trials.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != len(metadata["schedule"]):
        raise ValueError("Interrupted/incomplete benchmark: planned trials missing; do not report selective results")
    actual = sorted((r["case_id"], r["repeat"]) for r in rows)
    planned = sorted((r["case_id"], r["repeat"]) for r in metadata["schedule"])
    if actual != planned or len(actual) != len(set(actual)):
        raise ValueError("Missing or duplicate trials")
    for row in rows:
        if row.get("schema_version") != SCHEMA or row.get("status") not in ("ok", "failed"):
            raise ValueError("Unknown trial schema/status")
        if row["status"] == "ok":
            validate_observation(row["observation"])
    return metadata, rows
