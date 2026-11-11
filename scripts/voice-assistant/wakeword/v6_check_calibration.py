"""C6: the calibrated file crosses server 0.5 exactly where the uncalibrated one crosses the raw threshold, in the
pod's runtime (pyopen-wakeword). Audio: synthetic held-out context clips only (no household audio)."""
import sys, glob, json, importlib.metadata as md, numpy as np, soundfile as sf
sys.path.insert(0, '/home/agil/homelab/scripts/voice-assistant/wakeword')
import eval_stream as E
d = sys.argv[1]; mid = sys.argv[2]; thr = float(sys.argv[3])
feats, models = E.load_models([f'u={d}/{mid}_uncalibrated.tflite', f'c={d}/{mid}.tflite'])
files = sorted(glob.glob('/work/workspaces/kivi-wakeword/v6/clips_ctx/ctx_pos_eval/*.wav'))[:300]
x = np.concatenate([np.concatenate([sf.read(f, dtype='int16')[0], np.zeros(8000, np.int16)]) for f in files])
x = (x.astype(np.float32) * 4).clip(-32768, 32767).astype(np.int16)
p, t = E.stream(feats, models, x)
u, c = p['u'].astype(np.float64), p['c'].astype(np.float64)
lg = lambda q: np.log(np.clip(q, 1e-7, 1 - 1e-7) / (1 - np.clip(q, 1e-7, 1 - 1e-7)))
mask = (u > 1e-4) & (u < 1 - 1e-4)
shift = (lg(u) - lg(c))[mask]
out = {'pyopen_wakeword': md.version('pyopen-wakeword'), 'steps': int(len(u)), 'raw_threshold': thr,
       'steps_above_raw': int((u > thr).sum()), 'steps_above_0.5_calibrated': int((c > 0.5).sum()),
       'disagree': int(((u > thr) != (c > 0.5)).sum()), 'logit_shift_median': float(np.median(shift)),
       'expected_shift': float(np.log(thr / (1 - thr)))}
print(json.dumps(out, indent=1))
