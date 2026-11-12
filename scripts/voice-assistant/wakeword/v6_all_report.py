"""Consolidated model x dataset report (v6/all_numbers.md). Numbers only. Cells are tagged:
H = honest (never used by that model for training, threshold or selection), T = training/selection data for that model
(optimistic), I = information only (pre-front-end audio), - = not scored (v6.1 arms are kept off the pre-registered
gate sets)."""
import json
import math
from pathlib import Path

W = Path('/work/workspaces/kivi-wakeword')
A = W / 'v6/all'
PREFE = set(Path(W / 'v6/prefe_skip.txt').read_text().split())

ROWS = [('v4', 'v4', 0.9437), ('v5', 'v5', 0.9754), ('v6', 'v6', 0.9595), ('abl', 'abl', 0.9285)] + \
    [(f'{n}@{t}', f'{n}_{str(t)[2:]}', t) for n in ('p1a', 'p1b', 'p1c') for t in (0.9595, 0.9754)] + \
    [('v6.2', 'v62', 0.9825)]
CMP = W / 'v6p2/compare'
ARM = lambda m: m.startswith('p1')  # noqa: E731
DAYS = ['11-05', '11-06', '11-08', '11-09', '11-10', '11-11 day', '11-11 eve', 'pre-FE']


def room_tag(m, day):
    if m == 'v6.2':
        return 'H' if day in ('11-09', '11-11 day', '11-11 eve') else '-'
    if day == 'pre-FE':
        return 'I'
    if day in ('11-05', '11-06'):
        return 'T'
    if day in ('11-09', '11-11 day'):
        return '-' if ARM(m) else 'H'
    if day == '11-11 eve':
        return 'H'
    # 11-08, 11-10
    return 'H' if m in ('v4', 'v5') else 'T'


def poisson(k, t):
    from scipy.stats import chi2
    if t <= 0:
        return (0.0, 0.0)
    lo = 0.0 if k == 0 else chi2.ppf(0.025, 2 * k) / 2
    return (lo / t, chi2.ppf(0.975, 2 * k + 2) / 2 / t)


def room_cells():
    """{(model key, day, device): [k, k_evening, hours, evening_h]}"""
    cells = {}
    for f in (A / 'replay_test2.json', A / 'replay_other.json', CMP / 'room.json'):
        js = json.load(open(f))
        only = {'v62'} if f.parent == CMP else None
        for row in js['pieces_detail']:
            dev = row['device']
            pre = row['clip'] in PREFE
            for date, h in row['hours_by_date'].items():
                eh = row['evening_h_by_date'].get(date, 0.0)
                if pre:
                    day = 'pre-FE'
                elif row['set'] == 'negatives-1111eve':
                    day = '11-11 eve'
                elif date == '2026-11-11':
                    day = '11-11 day'
                else:
                    day = date[5:]
                for _, mk, _ in ROWS:
                    if mk not in js['thresholds'] or (only and mk not in only):
                        continue
                    c = cells.setdefault((mk, day, dev), [0, 0, 0.0, 0.0])
                    c[2] += h
                    c[3] += eh
                    k, ke = row['events'].get(mk, {}).get(date, [0, 0])
                    c[0] += k
                    c[1] += ke
    return cells


def fmt_room(c):
    k, ke, h, eh = c
    pe = f'{ke / (eh / 6):.2f}' if eh >= 0.5 else 'n/a'
    return f'{k}/{h:.1f}h={k / h:.3f}/h; ev {ke} ({eh:.1f}h, {pe}/ev)' if h > 0 else 'no audio'


def session_rows():
    """{(model key, session label, source): {'pos': [k, n], 'near': [k, n], cells...}} from kivi_score JSONs."""
    name = {'oye_kivi': 'v4', 'oye_kivi_next': 'v5', 'v6m': 'v6', 'v6abl': 'abl', 'p1a': 'p1a', 'p1b': 'p1b', 'p1c': 'p1c',
            'v62': 'v6.2'}
    files = {'11-08 s1': A / 'score_20261108T092940Z-spk01-tv-c3a.json', '11-08 s2': A / 'score_20261108T094310Z-spk01-tv-c3a.json',
             '11-08 s3 (near)': A / 'score_20261108T095130Z-spk01-tv-c3a.json', '11-11': W / 'v6/eval/score_1111.json',
             '11-11 ': CMP / 'score_1111.json'}
    out = {}
    for lab0, f in files.items():
        if not f.exists():
            continue
        js = json.load(open(f))
        lab = lab0.strip()
        for e in js:
            src = {'tv-c3a': 'TV', 'pc-mic': 'pc-desk', 'webcam-mic': 'webcam'}.get(e['source'], e['source'])
            kbc = e['overall']['kivi_by_condition']
            allc = kbc.get('ALL', {'cue_hits': {mm: {} for mm in e['overall']['near_total']['cue_hits']}})
            for mname, v in allc['cue_hits'].items():
                base = name.get(mname, mname)
                if (lab0 == '11-11 ') != (base == 'v6.2'):
                    continue
                for m, mk, t in ROWS:
                    if m.split('@')[0] != base:
                        continue
                    key = next((x for x in v if abs(float(x) - t) < 1e-6), None)
                    nh0 = e['overall']['near_total']['cue_hits'][mname]
                    if key is None and not any(abs(float(x) - t) < 1e-6 for x in nh0):
                        continue
                    nh = e['overall']['near_total']['cue_hits'][mname]
                    nkey = next(x for x in nh if abs(float(x) - t) < 1e-6)
                    cond = {c: [vv['cue_hits'][mname][next(x for x in vv['cue_hits'][mname] if abs(float(x) - t) < 1e-6)]['k'],
                                vv['cue_hits'][mname][next(x for x in vv['cue_hits'][mname] if abs(float(x) - t) < 1e-6)]['n']]
                            for c, vv in e['overall']['kivi_by_condition'].items() if c != 'ALL'}
                    out[(m, lab, src)] = {'pos': [v[key]['k'], v[key]['n']] if key else [0, 0],
                                          'near': [nh[nkey]['k'], nh[nkey]['n']], 'cond': cond}
    return out


def session_tag(m, lab):
    if m == 'v6.2':
        return 'H' if lab == '11-11' else '-'
    if lab == '11-11':
        return '-' if ARM(m) else 'H'
    if m == 'v4':
        return 'H'
    if m == 'v5':
        return {'11-08 s1': 'T', '11-08 s2': 'T*', '11-08 s3 (near)': 'T'}[lab]
    return 'T'


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (c - h), 100 * (c + h))


def main():
    cells = room_cells()
    sess = session_rows()
    live = json.load(open(A / 'live_all.json'))
    val = json.load(open(A / 'val_eval.json'))
    synth = json.load(open(W / 'v6/eval/synth.json'))
    # v6.2 (odd-day honest sets only, from v6p2_compare.sh) and its validation numbers (v6p2/selection.json)
    cs = json.load(open(CMP / 'synth.json'))
    for k in ('clean', 'tv10', 'quiet'):
        synth[k]['v62'] = cs[k]['v62']
    arm = json.load(open(W / 'v6p2/selection.json'))['arms']['p2b']
    val['v6.2@0.9825'] = {'pos_val': [round(arm['pos_val'] * 2367), 2367], 'ctx_val': [round(arm['ctx_val'] * 1000), 1000],
                          'near_val': [round(arm['near_val'] * 2999), 2999]}
    L = []
    P = L.append
    P('# All numbers: every model on every dataset (2026-11-11, late evening)\n')
    P('Numbers only (no audio played, transcribed or printed). Each model at its own raw threshold: v4 `oye_kivi` 0.9437, '
      'v5 `oye_kivi_next` 0.9754 (deployed), v6 `oye_kivi_ctx` 0.9595 (not deployed, failed C3), ablation 0.9285. '
      'v6.1 arms p1a/p1b/p1c have no threshold yet: shown at 0.9595 and 0.9754, **provisional**. Tags: **H** honest '
      '(never used by that model for training, threshold or selection), **T** training/selection data for that model '
      '(optimistic), **T*** v5 held this session out of training but used it to pick the head, **I** information only, '
      '**-** not scored (arms are kept off the pre-registered gate sets: test2-v5era, the 11-11 session, test-day live '
      'clips, the held-out-voice synthetic sets). Room: TFLite replay identical to the v5-era mining pass '
      '(`v6_replay.py`), 2 s refractory, evening = 18-24 Madrid, "/ev" = per evening (6 h).\n')
    # ---------------- room
    P('## Room false wakes (events / hours = per hour; evening events, evening hours, per evening)\n')
    for dev in ('c3-a', 'c3-b'):
        P(f'### {dev}\n')
        P('| Model | ' + ' | '.join(DAYS) + ' |')
        P('| --- |' + ' --- |' * len(DAYS))
        for m, mk, _ in ROWS:
            row = []
            for day in DAYS:
                tag = room_tag(m, day)
                c = cells.get((mk, day, dev))
                row.append('-' if tag == '-' or c is None else f'{tag} {fmt_room(c)}')
            P(f'| {m} | ' + ' | '.join(row) + ' |')
        P('')
    # pooled per day (both TVs)
    P('### Both TVs pooled, per day (events / hours)\n')
    P('| Model | ' + ' | '.join(DAYS) + ' |')
    P('| --- |' + ' --- |' * len(DAYS))
    for m, mk, _ in ROWS:
        row = []
        for day in DAYS:
            tag = room_tag(m, day)
            cs = [cells.get((mk, day, d)) for d in ('c3-a', 'c3-b')]
            cs = [c for c in cs if c]
            if tag == '-' or not cs:
                row.append('-')
                continue
            k = sum(c[0] for c in cs)
            ke = sum(c[1] for c in cs)
            h = sum(c[2] for c in cs)
            eh = sum(c[3] for c in cs)
            row.append(f'{tag} {k}/{h:.1f}h, ev {ke}/{eh / 6:.2f}')
        P(f'| {m} | ' + ' | '.join(row) + ' |')
    P('')
    # ---------------- sessions
    P('## Real takes: recall on "oye Kivi" cues and near-phrase accepts, per device class\n')
    P('| Model | 11-08 s1 TV / pc / webcam | 11-08 s2 TV / pc / webcam | 11-08 near (s1+s3) TV / pc / webcam | '
      '11-11 pc / webcam | 11-11 near pc / webcam |')
    P('| --- | --- | --- | --- | --- | --- |')
    for m, _, _ in ROWS:
        def get(lab, src, what):
            v = sess.get((m, lab, src))
            return f'{v[what][0]}/{v[what][1]}' if v else 'n/a'
        s1 = ' / '.join(get('11-08 s1', s, 'pos') for s in ('TV', 'pc-desk', 'webcam'))
        s2 = ' / '.join(get('11-08 s2', s, 'pos') for s in ('TV', 'pc-desk', 'webcam'))
        nr = []
        for s in ('TV', 'pc-desk', 'webcam'):
            a, b = sess.get((m, '11-08 s1', s)), sess.get((m, '11-08 s3 (near)', s))
            nr.append(f'{a["near"][0] + b["near"][0]}/{a["near"][1] + b["near"][1]}' if a and b else 'n/a')
        t11 = session_tag(m, '11-11')
        p11 = '-' if t11 == '-' else ' / '.join(get('11-11', s, 'pos') for s in ('pc-desk', 'webcam'))
        n11 = '-' if t11 == '-' else ' / '.join(get('11-11', s, 'near') for s in ('pc-desk', 'webcam'))
        P(f'| {m} | {session_tag(m, "11-08 s1")} {s1} | {session_tag(m, "11-08 s2")} {s2} | '
          f'{session_tag(m, "11-08 s3 (near)")} {" / ".join(nr)} | {t11} {p11} | {t11} {n11} |')
    P('')
    P('TV-on vs TV-off cells of the 11-11 session (pc / webcam):\n')
    P('| Model | 3 m off (36) | 3 m on (10) | 1.5 m on (10) | 1.5 m off (10) |')
    P('| --- | --- | --- | --- | --- |')
    for m, _, _ in ROWS:
        if ARM(m):
            continue
        cs = []
        for cond in ('3m_tv-off', '3m_tv-on', '1.5m_tv-on', '1.5m_tv-off'):
            cs.append(' / '.join(f'{sess[(m, "11-11", s)]["cond"][cond][0]}' for s in ('pc-desk', 'webcam')))
        P(f'| {m} | H ' + ' | '.join(cs) + ' |')
    P('')
    # ---------------- live
    P('## Live wake clips (wake-triggered runs, exact replay; label = owner or auto-v1)\n')
    P('Fired = the replay crosses the model\'s threshold on the wake tail. The tails end at the live model\'s detection '
      '(v4 on 11-08 before 13:28Z, v5 after), so the live model fires on all of them by construction. Train days 11-08 and '
      '11-10 (T for v6, ablation and arms; H but live-selected for v4/v5); test days 11-09 and 11-11 (H; arms not scored).\n')
    P('| Model | train: true | train: false (should not fire) | train: unclear | test: true | test: false | test: unclear | '
      'test true spk01 / spk02 / unknown |')
    P('| --- | --- | --- | --- | --- | --- | --- | --- |')
    for m, _, _ in ROWS:
        def lv(role, lab, spk='all'):
            v = live.get(f'{m}|{role}|{lab}|{spk}|all')
            return f'{v[0]}/{v[1]}' if v else '-'
        tt = 'T' if (m not in ('v4', 'v5')) else 'H'
        spk = ' / '.join(lv('test', 'true', s) for s in ('spk01', 'spk02', 'unknown'))
        P(f'| {m} | {tt} {lv("train", "true")} | {tt} {lv("train", "false")} | {tt} {lv("train", "unclear")} | '
          f'{lv("test", "true")} | {lv("test", "false")} | {lv("test", "unclear")} | {spk} |')
    P('')
    # ---------------- synthetic
    P('## Synthetic speech (accept rate)\n')
    P('Held-out voices (C5 sets; arms not scored) and validation windows of train voices that no model trained on '
      '(ctx_val: v6 trained on those clips, T).\n')
    P('| Model | held-out clean | TV 10 dB | quiet | phrase+cmd clean / TV10 | near (false) | confusers in sentences '
      '(false) | val isolated | val context | val near (false) |')
    P('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for m, _, t in ROWS:
        base = m.split('@')[0]
        sk = {'v4': 'v4', 'v5': 'v5', 'v6': 'v6m', 'abl': 'v6abl', 'v6.2': 'v62'}.get(base)
        def sy(s):
            if ARM(m) or sk is None or sk not in synth[s]:
                return '-'
            v = synth[s][sk]
            return f'{100 * v["rate"]:.1f} %'
        vk = f'{base}@{t}'
        vv = val.get(vk)
        def va(s):
            return f'{100 * vv[s][0] / vv[s][1]:.1f} %' if vv else '-'
        ctxtag = 'T ' if base == 'v6' else ''
        P(f'| {m} | {sy("clean")} | {sy("tv10")} | {sy("quiet")} | {sy("ctxpos_clean")} / {sy("ctxpos_tv10")} | '
          f'{sy("near")} | {sy("ctxneg_clean")} | {va("pos_val")} | {ctxtag}{va("ctx_val")} | {va("near_val")} |')
    P('')
    # ---------------- honest summary
    P('## Honest-only summary per model\n')
    P('Room: every H cell (the H set differs per model: v4/v5 include the even days 11-08 and 11-10, which they never saw; '
      'v6/ablation only the odd days; the arms only the new 11-11 evening). Odd-day room column = 11-09 + 11-11 day + '
      '11-11 evening for every model that may be scored there.\n')
    P('| Model | room H: events / h, per h [95 %] | room H: evening events, per evening | odd-day room: events / h | '
      'odd-day evening per evening | 11-11 pc / webcam recall | 11-11 near pc / webcam | test-day live true / false fired | '
      'held-out synthetic clean / TV10 / quiet |')
    P('| --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for m, mk, _ in ROWS:
        hk = hke = hh = heh = 0
        ok = oke = oh = oeh = 0
        for day in DAYS:
            for d in ('c3-a', 'c3-b'):
                c = cells.get((mk, day, d))
                if not c:
                    continue
                if room_tag(m, day) == 'H':
                    hk += c[0]; hke += c[1]; hh += c[2]; heh += c[3]
                if day in ('11-09', '11-11 day', '11-11 eve') and room_tag(m, day) == 'H':
                    ok += c[0]; oke += c[1]; oh += c[2]; oeh += c[3]
        lo, hi = poisson(hk, hh)
        pe = f'{hke}/{heh / 6:.2f} = {hke / (heh / 6):.2f}' if heh else 'n/a'
        ope = f'{oke}/{oeh / 6:.2f} = {oke / (oeh / 6):.2f}' if oeh else 'n/a'
        base = m.split('@')[0]
        s11 = '-' if ARM(m) else ' / '.join(f'{sess[(m, "11-11", s)]["pos"][0]}/66' for s in ('pc-desk', 'webcam'))
        n11 = '-' if ARM(m) else ' / '.join(f'{sess[(m, "11-11", s)]["near"][0]}/35' for s in ('pc-desk', 'webcam'))
        lt = live.get(f'{m}|test|true|all|all')
        lf = live.get(f'{m}|test|false|all|all')
        lvs = f'{lt[0]}/{lt[1]} / {lf[0]}/{lf[1]}' if lt and lf else '-'
        sk = {'v4': 'v4', 'v5': 'v5', 'v6': 'v6m', 'abl': 'v6abl', 'v6.2': 'v62'}.get(base)
        syn = '-' if ARM(m) else ' / '.join(f'{100 * synth[s][sk]["rate"]:.1f}' for s in ('clean', 'tv10', 'quiet'))
        P(f'| {m} | {hk}/{hh:.1f} h = {hk / hh:.3f} [{lo:.3f}, {hi:.3f}] | {pe} | '
          f'{ok}/{oh:.1f} h = {ok / oh:.3f} | {ope} | {s11} | {n11} | {lvs} | {syn} |' if hh and oh else
          f'| {m} | n/a | n/a | n/a | n/a | {s11} | {n11} | {lvs} | {syn} |')
    P('')
    (W / 'v6/all_numbers.md').write_text('\n'.join(L) + '\n')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
