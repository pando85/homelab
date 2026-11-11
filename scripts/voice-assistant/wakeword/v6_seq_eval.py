"""Clip-sequence npz (names, lengths, data: server-exact embeddings per clip) -> per clip max of each head, accept
rate at each model's threshold with Wilson 95 % CI. Usage: seq_eval.py --model n=head.onnx:thr --set label=file.npz ..."""
import argparse, json, sys, math
import numpy as np
sys.path.insert(0, '/home/agil/homelab/scripts/voice-assistant/wakeword')
import v6_room as R
def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0, c - h), 4), round(min(1, c + h), 4))
def clip_max(head, z):
    data = z['data']; out = []
    off = 0; spans = []
    for n in z['lengths']:
        spans.append((off, int(n))); off += int(n)
    from numpy.lib.stride_tricks import sliding_window_view
    W, owner = [], []
    for i, (o, n) in enumerate(spans):
        if n < 16: continue
        v = sliding_window_view(np.asarray(data[o:o + n], np.float32), 16, axis=0).transpose(0, 2, 1)
        W.append(v); owner.append(np.full(len(v), i))
    p = head(np.concatenate(W)); ow = np.concatenate(owner)
    m = np.full(len(spans), 0.0); np.maximum.at(m, ow, p)
    return m
if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', action='append', required=True)
    ap.add_argument('--set', action='append', required=True)
    ap.add_argument('--json')
    a = ap.parse_args()
    res = {}
    for ms in a.model:
        n, rest = ms.split('=', 1); path, thr = rest.rsplit(':', 1); thr = float(thr)
        h = R.Head(path)
        for ss in a.set:
            lab, f = ss.split('=', 1)
            z = np.load(f)
            m = clip_max(h, z); k = int((m > thr).sum())
            res.setdefault(lab, {})[n] = {'k': k, 'n': len(m), 'rate': round(k / len(m), 4), 'ci95': wilson(k, len(m)), 'thr': thr}
            print(f'{lab:22s} {n:6s} {k}/{len(m)} = {k/len(m):.3f} {wilson(k, len(m))}', flush=True)
    if a.json: json.dump(res, open(a.json, 'w'), indent=1)
