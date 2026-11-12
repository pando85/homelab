"""Phase 0 (2026-11-12): score every live wake tail with v5, v6, v6.2 on the server-exact grid (eval_stream.stream with n_total)
and on 8 shifted grids (160-sample steps). Writes $KIVI_HOME/audit/phase0/tails.csv (scores and levels only, private; no audio)."""
import os, sys, csv, json
for v in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS"): os.environ.setdefault(v,"1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, multiprocessing as mp
import eval_stream as E, kivi_real_common as C
KH=C.kivi_home(); ROOT=KH/'datasets/real'
M=KH.parent/'wakeword'
SPEC={'v5':(M/'oye_kivi_next/v5-oye-kivi-2026-11-08/oye_kivi_next_uncalibrated.tflite',0.9754),
      'v6':(M/'oye_kivi_ctx/v6-oye-kivi-2026-11-11/oye_kivi_ctx_uncalibrated.tflite',0.9595),
      'v62':(M/'oye_kivi_ctx2/v6-p2-oye-kivi-2026-11-12/oye_kivi_ctx2_uncalibrated.tflite',0.9825)}
W={}
def levels(x):
    f=x.astype(np.float64)/32768; n=len(f)//800
    fr=np.sqrt((f[:n*800].reshape(n,800)**2).mean(1)+1e-12); db=20*np.log10(fr)
    db_all=lambda v:20*np.log10(max(v,1e-9))
    return dict(rms_dbfs=round(db_all(float(np.sqrt((f**2).mean()))),1),peak_dbfs=round(db_all(float(np.abs(f).max())),1),
                floor_dbfs=round(float(np.percentile(db,10)),1),p90_dbfs=round(float(np.percentile(db,90)),1))
def task(r):
    if 'rt' not in W: W['rt']=E.load_models([f"{k}={v[0]}" for k,v in SPEC.items()])
    feats,models=W['rt']
    p=ROOT/'live'/r['device']/r['utc']/'wake_tail.wav'
    x=C.read_pcm16(p); n_total=E.wake_samples(r['wake_len'] or 0)
    if n_total<len(x): n_total=len(x)
    out={'run_id':r['run_id'],**levels(x)}
    for k in range(8):
        probs,times=E.stream(feats,models,x,n_total=n_total+160*k)
        t_end=len(x)/16000
        for n,pp in probs.items():
            m=times>=t_end-3.0
            out[f'{n}_p{k}']=round(float(pp[m].max()) if m.any() else 0.0,4)
            if k==0: out[f'{n}_all']=round(float(pp.max()) if len(pp) else 0.0,4)
    return out
if __name__=='__main__':
    rows=list(csv.DictReader(open(ROOT/'live/live_wakes.csv')))
    with mp.get_context('spawn').Pool(4) as pool: res=pool.map(task,rows,chunksize=2)
    keys=list(res[0].keys())
    with open(KH/'audit/phase0/tails.csv','w',newline='') as f:
        w=csv.DictWriter(f,keys); w.writeheader(); w.writerows(res)
    print(len(res),'rows')
