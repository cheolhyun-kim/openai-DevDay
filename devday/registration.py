"""Align the candidate recording to the reference one and transfer reference ROIs.

Two separate shots of the same machine are never framed identically. Instead of trusting
independently chosen candidate ROIs, the code estimates a similarity transform (scale, rotation,
translation) between the first frames with ORB + RANSAC. Reference ROIs are then mapped into the
candidate frame so both videos measure the same physical patch, and candidate amplitudes are
divided by the scale so both are expressed in reference pixels.
"""
import math
import cv2
import numpy as np
from .contracts import ROIPlan

MIN_INLIERS = 25
MIN_INLIER_FRACTION = .25


def register(reference_frame, candidate_frame, max_side=1280):
    """Similarity transform mapping reference pixel coordinates -> candidate pixel coordinates."""
    a = cv2.imread(str(reference_frame), cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(str(candidate_frame), cv2.IMREAD_GRAYSCALE)
    if a is None or b is None:
        return {'ok': False, 'reason': 'cannot read representative frames'}
    # Work at a bounded resolution for speed; convert back to full-resolution pixels afterwards.
    fa = min(1.0, max_side / max(a.shape)); fb = min(1.0, max_side / max(b.shape))
    sa = cv2.resize(a, None, fx=fa, fy=fa, interpolation=cv2.INTER_AREA) if fa < 1 else a
    sb = cv2.resize(b, None, fx=fb, fy=fb, interpolation=cv2.INTER_AREA) if fb < 1 else b
    orb = cv2.ORB_create(4000)
    ka, da = orb.detectAndCompute(sa, None); kb, db = orb.detectAndCompute(sb, None)
    if da is None or db is None or len(ka) < MIN_INLIERS or len(kb) < MIN_INLIERS:
        return {'ok': False, 'reason': 'too few ORB features'}
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m for m in (p for p in pairs if len(p) == 2) if m[0].distance < .75 * m[1].distance]
    if len(good) < MIN_INLIERS:
        return {'ok': False, 'reason': f'only {len(good)} distinctive matches'}
    pa = np.float32([ka[m[0].queryIdx].pt for m in good]) / fa
    pb = np.float32([kb[m[0].trainIdx].pt for m in good]) / fb
    M, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=3.0 / min(fa, fb), maxIters=5000, confidence=.999)
    if M is None:
        return {'ok': False, 'reason': 'RANSAC failed'}
    inl = inl[:, 0].astype(bool)
    proj = pa[inl] @ M[:, :2].T + M[:, 2]
    err = np.linalg.norm(proj - pb[inl], axis=1)
    scale = float(math.hypot(M[0, 0], M[1, 0])); angle = float(math.degrees(math.atan2(M[1, 0], M[0, 0])))
    info = {'matrix_ref_to_cand': M.tolist(), 'scale': scale, 'rotation_deg': angle,
            'translation_px': [float(M[0, 2]), float(M[1, 2])], 'matches': len(good), 'inliers': int(inl.sum()),
            'inlier_fraction': float(inl.mean()), 'median_reprojection_error_px': float(np.median(err)) if len(err) else None}
    ok = inl.sum() >= MIN_INLIERS and inl.mean() >= MIN_INLIER_FRACTION and .5 < scale < 2 and abs(angle) < 30
    info['ok'] = bool(ok)
    if not ok:
        info['reason'] = 'registration not reliable (inliers/scale/rotation outside limits)'
    return info


def transfer_plan(plan: ROIPlan, alignment, ref_size, cand_size):
    """Replace candidate geometry with the reference geometry mapped through the registration.

    Returns (new_plan, report). Regions whose mapped geometry leaves the frame keep the model's
    candidate geometry and are listed in the report.
    """
    if not alignment or not alignment.get('ok'):
        return plan, {'applied': False, 'reason': (alignment or {}).get('reason', 'no registration')}
    M = np.array(alignment['matrix_ref_to_cand'], float)
    rw, rh = ref_size; cw, ch = cand_size
    axis_aligned = abs(alignment['rotation_deg']) < 1.0
    data = plan.model_dump(); kept = []
    for region in data['regions']:
        g = region['reference']
        if g['shape'] == 'rectangle':
            x, y, w, h = g['bounds_xywh']
            pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], float)
        else:
            pts = np.array(g['points_xy'], float)
        px = pts * [rw, rh]
        mapped = (px @ M[:, :2].T + M[:, 2]) / [cw, ch]
        if mapped.min() < -.01 or mapped.max() > 1.01:
            kept.append(region['id']); continue
        mapped = np.clip(mapped, 0, 1)  # sub-percent overflow at the frame edge
        if g['shape'] == 'rectangle' and axis_aligned:
            x0, y0 = mapped.min(axis=0); x1, y1 = mapped.max(axis=0)
            region['candidate'] = {'shape': 'rectangle', 'bounds_xywh': [float(x0), float(y0), float(min(x1 - x0, 1 - x0)), float(min(y1 - y0, 1 - y0))], 'points_xy': None}
        else:
            region['candidate'] = {'shape': 'polygon', 'bounds_xywh': None, 'points_xy': [[float(a), float(b)] for a, b in mapped]}
    return ROIPlan.model_validate(data), {'applied': True, 'regions_kept_model_geometry': kept}
