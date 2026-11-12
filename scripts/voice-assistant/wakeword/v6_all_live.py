"""All live wake clips, exact replay (grid phase from wake_len, remainder fed): per model, detections by day role
(train = even Madrid date, test = odd), label, speaker and device. The v6.1 arms are scored on train-day clips only
(the test-day clips are the pre-registered C4 set). Numbers only."""
import csv, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import numpy as np
sys.path.insert(0, '/home/agil/homelab/scripts/voice-assistant/wakeword')
import eval_stream as E
import kivi_real_common as C
W = Path('/work/workspaces/kivi-wakeword'); ROOT = W / 'datasets/real/live'
MODELS = {'v4': ('v4/final/oye_kivi_uncalibrated.tflite', 0.9437), 'v5': ('releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated.tflite', 0.9754),
          'v6': ('v6/runs/v6m/oye_kivi_uncalibrated.tflite', 0.9595), 'abl': ('v6/runs/v6abl/oye_kivi_uncalibrated.tflite', 0.9285),
          'v6.2': ('v6p2/runs/p2b/oye_kivi_uncalibrated.tflite', 0.9825)}
ARMS = {f'{n}@{t}': (f'v6p1/runs/{n}/oye_kivi_uncalibrated.tflite', t) for n in ('p1a', 'p1b', 'p1c') for t in (0.9595, 0.9754)}
feats, models = E.load_models([f'{n}={W / p}' for n, (p, _) in {**MODELS, **{k.split('@')[0]: v for k, v in ARMS.items()}}.items()])
rows = list(csv.DictReader(open(ROOT / 'live_wakes.csv')))
cells = defaultdict(lambda: [0, 0])
for r in rows:
    d = datetime.strptime(r['utc'], '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Madrid')).date()
    role = 'test' if C.day_role(d) == 'test' else 'train'
    tail = ROOT / r['device'] / r['utc'] / 'wake_tail.wav'
    if not tail.exists() or r['label'] not in ('true', 'false', 'unclear'):
        continue
    if r['trigger'] != 'wake':
        continue
    probs, _ = E.stream(feats, models, C.read_pcm16(tail), n_total=E.wake_samples(r['wake_len']))
    spk = r['speaker'] if r['speaker'] in ('spk01', 'spk02') else 'unknown'
    src = 'owner' if not r['label_source'].startswith('auto') else 'auto'
    for name, (p, thr) in list(MODELS.items()) + list(ARMS.items()):
        base = name.split('@')[0]
        if name in ARMS and role == 'test':
            continue
        hit = int(len(probs[base]) and probs[base].max() > thr)
        for key in [(name, role, r['label'], 'all', 'all'), (name, role, r['label'], spk, 'all'), (name, role, r['label'], 'all', r['device'])]:
            cells['|'.join(key)][0] += hit; cells['|'.join(key)][1] += 1
out = {k: v for k, v in sorted(cells.items())}
json.dump(out, open(W / 'v6/all/live_all.json', 'w'), indent=1)
for k, v in out.items():
    if '|all|all' in k: print(k, f'{v[0]}/{v[1]}')
