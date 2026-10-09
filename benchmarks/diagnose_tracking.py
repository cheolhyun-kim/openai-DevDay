"""Export measured tracking losses, including failures, from a completed benchmark.

Usage: python -X utf8 benchmarks/diagnose_tracking.py results/<run>
This reads existing artifacts; it does not rerun or change analysis thresholds.
"""
import argparse
from collections import Counter
import csv
import html
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from devday.benchmark.data import read_json, write_json
from devday.benchmark.metrics import prediction
from devday.benchmark.runner import load_run


def tracking_counts(path, configs):
    roles = {r["id"]: r["role"] for r in configs["rois"]}
    with np.load(path, allow_pickle=False) as data:
        labels, tracked, kept, flicker = (data[k] for k in ("labels", "tracked", "keep", "flicker"))
        rows = []
        for roi in sorted(set(labels.tolist())):
            own = labels == roi
            rows.append({"roi_id": str(roi), "role": roles.get(str(roi), "unknown"),
                         "initial_points": int(own.sum()),
                         "tracked_points": int((own & tracked).sum()),
                         "retained_points": int((own & kept).sum()),
                         "median_flicker_gray": float(np.median(flicker[own]))})
        return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    metadata, trials = load_run(run)
    out = run / "diagnostics"
    out.mkdir(exist_ok=True)
    details, cases = [], []
    cache_path = metadata.get("options", {}).get("reference_cache_path")
    reference = []
    if cache_path:
        cache = read_json(cache_path)
        track_path = cache.get("provenance", {}).get("tracks_npz")
        if track_path and Path(track_path).exists():
            config_path = Path(track_path).parents[2] / "configs.json"
            reference = tracking_counts(track_path, read_json(config_path)["reference"])
            details.extend({"case_id": "shared_reference", "side": "reference", **r} for r in reference)
    for trial in sorted(trials, key=lambda r: (r["case_id"], r["repeat"])):
        artifact = Path(trial["run_dir"]) / "artifacts"
        track_path = artifact / "measurements" / "candidate" / "tracks.npz"
        records = []
        if track_path.exists() and (artifact / "configs.json").exists():
            records = tracking_counts(track_path, read_json(artifact / "configs.json")["candidate"])
        details.extend({"case_id": trial["case_id"], "repeat": trial["repeat"], "side": "candidate", **r} for r in records)
        reference_records = reference if cache_path else []
        reference_path = artifact / "measurements" / "reference" / "tracks.npz"
        if not cache_path and reference_path.exists() and (artifact / "configs.json").exists():
            reference_records = tracking_counts(reference_path, read_json(artifact / "configs.json")["reference"])
            details.extend({"case_id": trial["case_id"], "repeat": trial["repeat"], "side": "reference", **r} for r in reference_records)
        obs = trial.get("observation", {})
        case = {"case_id": trial["case_id"], "repeat": trial["repeat"], "condition": trial["condition"],
                "outcome": prediction(trial), "duration_s": trial["duration_s"],
                "measurement_errors": obs.get("measurement_errors", {}), "execution_error": trial.get("error"),
                "tracks_available": bool(records),
                "reference_background_retained_points": sum(r["retained_points"] for r in reference_records if r["role"] == "background") if reference_records else None}
        for role in ("background", "target"):
            for metric in ("initial_points", "tracked_points", "retained_points"):
                case[f"{role}_{metric}"] = sum(r[metric] for r in records if r["role"] == role) if records else None
        cases.append(case)
    write_json(out / "tracking.json", {"reference": reference, "cases": cases, "roi_details": details})
    if details:
        fields = list(dict.fromkeys(k for r in details for k in r))
        with (out / "tracking.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(details)
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    for ax, role in zip(axes, ("background", "target")):
        for metric, label, color in (("initial_points", "Initial features", "#93a5bc"),
                                     ("tracked_points", "Tracked to end", "#327a86"),
                                     ("retained_points", "After brightness filter", "#c87528")):
            values = [np.nan if r[f"{role}_{metric}"] is None else r[f"{role}_{metric}"] for r in cases]
            ax.plot(range(len(cases)), values, "o-", label=label, color=color)
        if role == "background":
            ax.axhline(6, linestyle="--", color="#b32f42", label="Minimum background count (6)")
        ax.set_ylabel(f"{role.title()} points")
        ax.legend(loc="upper left", bbox_to_anchor=(1, 1))
        ax.grid(alpha=.2)
    axes[0].set_title("Fresh candidate tracking: missing artifacts are gaps, not zero")
    axes[-1].set_xticks(range(len(cases)), [r["case_id"] for r in cases], rotation=60, ha="right")
    for ext in ("png", "svg"):
        fig.savefig(out / f"tracking.{ext}", dpi=160, bbox_inches="tight")
    plt.close(fig)
    counts = Counter(r["outcome"] for r in cases)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in
        (r["case_id"], r["condition"], r["outcome"], r["reference_background_retained_points"], r["background_tracked_points"],
         r["background_retained_points"], r["target_retained_points"],
         json.dumps(r["measurement_errors"], ensure_ascii=False) if r["measurement_errors"] else r["execution_error"])) + "</tr>" for r in cases)
    reference_text = ", ".join(f"{r['roi_id']}: {r['initial_points']} → {r['tracked_points']} → {r['retained_points']}"
                               for r in reference if r["role"] == "background")
    page = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>추적 실패 원인 진단</title>
<style>body{{font-family:system-ui;max-width:1200px;margin:32px auto;padding:16px;line-height:1.7}}img{{max-width:100%}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ccc;text-align:left;padding:8px;overflow-wrap:anywhere}}.note{{background:#fff0cd;padding:16px}}</style>
<h1>추적 실패 원인 진단</h1><p>전체 {len(cases)}회 시도: {html.escape(str(dict(counts)))}</p>
<p class="note">이 페이지는 저장된 특징점 추적 자료를 읽어 원인을 분리합니다. 기준·대상 영상의 품질, ROI 선택, 추적 및 필터 설정이 함께 영향을 줍니다.
특징점 개수만으로 진동 측정이 유효하다고 판단할 수 없습니다. 없는 추적 자료는 그래프에서 빈칸으로 표시합니다.</p>
<p>저장된 공통 정상 기준의 배경 점 수 (초기 → 끝까지 추적 → 밝기 필터 후): {html.escape(reference_text) or '자료 없음'}</p>
<p>공통 기준이 실패하면 대상에서 점이 남더라도 비교 근거가 확보되지 않습니다. 이 페이지의 지연시간은 저장된 기준 재사용 시 최초 기준 측정 비용을 포함하지 않습니다.</p>
<img src="tracking.png" alt="대상별 추적점 손실"><p><a href="tracking.svg">SVG 그래프</a> · <a href="tracking.csv">영역별 CSV</a> · <a href="tracking.json">전체 진단 JSON</a> · <a href="../report/report.html">벤치마크 보고서</a></p>
<table><tr><th>사례</th><th>조건</th><th>결과</th><th>기준 배경 유지</th><th>대상 배경 추적</th><th>대상 배경 유지</th><th>대상 유지</th><th>측정 오류</th></tr>{body}</table>
</html>'''
    (out / "report.html").write_text(page, encoding="utf-8")
    print(out / "report.html")


if __name__ == "__main__":
    main()
