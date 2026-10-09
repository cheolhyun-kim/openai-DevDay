"""Failures and abstentions stay in operational metric denominators."""
from collections import Counter, defaultdict
import math

import numpy as np


def validate_observation(value):
    if value.get("schema_version") != "devday.observation/1":
        raise ValueError("Adapter must return devday.observation/1")
    if value.get("assessment") not in (None, "suspected_abnormal", "no_clear_difference", "inconclusive"):
        raise ValueError("Unknown assessment")
    if value.get("decision") not in (None, "abnormal", "normal", "insufficient"):
        raise ValueError("Unknown final decision")
    score = value.get("score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0):
        raise ValueError("score must be null or a finite nonnegative number")
    if type(value.get("measurement_available")) is not bool:
        raise ValueError("measurement_available must be boolean")
    return value


def prediction(row, threshold=None):
    if row["status"] != "ok":
        return "failed"
    value = row["observation"]
    if threshold is not None:
        score = value.get("score")
        if score is None or not value["measurement_available"]:
            return "abstain"
        return "changed" if score >= threshold else "unchanged"
    decision = value.get("decision")
    if decision is not None:
        return {"abnormal": "changed", "normal": "unchanged", "insufficient": "abstain"}[decision]
    return {"suspected_abnormal": "changed", "no_clear_difference": "unchanged",
            "inconclusive": "abstain", None: "abstain"}[value.get("assessment")]


def divide(a, b):
    return a / b if b else None


def cluster_interval(values, seed=20261009):
    """Bootstrap independent session means, never clips/repeated API calls."""
    if len(values) < 5:
        return None
    means = np.array([np.mean(v) for v in values], dtype=float)
    rng = np.random.default_rng(seed)
    samples = rng.choice(means, size=(2000, len(means)), replace=True).mean(axis=1)
    return [float(x) for x in np.quantile(samples, [.025, .975])]


def summarize(rows, threshold=None):
    n = len(rows)
    tagged = [(r, prediction(r, threshold)) for r in rows]
    known = [(r, p) for r, p in tagged if r["expected"] != "unknown"]
    decided = [(r, p) for r, p in known if p in ("changed", "unchanged")]
    positives = [(r, p) for r, p in known if r["expected"] == "changed"]
    negatives = [(r, p) for r, p in known if r["expected"] == "unchanged"]
    tp = sum(p == "changed" for _, p in positives)
    fp = sum(p == "changed" for _, p in negatives)
    tn = sum(p == "unchanged" for _, p in negatives)
    fn = sum(p == "unchanged" for _, p in positives)
    correct = tp + tn
    group_correct, group_success = defaultdict(list), defaultdict(list)
    for row, pred in tagged:
        group_success[row["group_id"]].append(row["status"] == "ok")
        if row["expected"] != "unknown":
            group_correct[row["group_id"]].append(pred == row["expected"])
    by_case = defaultdict(list)
    for row, pred in tagged:
        by_case[row["case_id"]].append((row, pred))
    stability = []
    for case_id, trials in sorted(by_case.items()):
        counts = Counter(p for _, p in trials)
        total = len(trials)
        pairs = total * (total - 1) / 2
        agree = sum(c * (c - 1) / 2 for c in counts.values())
        scores = [r["observation"]["score"] for r, _ in trials if r["status"] == "ok" and r["observation"].get("score") is not None]
        stability.append({"case_id": case_id, "trials": total, "outcomes": dict(counts),
                          "pairwise_outcome_agreement": divide(agree, pairs),
                          "all_trials_decided_same": total >= 2 and len(counts) == 1 and next(iter(counts)) in ("changed", "unchanged"),
                          "score_mean": float(np.mean(scores)) if scores else None,
                          "score_std": float(np.std(scores, ddof=1)) if len(scores) > 1 else None})
    durations = [r["duration_s"] for r in rows if r.get("duration_s") is not None]
    confusion = {label: dict(Counter(p for r, p in tagged if r["expected"] == label)) for label in ("unchanged", "changed", "unknown")}
    return {"trials": n, "cases": len(by_case), "independent_session_groups": len(group_success),
            "completion_rate": divide(sum(r["status"] == "ok" for r in rows), n),
            "measurement_coverage": divide(sum(r["status"] == "ok" and r["observation"]["measurement_available"] for r in rows), n),
            "decision_coverage": divide(sum(p in ("changed", "unchanged") for _, p in tagged), n),
            "abstention_rate": divide(sum(p == "abstain" for _, p in tagged), n),
            "failure_rate": divide(sum(p == "failed" for _, p in tagged), n),
            "known_label_trials": len(known), "correct_known_trials": correct,
            "effective_correctness": divide(correct, len(known)),
            "conditional_accuracy": divide(correct, len(decided)),
            "precision": divide(tp, tp + fp),
            "end_to_end_changed_hit_rate": divide(tp, len(positives)),
            "conditional_changed_hit_rate": divide(tp, tp + fn),
            "false_alarm_rate_all_unchanged": divide(fp, len(negatives)),
            "conditional_false_alarm_rate": divide(fp, fp + tn),
            "macro_session_correctness": float(np.mean([np.mean(v) for v in group_correct.values()])) if group_correct else None,
            "macro_session_correctness_ci95": cluster_interval(list(group_correct.values())),
            "macro_session_completion_ci95": cluster_interval(list(group_success.values())),
            "latency_p50_s_all_trials": float(np.median(durations)) if durations else None,
            "latency_p95_s_all_trials": float(np.quantile(durations, .95)) if durations else None,
            "confusion": confusion, "repeatability": stability,
            "ci_note": "Session-cluster percentile bootstrap; suppressed below five independent sessions. Not a fault/industrial safety accuracy estimate."}


def compare_runs(left_meta, left, right_meta, right, threshold=None):
    if left_meta["dataset_id"] != right_meta["dataset_id"] or left_meta["split"] != right_meta["split"]:
        raise ValueError("Comparison requires identical dataset_id and split")
    key = lambda row: (row["case_id"], row["repeat"], row["seed"])
    a, b = {key(r): r for r in left}, {key(r): r for r in right}
    if len(a) != len(left) or len(b) != len(right) or set(a) != set(b):
        raise ValueError("Comparison requires exactly matching trials, including failed trials and seeds")
    differences = defaultdict(list)
    completion = []
    for k, old in a.items():
        new = b[k]
        if (old["expected"], old["group_id"], old["condition"], old["speed"]) != (new["expected"], new["group_id"], new["condition"], new["speed"]):
            raise ValueError("Trial ground truth/group mismatch")
        completion.append(int(new["status"] == "ok") - int(old["status"] == "ok"))
        if old["expected"] != "unknown":
            delta = int(prediction(new, threshold) == new["expected"]) - int(prediction(old, threshold) == old["expected"])
            differences[old["group_id"]].append(delta)
    pooled = [v for group in differences.values() for v in group]
    return {"left_revision": left_meta["revision"], "right_revision": right_meta["revision"],
            "matched_trials": len(a), "completion_delta": float(np.mean(completion)) if completion else None,
            "effective_correctness_delta": float(np.mean(pooled)) if pooled else None,
            "macro_session_correctness_delta": float(np.mean([np.mean(v) for v in differences.values()])) if differences else None,
            "paired_session_ci95": cluster_interval(list(differences.values())),
            "left": summarize(left, threshold), "right": summarize(right, threshold)}
