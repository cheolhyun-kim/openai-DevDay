"""Known synthetic motion + fake model responses; never a live diagnosis."""
from pathlib import Path
import cv2
import numpy as np
from .contracts import ROIPlan, Diagnosis
from .workflow import run_pipeline

PLAN={'object_name':'합성 측정 대상','same_setup_assessment':'consistent','regions':[{'id':'head','part_name':'합성 본체','role':'target','reference':{'shape':'rectangle','bounds_xywh':[105/320,75/240,110/320,90/240],'points_xy':None},'candidate':{'shape':'rectangle','bounds_xywh':[105/320,75/240,110/320,90/240],'points_xy':None},'semantic_confidence':'high','caution':'Synthetic fixture, not object recognition.'},{'id':'background_left','part_name':'고정 배경','role':'background','reference':{'shape':'rectangle','bounds_xywh':[5/320,5/240,75/320,230/240],'points_xy':None},'candidate':{'shape':'rectangle','bounds_xywh':[5/320,5/240,75/320,230/240],'points_xy':None},'semantic_confidence':'high','caution':''}], 'relative_pairs':[], 'limitations':['Synthetic offline fixture.']}

def videos(directory,frames=360):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True);rng=np.random.default_rng(4)
    base=np.full((240,320,3),35,np.uint8)
    for lo,hi in [(5,75),(245,315)]:
        for _ in range(65):
            x,y=int(rng.integers(lo,hi)),int(rng.integers(10,230));cv2.rectangle(base,(x,y),(x+3,y+3),(230,230,230),-1)
    obj=np.full((90,110,3),65,np.uint8)
    for x in range(10,105,15):
        for y in range(10,85,15):cv2.rectangle(obj,(x,y),(x+4,y+4),(250,250,250),-1)
    paths=[]
    for name,amplitude in [('normal',.35),('candidate',1.8)]:
        path=directory/(name+'.avi');writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),30,(320,240))
        if not writer.isOpened():raise RuntimeError('MJPG writer unavailable')
        try:
            for i in range(frames):
                t=i/120;shift=amplitude*np.sin(2*np.pi*23.4*t);frame=base.copy();frame[75:165,105:215]=obj
                moved=cv2.warpAffine(frame,np.array([[1,0,shift],[0,1,0]],float),(320,240));frame[70:170,95:225]=moved[70:170,95:225]
                frame=cv2.warpAffine(frame,np.array([[1,0,2*np.sin(2*np.pi*.8*t)],[0,1,np.sin(2*np.pi*.5*t)]],float),(320,240),borderMode=cv2.BORDER_REFLECT);writer.write(frame)
        finally:writer.release()
        paths.append(path)
    return paths

class SyntheticProvider:
    mode='offline_synthetic'
    def __init__(self):self.calls=[]
    def propose_rois(self,metadata,images,correction=None):
        self.calls.append({'stage':'roi','source':'synthetic_fixture'})
        return ROIPlan.model_validate(PLAN)
    def diagnose(self,evidence,images):
        self.calls.append({'stage':'diagnosis','source':'synthetic_fixture'})
        rows=[r for r in evidence['region_comparisons'] if r['roi_id']=='head' and r['eligible_for_interpretation']]
        row=next((r for r in rows if r['rule_flag']=='increase'),rows[0])
        basis=[{'evidence_id':row['evidence_id'],'metric':m,'value':row[m]} for m in ('ratio','z_score','candidate_snr') if row.get(m) is not None]
        abnormal=evidence['rule_prefilter']['suggested_decision']=='abnormal'
        return Diagnosis(assessment='suspected_abnormal' if abnormal else 'no_clear_difference',is_abnormal=abnormal,abnormality_suspected=abnormal,fault_confirmed=False,summary='오프라인 연결 검증용 합성 데이터입니다. API 모델 추론이나 실제 선풍기 진단 결과가 아닙니다.',decision_basis=basis,inspection_candidates=[{'roi_id':'head','reason':'합성 본체의 흔들림이 기준 영상보다 커졌습니다.','evidence_ids':[row['evidence_id']],'confidence':'low','alternative_explanations':['Known synthetic fixture'],'action':'실제 촬영 영상과 API로 별도 검증 필요'}] if abnormal else [],limitations=evidence['limitations'],recommended_validation=['실제 API와 촬영 영상으로 연결 검증'])

def run_demo(output_dir,on_progress=None):
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()):raise ValueError('Use new/empty demo directory')
    normal,candidate=videos(out/'input')
    return run_pipeline(normal,candidate,capture_fps=120,output_dir=out/'run',provider=SyntheticProvider(),bands=[('rotation',20.,27.)],on_progress=on_progress,conditions={'fixed_camera_confirmed':True})
