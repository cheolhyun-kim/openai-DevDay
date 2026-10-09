"""Strict model boundaries; geometry validation is code's responsibility."""
import math
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

class BoxOrPolygon(StrictModel):
    shape: Literal['rectangle', 'polygon']
    bounds_xywh: list[float] | None
    points_xy: list[list[float]] | None

    @model_validator(mode='after')
    def geometry(self):
        if self.shape == 'rectangle':
            if self.bounds_xywh is None or len(self.bounds_xywh) != 4 or self.points_xy is not None:
                raise ValueError('Rectangle requires four bounds and null points')
            x,y,w,h=self.bounds_xywh
            if not all(math.isfinite(v) for v in [x,y,w,h]) or min(x,y)<0 or min(w,h)<=0 or x+w>1.000001 or y+h>1.000001:
                raise ValueError('Normalized rectangle must lie inside [0,1]')
        else:
            if self.bounds_xywh is not None or self.points_xy is None or len(self.points_xy)<3:
                raise ValueError('Polygon requires at least three points and null bounds')
            if any(len(p)!=2 or not all(math.isfinite(v) and 0<=v<=1 for v in p) for p in self.points_xy):
                raise ValueError('Normalized polygon points must lie inside [0,1]')
            area=abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(self.points_xy,self.points_xy[1:]+self.points_xy[:1])))/2
            if area<1e-6: raise ValueError('Degenerate polygon')
        return self

class Region(StrictModel):
    id: str
    part_name: str
    role: Literal['target','background']
    reference: BoxOrPolygon
    candidate: BoxOrPolygon
    semantic_confidence: Literal['low','medium','high']
    caution: str

class RelativePair(StrictModel):
    a: str
    b: str

class ROIPlan(StrictModel):
    object_name: str
    same_setup_assessment: Literal['consistent','uncertain','different']
    regions: list[Region]
    relative_pairs: list[RelativePair]
    limitations: list[str]

    @model_validator(mode='after')
    def identities(self):
        names=[r.id for r in self.regions]
        if not names or len(set(names))!=len(names) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',n) for n in names):
            raise ValueError('ROI IDs must be unique ASCII identifiers')
        if len(names)>16: raise ValueError('At most 16 ROIs for this prototype')
        roles={r.id:r.role for r in self.regions}
        if 'background' not in roles.values() or 'target' not in roles.values():
            raise ValueError('Require static background and target ROIs: at least one region with role "background" '
                             f'and one with role "target". Got: {", ".join(f"{k}({v})" for k,v in roles.items())}')
        # Relative pairs are optional extras: drop invalid ones (background/unknown/self/duplicate) instead of
        # rejecting the whole plan, and say so in limitations.
        kept,seen,dropped=[],set(),[]
        for p in self.relative_pairs:
            key=frozenset((p.a,p.b))
            if roles.get(p.a)=='target' and roles.get(p.b)=='target' and p.a!=p.b and key not in seen:
                kept.append(p);seen.add(key)
            else:dropped.append(f'{p.a}-{p.b}')
        if dropped:
            self.relative_pairs=kept
            self.limitations=self.limitations+[f'Code dropped invalid relative pairs (need two different target IDs): {", ".join(dropped)}']
        return self

class Inspection(StrictModel):
    roi_id: str
    reason: str
    evidence_ids: list[str]
    confidence: Literal['low','medium','high']
    alternative_explanations: list[str]
    action: str

CITABLE_METRICS=('reference_rms_px','candidate_rms_px','ratio','z_score','reference_snr','candidate_snr','snr_ratio')

class Citation(StrictModel):
    """One number the model relies on; code checks it against evidence.json."""
    evidence_id: str
    metric: Literal['reference_rms_px','candidate_rms_px','ratio','z_score','reference_snr','candidate_snr','snr_ratio']
    value: float

class Diagnosis(StrictModel):
    assessment: Literal['suspected_abnormal','no_clear_difference','inconclusive']
    is_abnormal: bool | None
    abnormality_suspected: bool
    fault_confirmed: bool
    summary: str
    decision_basis: list[Citation]
    inspection_candidates: list[Inspection]
    limitations: list[str]
    recommended_validation: list[str]

    @model_validator(mode='after')
    def consistent_decision(self):
        # A camera comparison can flag abnormal behaviour, never confirm a physical fault.
        if self.fault_confirmed:
            raise ValueError('fault_confirmed must be false: video comparison cannot confirm a physical fault')
        expected={'suspected_abnormal':True,'no_clear_difference':False,'inconclusive':None}[self.assessment]
        if self.is_abnormal is not expected:
            raise ValueError('is_abnormal must be true for suspected_abnormal, false for no_clear_difference, null for inconclusive')
        if self.abnormality_suspected != (self.assessment=='suspected_abnormal'):
            raise ValueError('Inconsistent assessment and suspicion flag')
        if self.is_abnormal is not None and not self.decision_basis:
            raise ValueError('A normal/abnormal decision must cite numeric evidence in decision_basis')
        return self

def to_config(plan: ROIPlan, frame_size, capture_fps: float, side: str, nperseg=512):
    """API normalized coordinates -> existing pipeline's upright pixel coordinates."""
    if side not in ('reference','candidate'): raise ValueError('Invalid side')
    if not math.isfinite(capture_fps) or capture_fps<=0: raise ValueError('Explicit positive capture FPS required')
    w,h=frame_size
    rois=[]
    for r in plan.regions:
        g=getattr(r,side)
        roi={'id':r.id,'role':r.role,'shape':g.shape}
        if g.shape=='rectangle':
            x,y,rw,rh=g.bounds_xywh
            roi['bounds']=[x*w,y*h,rw*w,rh*h]
        else:
            roi['points']=[[min(x*w,w-1),min(y*h,h-1)] for x,y in g.points_xy]
        rois.append(roi)
    return {'capture_fps':capture_fps,'frame_size':list(frame_size),'nperseg':nperseg,'rois':rois,'relative_pairs':[[p.a,p.b] for p in plan.relative_pairs]}
