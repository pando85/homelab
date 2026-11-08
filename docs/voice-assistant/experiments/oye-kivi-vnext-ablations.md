# vnext ablations: real voice is what moves the needle (2026-11-08)

Source: [recovery plan](oye-kivi-recovery.md) P2, run ahead of schedule because the real recordings
arrived the same morning. Follows the [P0 real-voice baseline](oye-kivi-p0-baseline.md). Everything ran
offline on the workstation (`/work/workspaces/kivi-wakeword/vnext/`); no model, threshold, Helm value or
Home Assistant state was changed.

## Arms

All arms share the vnext synthetic set (59,973 positives, 59,976 negatives from
`vnext_oye_kivi.yaml`, the strict-target recipe) and the same ACAV / MLS / hard-negative / room-negative
mix as v4's selected arm. They differ **only** in the real inputs:

| Arm | Real negatives | Real positives | Warm start |
| --- | --- | --- | --- |
| `vn_ws4k_v4real` | v4's set (predates the confuser session) | none | v2 head, 4k steps |
| `vn_ws4k_rn` | refreshed: 31 old-"kivi" + 66 near-word + 83 background rows per source | none | v2 head, 4k steps |
| `vn_ws4k_rn_rp` | refreshed | **all 232 takes** (leaks the evaluation session) | v2 head, 4k steps |
| `vn_sc4k_rn` | refreshed | none | fresh head, 4k steps |
| `vn_ws6k_rn_rp` | refreshed | all 232 takes | v2 head, 6k steps |
| `vn_ws4k_rn_rp_h`, `_h3` | refreshed | session 1 only, **session 2 held out** | v2 head, 4k steps, seeds 2 / 3 |

Selection threshold per arm = the point that leaves 2 false activations on the 8.377 h of room
*selection* audio (0.24/h); the room **test** parts were never used here.

## Result 1: confuser negatives without real positives destroy recall

Real-take acceptance (all 232 take-sources, satellite + desk mics) at each arm's own threshold:

| Arm | Real takes accepted |
| --- | --- |
| `vn_ws4k_v4real` (no real positives) | 58/232 = 25 % |
| `vn_ws4k_rn` (+ 5.5x more real confuser negatives) | **3/232 = 1 %** |
| `vn_ws4k_sc4k_rn` (fresh head) | 0/232 = 0 % |
| `vn_ws4k_rn_rp` (real positives, leaky) | 209/232 = 90 % |
| `vn_ws6k_rn_rp` (real positives, leaky, 6k steps) | 214/232 = 92 % |

The synthetic selection metrics hide this: `vn_ws4k_rn` scores *better* on the synthetic held-out set
than the control (clean 0.678 vs 0.691 is a wash, val_pos 0.395 vs 0.343) while collapsing to 1 % on
real speech, and its acceptance of the real confuser windows drops 50x (0.0051 -> 0.0001). **Pushing the
confusers down with real negatives only works if the model also has real positives to tell them apart
from; the recipe change is the pair, not the negatives.**

## Result 2: generalisation to unseen real takes (the honest number)

`vn_ws4k_rn_rp` is leaky — session 2 is in its training set. So session 2 was held out entirely
(`v3_real.py feats --holdout-session`, new: `(block, cue)` collides across sessions because every block
numbers its cues from 1) and retrained at two seeds. Session 2 = 40 cues x 3 sources = 113 usable
take-sources, never seen in training.

Scored with `kivi_score.py` on the session's stored clips, satellite `tv-c3a` (what the server hears),
each model at its own selection threshold:

| Condition | v4 (deployed) | control (no real pos) | `rn_rp_h` | `rn_rp_h3` | Gate |
| --- | --- | --- | --- | --- | --- |
| TV off (1.5 m + 3 m) | 7/20 = 35 % | 9/20 = 45 % | **14/20 = 70 %** [48, 85] | **15/20 = 75 %** [53, 89] | >= 80 % |
| TV on (1.5 m + 3 m) | 3/20 = 15 % | 4/20 = 20 % | **14/20 = 70 %** [48, 85] | **13/20 = 65 %** [43, 82] | >= 60 % |
| ALL | 10/40 = 25 % | 13/40 = 32 % | 28/40 = 70 % [55, 82] | 28/40 = 70 % [55, 82] | — |
| Desk mic, ALL | 11/40 = 28 % | 11/40 = 28 % | 27/40 = 68 % | 31/40 = 78 % | — |

Per cell (satellite, 10 takes each): `rn_rp_h` 1.5 m TV off 10/10, 1.5 m TV on 7/10, 3 m TV off 4/10,
3 m TV on 7/10; `rn_rp_h3` 10/10, 5/10, 5/10, 8/10. (Wilson 95 % intervals on the pooled cells.)

**Real positives take unseen real speech from 25 % to 70 % on the channel that matters**, with the same
room-false-wake budget as the selection gate. The TV-on gate (>= 60 %) is met at both seeds; **TV-off
(>= 80 %) is not** — 1.5 m is saturated (10/10 both seeds) and the deficit is entirely at **3 m with the
TV off** (4/10 and 5/10), which is the odd-cell-out and the thing to attack next.

## What this does not prove

- **One speaker, one day.** Session 1 and session 2 are 14 minutes apart. This measures within-session
  continuity, not across-day or across-speaker robustness. spk02 is absent from the real data entirely
  (the gate needs >= 2 speakers).
- **20 takes per condition** — intervals are +-19 points wide.
- **No room false-wake re-measurement.** The candidates were *selected* at 0.24/h on the room selection
  parts; the deployed v4 measured 0.96/h on the room test parts, and no candidate has been replayed
  through those yet.
- Near-word acceptance for the candidates is only the aggregate confuser-window number (0.0001-0.0006);
  the real confuser *takes* of session 3 were not re-scored per word.
- The `--holdout-session` windows still let session 2's 6 background segments per source into the negative
  pool (room noise, no speech); its takes and near-words do not (its near block is marked excluded).

## Reproduce

```sh
cd /work/workspaces/kivi-wakeword
bash vnext/feats.sh && bash vnext/real.sh && bash vnext/runs.sh   # synthetic + real features, 5 arms, selection
bash vnext/real3.sh                                               # session 1 only + session 2 held out
bash vnext/runs3.sh                                               # two seeds + the real-take report
# candidates on the held-out session, comparable to the P0 baseline
.venv-tf/bin/python $H/export_tflite.py vnext/runs/vn_ws4k_rn_rp_h/kivi.onnx vnext/runs/vn_ws4k_rn_rp_h/oye_kivi_uncalibrated.tflite \
  --check-features vnext/feat/pos_test.npy
.venv/bin/python -I $H/kivi_score.py --session datasets/real/20261108T094310Z-spk01-tv-c3a \
  --model oye_kivi=v4/final/oye_kivi_uncalibrated.tflite --model h=vnext/runs/vn_ws4k_rn_rp_h/oye_kivi_uncalibrated.tflite \
  --thresholds 0.9437,0.97125 --also-pc --jobs 4 --json vnext/score_s2_holdout.json
```

## Next

1. **More real positives, more days** — the 3 m TV-off cell and the 80 % gate are the target; a
   `--split test` session on a later day is what turns this into a gate run.
2. **spk02 on c3-b** (gate needs >= 2 speakers).
3. Re-measure room false wakes for the best candidate on the room **test** parts (nothing of it is in
   training) before any threshold or deployment proposal.
4. Re-score session 3's 36 real confuser takes against the candidates, per word.
