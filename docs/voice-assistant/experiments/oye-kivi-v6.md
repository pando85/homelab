# "oye Kivi" v6: context-trained candidate (2026-11-11)

Plan and owner decisions: kivi-models `docs/plan/2026-11-v6-plan.md` (private). Follows the
[vnext ablations](oye-kivi-vnext-ablations.md) (v5). **Live 2026-11-11 22:11 to about 22:40 UTC by owner decision despite the C3 FAIL, then rolled back by the owner ("more false positives")**; v5 is the household wake word again (see [Deployment](#deployment-2026-11-11-by-owner-decision-despite-c3-fail-rolled-back-the-same-evening)).
Numbers only; speakers are spk01 and spk02. Work directory `/work/workspaces/kivi-wakeword/v6/` (`results.md` there has
every table; data in [data register](../data-register.md) row 22).

**Hypothesis:** adding real household positives in context, hard negatives and synthetic "...words... oye Kivi,
<command>" positives plus confusers in sentences to the v5 recipe raises recall with the TV on and for speakers other
than spk01 without raising false wakes per evening.

**Result:** recall up, confusers down, but **room false wakes up** at the pre-registered threshold. The candidate
`oye_kivi_ctx` (`v6-oye-kivi-2026-11-11`, raw 0.9595 = server 0.5) fails the auto-deploy criterion C3. **It was deployed anyway on 2026-11-11 by owner decision and rolled back
about 30 minutes later** (see [Deployment](#deployment-2026-11-11-by-owner-decision-despite-c3-fail-rolled-back-the-same-evening)). The other-speaker and TV-microphone parts of the hypothesis cannot be tested yet (no odd-day data).

## Deployment (2026-11-11, by owner decision despite the C3 FAIL; rolled back the same evening)

The owner ordered "Deploy V6 now" on the evening of 2026-11-11, knowing that C3 failed (13 replay events against v5's 5
over 79.5 h; some of the extra events may be real missed calls of the household that v5 did not wake on). This is an
owner override of the pre-registered auto-deploy rule for this one release.

**Outcome: rolled back by the owner at about 22:40 UTC (reason: "owner: more false positives", i.e. it felt like more
false wakes than v5). "Kiwi" is on `oye_kivi_next` (v5) again; v6 was live for about 30 minutes, so this is a subjective
report, not a measurement.** It is consistent with C3 (13 vs 5 replay events) but is not counted as evidence beyond the
owner's report. The release state's `demote` step was recorded afterwards at 22:42Z with `wake_release.py demote`
("Kiwi" already used `oye_kivi_next`, nothing written to Home Assistant). v6.1 and v6.2 below remain pre-registered.

- Release `v6-oye-kivi-2026-11-11` (kivi-models commit `8e989e5`, annotated tag of the same name), image
  `git.grigri.cloud/agil/kivi-models:v6-oye-kivi-2026-11-11@sha256:6448161ad63e` (CI run 7; anonymous pull refused).
  It carries `kivi` v1, `oye_kivi` v4, `oye_kivi_next` v5 and `oye_kivi_ctx` v6, so a `demote` needs no image rollback.
- Homelab pin commit `fe539af67`; Argo CD rolled the `openwakeword` pod; every `/custom/*.tflite` sha256 equals
  `models.lock` (`oye_kivi_ctx.tflite` `78dc9477851f747a80ec60d12c89185b707a8076577d17f3be87463467535686`); Wyoming
  ids served: `alexa hey_jarvis hey_mycroft hey_rhasspy kivi okay_nabu oye_kivi oye_kivi_ctx oye_kivi_next`.
  (`wake_release.py deploy` timed out waiting for an Argo revision equal to its own commit because another commit landed
  first; the rollout itself was verified by hand and by `status`.)
- `wake_release.py promote --owner-override` at 22:11 UTC: pipeline "Kiwi" (preferred) `wake_word_id`
  `oye_kivi_next` -> `oye_kivi_ctx`, nothing else changed; no per-satellite override; "Kiwi Test" untouched. Run check:
  c3-a OK, c3-b OK (newest run on "Kiwi" with `wake_word-start` and no error). Wake files of both TVs grew about
  32 kB/s over 30 s.
- Gate: not run; recorded as an owner override. Threshold raw 0.9595 = server 0.5 (not interchangeable with v5's 0.9754).
- **Rollback (done by the owner, about 22:40 UTC):** "Kiwi" back on `oye_kivi_next`; the command is `python3 wake_release.py demote
  --version v6-oye-kivi-2026-11-11`. The v6 image stays pinned (it also serves v5, v4 and `kivi`), so no image rollback.
- Open: the 30 live minutes are too short to count; the false-wake question stays for v6.1/v6.2 (selection and gate pre-registered below).

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

## C3 relabel rule for real attempts (pre-registered 2026-11-11, before any label exists)

Why: the owner reports many "oye Kivi" calls that v5 did not answer. In the room replay such a missed real attempt counts
as a "false wake" for every model that catches it, so v6's 13 test2-v5era events may include genuine attempts (the
2026-11-09 evening cluster could be retries).

- Events to review: the union of the TFLite replay events on test2-v5era of v5 at raw 0.9754, v6 (`oye_kivi_ctx`) at
  0.9595 and the ablation at 0.9285, merged when within +-2 s on the same TV. The v6.1 arms are not scored on them.
- Each event becomes a 6 s window in the format of the missed-wake candidates; the review table is shuffled and shows no
  model, score or rank; the event-to-model mapping is kept in a separate file that the review never shows.
- The owner labels blind, as for the miss candidates (`attempt`, `other-speech`, `tv-media`, `noise`, `unclear`).
- An event labelled `attempt` (any speaker) is removed from the false-wake count of EVERY model and counts as a missed
  attempt for each model that did not fire on it. `unclear`, `tv-media`, `other-speech` and `noise` stay false wakes
  (the conservative side).
- C3 is then recomputed for v5, v6 and the ablation with the same thresholds and the same evening rules. Nothing else
  changes: no threshold, no model, no other criterion.

## Owner labelling queue (in this order; every review is resumable, `q` quits, owner labels always win)

```
cd ~/homelab/scripts/voice-assistant/wakeword
P=/work/workspaces/kivi-wakeword/.venv/bin/python
$P kivi_live_wakes.py review --table c3_events.csv        # 1. the C3 events: decides v6 now (blind, shuffled)
$P kivi_live_wakes.py review --table miss_candidates.csv  # 2. missed-wake candidates of the v5-era room audio
$P kivi_live_wakes.py review --only-uncertain             # 3. the uncertain live wake clips (auto-labeller)
```

1. `c3_events.csv` (`datasets/real/live-misses/`): the union of the replay events of v5, v6 and the ablation on
   test2-v5era, merged within +-2 s, 6 s windows, shuffled; no model, score or rank is shown. Labels as for the miss
   candidates (a = attempt, o = other speech, t = TV / media, n = noise, u = unclear). The relabel rule above applies.
2. `miss_candidates.csv`: 40 windows where v5 did not fire but a model peaked (see the
   [v5-era mining note](oye-kivi-v5era-mining.md)).
3. The live wake clips that the auto-labeller left uncertain (`unclear`, low confidence, or `true` without a speaker).

### Amendment 1 (2026-11-11, about 23:00 Madrid; before any 2026-11-12 audio is read)

Risk: the mined hard negatives may contain real "oye Kivi" calls that v5 missed live. Every mined event (v6 raw >= 0.5,
v5 raw >= 0.3) with retry evidence was flagged and its windows **dropped** (not relabelled as positives); then p1a, p1b
and p1c were **retrained with the same recipe** (`v6p1_filter_hard.py`, `v6p1_train.sh`; the pre-filter arms are kept
in `v6p1/runs_prefilter/` and are not candidates). Retry evidence, same TV: a live wake detection or an stt-only run
(mic button, push to talk) within 30 s before or after, another v4/v5/v6 event (raw >= 0.5) 5 to 30 s before or after,
or a missed-wake candidate within 6 s.

| Model, day | events: kept / dropped (day; evening) | windows kept / dropped |
| --- | --- | --- |
| v6, 2026-11-06 | day 11 / 9; evening 1 / 2 | 40 / 33 |
| v6, 2026-11-08 | day 1 / 0; evening 2 / 6 | 11 / 19 |
| v6, 2026-11-10 | day 26 / 38; evening 14 / 16 | 137 / 193 |
| v6 total | 55 kept, 71 dropped (of 126) | 188 / 245 (day 130, evening 58 kept) |
| v5, 2026-11-06 | day 179 / 249; evening 49 / 58 | 997 / 1,324 |
| v5, 2026-11-08 | day 10 / 7; evening 149 / 188 | 629 / 828 |
| v5, 2026-11-10 | day 281 / 356; evening 98 / 109 | 1,522 / 2,082 |
| v5 total | 766 kept, 967 dropped (of 1,733) | 3,148 / 4,234 |

Reasons (an event can have several): other-event 1,031, miss-candidate 33, live 20, stt-only 8. Nothing else changes: the
stage-2 rule and the stage-3 gate above stand as written.

### Amendment 2 (2026-11-11, about 23:20 Madrid; before any 2026-11-12 audio is read)

Owner decisions of 23:10: v6 (`oye_kivi_ctx`) is deployed by a separate agent despite C3; a v6.2 is trained overnight
(pre-registered below). Stages 2 and 3 stay and become the honest confirmation of **the best of v6.1 and v6.2 against
whatever is live**:
- Stage 2 applies the stage-2 rule unchanged (threshold per arm on 2026-11-12 against v5's 2026-11-12 count, then the
  arm by R, same tie-break, with v6.2 arms placed after p1b in the final order) to the v6.1 arms p1a, p1b, p1c AND the
  v6.2 arms. One candidate comes out.
- Stage 3 runs the gate on that candidate with the live model as the reference instead of v5 wherever the criteria
  compare against v5 (C1, C2, C3a, C3b, C4, C5); v5 is still reported. If the live model is v6, C3b's reference is
  v6's test2-v5era count (13) and C3a's is v6's 2026-11-13 count.

## v6.2 (owner decision 2026-11-11 23:10): PRE-REGISTERED before training and before any 2026-11-12 audio

Recipe = v6.1 (stage 1, after amendment 1) plus the training-data quality fixes of the data-quality agent
(`/work/workspaces/kivi-wakeword/quality/`, applied as they land; each applied flag set is listed in results.md with
its counts):
- hard and room negatives: drop windows with a phrase flag (ASR finds "oye Kivi" / "Kivi" in the window,
  `hardneg_phrase_flags.csv`) or with retry evidence (amendment 1 rule; room windows within 30 s of a live wake or an
  stt-only run, or within 6 s of a miss candidate, are dropped too);
- real positives: drop takes flagged as mis-cut or flubbed; synthetic: drop the voices flagged as unintelligible;
- drop the high-confidence errors of the grouped 5-fold out-of-fold check that ASR confirms;
- keep the synthetic context data (it bought C2); the 500 held-out context clips stay held out.

**Threshold data (fixed now): 2026-11-10** (even, both TVs, 47.8 h, 2.0 device-evenings). v6.2 does not train on it:
its room negative windows and its mined hard negatives come only from 2026-11-05 (grandfathered), -06 and -08
(the v6.1 arms did use 2026-11-10; v6.2 does not). No 2026-11-12 or 2026-11-13 audio is used.

Threshold rule per arm (tied to v5, stricter than v6's): the lowest raw value t on the grid 0.9000, 0.9005, ..., 0.9995
such that at t and every grid value above it, on 2026-11-10: (a) total events <= max(E5, 1), E5 = v5's events at raw
0.9754 on the same audio and framing (embedding pass, `v6p1_select.py`), and (b) per TV, evening events <=
max(1, round(evening hours / 6)). No such t: the arm is not eligible.

Arms (two, both warm-started from v6 `oye_kivi_ctx`, v6.1 data weights otherwise):
- `p2a`: v6 hard negatives day 32/step w6, evening 32/step w12, v5 hard negatives 128/step w3 (as p1a)
- `p2b`: v6 hard negatives day 48/step w12, evening 48/step w24, v5 hard 128/step w6 (as p1c)

Arm rule: the highest R = mean accept rate at its threshold on the isolated synthetic validation windows
(`v6/feat/pos_test.npy`, minus any window the quality flags remove from evaluation) and the 1,000 held-out context
validation windows; tie (within 1.0 point): fewer 2026-11-10 events at their thresholds, then lower near-phrase
validation accepts (`v6/feat/near_test.npy`), then p2a.

Morning comparison, v6.2 (chosen arm) against v6 (`oye_kivi_ctx`, raw 0.9595), odd-day honest sets only;
**test2-v5era and the 2026-11-11 session are REUSED test sets (weaker evidence)**. v6.2 replaces v6 only if ALL hold:
- C1: no cell of the 2026-11-11 session (3 m off pooled, 3 m on, 1.5 m on, 1.5 m off; pc-desk and webcam) more than
  1 take below v6;
- C2: near-phrase false accepts on that session <= v6's, per device;
- C3: room false wakes (TFLite replay as the mining pass) <= v6's on test2-v5era AND <= v6's on the 2026-11-11
  evening copy (`negatives-1111eve`);
- C4: test-day live clips (2026-11-09 and -11, exact replay): `true` detected >= v6's;
- C5: held-out-voice synthetic clean / TV 10 dB / quiet within 5 points of v6 (not lower by more than 5).
No deploy by this agent; the coordinator runs any deploy.

### v6.2 amendment 1 (2026-11-11, about 23:25 Madrid; before v6.2 training, no 2026-11-12 audio)

After the data-quality ASR check of the mined hard negatives (`quality/hardneg_phrase_flags.csv`: the phrase is
supported in only 5 of the events the amendment-1 retry filter dropped), the v6.2 hard-negative drop rule is: an event is
dropped when it has strong retry evidence (a live wake or an stt-only run within 30 s, or a miss candidate within 6 s)
or an ASR phrase flag (`phrase`, `kivi_word`, or `hotword_only` with confidence >= 0.5). "Another model event 5-30 s
away" alone no longer drops (TV content clusters; ASR does not support a phrase there). Mined on 2026-11-06 and -08
only: v6 raw >= 0.5: 32 events, 1 dropped, 99 windows kept (day 64, evening 35); v5 raw >= 0.3: 889 events, 67 dropped,
3,478 windows kept. Reasons: asr-phrase 50, live 15, miss-candidate 11, stt-only 5. Room negative windows (stride 8,
2026-11-05, -06, -08): 247,987 kept, 851 dropped (within 30 s of a live wake or stt-only run, or 6 s of a miss
candidate). Per-step counts follow the smaller sets: `p2a` v6 day 32 w6, evening 32 w12; `p2b` v6 day 48 w12, evening 32
w24; v5 hard 128 per step (w3 / w6). The 3 even-day live clips labelled false whose tail holds a kivi-like word (data
quality 1b) are dropped from live_hard and live_negpart. Everything else in the v6.2 pre-registration stands.

### v6.2 amendment 2 (2026-11-11, about 23:55 Madrid; before v6.2 training, no 2026-11-12 audio)

The owner rolled back to v5 at about 22:40Z (v6 felt like more false wakes). Changes, everything else stands:
- **Reference = v5** (`oye_kivi_next`, raw 0.9754), the live model, with the ORIGINAL C1-C6 (C1 >= v5 on pc-desk and
  webcam and no cell more than 1 take below v5; C2 <= v5 + 1 per device; C4 true detected >= v5 and false fired <= v5;
  C5 within 5 points of v5; C6 sanity). **C3 strict, both**: test2-v5era (REUSED) total <= v5 + 1 = 6 and per evening
  <= 1 (per TV evening events <= max(1, round(evening hours / 6)) and pooled per evening <= 1); the fresh 2026-11-11
  evening copy (`negatives-1111eve`) total <= v5's count there + 1. v6 is reported alongside as information only.
- **Arm rule (false wakes first):** among eligible arms, the one with the fewest 2026-11-10 room events at its
  threshold; tie: higher R (validation recall), then fewer near-phrase validation accepts, then p2a. The threshold rule
  already aims at v5's 2026-11-10 count (total <= max(E5, 1)), never above it.
- Data fixes kept: 1a (hard-negative ASR flags), 1b (5 live_hard clips, 2 real takes, flagged TTS voices; the 2 stt-path live
  positives are kept, amendment 4), 1c (room-negative ASR flags, all non-`none` flags), the retry filter on room windows and on the hard
  negatives (live wake / stt-only within 30 s, miss candidate within 6 s), all mined only on 2026-11-05/-06/-08.

### v6.2 amendment 3 (2026-11-11, about 23:35 Madrid; before v6.2 training, no 2026-11-12 audio read)

Grouped 5-fold out-of-fold flags (`quality/oof_flags_*.csv`), applied as "OOF + ASR" where an ASR check exists:
- real positives: the 7 OOF-flagged real items (pc-mic 4, webcam 2, one live clip) are KEPT, because the ASR check
  supports the phrase in every one of them (strict hotword match); their OOF failure is not a label error;
- real negatives (prompted near-word takes, live false wakes) are KEPT: they are the confusers C2 rests on; room
  negatives are covered by the 1c ASR flags (already dropped);
- synthetic positives with an OOF median below 0.01 are dropped (2,435 isolated and 208 context windows, after the voice
  filter); synthetic negatives that a held-out fold accepts at the v6-matched operating point (n_above_fold_thr >= 1)
  are dropped (near_train 651, confusers-in-sentences 423, same-clip negatives 6 windows).
Resulting v6.2 inputs: isolated synthetic positives 36,151; context positives 6,834; near_train 59,314; context
confusers 9,097; live positives 900 / 450 / 300 (spk01 / spk02 / unknown, amendment 4); live hard 2,700; live same-clip negatives 810;
real takes rebuilt from 228 of 232 takes (120 copies); hard negatives v6 99, v5 3,319; room windows 247,647.

(Paragraph on mis-cut live positives withdrawn 2026-11-11: see amendment 4.)

### v6.2 amendment 4 (2026-11-11, about 23:45 Madrid; before v6.2 training, no 2026-11-12 audio read)

Correction to 1b: the two stt-path live positives (c3-b 2026-11-07 23:58:40Z and 23:59:06Z) are KEPT. The data-quality
check had compared them with the wake tail; `v6_cuts.py` cuts them inside stt.wav, where the phrase ends about 0.3-0.4 s
before the cut, and they score 0.998 / 0.999 out of fold. The live positives of v6.2 are therefore the v6 ones
(spk01 900, spk02 450, unknown 300 windows) and the same-clip negatives 810. The mis-cut hypothesis for v6's false
wakes is void. The live clip c3-b 2026-11-08 07:54:59Z (spk01, wake) scores about 0.0002 out of fold although ASR finds
the phrase: a coverage gap, not a label error; it is kept. Items 2-4 of 1b stand.

### v6.2 amendment 5 (2026-11-11, about 23:52 Madrid; final data and weights, before v6.2 training)

Final data-quality decisions applied (out-of-fold + ASR, coverage):
- dropped as negatives that are really attempts: the room events of 2026-11-08 20:41:16Z (c3-a), 19:29:06Z (c3-b, miss
  candidate mc001), 2026-11-06 12:25:50Z (c3-b) and the c3-b event near 2026-11-08 22:33:11Z (window +-2 s, room windows
  and hard negatives), and the 5 live_hard clips of 1b;
- synthetic voices: vctk_tr, siwis and upmc dropped (isolated and context sets), riccardo dropped (context); davefx and
  ona at half weight (both sets), north_m and alba at half weight (context);
- confusers "oye kiri", "oye kili", "oye Kibo", "oye Gibi", "oye chivi" doubled in the synthetic near-phrase and
  confuser-in-sentence negatives (5,114 and 1,724 extra windows);
- kept: neg_kivi and neg_near (genuine confusers), the low-scoring real takes and live clip 07:54:59Z (coverage gaps);
- coverage weights per step: real TV-mic session takes 96 (was 64), live spk01 24 (16), spk02 16, unknown 8, pc 48,
  webcam 16, synthetic isolated 192, synthetic context positives 32 (was 64; a regulariser).
Final counts: synthetic isolated 35,878; context positives 6,790; near_train 64,428; context confusers 10,821; same-clip
synthetic negatives 4,949; real takes TV 9,000 / pc 9,480 / webcam 8,880; live positives 900 / 450 / 300; live hard
2,700; live same-clip negatives 810; hard negatives v6 99 (day 64, evening 35), v5 3,312; room windows 247,640.

### v6.2 trained and selected (2026-11-11, 23:58 Madrid; pre-registered rule, 2026-11-10 room audio only)

Arms p2a and p2b trained (about 30 s each, warm start v6, amendment-5 data). Selection (`v6p1_select.py --rule
fewest-events`, 2026-11-10, 47.8 h, 2.0 device-evenings): v5 at 0.9754 has E5 = 2 events. p2a: raw 0.985, 2 events
(1 in the evening, c3-a); p2b: raw **0.9825**, 2 events (1 in the evening, c3-a). Tie on events, so R decides:
p2b 34.4 % (isolated validation 39.5 %, context validation 29.4 %) against p2a 33.6 %. **Chosen: p2b at raw 0.9825.**
Near-phrase validation accepts 0.47 % for both (v5 at 0.9754: 0.03 %). Calibrated file
`v6p2/final/oye_kivi_ctx2.tflite` (server 0.5 = raw 0.9825; 0 of 9,378 steps disagree; pyopen-wakeword 1.1.0),
sha256 95846976146978b6b7b9ee4a6caf9bd4233a6d315318802fe8b90bf81abe77f5; uncalibrated 55594bd78b7e... Nothing
packaged. The morning comparison against v5 (`v6p2_compare.sh`) runs next.

### C3 review table built (2026-11-12, about 00:50 Madrid)

The like-for-like TFLite replay of test2-v5era (`v6_replay.py`, same as the mining pass) reproduces v4 26, v5 5, v6 13
and the ablation 21 events. Their union for v5, v6 and the ablation, merged within +-2 s per TV, gives **29 windows**
(c3-a 8, c3-b 21): v6 or the ablation only 24, v5 only 4, all three 1. `datasets/real/live-misses/c3_events.csv`
(shuffled, no model or score), mapping in `/work/workspaces/kivi-wakeword/v6/all/c3_events_mapping.json`
(`v6_c3_review_table.py`). Owner review: `kivi_live_wakes.py review --table c3_events.csv` (first in the queue above).

### C3 after the owner's blind labels (2026-11-12; pre-registered relabel rule applied)

The owner labelled 24 of the 29 test2-v5era replay windows: attempt 2 (both spk01, c3-a, daytime), other speech 10,
noise 8, TV / media 4; 5 were skipped and count as unclear, i.e. false wakes. Attempts leave every model's false-wake
count and count as misses for the models that did not fire on them.

| Model @ raw | events before | labels of its events | false wakes after | evening after (per TV c3-a / c3-b) | missed attempts | C3 (<= v5 + 1, per evening <= 1) |
| --- | --- | --- | --- | --- | --- | --- |
| v5 @ 0.9754 | 5 | attempt 1, other speech 2, noise 2 | **4** (c3-a 0, c3-b 4) | 0 (0 / 0) | 1 of 2 | reference |
| v6 @ 0.9595 | 13 | attempt 2, TV / media 4, other speech 4, unlabelled 3 | **11** (c3-a 5, c3-b 6) | 4 = 2.18 per evening (1 / 3) | 0 of 2 | **FAIL** (11 > 5; evenings) |
| ablation @ 0.9285 | 21 | attempt 2, other speech 6, noise 6, TV / media 2, unlabelled 5 | **19** (c3-a 5, c3-b 14) | 2 = 1.09 per evening (1 / 1) | 0 of 2 | **FAIL** |

Most of v6's events are real false wakes (other speech, TV, noise), not missed calls: the owner's rollback stands
justified. For the v6.2 comparison the same labels apply: a v6.2 event within +-2 s of an `attempt` window is not a
false wake; every other v6.2 event stays one (also when it is not in the table). Reference after the relabel: v5 = 4.

### v6.2 morning comparison against v5: FAIL (2026-11-12, about 02:20 Madrid)

Pre-registered criteria (amendment 2), odd-day sets only; test2-v5era and the 2026-11-11 session are REUSED test sets.
v6.2 = p2b at raw 0.9825 (`oye_kivi_ctx2`); v5 at 0.9754; v6 at 0.9595 for information. TFLite replay as the mining pass.

| # | v5 | v6.2 | v6 (info) | Result |
| --- | --- | --- | --- | --- |
| C1 2026-11-11 session pc / webcam (66 each; cells 3 m off / 3 m on / 1.5 m on / 1.5 m off) | 55 / 53 (pc 33/6/6/10, webcam 32/5/8/8) | 60 / 62 (pc 35/8/7/10, webcam 35/9/9/9) | 60 / 62 | PASS |
| C2 near-phrases pc / webcam (35) | 0 / 1 | 0 / 1 | 0 / 2 | PASS |
| C3 test2-v5era, owner labels applied (<= v5 + 1, evenings) | 4 (c3-a 0, c3-b 4); 0 in the evening | **17** (c3-a 4, c3-b 13); **9 in the evening** (c3-b 8, all 2026-11-09) | 11; 4 | **FAIL** |
| C3 2026-11-11 evening copy (3.3 h; <= v5 + 1) | 4 (c3-a 3 in 0.4 h, c3-b 1) | **7** (c3-a 4, c3-b 3) | 6 | **FAIL** |
| C4 test-day live clips: true detected / false fired | 16/16 / 15/16 | **15/16** / 7/16 | 16/16 / 6/16 | **FAIL** (one true wake fewer) |
| C5 held-out synthetic clean / TV 10 dB / quiet | 51.2 / 30.1 / 39.5 % | 59.1 / 41.7 / 52.3 % | 59.8 / 39.8 / 53.5 % | PASS |
| C6 calibration (0 of 9,378 steps disagree), runtime 1.1.0, no odd-day data in training or selection | | checked | | PASS |

Verdict: **v6.2 does not replace v5.** Its recall equals v6's, but on the reused odd-day room audio it fires more often
than v6 even at the stricter threshold: 10 of its 17 test2 events are not in the labelled table (new events, counted as
false wakes, conservative), and 8 fall in the 2026-11-09 evening on c3-b. The threshold matched v5's rate on 2026-11-10
(2 events each) but did not transfer to other evenings: 2.0 device-evenings of selection audio again proved too few,
and the evening TV content of 2026-11-09 behaves differently. Labels of the 10 new events would be needed before reading
more into it; the 2026-11-12 / -13 stages remain the honest test.

### Labelling round 2 (2026-11-12, about 02:30 Madrid)

`datasets/real/live-misses/c3_events_r2.csv`: **17 windows**, blind as round 1: the 10 v6.2 events on test2-v5era that
are not within +-2 s of a round-1 window, and the union of the v6 and v6.2 events on the 2026-11-11 evening copy
(6 shared, 1 v6.2 only); c3-a 5, c3-b 12. Mapping `v6/all/c3_events_r2_mapping.json`. It is in the owner's queue
(`kivi_live_wakes.py queue` reads every `live-misses/*.csv`), or alone: `kivi_live_wakes.py review --table c3_events_r2.csv`.
The same relabel rule applies (attempt = not a false wake, every other label stays one).
