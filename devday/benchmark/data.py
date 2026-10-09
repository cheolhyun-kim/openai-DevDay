"""Frame-indexed datasets. Crops are lossless and tracking restarts per crop."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

import cv2
import numpy as np


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def empty_output(path):
    path = Path(path).resolve()
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"Use a new/empty output directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def regular_timestamps(times):
    dt = np.diff(times)
    return bool(len(dt) and np.all(np.isfinite(dt)) and np.all(dt > 0) and np.std(dt) / np.mean(dt) <= .01)


def probe(path, capture_fps=None):
    cap = cv2.VideoCapture(str(Path(path).resolve()))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {path}")
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        playback = float(cap.get(cv2.CAP_PROP_FPS))
        ok, frame = cap.read()
        if not ok:
            raise ValueError(f"Cannot decode video: {path}")
        return {"frame_count": n, "playback_fps": playback,
                "decoded_size": [frame.shape[1], frame.shape[0]],
                "rotation_metadata_deg": float(cap.get(cv2.CAP_PROP_ORIENTATION_META)),
                "playback_duration_estimate_s": n / playback if playback > 0 else None,
                "capture_duration_if_preserved_s": n / capture_fps if capture_fps else None,
                "note": "Container FPS/duration do not verify capture FPS or frame preservation."}
    finally:
        cap.release()


def validate_manifest(manifest, require_fps=True):
    if manifest.get("schema_version") != "devday.dataset/1":
        raise ValueError("Expected devday.dataset/1")
    sources = manifest.get("recordings", [])
    clips = manifest.get("clips", [])
    cases = manifest.get("cases", [])
    for kind, rows in [("recordings", sources), ("clips", clips), ("cases", cases)]:
        ids = [r["id"] for r in rows]
        if len(ids) != len(set(ids)) or not all(isinstance(x, str) and x for x in ids):
            raise ValueError(f"Invalid/duplicate {kind} IDs")
    source_map = {r["id"]: r for r in sources}
    clip_map = {r["id"]: r for r in clips}
    intervals = {}
    for source in sources:
        if not source.get("path") or not source.get("speed") or not source.get("condition"):
            raise ValueError("Recording needs path, speed and condition")
        fps = source.get("capture_fps")
        if (require_fps or fps is not None) and (isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0):
            raise ValueError(f"{source['id']}: supply confirmed positive capture_fps")
        if not source.get("group_id"):
            raise ValueError("Every recording needs a capture-session group_id")
    for clip in clips:
        if clip["recording_id"] not in source_map:
            raise ValueError("Unknown recording_id")
        a, b = clip["start_frame"], clip["end_frame"]
        if type(a) is not int or type(b) is not int or a < 0 or b - a < 8:
            raise ValueError("Clips need integer [start_frame,end_frame) with >=8 frames")
        intervals.setdefault(clip["recording_id"], []).append((a, b))
    gap = manifest.get("minimum_gap_frames", 0)
    if type(gap) is not int or gap < 0:
        raise ValueError("minimum_gap_frames must be a nonnegative integer")
    for spans in intervals.values():
        spans.sort()
        if any(right[0] - left[1] < gap for left, right in zip(spans, spans[1:])):
            raise ValueError("Overlapping clips or insufficient gap in one recording")
    split_by_source, split_by_group = {}, {}
    for case in cases:
        split = case["split"]
        if split not in ("dev", "test") or case["expected"] not in ("changed", "unchanged", "unknown"):
            raise ValueError("Use split dev/test and expected changed/unchanged/unknown")
        if case["reference_clip"] == case["candidate_clip"]:
            raise ValueError("Self-comparisons cannot serve as negative controls")
        pair = []
        for key in ("reference_clip", "candidate_clip"):
            if case[key] not in clip_map:
                raise ValueError("Unknown clip ID")
            source = source_map[clip_map[case[key]]["recording_id"]]
            pair.append(source)
            for mapping, unit in [(split_by_source, source["id"]), (split_by_group, source["group_id"])]:
                if unit in mapping and mapping[unit] != split:
                    raise ValueError("Data leakage: a recording/session, including references, crosses dev/test")
                mapping[unit] = split
        if pair[0].get("speed") != pair[1].get("speed"):
            raise ValueError("Reference and candidate must have the same speed")
        if pair[0]["group_id"] != pair[1]["group_id"]:
            raise ValueError("Pair must share a capture-session group (record separate sessions as separate cases)")
    return source_map, clip_map


def init_manifest(path):
    path = Path(path)
    if path.exists():
        raise ValueError("Manifest already exists")
    recordings = []
    for speed in ("weak", "medium", "strong"):
        for condition in ("normal", "cover_removed_1", "cover_removed_2", "coin"):
            recordings.append({"id": f"{speed}_{condition}", "path": f"input/{speed}_{condition}.mov",
                               "speed": speed, "condition": condition, "group_id": "session-01",
                               "capture_fps": None, "frame_preservation_verified": False})
    write_json(path, {"schema_version": "devday.dataset/1", "name": "fan-poc", "minimum_gap_frames": 0,
                      "recordings": recordings, "clips": [], "cases": []})


def register_existing(manifest_path, output):
    """Register already-cut videos with byte-identical copies and original PTS.

    Each clip must cover its complete input file. Use prepare for subranges.
    This path needs no FFmpeg and cannot replace VFR timestamps with CFR.
    """
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    source_map, _ = validate_manifest(manifest)
    if not manifest["clips"] or not manifest["cases"]:
        raise ValueError("Add clips and cases to the manifest before registering")
    out = empty_output(output)
    prepared = {**manifest, "recordings": [], "clips": []}
    used = {c["recording_id"] for c in manifest["clips"]}
    for source in source_map.values():
        if source["id"] not in used:
            continue
        path = (manifest_path.parent / source["path"]).resolve()
        prepared["recordings"].append({**source, "path": str(path), "sha256": file_hash(path),
                                         "probe": probe(path, source["capture_fps"])})
    records = {r["id"]: r for r in prepared["recordings"]}
    for index, clip in enumerate(manifest["clips"]):
        source = records[clip["recording_id"]]
        if clip["start_frame"] != 0 or clip["end_frame"] != source["probe"]["frame_count"]:
            raise ValueError("register requires whole-file clips; use prepare for subranges")
        original = Path(source["path"])
        path = out / "clips" / f"c{index:05d}{original.suffix.lower()}"
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(original, path)
        copied_hash = file_hash(path)
        if copied_hash != source["sha256"]:
            raise ValueError("Input changed while copying")
        prepared["clips"].append({**clip, "path": str(path), "sha256": copied_hash,
                                     "timing_backend": "original_file_byte_copy",
                                     "capture_duration_if_preserved_s": clip["end_frame"] / source["capture_fps"]})
        print(f"Registered {clip['id']}: original bytes/timestamps preserved", flush=True)
    prepared["cases"] = [dict(case) for case in manifest["cases"]]
    for case in prepared["cases"]:
        if case.get("roi_plan_path"):
            case["roi_plan_path"] = str((manifest_path.parent / case["roi_plan_path"]).resolve())
    prepared["dataset_id"] = digest({"manifest": manifest,
                                      "sources": [(r["id"], r["sha256"]) for r in prepared["recordings"]],
                                      "clips": [(c["id"], c["sha256"]) for c in prepared["clips"]]})
    write_json(out / "dataset.json", prepared)
    return out / "dataset.json"


def prepare(manifest_path, output):
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    source_map, _ = validate_manifest(manifest)
    if not manifest["clips"] or not manifest["cases"]:
        raise ValueError("Add clips and cases to the manifest before preparing")
    out = empty_output(output)
    prepared = dict(manifest)
    prepared["recordings"] = []
    prepared["clips"] = []
    for source in source_map.values():
        source_path = (manifest_path.parent / source["path"]).resolve()
        selected = sorted([c for c in manifest["clips"] if c["recording_id"] == source["id"]], key=lambda c: c["start_frame"])
        if not selected:
            continue
        info = probe(source_path, source["capture_fps"])
        if selected[-1]["end_frame"] > info["frame_count"]:
            raise ValueError(f"Clip exceeds frame count: {source['id']}")
        prepared["recordings"].append({**source, "path": str(source_path), "sha256": file_hash(source_path), "probe": info})
        cap = cv2.VideoCapture(str(source_path))
        frame_index = 0
        ffmpeg = shutil.which("ffmpeg")
        try:
            # Sequential decoding avoids unreliable GOP/VFR seeking. OpenCV's
            # upright decoded frames are persisted with no rotation metadata.
            for clip in selected:
                suffix = ".mov" if ffmpeg else ".avi"
                clip_path = out / "clips" / f"c{len(prepared['clips']):05d}{suffix}"
                clip_path.parent.mkdir(exist_ok=True)
                if ffmpeg:
                    # PNG-in-MOV is lossless, keeps a microsecond timestamp grid,
                    # and is readable by the existing pipeline. Do not use CFR,
                    # stream-copy seeking, interpolation or low-precision MKV PTS.
                    args = [ffmpeg, "-nostdin", "-v", "error", "-n", "-i", str(source_path),
                            "-map", "0:v:0", "-vf", f"trim=start_frame={clip['start_frame']}:end_frame={clip['end_frame']},setpts=PTS-STARTPTS",
                            "-fps_mode", "passthrough", "-c:v", "png", "-pix_fmt", "rgb24",
                            "-video_track_timescale", "1000000", "-map_metadata", "-1",
                            "-metadata:s:v:0", "rotate=0", "-an", "-sn", "-dn", str(clip_path)]
                    subprocess.run(args, check=True, capture_output=True, timeout=1800)
                    written = probe(clip_path)["frame_count"]
                    if written != clip["end_frame"] - clip["start_frame"]:
                        raise ValueError("FFmpeg crop frame count mismatch")
                    prepared["clips"].append({**clip, "path": str(clip_path), "sha256": file_hash(clip_path),
                                             "timing_backend": "ffmpeg_passthrough_png_mov",
                                             "capture_duration_if_preserved_s": written / source["capture_fps"]})
                    print(f"Prepared {clip['id']}: {written} frames (timestamp passthrough)", flush=True)
                    continue
                writer = None
                written = 0
                timestamps = []
                try:
                    while frame_index < clip["end_frame"]:
                        ok, frame = cap.read()
                        if not ok:
                            raise ValueError(f"Decode failed at source frame {frame_index}")
                        if frame_index >= clip["start_frame"]:
                            timestamps.append(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000)
                            if writer is None:
                                writer = cv2.VideoWriter(str(clip_path), cv2.VideoWriter_fourcc(*"FFV1"),
                                                         info["playback_fps"], (frame.shape[1], frame.shape[0]))
                                if not writer.isOpened():
                                    raise RuntimeError("Lossless FFV1 codec unavailable; no lossy fallback")
                            writer.write(frame)
                            written += 1
                        frame_index += 1
                finally:
                    if writer is not None:
                        writer.release()
                if written != clip["end_frame"] - clip["start_frame"] or probe(clip_path)["frame_count"] != written:
                    raise ValueError("Written clip frame count mismatch")
                if not regular_timestamps(timestamps):
                    raise ValueError("Variable/unknown frame timing: install FFmpeg for timestamp-preserving crops; refusing a CFR substitute")
                prepared["clips"].append({**clip, "path": str(clip_path), "sha256": file_hash(clip_path),
                                         "timing_backend": "opencv_verified_regular_pts_ffv1",
                                         "capture_duration_if_preserved_s": written / source["capture_fps"]})
                print(f"Prepared {clip['id']}: {written} frames", flush=True)
        finally:
            cap.release()
    # ROI plans may be case-specific; resolve relative paths before relocating the manifest.
    for case in prepared["cases"]:
        if case.get("roi_plan_path"):
            case["roi_plan_path"] = str((manifest_path.parent / case["roi_plan_path"]).resolve())
    prepared["dataset_id"] = digest({"manifest": manifest,
                                      "sources": [(r["id"], r["sha256"]) for r in prepared["recordings"]],
                                      "clips": [(c["id"], c["sha256"]) for c in prepared["clips"]]})
    write_json(out / "dataset.json", prepared)
    return out / "dataset.json"
