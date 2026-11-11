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
