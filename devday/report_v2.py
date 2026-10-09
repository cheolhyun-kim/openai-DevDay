"""Concise, reader-oriented standalone report renderer."""
import base64
import html
from pathlib import Path

def reports(directory, diagnosis, evidence, artifacts, mode, decision=None):
    """Render the summary first and keep secondary maps and values in a details panel."""
    out = Path(directory).resolve()
    esc = lambda value: html.escape(str(value))
    decision = decision or {}
    rows = evidence.get("region_comparisons", [])
    names = {r["id"]: r.get("part_name", r["id"])
             for r in evidence.get("roi_plan", {}).get("regions", [])}
    names.update({r["roi_id"]: r["part_name"] for r in rows if r.get("part_name")})

    label = decision.get("label", "insufficient")
    labels = {"abnormal": "비정상 의심", "normal": "정상", "insufficient": "판단 보류"}
    verdict = labels.get(label, "판단 보류")
    verdict_class = label if label in labels else "insufficient"
    ai_labels = {"abnormal": "비정상 의심", "normal": "정상", "insufficient": "판단 보류"}
    ai = ai_labels.get(decision.get("model_decision"), "확인 불가")
    rule = ai_labels.get(decision.get("rule_decision"), "확인 불가")
    agreement = decision.get("agreement")
    agreement_text = "일치" if agreement is True else "불일치" if agreement is False else "확인 불가"
    summary = diagnosis.summary.strip()
    if label == "normal":
        summary = "수치 기준에서 뚜렷한 흔들림 증가는 확인되지 않았습니다. 영상 비교만으로 제품의 모든 상태를 보장하지는 않습니다."
    elif label == "insufficient":
        summary = "이번 영상만으로는 신뢰할 만한 결론을 내리기 어렵습니다. 촬영 위치와 조건을 맞춰 다시 확인해 주세요."
    elif not summary:
        summary = "정상 영상보다 흔들림이 커진 부위가 관찰되어 확인이 필요합니다."

    supported = [r for r in rows if r.get("eligible_for_interpretation") and r.get("rule_flag") == "increase"]
    supported.sort(key=lambda r: (r.get("ratio") or 0, r.get("z_score") or 0), reverse=True)
    findings, seen = [], set()
    for row in supported:
        if row.get("roi_id") not in seen:
            seen.add(row["roi_id"])
            findings.append(row)
        if len(findings) == 3:
            break
    finding_html, plain_findings = [], []
    for number, row in enumerate(findings, 1):
        lo, hi = row.get("band_hz", (0, 0))
        band_name = "느린 흔들림" if hi <= 10 else "빠른 떨림" if lo >= 10 else "주파수 대역"
        ref, cand, ratio = row.get("reference_rms_px"), row.get("candidate_rms_px"), row.get("ratio")
        snr = row.get("candidate_snr")
        noise_text = f"배경 잡음 대비 {snr:.1f}배" if isinstance(snr, (int, float)) else "배경 잡음 비교 불가"
        if isinstance(snr, (int, float)) and snr < 2:
            noise_text = "배경 잡음과 구분이 충분하지 않음"
        if all(isinstance(v, (int, float)) for v in (ref, cand, ratio)):
            metric = f"{band_name} {lo:g}–{hi:g} Hz · 정상 {ref:.2f} px → 확인 {cand:.2f} px · {ratio:.1f}배 · {noise_text}"
        else:
            metric = f"{band_name} {lo:g}–{hi:g} Hz · 세부 수치는 상세 근거에서 확인"
        name = names.get(row["roi_id"], row["roi_id"])
        action = "해당 부위의 고정 상태와 연결부를 살펴보세요."
        finding_html.append(f'<article class="finding"><span class="number">{number}</span><div>'
                            f'<h3>{esc(name)}</h3><p class="metric">{esc(metric)}</p>'
                            f'<p class="action">{action}</p></div></article>')
        plain_findings.append(f"{number}. {name}: {metric}. {action}")
    if not findings:
        finding_html = ['<p class="empty">점검 대상으로 안내할 만큼 근거가 충분한 증가 부위가 없습니다. 측정 품질과 상세 근거를 확인해 주세요.</p>']

    other_images, part_images = [], []
    for item in artifacts:
        path = Path(item)
        if not path.is_file() or "heatmap" in path.name or path.name in ("inspection_roi.png", "report_difference_map.png"):
            continue
        figure = (f'<figure><img loading="lazy" src="{esc(path.relative_to(out).as_posix())}" '
                  f'alt="{esc(path.stem)}"><figcaption>{esc(path.stem.replace("_", " "))}</figcaption></figure>')
        if path.name == "roi.png" and "measurements" in path.parts:
            part_images.append(figure)
        else:
            other_images.append(figure)
    by_id = {r.get("evidence_id"): r for r in rows + evidence.get("relative_comparisons", [])}
    table_rows = []
    for row in rows:
        lo, hi = row.get("band_hz", ("—", "—"))
        fmt = lambda key: f'{row[key]:.4g}' if isinstance(row.get(key), (int, float)) else "—"
        rule_name = {"increase": "증가", "decrease": "감소", "no_significant_change": "뚜렷한 차이 없음",
                     "below_noise_floor": "측정 제외", "not_measurable": "측정 불가"}.get(row.get("rule_flag"), "—")
        table_rows.append(f'<tr><td>{esc(names.get(row.get("roi_id"), row.get("roi_id")))}</td>'
                          f'<td>{lo}–{hi} Hz</td><td>{fmt("reference_rms_px")} → {fmt("candidate_rms_px")}</td>'
                          f'<td>{fmt("ratio")}배</td><td>{fmt("candidate_snr")}배</td><td>{rule_name}</td></tr>')
    cited_rows = []
    for item in decision.get("cited_numbers", []):
        row = by_id.get(item.get("evidence_id"), {})
        metric_value = item.get("value")
        cited_rows.append(f'<tr><td>{esc(names.get(row.get("roi_id"), row.get("roi_id", item.get("evidence_id"))))}</td>'
                          f'<td>{esc(item.get("metric"))}</td><td>{metric_value:.4g}</td></tr>')
    thresholds = evidence.get("rule_prefilter", {}).get("thresholds", {})
    cited_body = "".join(cited_rows) or '<tr><td colspan="3">인용 수치가 없습니다.</td></tr>'
    detail_html = (f'<details class="card"><summary>판단 근거 수치 보기</summary><div class="detail-body">'
                   f'<p>규칙은 신호/배경 잡음 {esc(thresholds.get("snr_min", "—"))}배, 흔들림 배율 '
                   f'{esc(thresholds.get("ratio_min", "—"))}배, z 점수 {esc(thresholds.get("z_min", "—"))} 이상 등을 함께 살핍니다.</p>'
                   '<h3>전체 부위 수치</h3><div class="table-wrap"><table><thead><tr><th>부위</th><th>대역</th><th>정상 → 확인 (px)</th><th>배율</th><th>신호/잡음</th><th>규칙</th></tr></thead>'
                   f'<tbody>{"".join(table_rows)}</tbody></table></div><h3>AI 판단에 인용된 수치</h3>'
                   '<div class="table-wrap"><table><thead><tr><th>항목</th><th>수치</th><th>값</th></tr></thead>'
                   f'<tbody>{cited_body}</tbody></table></div>'
                   f'<p class="note">{esc(decision.get("reason", ""))}</p>'
                   f'<h3>주파수 비교 자료</h3><div class="gallery">{"".join(other_images) or "<p>추가 자료가 없습니다.</p>"}</div></div></details>')
    parts_html = (f'<details class="card"><summary>측정 부위 보기</summary><div class="detail-body">'
                  '<p>정상 영상과 확인 영상에서 분석에 사용한 영역입니다.</p>'
                  f'<div class="gallery">{"".join(part_images) or "<p>측정 부위 이미지가 없습니다.</p>"}</div></div></details>')

    relative_rows = [r for r in evidence.get("relative_comparisons", [])
                     if r.get("eligible_for_interpretation") and isinstance(r.get("ratio"), (int, float))]
    relative_rows.sort(key=lambda r: abs(r["ratio"] - 1), reverse=True)
    relative_lines = []
    for row in relative_rows[:5]:
        lo, hi = row.get("band_hz", (0, 0))
        left, right = names.get(row.get("a"), row.get("a")), names.get(row.get("b"), row.get("b"))
        relative_lines.append(f'<tr><td>{esc(left)} ↔ {esc(right)}</td><td>{lo:g}–{hi:g} Hz</td><td>{row["ratio"]:.2f}배</td></tr>')
    relative_body = "".join(relative_lines) or '<tr><td colspan="3">비교할 상대 움직임이 없습니다.</td></tr>'
    relative_html = (f'<section class="card"><h2>주요 상대 움직임 비교</h2>'
                     '<p class="note">두 부위 사이 움직임 차이가 정상 영상과 비교해 얼마나 달라졌는지 보여줍니다. 1배는 비슷한 수준이며, 이 값만으로 고장 원인을 알 수는 없습니다.</p>'
                     f'<div class="table-wrap"><table><thead><tr><th>비교 부위</th><th>대역</th><th>확인/정상</th></tr></thead><tbody>{relative_body}</tbody></table></div></section>'
                     if relative_rows else "")

    note = "안내는 영상에서 관찰된 흔들림 차이를 요약하며, 특정 고장 원인을 확정하지 않습니다."
    banner = '<div class="demo">예시 실행 결과입니다. 실제 AI 판정 결과가 아닙니다.</div>' if mode not in {"openai_live", "claude_live"} else ""
    (out / "diagnosis.txt").write_text("\n".join(["분석 결과", verdict, summary,
        f"AI 판단: {ai} · 수치 규칙: {rule} · 일치 여부: {agreement_text}", "", *plain_findings, note]), encoding="utf-8")
    logo = Path(__file__).resolve().parent / "web" / "static" / "assets" / "prometheus-logo.png"
    logo_src = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode("ascii") if logo.is_file() else ""
    mismatch = f'<p class="note">{esc(decision.get("reason", "AI 판단과 수치 규칙 판단이 다릅니다."))}</p>' if agreement is False else ""
    page = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>분석 결과 · Wi-ing Wi-ing</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f4f7fb;color:#24364d;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","Malgun Gothic",sans-serif;line-height:1.6}}.shell{{max-width:1100px;margin:auto;padding:22px clamp(16px,4vw,46px) 60px}}.top{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #d6e0ed;padding-bottom:16px}}.brand{{display:flex;align-items:center;gap:12px;color:#173555;text-decoration:none;font-weight:750;font-size:20px}}.brand img{{height:42px}}.brand small{{display:block;font-size:9px;letter-spacing:.16em;color:#536f8e}}.tag,.kicker{{color:#536f8e;font-size:12px;font-weight:700;letter-spacing:.12em}}.heading{{margin:34px 0 20px}}h1{{font-size:clamp(32px,5vw,46px);line-height:1.15;margin:8px 0;color:#203854}}.card,.verdict{{background:#fff;border:1px solid #d7e1ed;border-radius:18px;margin-bottom:16px;box-shadow:0 10px 30px #2a456312}}.verdict{{padding:24px 28px}}.verdict.abnormal{{background:#fff6f1;border-color:#f0d4c5}}.verdict.normal{{background:#f2fbf5;border-color:#cfe8d9}}.verdict.insufficient{{background:#f4f8fd}}.big{{font-size:34px;font-weight:800;margin:2px 0}}.abnormal .big{{color:#c2410c}}.normal .big{{color:#15803d}}.insufficient .big{{color:#475569}}.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}}.chip{{border:1px solid #dce6f1;border-radius:99px;padding:5px 10px;background:#f4f8fc;color:#516780;font-size:13px}}.demo{{padding:12px;background:#eff6ff;border-radius:12px;margin-bottom:16px}}.columns{{display:grid;grid-template-columns:minmax(280px,.9fr) minmax(0,1.1fr);gap:18px;align-items:start}}figure{{margin:0;background:white;border:1px solid #e0e7f0;border-radius:14px;padding:10px}}figure img{{width:100%;height:auto;max-height:620px;object-fit:contain;border-radius:9px}}figcaption,.note{{font-size:13px;color:#687d96}}.legend{{display:flex;flex-wrap:wrap;gap:10px 16px;padding:9px 3px 2px;font-size:12px;color:#526880}}.legend span{{display:flex;align-items:center;gap:6px}}.legend i{{display:inline-block;width:12px;height:12px;border-radius:3px}}.increase{{background:#d35b36}}.decrease{{background:#348f82}}.unchanged{{background:#687d96}}.excluded{{background:#aaa}}.guide{{padding:22px}}h2{{margin:0 0 15px;font-size:20px;color:#243e5c}}.finding{{display:flex;gap:12px;padding:14px 0;border-top:1px solid #e5ebf2}}.number{{flex:none;display:grid;place-items:center;width:28px;height:28px;border-radius:50%;background:#3478c9;color:white;font-weight:800}}h3{{margin:0 0 5px;font-size:16px;color:#263e59}}.metric{{margin:3px 0;color:#526880;font-size:14px}}.action{{margin:5px 0 0;color:#285f9d;font-weight:650}}.empty{{padding:15px;background:#f7faff;border-radius:12px;color:#687d96}}details.card{{padding:0;overflow:hidden}}details summary{{padding:20px 24px;cursor:pointer;font-weight:700;color:#243e5c}}.detail-body{{padding:0 24px 24px}}.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:10px 0 22px}}.table-wrap{{overflow:auto;margin:10px 0 22px}}table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}}td,th{{padding:9px 8px;text-align:left;border-bottom:1px solid #e0e7f0}}th{{color:#61758e}}footer{{border-top:1px solid #d6e0ed;padding-top:15px;margin-top:22px;color:#687d96;font-size:13px}}.button{{float:right;color:#2869b7}}@media(max-width:760px){{.columns{{grid-template-columns:1fr}}.shell{{padding:14px 16px 40px}}.verdict{{padding:20px}}.top .tag{{font-size:9px}}}}
    </style></head><body><main class="shell"><header class="top"><a class="brand" href="#"><img src="{logo_src}" alt=""><span>Wi-ing Wi-ing<small>GENIUSES, OBVIOUSLY</small></span></a><span class="tag">INSPECTION REPORT</span></header><section class="heading"><div class="kicker">VIDEO MOTION ANALYSIS</div><h1>분석 결과</h1><p>기준 영상과 확인 영상의 움직임을 비교한 결과입니다.</p></section>{banner}<section class="verdict {verdict_class}"><div class="kicker">결과와 판단 근거</div><div class="big">{esc(verdict)}</div><p>{esc(summary)}</p><div class="chips"><span class="chip">AI 판단: {esc(ai)}</span><span class="chip">수치 규칙: {esc(rule)}</span><span class="chip">판단 {agreement_text}</span></div>{mismatch}</section>{relative_html}<section class="card guide"><div class="kicker">NEXT CHECK</div><h2>먼저 확인할 부위와 방법</h2>{''.join(finding_html)}</section>{detail_html}{parts_html}<p class="note">{note}</p><footer>Wi-ing Wi-ing · Video Motion Analysis <a class="button" href="diagnosis.txt" download>안내문 저장</a></footer></main></body></html>'''
    (out / "report.html").write_text(page, encoding="utf-8")
