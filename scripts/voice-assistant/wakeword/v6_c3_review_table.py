"""Blind review table of the C3 events (pre-registered relabel rule, 2026-11-11): union of the TFLite replay events on
test2-v5era of v5 @0.9754, v6 @0.9595 and the ablation @0.9285 (v6/all/replay_test2.json), merged within +-2 s per TV,
cut as 6 s windows like the miss candidates, shuffled, no model or score in the table. The mapping goes to
v6/all/c3_events_mapping.json (never read by the review). Numbers only."""
import json, random, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, '/home/agil/homelab/scripts/voice-assistant/wakeword')
import kivi_real_common as C
import kivi_live_wakes as L
import kivi_replay_mine as K
W = Path('/work/workspaces/kivi-wakeword'); root = W / 'datasets/real'
js = json.load(open(W / 'v6/all/replay_test2.json'))
pieces = {p['clip']: p for p in K.read_pieces(root, 'negatives-v5era')}
models = {'v5': 'v5', 'v6': 'v6', 'abl': 'abl'}
ev = []
for row in js['pieces_detail']:
    pc = pieces[row['clip']]
    for m in models:
        for x in row.get('times', {}).get(m, []):
            ev.append((pc['device'], pc['start'] + x, m, row['clip']))
ev.sort(key=lambda e: (e[0], e[1]))
groups = []
for d, t, m, clip in ev:
    if groups and groups[-1]['device'] == d and t - groups[-1]['t_last'] <= 2.0:
        g = groups[-1]; g['models'].add(m); g['t_last'] = t; g['times'].append(t)
    else:
        groups.append({'device': d, 't': t, 't_last': t, 'times': [t], 'models': {m}, 'clip': clip})
rng = random.Random(20261111)
rng.shuffle(groups)
misses = root / 'live-misses'
rows, mapping = [], []
for i, g in enumerate(groups, 1):
    pc = pieces[g['clip']]
    tc = sum(g['times']) / len(g['times'])
    utc = L.dir_stamp(tc)
    rel = f"c3-events/{pc['device']}/{utc}/window.wav"
    C.private_mkdir(misses / 'c3-events' / pc['device'] / utc, root)
    first, last = K.window_bounds(tc, pc['start'], pc['n'])
    if not (misses / rel).exists():
        rms, pk = K.cut_window(pc['path'], first, last, misses / rel)
    else:
        rms, pk = None, None
    eid = f"ce{i:03d}"
    rows.append({'id': eid, 'device': pc['device'], 'part': 'test2-v5era', 'utc': utc, 'peak_v1': '', 'peak_v4': '', 'peak_v5': '',
                 'retry': '', 'retry_detail': '', 'rank': round(rng.random(), 6), 'label': '', 'speaker': '',
                 'level_dbfs': rms, 'peak_dbfs': pk, 'clip': rel})
    mapping.append({'id': eid, 'device': pc['device'], 'utc': utc, 't_abs': round(tc, 2), 'models': sorted(g['models']),
                    'n_merged': len(g['times'])})
K.write_csv(misses / 'c3_events.csv', rows, K.MISS_FIELDS)
Path(W / 'v6/all/c3_events_mapping.json').write_text(json.dumps(mapping, indent=1))
import os; os.chmod(misses / 'c3_events.csv', 0o600); os.chmod(W / 'v6/all/c3_events_mapping.json', 0o600)
from collections import Counter
print(len(rows), 'windows;', Counter(tuple(m['models']) for m in mapping), Counter(m['device'] for m in mapping))
