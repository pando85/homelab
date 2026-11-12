"""Phase 0 (2026-11-12): score the replay events (daily_false_wakes, c3_events, c3_events_r2) with v5, v6, v6.2 over the room
piece around each event on 8 grid phases (piece sample 0 = stream sample k*160). The room pieces are cut from the middle of the
wake file, so the replay grid is NOT the server grid; a piece cut 0.3 s into the file has s0 = 4800 (k = 6), one cut 7 s in has
s0 = 112000 (k = 4). Writes $KIVI_HOME/audit/phase0/events.csv (scores only, private)."""
import os, sys, csv, json
for v in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS"): os.environ.setdefault(v,"1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, multiprocessing as mp
from datetime import datetime, timezone
import eval_stream as E, kivi_real_common as C, kivi_extract as X, kivi_replay_mine as K, kivi_live_wakes as L
KH=C.kivi_home(); ROOT=KH/'datasets/real'
from kivi_phase0_tails import SPEC
W={}
def epc(s): return datetime.strptime(s,"%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
def pieces_of(st):
    return K.read_pieces(ROOT,st)
SETFOR={'d-20261111':'negatives-daily','test2-v5era':'negatives-v5era','test-1111eve':'negatives-1111eve','mine-v5era':'negatives-v5era'}
def task(a):
    ev,pc=a
    if 'rt' not in W: W['rt']=E.load_models([f"{k}={v[0]}" for k,v in SPEC.items()])
    feats,models=W['rt']
    t=ev['t']; n=pc['n']; rate=16000
    c=int(round((t-pc['start'])*rate))
    a0=max(0,((c-40*rate)//1280)*1280); b=min(n,c+8*rate)
    with open(pc['path'],'rb') as f:
        f.seek(44+2*a0); x=np.frombuffer(f.read(2*(b-a0)),dtype='<i2')
    out={'id':ev['id'],'device':ev['device'],'utc':ev['utc'],'piece':pc['clip'],'off_start_s':round(t-pc['start'],2),'off_end_s':round(n/rate-(t-pc['start']),2)}
    for k in range(8):                        # s0 = k*160 (mod 1280): grid anchored so that piece sample 0 is stream sample s0
        probs,times=E.stream(feats,models,x,n_total=a0+len(x)+k*160)
        # step time (s from segment start) -> piece time
        tp=pc['start']+a0/rate+times
        m=np.abs(tp-t)<=3.0
        for nm,pp in probs.items():
            out[f'{nm}_s{k}']=round(float(pp[m].max()) if m.any() else 0.0,4)
    return out
if __name__=='__main__':
    lm=ROOT/'live-misses'
    evs=[]
    for name,part_set in (('daily_false_wakes.csv',None),('c3_events.csv',None),('c3_events_r2.csv',None)):
        for r in L.read_miss_table(lm/name):
            evs.append({'id':r['id'],'table':name,'device':r['device'],'utc':r['utc'],'t':epc(r['utc']),'part':r['part'],'label':r['label'],'speaker':r['speaker']})
    # pieces per set
    P={}
    for st in ('negatives-daily','negatives-v5era','negatives-1111eve'):
        P[st]=[p for p in pieces_of(st)]
    jobs=[]; meta=[]
    for e in evs:
        st=SETFOR.get(e['part'])
        pc=None
        for p in P.get(st,[]):
            if p['device']==e['device'] and p['start']<=e['t']<=p['start']+p['n']/16000: pc=p; break
        if pc is None:
            # fall back to other sets
            for s2,pl in P.items():
                for p in pl:
                    if p['device']==e['device'] and p['start']<=e['t']<=p['start']+p['n']/16000: pc=p; break
                if pc: break
        if pc is None: print('no piece',e['id']); continue
        jobs.append((e,pc)); meta.append(e)
    print(len(jobs),'events',flush=True)
    with mp.get_context('spawn').Pool(4) as pool: res=pool.map(task,jobs,chunksize=1)
    for r,e in zip(res,meta): r.update({'table':e['table'],'part':e['part'],'label':e['label'],'speaker':e['speaker']})
    keys=list(res[0].keys())
    with open(KH/'audit/phase0/events.csv','w',newline='') as f:
        w=csv.DictWriter(f,keys); w.writeheader(); w.writerows(res)
    print('done')
