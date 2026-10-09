"""Maps are drawn by code from measured values, never invented by the LLM."""
import html
import json
from pathlib import Path
import cv2
import numpy as np
import os
import tempfile
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "devday-matplotlib-cache"))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from .vendor.fanvib.pipeline import roi_mask
from scipy.signal import welch

LABELS={'suspected_abnormal':'이상 의심','no_clear_difference':'뚜렷한 차이 없음','inconclusive':'측정·판단 불충분'}
LIVE_MODES={'openai_live','claude_live'}
DECISION_KO={'abnormal':'비정상','normal':'정상','insufficient':'판단 불가'}

def heatmaps(directory,frame_paths,maps,evidence,bands):
    directory=Path(directory);directory.mkdir(exist_ok=True);paths=[]
    for bn,lo,hi in bands:
        rows={r['roi_id']:r for r in evidence['region_comparisons'] if r['evidence_id'].endswith(':'+bn)}
        vals=[]
        for side in ['reference','candidate']:
            m=maps[side];v=m['band_values'][bn]
            if v is not None:
                mask=np.array([n in rows for n in m['labels']]);vals.extend(v[mask].tolist())
        if not vals: continue
        vmax=max(float(np.percentile(vals,95)),1e-8)
        diffs=[abs(r['difference_px']) for r in rows.values() if r['eligible_for_interpretation'] and r['difference_px'] is not None]
        dmax=max(max(diffs,default=0),1e-8)
        fig,axs=plt.subplots(1,3,figsize=(15,8),layout='constrained')
        for ax,side,difference in [(axs[0],'reference',False),(axs[1],'candidate',False),(axs[2],'candidate',True)]:
            im=cv2.cvtColor(cv2.imread(str(frame_paths[side])),cv2.COLOR_BGR2RGB);ax.imshow(im,alpha=.65);m=maps[side];xy=m['points'];labels=m['labels'];good=np.array([n in rows and rows[n]['eligible_for_interpretation'] for n in labels]);bad=np.array([n in rows for n in labels])&~good
            ax.scatter(xy[bad,0],xy[bad,1],s=15,c='gray',marker='x')
            if difference:
                color=np.array([rows[n]['difference_px'] for n in labels[good]])
            else:
                v=m['band_values'][bn];color=v[good] if v is not None else np.zeros(good.sum())
            if good.any():
                sc=ax.scatter(xy[good,0],xy[good,1],c=color,s=23,cmap='coolwarm' if difference else 'turbo',vmin=-dmax if difference else 0,vmax=dmax if difference else vmax,edgecolors='black',linewidths=.2)
                fig.colorbar(sc,ax=ax,shrink=.5,label='Difference (px RMS)' if difference else 'px RMS')
            for name in rows:
                sel=labels==name
                if sel.any():
                    x,y=np.median(xy[sel],axis=0);ax.text(x,y,name,fontsize=7,bbox={'facecolor':'white','alpha':.8,'edgecolor':'none'})
            ax.set_title('Candidate - reference regional medians' if difference else side);ax.axis('off')
        fig.suptitle(f'{lo:g}-{hi:g} Hz measured image movement; gray X: excluded; NOT fault probability')
        path=directory/f'heatmap_{bn}.png';fig.savefig(path,dpi=140);plt.close(fig);paths.append(path)
    return paths

def spectrum_comparison(directory, measurement_dirs, regions, alignment, bands):
    """Plot paired Welch spectra from the same tracked target ROIs."""
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    traces=[]
    for region in regions:
        roi_id=region['id']; paired=[]
        for side in ('reference','candidate'):
            path=Path(measurement_dirs[side])/'signals.npz'
            if not path.is_file(): break
            try:
                with np.load(path) as data:
                    key=f'{roi_id}__affine'
                    if key not in data: break
                    signal=np.asarray(data[key],dtype=float)
                    fps=float(data['capture_fps'])
            except (OSError,KeyError,ValueError): break
            if signal.ndim!=2 or signal.shape[0]<8 or not np.isfinite(signal).all() or fps<=0: break
            if side=='candidate' and alignment.get('ok') and alignment.get('scale',0)>0:
                signal=signal/float(alignment['scale'])
            nperseg=min(512,signal.shape[0])
            hz,psd=welch(signal,fs=fps,nperseg=nperseg,detrend='linear',axis=0)
            paired.append((hz,np.maximum(psd.sum(axis=1),1e-16)))
        if len(paired)==2:
            traces.append((region['part_name'],paired))
    if not traces:return None
    cols=2; rows=(len(traces)+cols-1)//cols
    fig,axs=plt.subplots(rows,cols,figsize=(13,4.1*rows),squeeze=False,layout='constrained')
    for ax,(name,paired) in zip(axs.flat,traces):
        (href,pref),(hcand,pcand)=paired
        max_hz=min(href[-1],hcand[-1])
        for i,(_,lo,hi) in enumerate(bands):
            if lo<max_hz: ax.axvspan(lo,min(hi,max_hz),color='#f0b44d',alpha=.10,zorder=0)
        ax.semilogy(href,pref,label='Normal',color='#3182bd',linewidth=1.5)
        ax.semilogy(hcand,pcand,label='Inspection',color='#e45745',linewidth=1.5)
        ax.set_xlim(0,max_hz);ax.set_title(str(name));ax.set_xlabel('Frequency (Hz)');ax.set_ylabel('Motion PSD (px²/Hz)')
        ax.grid(True,which='both',alpha=.2)
    for ax in list(axs.flat)[len(traces):]:ax.remove()
    handles,labels=axs.flat[0].get_legend_handles_labels()
    fig.legend(handles,labels,loc='upper center',ncol=2,frameon=False)
    fig.suptitle('Frequency spectrum comparison · measured image motion',y=1.02,fontsize=15)
    path=directory/'spectrum_comparison.png';fig.savefig(path,dpi=150,bbox_inches='tight');plt.close(fig)
    return path

def display_candidates(diagnosis):
    """Preserve provider priority order and show at most five candidates."""
    return diagnosis.inspection_candidates[:5]

def inspection_map(frame,config,diagnosis,path,evidence=None,maps=None):
    im=cv2.imread(str(frame))
    if im is None:raise OSError(f'Cannot read inspection frame: {frame}')
    rois={r['id']:r for r in config['rois']}
    evidence=evidence or {};maps=maps or {}
    rows=[r for r in evidence.get('region_comparisons',[]) if r.get('rule_flag')=='increase' and r.get('eligible_for_interpretation')]
    rows.sort(key=lambda r:(r.get('ratio') or 0,r.get('z_score') or 0),reverse=True)
    measured=[]
    for row in rows:
        if row['roi_id'] not in measured:measured.append(row['roi_id'])
    ai=[item.roi_id for item in display_candidates(diagnosis) if item.roi_id not in measured]
    targets=[(roi_id,'measured') for roi_id in measured]+[(roi_id,'ai') for roi_id in ai]
    palette={'measured':(45,65,235),'ai':(0,140,255)}
    candidate_map=maps.get('candidate') or {}
    xy=np.asarray(candidate_map.get('points',[]));labels=np.asarray(candidate_map.get('labels',[]))
    number=0
    for roi_id,kind in targets:
        r=rois.get(roi_id)
        if r is None:continue
        number+=1
        mask=roi_mask(im.shape[:2],r)
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        color=palette[kind]
        cv2.drawContours(im,contours,-1,color,3)
        ys,xs=np.where(mask);center=(int(xs.mean()),int(ys.mean()))
        cv2.circle(im,center,22,color,-1)
        cv2.putText(im,str(number),(center[0]-8,center[1]+8),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
        if len(labels)==len(xy):
            for point in xy[labels==roi_id]:cv2.circle(im,tuple(np.round(point).astype(int)),3,(230,220,30),-1)
    # Legend uses English to avoid platform-dependent OpenCV font rendering.
    legend=[('Measured increase',(45,65,235)),('AI review candidate',(0,140,255)),('Tracked points',(230,220,30))]
    for i,(text,color) in enumerate(legend):
        y=28+i*25;cv2.rectangle(im,(10,y-15),(245,y+7),(255,255,255),-1);cv2.circle(im,(22,y-4),6,color,-1);cv2.putText(im,text,(36,y),cv2.FONT_HERSHEY_SIMPLEX,.48,(30,30,30),1,cv2.LINE_AA)
    if not cv2.imwrite(str(path),im):raise OSError('Cannot write inspection overlay')

def reports(directory,diagnosis,evidence,artifacts,mode,decision=None):
    """Plain-language user report; full numerical evidence stays in JSON."""
    import re
    out=Path(directory).resolve();esc=lambda v:html.escape(str(v))
    title={'suspected_abnormal':'확인해볼 흔들림이 있어요','no_clear_difference':'뚜렷하게 달라진 흔들림은 찾지 못했어요','inconclusive':'이번 영상으로는 판단하기 어려워요'}[diagnosis.assessment]
    summary={'suspected_abnormal':'정상 영상과 비교했을 때 움직임에 차이가 있어요. 아래에 표시한 곳을 한 번 확인해보세요.','no_clear_difference':'이번 비교에서는 큰 차이를 찾지 못했어요. 다만 영상만으로 정상 상태를 보장할 수는 없어요.','inconclusive':'부위가 잘 보이지 않거나 움직임을 안정적으로 따라가기 어려웠어요. 같은 위치와 풍속으로 다시 촬영해보세요.'}[diagnosis.assessment]
    if decision is not None:
        title={'abnormal':'비정상: 정상 영상보다 흔들림이 커졌어요','normal':'정상: 정상 영상과 비교해 커진 흔들림이 없어요','insufficient':'판단 불가: 이번 영상으로는 판단하기 어려워요'}[decision['label']]
        if decision['label']=='insufficient' and decision['model_decision']!=decision['rule_decision']:
            summary='AI 판단과 수치 규칙이 서로 달라 결론을 보류했어요. 같은 위치와 조건에서 다시 촬영해 확인해보세요.'
        elif decision['label']=='normal':
            summary='배경 잡음보다 큰 움직임은 측정됐지만, 정상 영상보다 의미 있게 커진 곳은 없었어요.'
    names={r['id']:r['part_name'] for r in evidence.get('roi_plan',{}).get('regions',[])}
    numbers='';number_lines=[]
    if decision is not None:
        metric_ko={'reference_rms_px':'정상 영상 흔들림(px)','candidate_rms_px':'대상 영상 흔들림(px)','ratio':'흔들림 배율(대상/정상)','z_score':'변화 크기(z)','reference_snr':'정상 영상 신호/배경잡음','candidate_snr':'대상 영상 신호/배경잡음','snr_ratio':'신호/잡음 배율'}
        rows_html=[]
        for c in decision['cited_numbers']:
            row=next((r for r in evidence.get('region_comparisons',[])+evidence.get('relative_comparisons',[]) if r['evidence_id']==c['evidence_id']),{})
            part=names.get(row.get('roi_id'),row.get('roi_id') or c['evidence_id'])
            band=row.get('band_hz');band_txt=f"{band[0]:g}–{band[1]:g} Hz" if band else ''
            flag={'increase':'증가 기준 충족','decrease':'감소','no_significant_change':'유의한 변화 없음','below_noise_floor':'배경 잡음 수준'}.get(row.get('rule_flag'),'')
            rows_html.append(f"<tr><td>{esc(part)}</td><td>{esc(band_txt)}</td><td>{esc(metric_ko.get(c['metric'],c['metric']))}</td><td>{c['value']:.4g}</td><td>{esc(flag)}</td></tr>")
            number_lines.append(f"- {part} {band_txt} {metric_ko.get(c['metric'],c['metric'])}: {c['value']:.4g} ({flag})")
        th=evidence.get('rule_prefilter',{}).get('thresholds',{})
        rule_txt=f"규칙 기준: 신호/배경잡음 ≥ {th.get('snr_min')}, 배율 ≥ {th.get('ratio_min')}, z ≥ {th.get('z_min')} · 규칙 판정 {DECISION_KO[decision['rule_decision']]} · AI 판정 {DECISION_KO[decision['model_decision']]}"
        numbers=f'<section class="numbers"><h2>판단 근거 수치</h2>'+(f'<table><tr><th>부위</th><th>대역</th><th>지표</th><th>값</th><th>규칙</th></tr>{"".join(rows_html)}</table>' if rows_html else '<p>인용된 수치가 없어요.</p>')+f'<p class="rule">{esc(rule_txt)}<br>{esc(decision["reason"])}</p></section>'
        number_lines=['',f'판정: {decision["label_ko"]}','판단 근거 수치']+number_lines+[rule_txt,decision['reason']]
    cards=[];plain=[]
    for number,item in enumerate(display_candidates(diagnosis),1):
        name=names.get(item.roi_id,'표시한 부위')
        rows=[r for r in evidence.get('region_comparisons',[]) if r['roi_id']==item.roi_id and r['eligible_for_interpretation']]
        increased=any(r.get('difference_px') is not None and r['difference_px']>0 for r in rows)
        label='추가 확인 후보 · 근거가 약해요' if item.confidence=='low' else '우선 확인 후보'
        observation=('흔들림이 더 커 보이지만, 다시 촬영해서 확인할 필요가 있어요.' if increased else '움직임 차이가 의심돼 추가 확인이 필요한 부위예요.') if item.confidence=='low' else ('정상 영상보다 흔들림이 더 크게 관찰됐어요.' if increased else '정상 영상과 움직임의 차이가 있어 확인해볼 부위예요.')
        action=item.action.strip()
        if not action or len(action)>140 or re.search(r'RMS|FFT|PSD|Hz|px|ROI|evidence|추적점',action,re.I):
            action='전원을 끈 뒤 표시한 부위의 고정 상태와 연결 부위가 헐겁지 않은지 확인해보세요.'
        if mode=='offline_synthetic':action='실제 촬영 영상으로 분석한 뒤 이 위치를 확인해보세요.'
        elif action.endswith('점검'):action=action[:-2].strip()+'를 확인해보세요.'
        plain.append((number,name,label,observation,action))
        cards.append(f'<article class="check"><div class="number">{number}</div><div><h3>{esc(name)}</h3><div class="label">{esc(label)}</div><p>{esc(observation)}</p><p class="action">{esc(action)}</p></div></article>')
    note='영상에서 관찰한 차이를 바탕으로 한 추측이에요. 표시한 곳이 고장 원인이라는 뜻은 아니에요.'
    banner='<div class="demo">예시 실행 결과예요. 실제 AI가 판독한 진단서는 아니에요.</div>' if mode not in LIVE_MODES else ''
    image=next((Path(x) for x in artifacts if Path(x).name=='inspection_roi.png'),None)
    picture=''
    if image and diagnosis.inspection_candidates:
        picture=f'<figure><img src="{esc(image.relative_to(out).as_posix())}" alt="확인해볼 부위의 위치"><figcaption>주황색 표시와 번호는 아래 점검 안내의 위치예요.</figcaption></figure>'
    empty='<p>현재 특정 부위를 점검 후보로 지목할 근거가 충분하지 않아요.</p>' if not cards else ''
    lines=['영상 점검 안내',title,summary]
    if mode not in LIVE_MODES:lines+=['예시 실행 결과: 실제 AI 판독이 아닙니다.']
    for number,name,label,observation,action in plain:lines+=['',f'{number}. {name}',label,observation,action]
    lines+=number_lines+['',note]
    (out/'diagnosis.txt').write_text('\n'.join(lines),encoding='utf-8')
    text=f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>흔들림 점검 안내</title><style>
    *{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,sans-serif;background:#f5f7fa;color:#253449;margin:0}}main{{max-width:880px;margin:auto;padding:36px 24px}}.label{{color:#687789;font-size:14px}}h1{{font-size:30px;line-height:1.35;margin:12px 0}}.intro{{font-size:18px;line-height:1.7}}.demo{{background:#fff0ce;color:#735211;border-radius:10px;padding:12px 16px;margin:18px 0;font-size:14px}}.layout{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px;align-items:start}}figure{{margin:0;background:#fff;border-radius:16px;padding:12px}}img{{display:block;width:100%;max-height:620px;object-fit:contain}}figcaption{{font-size:13px;color:#687789;line-height:1.6;padding:10px 6px}}.check{{background:#fff;border-radius:14px;padding:20px;display:flex;gap:14px;margin-bottom:14px}}.number{{background:#f56918;color:white;min-width:30px;height:30px;border-radius:50%;text-align:center;line-height:30px;font-weight:700}}h2{{font-size:20px;margin:0 0 16px}}h3{{font-size:19px;margin:2px 0 12px}}p{{line-height:1.7}}.check p{{margin:8px 0}}.action{{font-weight:600}}.note{{font-size:14px;color:#687789;line-height:1.7;margin-top:24px}}footer{{margin-top:20px}}a{{color:#376c96;font-size:14px}}.numbers{{background:#fff;border-radius:14px;padding:20px;margin-top:24px}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid #e3e8ef;padding:6px 8px;text-align:left}}.rule{{font-size:13px;color:#687789}}@media(max-width:640px){{main{{padding:24px 16px}}h1{{font-size:25px}}.layout{{grid-template-columns:1fr}}img{{max-height:430px}}}}
    </style></head><body><main><div class="label">영상으로 살펴본 흔들림 · 추측 점검 안내</div><h1>{esc(title)}</h1><p class="intro">{esc(summary)}</p>{banner}<div class="layout">{picture}<section><h2>어디를 확인하면 좋을까요?</h2>{''.join(cards)}{empty}</section></div>{numbers}<p class="note">{esc(note)}</p><footer><a href="diagnosis.txt" download>점검 안내 저장</a></footer></main></body></html>'''
    (out/'report.html').write_text(text,encoding='utf-8')
