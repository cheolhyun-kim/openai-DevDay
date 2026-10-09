"""API -> measurement -> API, callable from CLI, web or desktop UI."""
import json
import math
import shutil
from pathlib import Path
from .contracts import ROIPlan, Diagnosis, to_config
from .providers import OpenAIProvider
from .video import prepare_video
from .measurement import measure, validate_bands, build_evidence
from .render import heatmaps, spectrum_comparison, inspection_map, reports
from .registration import register, transfer_plan
from .vendor.fanvib.pipeline import roi_mask
import cv2

class PipelineError(RuntimeError): pass

def write_json(path,value):
    destination=Path(path)
    temporary=destination.with_suffix(destination.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(destination)

def check_plan(plan,metadata):
    configs={}
    for side in ['reference','candidate']:
        cfg=to_config(plan,metadata[side]['frame_size'],metadata[side]['capture_fps'],side)
        first=cv2.imread(metadata[side]['frames'][0]['path']);gray=cv2.cvtColor(first,cv2.COLOR_BGR2GRAY)
        bg_count=0
        for roi in cfg['rois']:
            mask=roi_mask(gray.shape,roi)
            if mask.sum()/255<16: raise ValueError(f'{side}/{roi["id"]}: ROI too small')
            points=cv2.goodFeaturesToTrack(gray,150,.008,8,mask=mask,blockSize=7)
            if roi['role']=='background':bg_count+=0 if points is None else len(points)
        if bg_count<6: raise ValueError(f'{side}: background needs at least six initial features')
        configs[side]=cfg
    return configs

def _evidence_row(evidence,eid):
    return next((r for r in evidence.get('region_comparisons',[])+evidence.get('relative_comparisons',[]) if r['evidence_id']==eid),None)

def validate_diagnosis(diagnosis,evidence):
    allowed=set(evidence['allowed_evidence_ids']);eligible={r['roi_id'] for r in evidence['region_comparisons'] if r['eligible_for_interpretation']}
    for item in diagnosis.inspection_candidates:
        if item.roi_id not in eligible or not item.evidence_ids or not set(item.evidence_ids)<=allowed:
            raise ValueError('Model referenced unknown/excluded ROI or evidence')
        for eid in item.evidence_ids:
            match=next((r for r in evidence['region_comparisons'] if r['evidence_id']==eid),None)
            rel=next((r for r in evidence['relative_comparisons'] if r['evidence_id']==eid),None)
            if not (match and match['roi_id']==item.roi_id or rel and item.roi_id in [rel['a'],rel['b']]):
                raise ValueError('Inspection evidence belongs to another region')
    # Every number the model cites must exist in the evidence (no invented figures).
    for c in diagnosis.decision_basis:
        if c.evidence_id not in allowed:
            raise ValueError(f'decision_basis cites excluded/unknown evidence {c.evidence_id}')
        row=_evidence_row(evidence,c.evidence_id);actual=None if row is None else row.get(c.metric)
        if actual is None:
            raise ValueError(f'decision_basis cites {c.metric} that is absent for {c.evidence_id}')
        tolerance=max(.02*abs(actual),5e-4 if c.metric.endswith('_px') else .02)
        if abs(c.value-actual)>tolerance:
            raise ValueError(f'decision_basis value {c.value} for {c.evidence_id}.{c.metric} does not match evidence {actual}')
    return diagnosis

def model_view(evidence):
    """What the diagnosis model needs: comparisons, floors, pre-filter, setup facts and per-region tracking
    quality. Raw per-mode spectra/peaks stay in evidence.json (fewer tokens, same allowed IDs)."""
    view={k:v for k,v in evidence.items() if k!='measurements'}
    keep_meta=('decoded_frames','timestamp_resampled','missing_frame_fraction','timestamp_warning','capture_fps','nyquist_hz')
    view['tracking_summary']={side:({'error':m['error']} if m.get('status')=='failed' else {
        'metadata':{k:m.get('metadata',{}).get(k) for k in keep_meta},'quality':m.get('quality'),
        'regions':{n:{k:r.get(k) for k in ('role','status','initial_points','retained_points','tracking_lost_points','flicker_excluded_points')} for n,r in m.get('regions',{}).items()}})
        for side,m in evidence.get('measurements',{}).items()}
    for key in ('region_comparisons',):
        view[key]=[{k:v for k,v in row.items() if not k.endswith('compensation_sensitivity')} for row in evidence.get(key,[])]
    return view

def final_decision(diagnosis,evidence):
    """The API is the final adjudicator; the rule pre-filter is retained as advice."""
    prefilter=evidence.get('rule_prefilter',{});rule=prefilter.get('suggested_decision','insufficient')
    model={True:'abnormal',False:'normal',None:'insufficient'}[diagnosis.is_abnormal]
    if not evidence.get('allowed_evidence_ids') and model!='insufficient':
        model='insufficient'
    labels={'abnormal':'비정상','normal':'정상','insufficient':'판단 불가'}
    reason='AI가 측정 수치와 품질 정보를 종합해 최종 판정했습니다.'
    return {'label':model,'label_ko':labels[model],'model_decision':model,'rule_decision':rule,
            'agreement':model==rule,'reason':reason,
            'cited_numbers':[c.model_dump() for c in diagnosis.decision_basis],
            'rule_increase_evidence_ids':sorted(prefilter.get('increase_evidence_ids',[])),
            'rule_decrease_evidence_ids':sorted(prefilter.get('decrease_evidence_ids',[])),
            'rule_frequency_change_evidence_ids':sorted(prefilter.get('frequency_change_evidence_ids',[]))}

def align_plan(plan,metadata,enabled=True):
    """Map reference ROIs into the candidate frame via ORB+RANSAC; fall back to model geometry."""
    if not enabled:return plan,{'ok':False,'reason':'disabled'},{'applied':False,'reason':'disabled'}
    alignment=register(metadata['reference']['frames'][0]['path'],metadata['candidate']['frames'][0]['path'])
    moved,report=transfer_plan(plan,alignment,metadata['reference']['frame_size'],metadata['candidate']['frame_size'])
    if report.get('applied'):
        try:check_plan(moved,metadata);return moved,alignment,report
        except ValueError as exc:report={'applied':False,'reason':f'transferred ROI rejected: {exc}'}
    return plan,alignment,report

def run_pipeline(reference_video,candidate_video,*,capture_fps,output_dir,candidate_capture_fps=None,provider=None,model=None,bands=None,frame_count=3,roi_plan_path=None,roi_repair_attempts=1,conditions=None,on_progress=None,align_candidate=True,rule_thresholds=None,diagnosis_repair_attempts=1):
    """Returns artifact paths and structured diagnosis. Does not depend on a UI.

    Live mode uploads representative/derived images and evidence, never MP4s.
    on_progress receives {stage,status,detail}; failures remain in status.json.
    A new, empty output directory is required to avoid overwriting a prior run.
    """
    out=Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()): raise ValueError('Use a new/empty output directory')
    if not 0<=roi_repair_attempts<=1:raise ValueError('At most one explicit ROI repair retry')
    fps2=candidate_capture_fps if candidate_capture_fps is not None else capture_fps
    if not all(math.isfinite(f) and f>0 for f in [capture_fps,fps2]): raise ValueError('Explicit capture FPS must be positive')
    selected_bands=validate_bands(bands,[capture_fps,fps2]);out.mkdir(parents=True,exist_ok=True)
    events=[];model_provider=provider
    def emit(stage,status,detail=''):
        event={'stage':stage,'status':status,'detail':detail};events.append(event);write_json(out/'status.json',{'events':events,'stage':stage,'status':status})
        if on_progress:on_progress(event)
    try:
        emit('frames','running')
        metadata={side:prepare_video(video,fps,out/'frames'/side,frame_count) for side,video,fps in [('reference',reference_video,capture_fps),('candidate',candidate_video,fps2)]}
        write_json(out/'frames.json',metadata);emit('frames','completed')
        if model_provider is None:model_provider=OpenAIProvider(model=model)
        images=[(f'{side} frame={f["frame_index"]}, upright_size={metadata[side]["frame_size"]}',f['path']) for side in metadata for f in metadata[side]['frames']]
        # Avoid leaking absolute local paths into the API's metadata text.
        api_metadata={side:{k:v for k,v in m.items() if k!='frames'} for side,m in metadata.items()}
        emit('roi_api','running','saved plan' if roi_plan_path else model_provider.mode)
        correction=None
        for attempt in range(roi_repair_attempts+1):
            try:
                plan=ROIPlan.model_validate_json(Path(roi_plan_path).read_text(encoding='utf-8')) if roi_plan_path else model_provider.propose_rois(api_metadata,images,correction)
                plan=ROIPlan.model_validate(plan.model_dump());configs=check_plan(plan,metadata);break
            except ValueError as exc:
                write_json(out/f'roi_rejection_{attempt}.json',{'reason':str(exc)})
                if roi_plan_path or attempt==roi_repair_attempts:raise PipelineError('ROI rejected; review roi_rejection file and provide a corrected plan') from exc
                correction=('Your previous ROIPlan was rejected by the validator. Fix exactly the problem below and return the FULL corrected ROIPlan.\n'
                            'Checklist: unique ASCII ids; at least one role="background" (static wall/floor/furniture texture) and at least one role="target"; '
                            'every rectangle/polygon inside [0,1]; relative_pairs only between two different target ids (or an empty list).\n'
                            'Validator message:\n'+str(exc))
        emit('roi_api','completed');emit('alignment','running')
        plan,alignment,transfer=align_plan(plan,metadata,align_candidate);configs=check_plan(plan,metadata)
        write_json(out/'alignment.json',{'registration':alignment,'roi_transfer':transfer})
        emit('alignment','completed' if transfer.get('applied') else 'skipped',f"scale={alignment.get('scale',1):.4f}" if alignment.get('ok') else alignment.get('reason',''))
        write_json(out/'roi_plan.json',plan.model_dump());write_json(out/'configs.json',configs)
        # Format/coordinate repair must not consume the separate tracking repair budget.
        tracking_repairs_used=0
        tracking_attempt=0
        while True:
            measurements={};maps={}
            for side,video in [('reference',reference_video),('candidate',candidate_video)]:
                emit('measurement_'+side,'running')
                try:
                    measurements[side],maps[side]=measure(video,configs[side],out/'measurements'/side,selected_bands)
                except ValueError as exc:
                    measurements[side]={'status':'failed','error':str(exc),'regions':{},'relative':{}}
                emit('measurement_'+side,'completed' if side in maps else 'insufficient',measurements[side].get('error',''))
            failures={side:m['error'] for side,m in measurements.items() if m.get('status')=='failed' and ('background features' in m['error'].lower() or 'background transform failed' in m['error'].lower())}
            if not failures or roi_plan_path or tracking_repairs_used>=roi_repair_attempts:
                break
            archive=out/'roi_attempts'/f'tracking_{tracking_attempt}'
            archive.mkdir(parents=True,exist_ok=True)
            write_json(archive/'roi_plan.json',plan.model_dump())
            write_json(archive/'configs.json',configs)
            write_json(archive/'measurement_summary.json',measurements)
            if (out/'measurements').exists():shutil.move(str(out/'measurements'),str(archive/'measurements'))
            correction=json.dumps({'stage':'full_video_background_tracking','failures':failures,'previous_plan':plan.model_dump(),'request':'Replace failed background ROIs with larger static textured regions visible in all supplied frames. Preserve target IDs and target coordinates where suitable. Avoid moving blades, the fan, blank walls, reflections and occlusions. Never relax measurement thresholds.'},ensure_ascii=False)
            emit('roi_tracking_repair','running','배경 추적 실패로 ROI 재선택')
            tracking_repairs_used+=1
            repaired=model_provider.propose_rois(api_metadata,images,correction)
            try:
                repaired=ROIPlan.model_validate(repaired.model_dump())
                repaired_configs=check_plan(repaired,metadata)
            except ValueError as exc:
                write_json(archive/'repair_rejection.json',{'reason':str(exc)})
                # Keep the failed measurements and original plan for an honest inconclusive report.
                if (archive/'measurements').exists():shutil.move(str(archive/'measurements'),str(out/'measurements'))
                emit('roi_tracking_repair','rejected',str(exc))
                break
            plan,configs=repaired,repaired_configs
            plan,alignment,transfer=align_plan(plan,metadata,align_candidate);configs=check_plan(plan,metadata)
            write_json(out/'alignment.json',{'registration':alignment,'roi_transfer':transfer})
            write_json(out/'roi_plan.json',plan.model_dump());write_json(out/'configs.json',configs)
            emit('roi_tracking_repair','completed','새 ROI로 정상·대상 영상 모두 재측정')
            tracking_attempt+=1
        facts={'fixed_camera_confirmed':False,'same_setup_declared':True}
        if conditions:facts.update(conditions)
        evidence=build_evidence(plan,api_metadata,measurements,selected_bands,facts,alignment=alignment,rules=rule_thresholds);write_json(out/'evidence.json',evidence)
        emit('maps','running');artifacts=[]
        if len(maps)==2:
            artifacts=heatmaps(out/'visuals',{s:metadata[s]['frames'][0]['path'] for s in metadata},maps,evidence,selected_bands)
            spectrum=spectrum_comparison(out/'visuals',{s:out/'measurements'/s for s in ('reference','candidate')},
                [r.model_dump() for r in plan.regions if r.role=='target'],alignment,selected_bands)
            if spectrum is not None:artifacts.append(spectrum)
        diagnosis_images=images.copy()
        for side in metadata:
            p=out/'measurements'/side/'roi.png'
            if p.exists():artifacts.append(p);diagnosis_images.append((side+' proposed ROI overlay',p))
        diagnosis_images.extend(('Measured heatmap '+p.stem,p) for p in artifacts if p.parent.name=='visuals' and p.name!='spectrum_comparison.png')
        emit('maps','completed');emit('diagnosis_api','running')
        if not evidence['allowed_evidence_ids']:
            diagnosis=Diagnosis(assessment='inconclusive',is_abnormal=None,abnormality_suspected=False,fault_confirmed=False,summary='배경 잡음보다 큰 움직임이 없거나 촬영 대응이 불확실해 판정을 보류합니다.',decision_basis=[],inspection_candidates=[],limitations=evidence['limitations'],recommended_validation=['ROI와 추적 품질을 확인하고 동일 조건에서 다시 촬영하세요.'])
            emit('diagnosis_api','skipped','No eligible evidence; local inconclusive result, not an API inference')
        else:
            request=model_view(evidence)
            for attempt in range(diagnosis_repair_attempts+1):
                try:
                    diagnosis=validate_diagnosis(Diagnosis.model_validate(model_provider.diagnose(request,diagnosis_images).model_dump()),evidence);break
                except ValueError as exc:
                    write_json(out/f'diagnosis_rejection_{attempt}.json',{'reason':str(exc)})
                    if attempt==diagnosis_repair_attempts:raise PipelineError('Diagnosis rejected by evidence check; see diagnosis_rejection file') from exc
                    emit('diagnosis_api','running','근거 수치 불일치로 진단 재요청')
                    request={**model_view(evidence),'previous_answer_rejected':str(exc)}
            emit('diagnosis_api','completed')
        write_json(out/'diagnosis.json',diagnosis.model_dump())
        decision=final_decision(diagnosis,evidence);write_json(out/'decision.json',decision)
        visual=out/'visuals';visual.mkdir(exist_ok=True)
        overlay=visual/'inspection_roi.png'
        inspection_map(metadata['candidate']['frames'][0]['path'],configs['candidate'],diagnosis,overlay,evidence=evidence,maps=maps);artifacts.append(overlay)
        reports(out,diagnosis,evidence,artifacts,model_provider.mode,decision=decision)
        result={'mode':model_provider.mode,'output_dir':str(out),'report_html':str(out/'report.html'),'diagnosis_json':str(out/'diagnosis.json'),'evidence_json':str(out/'evidence.json'),'roi_plan_json':str(out/'roi_plan.json'),'visualizations':[str(p) for p in artifacts],'diagnosis':diagnosis.model_dump(),'decision':decision,'model_calls':getattr(model_provider,'calls',[])}
        write_json(out/'result.json',result);emit('finished','completed');return result
    except Exception as exc:
        emit('failed','failed',f'{type(exc).__name__}: {exc}')
        if model_provider is not None:write_json(out/'model_calls.json',getattr(model_provider,'calls',[]))
        raise
