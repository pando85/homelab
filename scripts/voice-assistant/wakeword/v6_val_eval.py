"""Validation windows (never trained on by the model, except where marked): accept rate per model at its threshold.
pos_val = v6/feat/pos_test.npy (isolated synthetic, train voices), ctx_val = v6p1/feat/ctxp_syn_pos_val.npy (500
context clips; v6 trained on them, the ablation and v4/v5 did not), near_val = v6/feat/near_test.npy (near-phrases)."""
import json, sys
import numpy as np
sys.path.insert(0, '/home/agil/homelab/scripts/voice-assistant/wakeword')
import v6_room as R
W = '/work/workspaces/kivi-wakeword/'
M = {'v4': ('v4/final/oye_kivi_uncalibrated.onnx', [0.9437]), 'v5': ('releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated.onnx', [0.9754]),
     'v6': ('v6/runs/v6m/kivi.onnx', [0.9595]), 'abl': ('v6/runs/v6abl/kivi.onnx', [0.9285])}
for n in ('p1a', 'p1b', 'p1c'): M[n] = (f'v6p1/runs/{n}/kivi.onnx', [0.9595, 0.9754])
S = {'pos_val': 'v6/feat/pos_test.npy', 'ctx_val': 'v6p1/feat/ctxp_syn_pos_val.npy', 'near_val': 'v6/feat/near_test.npy'}
X = {k: np.load(W + v).astype(np.float32) for k, v in S.items()}
out = {}
for n, (p, ts) in M.items():
    h = R.Head(W + p)
    P = {k: h(x) for k, x in X.items()}
    for t in ts:
        out[f'{n}@{t}'] = {k: [int((P[k] > t).sum()), int(len(P[k]))] for k in S}
        print(n, t, {k: f'{v[0]}/{v[1]}' for k, v in out[f"{n}@{t}"].items()})
json.dump(out, open(W + 'v6/all/val_eval.json', 'w'), indent=1)
