import argparse
import json
import sys
from pathlib import Path
from .workflow import run_pipeline,write_json
from .providers import ReplayProvider
from .video import prepare_video
from .demo import run_demo

def parse_band(value):
    try:
        name,lo,hi=value.split(':');return name,float(lo),float(hi)
    except (ValueError,TypeError):raise argparse.ArgumentTypeError('Use name:low:high, e.g. low:1:10')

def main(argv=None):
    parser=argparse.ArgumentParser(description='DevDay: API -> rule-based -> API, independent of demo UI')
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run',help='Analyze paired videos; live mode sends derived images and evidence to OpenAI')
    run.add_argument('--normal',required=True);run.add_argument('--candidate',required=True);run.add_argument('--capture-fps',type=float,required=True)
    run.add_argument('--candidate-capture-fps',type=float);run.add_argument('--out',required=True);run.add_argument('--model');run.add_argument('--frames',type=int,default=3)
    run.add_argument('--band',type=parse_band,action='append');run.add_argument('--roi-plan',help='Reviewed saved ROI plan: skips first API call')
    run.add_argument('--provider',choices=['openai','replay'],default='openai');run.add_argument('--replay-roi');run.add_argument('--replay-diagnosis')
    run.add_argument('--fixed-camera',action='store_true');run.add_argument('--no-roi-retry',action='store_true')
    demo=commands.add_parser('demo',help='Synthetic OFFLINE end-to-end test; no API key or model call')
    demo.add_argument('--out',required=True)
    prepare=commands.add_parser('prepare',help='Extract representative frames only; no API')
    prepare.add_argument('video');prepare.add_argument('--capture-fps',required=True,type=float);prepare.add_argument('--frames',type=int,default=3);prepare.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    def progress(event):print(f"[{event['stage']}] {event['status']} {event['detail']}",flush=True)
    try:
        if args.command=='demo':result=run_demo(args.out,progress)
        elif args.command=='prepare':
            path=Path(args.out)
            if path.exists() and any(path.iterdir()):raise ValueError('Use new/empty output directory')
            result=prepare_video(args.video,args.capture_fps,path,args.frames);write_json(path/'frames.json',result)
        else:
            provider=None
            if args.provider=='replay':
                if not args.replay_roi or not args.replay_diagnosis:parser.error('Replay requires --replay-roi and --replay-diagnosis')
                provider=ReplayProvider(args.replay_roi,args.replay_diagnosis)
            result=run_pipeline(args.normal,args.candidate,capture_fps=args.capture_fps,candidate_capture_fps=args.candidate_capture_fps,output_dir=args.out,provider=provider,model=args.model,frame_count=args.frames,bands=args.band,roi_plan_path=args.roi_plan,roi_repair_attempts=0 if args.no_roi_retry else 1,conditions={'fixed_camera_confirmed':args.fixed_camera},on_progress=progress)
        print(json.dumps({k:v for k,v in result.items() if k in ['mode','output_dir','report_html','diagnosis_json','filename']},ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr);return 1
