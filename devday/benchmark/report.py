"""Portable HTML report and publication/export friendly PNG/SVG figures."""
from collections import defaultdict
import csv
import html
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import numpy as np

from .data import write_json
from .metrics import prediction, summarize


COLORS = {"unchanged": "#2b7a78", "changed": "#db6f24", "abstain": "#a6aebb", "failed": "#b32f42"}
METRICS = ["completion_rate", "measurement_coverage", "decision_coverage", "effective_correctness",
           "conditional_accuracy", "end_to_end_changed_hit_rate", "false_alarm_rate_all_unchanged",
           "abstention_rate", "failure_rate"]


def number(value):
    return "N/A" if value is None else f"{value:.1%}"


def save_figure(fig, out, name):
    for ext in ("png", "svg"):
        fig.savefig(out / f"{name}.{ext}", dpi=160, bbox_inches="tight")
    plt.close(fig)


def render(output, metadata, rows, threshold=None):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows, threshold)
    summary["decision_source"] = "score_threshold" if threshold is not None else "service_final_decision"
    summary["score_threshold"] = threshold
    write_json(out / "summary.json", summary)
    # Use opaque case IDs for chart labels; full condition names stay in the HTML/CSV.
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["case_id"]].append(row)
    cases = sorted(grouped)
    outcomes = list(COLORS)
    max_repeats = max((len(v) for v in grouped.values()), default=1)
    matrix = np.full((max(len(cases), 1), max_repeats), np.nan)
    for i, case in enumerate(cases):
        for j, row in enumerate(sorted(grouped[case], key=lambda r: r["repeat"])):
            matrix[i, j] = outcomes.index(prediction(row, threshold))
    fig, ax = plt.subplots(figsize=(max(6, max_repeats * .35), max(3, len(cases) * .25)))
    cmap = ListedColormap(list(COLORS.values()))
    ax.imshow(np.ma.masked_invalid(matrix), aspect="auto", cmap=cmap, vmin=-.5, vmax=3.5)
    ax.set_yticks(range(len(cases)), cases)
    ax.set_xlabel("Repeated trials (all attempts retained)")
    ax.set_title("Outcome repeatability")
    ax.set_xticks(range(max_repeats), range(1, max_repeats + 1))
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=c, label=k) for k, c in COLORS.items()], loc="upper left", bbox_to_anchor=(1, 1))
    save_figure(fig, out, "repeatability")

    labels = ["unchanged", "changed", "unknown"]
    matrix = np.array([[summary["confusion"][label].get(p, 0) for p in outcomes] for label in labels])
    fig, ax = plt.subplots(figsize=(7, 3))
    ax.imshow(matrix, cmap="Blues", aspect="auto")
    ax.set_xticks(range(4), outcomes)
    ax.set_yticks(range(3), labels)
    ax.set_xlabel("Output (abstentions and failures included)")
    ax.set_ylabel("Experimental target label")
    ax.set_title("Counts; unknown labels are not scored")
    for i in range(3):
        for j in range(4):
            ax.text(j, i, str(matrix[i, j]), ha="center", va="center")
    save_figure(fig, out, "confusion")

    strata = defaultdict(list)
    for row in rows:
        strata[(row["speed"], row["condition"])].append(row)
    fig, ax = plt.subplots(figsize=(9, max(3, len(strata) * .35)))
    for i, ((speed, condition), subset) in enumerate(sorted(strata.items())):
        total = len(subset)
        start = 0
        for outcome, color in COLORS.items():
            fraction = sum(prediction(r, threshold) == outcome for r in subset) / total
            ax.barh(i, fraction, left=start, color=color, label=outcome if i == 0 else None)
            start += fraction
    ax.set_yticks(range(len(strata)), [f"{s}/{c}" for s, c in sorted(strata)])
    ax.set_xlim(0, 1)
    ax.set_xlabel("Fraction of all attempts")
    ax.set_title("Outcomes by speed and experimental condition")
    if strata:
        ax.legend(loc="upper left", bbox_to_anchor=(1, 1))
    save_figure(fig, out, "conditions")

    fig, ax = plt.subplots(figsize=(9, 4))
    for i, case in enumerate(cases):
        values = [r["observation"].get("score") for r in grouped[case] if r["status"] == "ok" and r["observation"].get("score") is not None]
        if values:
            ax.scatter(np.linspace(i - .12, i + .12, len(values)), values, s=22, color="#3965a5", alpha=.7)
    if threshold is not None:
        ax.axhline(threshold, color="#b32f42", linestyle="--", label="Frozen score threshold")
        ax.legend()
    ax.set_xticks(range(len(cases)), cases, rotation=60, ha="right")
    ax.set_ylabel("Motion change score (not risk probability)")
    ax.set_title("Every available score; missing measurements remain in other charts")
    save_figure(fig, out, "scores")

    csv_rows = []
    for row in rows:
        obs = row.get("observation", {})
        csv_rows.append({k: row.get(k) for k in ("case_id", "repeat", "seed", "group_id", "speed", "condition", "expected", "status", "duration_s", "error")} |
                        {"prediction": prediction(row, threshold), "score": obs.get("score"),
                         "measurement_available": obs.get("measurement_available"), "run_dir": row.get("run_dir")})
    fields = list(csv_rows[0]) if csv_rows else ["case_id"]
    with (out / "trials.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_rows)
    cards = "".join(f"<tr><td>{key}</td><td>{number(summary[key])}</td></tr>" for key in METRICS)
    per_case = "".join(f"<tr><td>{html.escape(c)}</td><td>{html.escape(grouped[c][0]['speed'])}</td>"
                       f"<td>{html.escape(grouped[c][0]['condition'])}</td><td>{html.escape(grouped[c][0]['expected'])}</td>"
                       f"<td>{len(grouped[c])}</td><td>{number(summarize(grouped[c], threshold)['effective_correctness'])}</td></tr>" for c in cases)
    errors = "".join(f"<li>{html.escape(r['case_id'])} #{r['repeat']}: {html.escape(str(r.get('error', '')))}</li>" for r in rows if r["status"] != "ok")
    figs = "".join(f'<figure><img src="{name}.png" alt="{name}"><figcaption><a href="{name}.svg">SVG</a></figcaption></figure>' for name in ("repeatability", "conditions", "confusion", "scores"))
    mode = html.escape(str(metadata.get("options", {}).get("mode", "custom")))
    reuse_note = ""
    if metadata.get("options", {}).get("reference_cache_path"):
        reuse_note = '<p class="note">저장된 정상 기준 측정을 재사용하고 각 대상 영상을 새로 분석했습니다. 지연시간에는 정상 기준의 최초 측정 비용이 포함되지 않습니다. 정상 기준 측정 실패가 저장되어 있으면 모든 비교의 보류에 공통으로 영향을 줍니다.</p>'
    report = f"""<!doctype html><html lang="ko"><meta charset="utf-8"><title>진동 비교 벤치마크</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:36px auto;padding:0 20px;color:#223047;background:#f7f9fc}}table{{border-collapse:collapse;background:white;width:100%}}td,th{{padding:10px;border-bottom:1px solid #dde3ec;text-align:left}}img{{max-width:100%}}figure{{margin:24px 0;background:white;padding:12px}}.note{{background:#fff0cd;padding:16px;line-height:1.7}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style>
<h1>진동 비교 벤치마크</h1><p>Revision: {html.escape(metadata['revision'])} · Split: {html.escape(metadata['split'])} · Mode: {mode}</p>
<p>{summary['cases']}개 비교 사례 / {summary['trials']}회 시도 / {summary['independent_session_groups']}개 촬영 세션 그룹</p>
<div class="note">이 결과는 실험 조건 구분과 실행 재현성 평가입니다. 공정 설비 고장·위험·조기 감지 성능을 입증하지 않습니다.
같은 원본의 클립과 같은 입력의 반복 실행은 독립적인 새 촬영이 아닙니다. 합성/리플레이/measurement 모드는 실제 AI 성능 평가에 사용할 수 없습니다.
실행 오류와 판단 보류도 전체 분모에 포함합니다. 바뀐 실험 조건이 실제 진동 증가나 위험 증가를 보장하지 않습니다.</div>
<p>판정 기준: {summary['decision_source']} (threshold={threshold}). AI 평가에서는 서비스의 최종 보류 처리까지 평가합니다.</p>
{reuse_note}
<table><tr><th>지표</th><th>값</th></tr>{cards}</table>
<p>전체 기준 정답 {summary['correct_known_trials']}/{summary['known_label_trials']} · 세션 평균 정답률 95% 구간: {summary['macro_session_correctness_ci95'] or '표시 안 함 (독립 세션 5개 미만 또는 라벨 없음)'}</p>
<p>전체 시도 지연시간 p50/p95: {summary['latency_p50_s_all_trials']} / {summary['latency_p95_s_all_trials']}초</p>
{figs}<table><tr><th>사례</th><th>속도</th><th>조건</th><th>평가 라벨</th><th>시도</th><th>전체 정답률</th></tr>{per_case}</table>
<h2>실패 기록</h2><ul>{errors}</ul><p><a href="trials.csv">전체 시도 CSV</a> · <a href="summary.json">요약 JSON</a></p>
<details><summary>실행 정보</summary><pre>{html.escape(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre></details></html>"""
    (out / "report.html").write_text(report, encoding="utf-8")
    return summary


def render_comparison(output, comparison):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "comparison.json", comparison)
    fig, ax = plt.subplots(figsize=(10, 4))
    keys = ["completion_rate", "decision_coverage", "effective_correctness", "false_alarm_rate_all_unchanged", "failure_rate"]
    x = np.arange(len(keys))
    for side, offset, color in [("left", -.18, "#899bb5"), ("right", .18, "#337978")]:
        values = [np.nan if comparison[side][k] is None else comparison[side][k] for k in keys]
        ax.bar(x + offset, values, .36, label=comparison[f"{side}_revision"], color=color)
    ax.set_xticks(x, keys, rotation=20, ha="right")
    ax.set_ylim(0, 1.05)
    ax.legend()
    ax.set_title("Matched dataset/trials; failures retained; missing metrics omitted")
    save_figure(fig, out, "comparison")
    (out / "report.html").write_text('<meta charset="utf-8"><h1>같은 사례의 변경 전후 비교</h1><img style="max-width:100%" src="comparison.png"><p>독립 촬영 세션 5개 미만이면 신뢰구간을 표시하지 않습니다. 합성 자료의 개선은 실제 설비 성능을 입증하지 않습니다.</p><pre>' + html.escape(json.dumps(comparison, ensure_ascii=False, indent=2)) + '</pre>', encoding="utf-8")
