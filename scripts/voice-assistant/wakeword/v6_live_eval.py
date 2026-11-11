"""Live wake clips (datasets/real/live) replayed with the server's exact framing (eval_stream.stream with n_total from
wake_len). Numbers only. Usage: live_eval.py --role test|mine --model name=uncal.tflite:thr ... --json out.json"""
import argparse, csv, json, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import numpy as np
sys.path.insert(0, '/home/agil/homelab/scripts/voice-assistant/wakeword')
import eval_stream as E
import kivi_real_common as C
ROOT = Path('/work/workspaces/kivi-wakeword/datasets/real/live')
ap = argparse.ArgumentParser()
ap.add_argument('--role', default='test')
ap.add_argument('--model', action='append', required=True)
ap.add_argument('--json')
a = ap.parse_args()
specs, thr = [], {}
for s in a.model:
    n, rest = s.split('=', 1)
    p, t = rest.rsplit(':', 1)
    specs.append(f'{n}={p}'); thr[n] = float(t)
feats, models = E.load_models(specs)
rows = list(csv.DictReader(open(ROOT / 'live_wakes.csv')))
out = []
for r in rows:
    t = datetime.strptime(r['utc'], '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc)
    d = t.astimezone(ZoneInfo('Europe/Madrid')).date()
    if C.day_role(d) != a.role:
        continue
    run = r['run_id'].split('/')[-1]
    cdir = [p for p in (ROOT / r['device']).iterdir() if p.is_dir() and p.name == r['utc']]
    tail_p = cdir[0] / 'wake_tail.wav' if cdir else None
    if not tail_p or not tail_p.exists():
        continue
    tail = C.read_pcm16(tail_p)
    n_total = E.wake_samples(r['wake_len'])
    probs, times = E.stream(feats, models, tail, n_total=n_total)
    res = {'date': d.isoformat(), 'device': r['device'], 'label': r['label'], 'label_source': r['label_source'] or 'owner?',
           'speaker': r['speaker'], 'trigger': r['trigger'], 'live_model': r['model'],
           'continuous_command': r['continuous_command'], 'phrase_in_stt': r['phrase_in_stt']}
    stt_p = cdir[0] / 'stt.wav'
    ext = None
    if stt_p.exists() and not r.get('stt_run'):
        stt = C.read_pcm16(stt_p)
        ext = np.concatenate([tail, stt[:16000]])
        pe, te = E.stream(feats, models, ext, n_total=n_total + 16000)
    for n in models:
        res[f'max_{n}'] = round(float(probs[n].max()) if len(probs[n]) else 0.0, 4)
        res[f'det_{n}'] = bool(len(probs[n]) and probs[n].max() > thr[n])
        if ext is not None:
            res[f'det_ext_{n}'] = bool(pe[n].max() > thr[n])
    out.append(res)
def tab(sel, key):
    return {n: f"{sum(r[f'{key}_{n}'] for r in sel if f'{key}_{n}' in r)}/{len(sel)}" for n in models}
summ = {}
for lab in ('true', 'false', 'unclear', 'not-a-wake'):
    sel = [r for r in out if r['label'] == lab]
    summ[lab] = {'n': len(sel), 'detected': tab(sel, 'det'), 'detected_tail+1s_stt': tab(sel, 'det_ext')}
    for spk in sorted({r['speaker'] for r in sel}):
        s2 = [r for r in sel if r['speaker'] == spk]
        summ[lab][f'spk:{spk}'] = tab(s2, 'det')
    for dev in sorted({r['device'] for r in sel}):
        s2 = [r for r in sel if r['device'] == dev]
        summ[lab][f'dev:{dev}'] = tab(s2, 'det')
    s2 = [r for r in sel if r['continuous_command'] == 'true']
    summ[lab]['phrase+command'] = tab(s2, 'det')
print(json.dumps(summ, indent=1))
if a.json:
    json.dump({'role': a.role, 'thresholds': thr, 'models': specs, 'rows': out, 'summary': summ}, open(a.json, 'w'), indent=1)
