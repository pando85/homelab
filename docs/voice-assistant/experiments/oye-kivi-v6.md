# "oye Kivi" v6: context-trained candidate (2026-11-11)

Plan and owner decisions: kivi-models `docs/plan/2026-11-v6-plan.md` (private). Follows the
[vnext ablations](oye-kivi-vnext-ablations.md) (v5). Offline only: **not deployed**, no image, no Home Assistant change.
Numbers only; speakers are spk01 and spk02. Work directory `/work/workspaces/kivi-wakeword/v6/` (`results.md` there has
every table; data in [data register](../data-register.md) row 22).

**Hypothesis:** adding real household positives in context, hard negatives and synthetic "...words... oye Kivi,
<command>" positives plus confusers in sentences to the v5 recipe raises recall with the TV on and for speakers other
than spk01 without raising false wakes per evening.

**Result:** recall up, confusers down, but **room false wakes up** at the pre-registered threshold. The candidate
`oye_kivi_ctx` (`v6-oye-kivi-2026-11-11`, raw 0.9595 = server 0.5) fails the auto-deploy criterion C3 and is not
deployed. The other-speaker and TV-microphone parts of the hypothesis cannot be tested yet (no odd-day data).

## Step 0: the replay bug

`eval_stream.stream` dropped the audio after the last whole 1280-sample chunk and anchored the grid at the start of a
tail, so the replay of a live wake tail never formed the detection step. It now feeds the remainder in 160-sample frames
and takes the server's grid phase (`n_total` from `wake_len`); whole streams from sample 0 are unchanged except one
final step (the 2026-11-11 session re-score is identical). `kivi_autolabel.py` lost the `NEAR_MISS` patch;
`kivi_live_wakes.py autolabel --trigger-only` re-ran only the model step: trigger wake 37 -> 51, button 14 -> 12, unknown
13 -> 1 of 64 clips (51 of 52 same-run tails cross, as the data audit predicted). 11 audit exclusions applied.

## Recipe

v5 hyper-parameters (warm start from v5, layer 64, 4000 steps, lr 2e-4, adv 128, seed 2), new data:

- synthetic positives filtered (no clip > 1.5 s, no zero-heavy clip, 8 weak voices out: 46,552 of 59,973);
- 5,000 synthetic context clips "...oye Kivi, <command>" with no pause (`v6_gen_ctx.py`, `v6_ctx.yaml`) cut at the phrase
  end (positives) and before "Kivi" / in the command (negatives), 5,000 confuser and "Kivi"-without-"oye" sentences;
- live wake clips of train days: 11 true (spk01 6, spk02 3, unknown 2) as positives per speaker, cut at the
  exact-replay detection step; 20 false wakes as hard negatives (`v6_cuts.py`, `v6_augment.py ctx`);
- level and floor augmentation widened to the measured satellite range (owner-level speech, quiet c3-b, c3-a floor);
- room negatives from even days only, minus 6.7 h of pre-front-end audio; v5-mined hard negatives;
- threshold: most sensitive raw value with <= 1 false wake per evening on even-day room audio **not used in training**
  (2026-11-10, 47.8 h; `v6_room.py curve`). Using the whole even-day set first gave 0.76 because the same audio was in
  the training negatives, so the even days were split by date before any test number was seen.

`v6_room.py` computes server-exact embeddings of the room audio once (172 h in about 20 min on the GPU) and scores any
head on them; it matches the pyopen-wakeword TFLite runtime to 0.001 on room audio.

## Pre-registered criteria (odd days only, each model at its own threshold)

| # | Criterion | v5 (raw 0.9754) | v6 (raw 0.9595) | |
| --- | --- | --- | --- | --- |
| C1 | 2026-11-11 session, pc-desk / webcam, 66 cues each | 55 / 53 | **60 / 62**, no cell lower | PASS |
| C2 | near-phrase false accepts (35 cues) | 0 / 1 | 0 / 2 | PASS |
| C3 | room false wakes, odd days 2026-11-09 and -11 (79.5 h), TFLite replay | 5 (0.063/h [0.020, 0.147]); 0 in evening hours | **13 (0.164/h [0.087, 0.280]); 4 in the evening = 2.2 per evening [0.6, 5.6]** | **FAIL** |
| C4 | live test-day wakes (3 true, all spk02) / false wakes (4) | 3 / 4 fired | 3 / 0 fired | PASS (set selected by v5) |
| C5 | synthetic held-out voices clean / TV 10 dB / quiet | 51 / 30 / 40 % | 60 / 40 / 54 % | PASS |
| C6 | no test-day data in training or selection; calibration; runtime 1.1.0 | | checked | PASS |

The ablation without the synthetic context data reaches the same session recall at a lower threshold (0.9285) but
accepts more near-phrases (1 / 4) and more room events (20): the context data buys confuser rejection, not recall.

## What this means

- v6 is a better listener on the PC microphones of one held-out session, but at the threshold its rule gave it fires
  more than twice as often as v5 on two odd days of room audio, and its evening events score high (3 above raw 0.975, where
  v5 has none): some evening (TV) content now looks like the phrase.
- Selection on 2 evening-equivalents of room audio cannot pin a threshold (v6 had 4 events there and 12 on the test
  part, both within Poisson noise of each other). The next selection needs more even-day evenings and a rule tied to
  v5's measured rate; the test must be a fresh odd day, because the odd-day room part has now been looked at for v6.
- Still missing for the gate: an odd-day session on the TV microphone, and spk02 takes on c3-b.

## Reproduce

```sh
cd /work/workspaces/kivi-wakeword
H=$HOME/homelab/scripts/voice-assistant/wakeword
.venv/bin/python -I $H/kivi_live_wakes.py autolabel --trigger-only          # step 0 (owner labels win)
.venv/bin/python -I $H/v6_gen_ctx.py gen --voices-dir v4/voices --out v6/clips_ctx --set all --jobs 6
.venv/bin/python -I $H/v6_room.py embed --manifest datasets/real/negatives-v5era/manifest.jsonl \
  --manifest datasets/real/negatives/manifest.jsonl --skip-list v6/prefe_skip.txt --out-dir v6/room_emb
.venv/bin/python -I $H/v6_cuts.py live --out v6/cuts/live.jsonl
bash v6/feats.sh; bash v6/room.sh; bash v6/train.sh; bash v6/eval.sh   # in systemd scopes (MemoryMax, CPUQuota)
```

## v6.1 (owner approval 2026-11-11): stage 1 done, stages 2 and 3 PRE-REGISTERED

Written and committed on 2026-11-11 (evening, Madrid), before any 2026-11-12 or 2026-11-13 audio exists. v6 stays
undeployed. **test2-v5era is not read again for any purpose** until its one final run in stage 3.

### Stage 1 (done 2026-11-11)

- Hard negatives on ALL even-day room audio (2026-11-06 old set, 2026-11-08 and 2026-11-10 v5 era; both TVs; 11-10 is
  training data now, 2026-11-12 replaces it for selection), `v6_room.py mine --split-evening`:
  - v6 (uncalibrated `oye_kivi_ctx`) raw >= 0.5, +-1 step: **126 events, 433 windows** (day 298, evening 135):
    11-06 day 20 events / 64 windows, evening 3 / 9; 11-08 day 1 / 4, evening 8 / 26; 11-10 day 64 / 230,
    evening 30 / 100 (c3-a 38 events, c3-b 88).
  - v5 raw >= 0.3, +-1 step: 1,733 events, 7,382 windows (11-06 535 events, 11-08 354, 11-10 844).
  - room negative windows (stride 8) of 2026-11-05 (grandfathered), -06, -08, -10: 517,570.
- 500 synthetic context clips (1,000 windows) held out of training for stage 2.
- Three arms (v6 data and settings otherwise: 4000 steps, lr 2e-4, seed 2; synthetic context data kept):
  - `p1a`: warm start v6; v6 hard negatives day 32/step weight 6, evening 32/step weight 12; v5 hard 128/step w3
  - `p1b`: warm start v5; same data and weights as p1a
  - `p1c`: warm start v6; v6 hard negatives day 48/step w12, evening 48/step w24; v5 hard 128/step w6

  Script `scripts/voice-assistant/wakeword/v6p1_train.sh`, about 1.5 min per arm. No arm has been scored on any day-split audio.

### Stage 2 rule (selection on 2026-11-12, even, fresh): `v6p1_select.py`, `v6p1_stage2.sh`

1. Reference: v5 `oye_kivi_next` at raw 0.9754 on the 2026-11-12 room audio (part `sel-v6p1`, local date 2026-11-12):
   total false wakes E5 (server rule: above raw, 2 s refractory).
2. Threshold per arm: the lowest raw value t on the grid 0.9000, 0.9005, ..., 0.9995 such that at t **and at every grid
   value above it** (a) total events <= max(E5, 1) and (b) on each TV, evening (18-24 Madrid) events <= max(1,
   round(evening hours / 6)). No such t: the arm is not eligible. No eligible arm: stop (no v6.1).
3. Arm: the highest R = mean accept rate at its threshold over (i) `v6/feat/pos_test.npy` (2,367 isolated synthetic
   windows, train voices, held out of every run), (ii) `v6p1/feat/ctxp_syn_pos_val.npy` (1,000 windows of the 500 held-out
   context clips), (iii) the 2026-11-12 live true wakes (label true, trigger wake, exact replay) when there are at least 3.
4. Tie-break when the best two R are within 1.0 point: fewer 2026-11-12 room events at their thresholds, then the lower
   accept rate on `v6/feat/near_test.npy` (synthetic near-phrases, train voices), then the order p1a, p1c, p1b.

Stage 2 reads nothing else: no odd-day data, not the 2026-11-11 session, not the held-out-voice synthetic sets.

### Stage 3 gate (run once, after 2026-11-13 ends): `v6p1_stage3.sh`; the chosen arm at its stage-2 threshold

| # | Criterion (PASS needs all) |
| --- | --- |
| C1 | 2026-11-11 session: v6.1 >= v5 on pc-desk AND webcam (66 cues each), and no cell (3 m off pooled, 3 m on, 1.5 m on, 1.5 m off) lower than v5 by more than 1 take on either device |
| C2 | near-phrase false accepts on that session <= v5 + 1 per device |
| C3a | 2026-11-13 room audio (part `test3-v6p1`, local date 2026-11-13; TFLite replay `v6_replay.py`): per TV evening events <= max(1, round(evening hours / 6)), pooled per-evening point estimate <= 1, and total <= v5's total on 2026-11-13 + 1 |
| C3b | test2-v5era, one final TFLite replay: total <= v5 + 1 (= 6 if v5 reproduces its 5), per TV evening events <= max(1, round(evening hours / 6)) and pooled per evening <= 1 |
| C4 | live clips of the test days 2026-11-09, -11 and -13 (exact replay): v6.1 detects >= as many `true` as v5 and fires on <= as many `false` |
| C5 | held-out-voice synthetic sets clean / TV 10 dB / quiet: not lower than v5 by more than 5 points on any |
| C6 | no odd-day data in training or selection (2026-11-05 and -07 grandfathered); the calibrated tflite crosses 0.5 exactly where the raw file crosses t; sha256 recorded; loads in pyopen-wakeword 1.1.0 |

Any FAIL: no deploy. Nothing is packaged before a PASS (the v6 package output stays where it is, uncommitted).

### Exact commands

- Stage 2 (morning of 2026-11-13, at or after 08:00 Madrid, so the files of the 11-12 evening are closed):
  `UNTIL=<UTC ISO time about 15 min ago> bash ~/homelab/scripts/voice-assistant/wakeword/v6p1_stage2.sh`. It runs
  `kivi_negatives.py --hours 1000 --since 2026-11-11T16:52:39Z --until $UNTIL --exclude-pipeline "Kiwi Test"
  --out-name negatives-v6p1 --part-test-day test3-v6p1 --part-mine-day sel-v6p1` (dry run first),
  `kivi_live_wakes.py extract` and `autolabel` (local), `v6_room.py embed`, then `v6p1_select.py` -> `v6p1/selection.json`.
  The 11-13 pieces this copy also writes are `test3-v6p1` and are not read before stage 3.
- Stage 3 (morning of 2026-11-14): `UNTIL=<...> bash ~/homelab/scripts/voice-assistant/wakeword/v6p1_stage3.sh`: the same copy
  command (existing pieces are skipped), extract and autolabel, calibration, `v6_replay.py` on test3-v6p1 (2026-11-13) and
  on test2-v5era, `kivi_score.py`, `live_eval.py`, `seq_eval.py` -> `v6p1/gate/`.
