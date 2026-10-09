"""Retain the original LK/Welch core; add paired evidence and point maps."""
import math
import re
from pathlib import Path
import numpy as np
from scipy.signal import welch, periodogram, find_peaks
from scipy.integrate import trapezoid
from scipy.stats import kurtosis
from .vendor.fanvib.pipeline import analyze_video

DEFAULT_MODES=('raw','translation','affine')
# Primary comparison metric: band RMS of the ROI's median trajectory after background-fitted
# similarity (affine-partial) camera compensation. Averaging over points suppresses per-point
# tracking noise, and the similarity model removes camera roll/zoom that translation leaves behind.
PRIMARY_MODE='affine'
DEFAULT_RULES={'snr_min':2.0,'ratio_min':1.5,'z_min':3.0,'min_points':10}
# Code-verified framing: when registration finds the same scene at nearly the same scale/rotation,
# small shifts are already compensated by ROI transfer, so the model's 'uncertain' should not veto.
REGISTRATION_LIMITS={'min_inliers':50,'max_scale_change':.10,'max_rotation_deg':3.0}

def registration_verified(alignment,limits=REGISTRATION_LIMITS):
    if not alignment or not alignment.get('ok'):return False
    return (alignment.get('inliers',0)>=limits['min_inliers'] and abs(float(alignment.get('scale',0))-1)<=limits['max_scale_change']
            and abs(float(alignment.get('rotation_deg',99)))<=limits['max_rotation_deg'])

def setup_decision(plan,alignment):
    """(setup_ok, basis): 'different' always blocks; 'uncertain' passes only with verified registration."""
    if plan.same_setup_assessment=='consistent':return True,'model_consistent'
    if plan.same_setup_assessment=='uncertain' and registration_verified(alignment):return True,'model_uncertain_but_registration_verified'
    return False,'model_'+plan.same_setup_assessment+('' if plan.same_setup_assessment=='different' else '_and_registration_not_verified')

def n_windows(length):
    return 6 if length>=6*96 else 3

def band_shape(signal,fs,lo,hi):
    """Dominant frequency, its share of in-band power, and kurtosis of the band-passed signal."""
    x=np.asarray(signal,float)
    if x.ndim==1:x=x[:,None]
    x=x-x.mean(axis=0)
    # principal motion axis
    u=np.linalg.svd(x,full_matrices=False)[2][0];y=x@u
    f,p=periodogram(y,fs=fs,window='hann',detrend='linear')
    sel=(f>=lo)&(f<=hi)
    if sel.sum()<3 or p[sel].sum()<=0:return {'dominant_hz':None,'peak_power_share':None,'kurtosis':None}
    i=np.flatnonzero(sel)[np.argmax(p[sel])]
    share=float(p[max(i-2,0):i+3].sum()/p[sel].sum())
    spec=np.fft.rfft(y-y.mean());ff=np.fft.rfftfreq(len(y),1/fs);spec[(ff<lo)|(ff>hi)]=0
    bp=np.fft.irfft(spec,len(y))
    return {'dominant_hz':float(f[i]),'peak_power_share':share,'kurtosis':float(kurtosis(bp))}

def window_rms(signal,fs,lo,hi,nperseg):
    out=[]
    for part in np.array_split(np.asarray(signal),n_windows(len(signal))):
        v=band_rms(part,fs,lo,hi,nperseg);out.append(None if v is None else float(v))
    return out

def validate_bands(bands, fps_values):
    nyquist=min(fps_values)/2
    if bands is None:
        bands=[('low',1.,min(10.,nyquist*.8))]
        if nyquist>15: bands.append(('higher',10.,min(100.,nyquist*.8)))
    if not bands: raise ValueError('At least one frequency band required')
    names=[]
    for name,lo,hi in bands:
        if name in names or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}',name) or not all(math.isfinite(v) for v in [lo,hi]) or not 0<lo<hi<nyquist:
            raise ValueError('Bands must have unique IDs and 0 < low < high < both Nyquist limits')
        names.append(name)
    return bands

def band_rms(signal, fs, lo, hi, nperseg=512):
    f,p=welch(signal,fs=fs,nperseg=min(nperseg,len(signal)),detrend='linear',axis=0)
    p=p.sum(axis=-1);sel=(f>=lo)&(f<=hi)
    if sel.sum()<2: return None
    return np.sqrt(np.maximum(0,trapezoid(p[sel],f[sel],axis=0)))

def candidate_peaks(signal,fs):
    f,p=periodogram(signal,fs=fs,window='hann',detrend='linear',axis=0);p=p.sum(axis=-1)
    ix=find_peaks(p)[0];ix=[i for i in ix if f[i]>=.5];ix=sorted(ix,key=lambda i:p[i],reverse=True)[:5]
    return [{'hz':float(f[i]),'psd_px2_per_hz':float(p[i])} for i in ix]

def measure(video, config, directory, bands):
    result=analyze_video(video,config,directory)
    d=np.load(Path(directory)/'tracks.npz',allow_pickle=False)
    tr=d['tr'][:,d['keep']];labels=d['labels'][d['keep']];raw=tr-tr[0]
    bg_names=[r['id'] for r in config['rois'] if r['role']=='background'];bg=np.isin(labels,bg_names)
    translation=raw-np.median(raw[:,bg],axis=1)[:,None,:]
    signals=np.load(Path(directory)/'signals.npz',allow_pickle=False)
    fs=config['capture_fps'];regions={};point_values={};rel={}
    for name,lo,hi in bands:
        values=band_rms(translation,fs,lo,hi,config['nperseg'])
        point_values[name]=values
    for roi in config['rois']:
        n=roi['id'];mask=labels==n;r=result['regions'][n]
        rec={'status':r['status'],'role':roi['role'],'initial_points':r['initial_points'],'retained_points':r['retained_points'],'tracking_lost_points':r.get('tracking_lost_points'),'flicker_excluded_points':r.get('flicker_excluded_points'),'median_point_flicker_gray':r.get('median_point_flicker_gray'),'bands':{},'peaks':{}}
        if r['status']=='measured':
            for mode in DEFAULT_MODES:
                rec['peaks'][mode]=candidate_peaks(signals[n+'__'+mode],fs)
            for name,lo,hi in bands:
                vals=point_values[name]
                row={'point_median_rms_px':None if vals is None else float(np.median(vals[mask])), 'region_median_signal_rms_by_mode':{}, 'full_survivor_third_rms_px':[]}
                primary=signals[n+'__'+PRIMARY_MODE]
                v=band_rms(primary,fs,lo,hi,config['nperseg'])
                row['region_rms_px']=None if v is None else float(v)
                row['window_rms_px']=window_rms(primary,fs,lo,hi,config['nperseg'])
                row.update(band_shape(primary,fs,lo,hi))
                for mode in DEFAULT_MODES:
                    v=band_rms(signals[n+'__'+mode],fs,lo,hi,config['nperseg'])
                    row['region_median_signal_rms_by_mode'][mode]=None if v is None else float(v)
                for part in np.array_split(translation[:,mask],3):
                    v=band_rms(part,fs,lo,hi,config['nperseg'])
                    row['full_survivor_third_rms_px'].append(None if v is None else float(np.median(v)))
                rec['bands'][name]=row
        regions[n]=rec
    for a,b in config['relative_pairs']:
        name=a+'_minus_'+b;key=name+'__raw'
        if key not in signals.files: continue
        rel[name]={'a':a,'b':b,'bands':{}}
        for bn,lo,hi in bands:
            v=band_rms(signals[key],fs,lo,hi,config['nperseg'])
            rel[name]['bands'][bn]=None if v is None else float(v)
            rel[name].setdefault('window_rms_px',{})[bn]=window_rms(signals[key],fs,lo,hi,config['nperseg'])
    # Noise floor: what the same primary metric reports on static background regions.
    background={}
    for bn,lo,hi in bands:
        per={k:v['bands'][bn]['region_rms_px'] for k,v in regions.items() if v['role']=='background' and v['status']=='measured' and v['bands'][bn].get('region_rms_px') is not None}
        background[bn]={'floor_px':float(np.median(list(per.values()))) if per else None,'per_region_px':per}
    return {'metadata':result['metadata'],'quality':result['quality'],'warnings':result['warnings'],'regions':regions,'relative':rel,'background':background}, {'points':tr[0],'labels':labels,'band_values':point_values}

def safe_ratio(candidate,reference):
    if candidate is None or reference is None or reference<=1e-8: return None
    return candidate/reference

def _r(v,digits):
    return None if v is None else round(float(v),digits)

def compare(ref,cand,ref_floor,cand_floor,ref_windows,rules,strict=False):
    """Shared numbers + rule pre-flag for one reference/candidate pair (candidate already in reference pixels).

    strict: the ROI lost points to brightness flicker (guard wires with blades behind). Residual flicker can
    mimic a modest increase, so an increase there must be twice as strong (ratio and z)."""
    k=2.0 if strict else 1.0
    ratio=safe_ratio(cand,ref)
    ref_snr=safe_ratio(ref,ref_floor);cand_snr=safe_ratio(cand,cand_floor)
    snr_ratio=safe_ratio(cand_snr,ref_snr)
    w=[x for x in (ref_windows or []) if x is not None]
    z=None
    if ref is not None and cand is not None and len(w)>=3:
        sd=max(float(np.std(w,ddof=1)),.1*ref,1e-9);z=(cand-ref)/sd
    if ref is None or cand is None or ref_snr is None or cand_snr is None:flag='not_measurable'
    elif max(ref_snr,cand_snr)<rules['snr_min']:flag='below_noise_floor'
    elif ratio is not None and ratio>=k*rules['ratio_min'] and z is not None and z>=k*rules['z_min'] and cand_snr>=rules['snr_min'] and (snr_ratio or 0)>=rules['ratio_min']:flag='increase'
    elif ratio is not None and ratio<=1/rules['ratio_min'] and z is not None and z<=-rules['z_min']:flag='decrease'
    else:flag='no_significant_change'
    return {'ratio':_r(ratio,3),'z_score':_r(z,2),'reference_snr':_r(ref_snr,2),'candidate_snr':_r(cand_snr,2),'snr_ratio':_r(snr_ratio,3),'rule_flag':flag}

def region_usable(region,a,b,rules):
    """Medium/high-confidence ROIs need >=3 tracked points. A low-confidence ROI (the model is unsure of the
    part NAME) still counts as motion evidence with >= min_points on both sides. Flicker-affected ROIs are
    usable but judged with strict thresholds (see flickered/compare)."""
    pa,pb=a.get('retained_points',0),b.get('retained_points',0)
    if region.semantic_confidence!='low':return pa>=3 and pb>=3
    return min(pa,pb)>=rules['min_points']

def flickered(a,b):
    return bool((a.get('flicker_excluded_points') or 0) or (b.get('flicker_excluded_points') or 0))

def build_evidence(plan,metadata,measurements,bands,conditions,alignment=None,rules=None):
    rules={**DEFAULT_RULES,**(rules or {})}
    comparisons=[];relative=[];allowed=[];setup_ok,setup_basis=setup_decision(plan,alignment)
    ref=measurements.get('reference',{});cand=measurements.get('candidate',{})
    semantics={r.id:r for r in plan.regions}
    # Candidate pixels -> reference pixels (registration scale: candidate px per reference px).
    scale=float(alignment['scale']) if alignment and alignment.get('ok') else 1.0
    to_ref=lambda v:None if v is None else v/scale
    floors={bn:((ref.get('background') or {}).get(bn,{}).get('floor_px'),to_ref((cand.get('background') or {}).get(bn,{}).get('floor_px'))) for bn,_,_ in bands}
    for region in plan.regions:
        if region.role!='target': continue
        a=ref.get('regions',{}).get(region.id,{});b=cand.get('regions',{}).get(region.id,{})
        for bn,lo,hi in bands:
            av=a.get('bands',{}).get(bn,{});bv=b.get('bands',{}).get(bn,{})
            ar=av.get('region_rms_px');br=to_ref(bv.get('region_rms_px'))
            rf,cf=floors[bn]
            c=compare(ar,br,rf,cf,av.get('window_rms_px'),rules,strict=flickered(a,b))
            setup_eligible=setup_ok and region_usable(region,a,b,rules) and ar is not None and br is not None
            eligible=setup_eligible and c['rule_flag'] not in ('not_measurable','below_noise_floor')
            eid=region.id+':'+bn
            pm_a=av.get('point_median_rms_px');pm_b=to_ref(bv.get('point_median_rms_px'))
            row={'evidence_id':eid,'roi_id':region.id,'part_name':region.part_name,'band_hz':[lo,hi],
                 'reference_rms_px':_r(ar,5),'candidate_rms_px':_r(br,5),'difference_px':None if ar is None or br is None else _r(br-ar,5),
                 **c,
                 'reference_background_floor_px':_r(rf,5),'candidate_background_floor_px':_r(cf,5),
                 'reference_dominant_hz':_r(av.get('dominant_hz'),2),'candidate_dominant_hz':_r(bv.get('dominant_hz'),2),
                 'reference_peak_power_share':_r(av.get('peak_power_share'),3),'candidate_peak_power_share':_r(bv.get('peak_power_share'),3),
                 'reference_kurtosis':_r(av.get('kurtosis'),2),'candidate_kurtosis':_r(bv.get('kurtosis'),2),
                 'reference_points':a.get('retained_points',0),'candidate_points':b.get('retained_points',0),
                 'flicker_excluded_points':[a.get('flicker_excluded_points'),b.get('flicker_excluded_points')],
                 'eligible_for_interpretation':eligible,'low_semantic_confidence':region.semantic_confidence=='low','flicker_strict_thresholds':flickered(a,b),'sparse_tracking':min(a.get('retained_points',0),b.get('retained_points',0))<rules['min_points'],
                 'point_median_rms_px':[_r(pm_a,5),_r(pm_b,5)],
                 'third_ratios':[_r(safe_ratio(to_ref(y),x),3) for x,y in zip(av.get('full_survivor_third_rms_px',[]),bv.get('full_survivor_third_rms_px',[]))],
                 'reference_compensation_sensitivity':av.get('region_median_signal_rms_by_mode',{}),'candidate_compensation_sensitivity':bv.get('region_median_signal_rms_by_mode',{})}
            comparisons.append(row)
            if eligible: allowed.append(eid)
    for name,a in ref.get('relative',{}).items():
        b=cand.get('relative',{}).get(name)
        if b is None: continue
        for bn,lo,hi in bands:
            av=a['bands'][bn];bv=to_ref(b['bands'][bn]);eid='relative:'+name+':'+bn
            rf,cf=floors[bn]
            # Difference of two ROI trajectories: independent noise adds in quadrature.
            c=compare(av,bv,None if rf is None else rf*math.sqrt(2),None if cf is None else cf*math.sqrt(2),a.get('window_rms_px',{}).get(bn),rules)
            eligible=setup_ok and all(region_usable(semantics[x],ref['regions'][x],cand['regions'][x],rules) for x in [a['a'],a['b']]) and av is not None and bv is not None and c['rule_flag'] not in ('not_measurable','below_noise_floor')
            relative.append({'evidence_id':eid,'a':a['a'],'b':a['b'],'band_hz':[lo,hi],'reference_rms_px':_r(av,5),'candidate_rms_px':_r(bv,5),**c,'eligible_for_interpretation':eligible})
            if eligible: allowed.append(eid)
    increases=[r['evidence_id'] for r in comparisons if r['eligible_for_interpretation'] and r['rule_flag']=='increase' and not r['sparse_tracking']]
    increases+=[r['evidence_id'] for r in relative if r['eligible_for_interpretation'] and r['rule_flag']=='increase']
    measurable=[r['evidence_id'] for r in comparisons+relative if r['eligible_for_interpretation']]
    sparse_increases=[r['evidence_id'] for r in comparisons if r['eligible_for_interpretation'] and r['rule_flag']=='increase' and r['sparse_tracking']]
    solid=[r['evidence_id'] for r in comparisons if r['eligible_for_interpretation'] and not r['sparse_tracking']]+[r['evidence_id'] for r in relative if r['eligible_for_interpretation']]
    if increases:decision,reason='abnormal','At least one eligible comparison passes every increase rule.'
    elif sparse_increases:decision,reason='insufficient','Only sparsely tracked comparisons show an increase; not enough to decide either way.'
    elif solid:decision,reason='normal','Well-tracked eligible comparisons exist above the noise floor, and none passes the increase rules.'
    else:decision,reason='insufficient','No comparison is both setup-eligible and above the background noise floor.'
    prefilter={'suggested_decision':decision,'reason':reason,'increase_evidence_ids':increases,'measurable_evidence_ids':measurable,
               'thresholds':{'snr_min':rules['snr_min'],'ratio_min':rules['ratio_min'],'z_min':rules['z_min'],'min_points_for_abnormal':rules['min_points']},
               'rules':{'below_noise_floor':'max(reference_snr, candidate_snr) < snr_min',
                        'increase':'ratio >= ratio_min AND snr_ratio >= ratio_min AND z_score >= z_min AND candidate_snr >= snr_min',
                        'decrease':'ratio <= 1/ratio_min AND z_score <= -z_min',
                        'abnormal':'any eligible non-sparse comparison flagged increase','normal':'no increase at all and at least one eligible non-sparse comparison','low_semantic_confidence':'counts only with >= min_points on both sides','flicker_affected':'ROIs that lost points to brightness flicker need ratio >= 2*ratio_min and z >= 2*z_min for increase'}}
    return {'schema_version':'devday.evidence/2','roi_plan':plan.model_dump(),'videos':metadata,'conditions':conditions,'alignment':alignment,'setup':{'eligible':setup_ok,'basis':setup_basis,'model_assessment':plan.same_setup_assessment,'registration_limits':REGISTRATION_LIMITS},'measurements':measurements,
            'background_noise_floor_px':{bn:{'reference':_r(floors[bn][0],5),'candidate':_r(floors[bn][1],5)} for bn,_,_ in bands},
            'region_comparisons':comparisons,'relative_comparisons':relative,'rule_prefilter':prefilter,'allowed_evidence_ids':allowed,
            'method':{'primary_metric':'band RMS of the ROI median trajectory after background-fitted similarity camera compensation (affine mode)',
                      'noise_floor':'median of the same metric over static background ROIs; snr = target / floor',
                      'z_score':'(candidate - reference) / std of the reference metric over equal temporal windows (floor 10% of reference); windows of one clip are not independent experiments',
                      'candidate_scaling':'candidate pixel values divided by registration scale so both are in reference pixels' if scale!=1.0 else 'none (scale 1)',
                      'flicker_exclusion':'tracked points whose 7x7 brightness flickers above the threshold (e.g. blades behind a guard) are excluded',
                      'secondary_metrics':'point_median_rms_px = median per-point RMS after background-median translation; third_ratios use those points',
                      'relative_metric':'difference of ROI median trajectories; not a calibrated joint angle','unit':'image_pixel (reference video)','physical_calibration':None},
            'limitations':['Only one recording per condition; thresholds are engineering defaults, not validated on labelled faults.','Same setup is inferred/user-declared; registration only checks the first frames.','Low-frequency movement may include camera stabilization/parallax/compression/tracking noise.','Capture FPS is user supplied; VFR phone video is resampled from container timestamps when available.','Spectral peaks are not proven rotor RPM or fault orders.','Motion location is not excitation source.','Sparse/low-semantic-confidence ROIs must not support localization.']}
