"""Blind review round 2 (2026-11-12): v6.2 events on test2-v5era that are not within +-2 s of a c3_events window, plus
the union of v6 and v6.2 events on the 2026-11-11 evening copy (negatives-1111eve), merged within +-2 s per TV.
Same 6 s window format as the miss candidates; shuffled; no model or score shown. Mapping in
v6/all/c3_events_r2_mapping.json. Source of the events: v6p2/compare/room.json (TFLite replay as the mining pass)."""
import json, random, sys, os
from pathlib import Path
sys.path.insert(0, '/home/agil/homelab/scripts/voice-assistant/wakeword')
import kivi_real_common as C
import kivi_live_wakes as L
import kivi_replay_mine as K
W = Path('/work/workspaces/kivi-wakeword'); root = W / 'datasets/real'; misses = root / 'live-misses'
rj = json.load(open(W / 'v6p2/compare/room.json'))
old = json.load(open(W / 'v6/all/c3_events_mapping.json'))
P = {}
for s in ('negatives-v5era', 'negatives-1111eve'):
    for p in K.read_pieces(root, s):
        P[(s, p['clip'])] = p
ev = []
for row in rj['pieces_detail']:
    pc = P[(row['set'], row['clip'])]
    models = ('v62',) if row['set'] == 'negatives-v5era' else ('v6', 'v62')
    for m in models:
        for x in row.get('times', {}).get(m, []):
            t = pc['start'] + x
            if row['set'] == 'negatives-v5era' and any(o['device'] == pc['device'] and abs(o['t_abs'] - t) <= 2 for o in old):
                continue
            ev.append((row['set'], pc['device'], t, m, row['clip']))
ev.sort(key=lambda e: (e[1], e[2]))
groups = []
for st, d, t, m, clip in ev:
    if groups and groups[-1]['device'] == d and t - groups[-1]['t_last'] <= 2.0:
        g = groups[-1]; g['models'].add(m); g['t_last'] = t; g['times'].append(t)
    else:
        groups.append({'set': st, 'device': d, 't_last': t, 'times': [t], 'models': {m}, 'clip': clip})
rng = random.Random(20261112)
rng.shuffle(groups)
rows, mapping = [], []
for i, g in enumerate(groups, 1):
    pc = P[(g['set'], g['clip'])]
    tc = sum(g['times']) / len(g['times'])
    utc = L.dir_stamp(tc)
    part = 'test2-v5era' if g['set'] == 'negatives-v5era' else 'test-1111eve'
    rel = f"c3-events-r2/{pc['device']}/{utc}/window.wav"
    C.private_mkdir(misses / 'c3-events-r2' / pc['device'] / utc, root)
    first, last = K.window_bounds(tc, pc['start'], pc['n'])
    rms, pk = K.cut_window(pc['path'], first, last, misses / rel) if not (misses / rel).exists() else (None, None)
    eid = f"cf{i:03d}"
    rows.append({'id': eid, 'device': pc['device'], 'part': part, 'utc': utc, 'peak_v1': '', 'peak_v4': '', 'peak_v5': '',
                 'retry': '', 'retry_detail': '', 'rank': round(rng.random(), 6), 'label': '', 'speaker': '',
                 'level_dbfs': rms, 'peak_dbfs': pk, 'clip': rel})
    mapping.append({'id': eid, 'set': g['set'], 'device': pc['device'], 'utc': utc, 't_abs': round(tc, 2),
                    'models': sorted(g['models']), 'n_merged': len(g['times'])})
K.write_csv(misses / 'c3_events_r2.csv', rows, K.MISS_FIELDS)
Path(W / 'v6/all/c3_events_r2_mapping.json').write_text(json.dumps(mapping, indent=1))
os.chmod(misses / 'c3_events_r2.csv', 0o600); os.chmod(W / 'v6/all/c3_events_r2_mapping.json', 0o600)
from collections import Counter
print(len(rows), 'windows;', Counter((m['set'], tuple(m['models'])) for m in mapping), Counter(m['device'] for m in mapping))
