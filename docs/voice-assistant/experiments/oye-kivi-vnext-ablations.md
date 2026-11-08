# vnext ablations: real voice is what moves the needle (2026-11-08)

Source: [recovery plan](oye-kivi-recovery.md) P1 (source attribution) and P2 (candidates), run ahead of
schedule because the real recordings arrived the same morning, plus the first room evidence for P3.
Follows the [P0 real-voice baseline](oye-kivi-p0-baseline.md). Everything ran offline on the workstation
(`/work/workspaces/kivi-wakeword/vnext/`). The result of this note **is now deployed on one TV**: the
chosen head is `h_c120_w64`, released as `oye_kivi_next` `v5-oye-kivi-2026-11-08` and trialling on c3-a
([Deployment](#deployment-oye_kivi_next-v5-oye-kivi-2026-11-08-one-tv)); the rest of the household is
untouched.

**Bottom line:** consented real positive takes move recall on *unseen* real takes from 25 % to 75 % on the
satellite at a *lower* measured room false-wake rate (0.12/h against 0.96/h), and the real confusers go
from 10/54 to 0/54. No front-end filter helps (the model was the problem), and more augmentation of the
same 40 cues does nothing — the next improvement has to come from new recordings: a `test`-split session on
a later day, spk02, and ~15 h per device of fresh room exposure. The head that shows this (`h_c120_w64`)
is released as `oye_kivi_next` `v5-oye-kivi-2026-11-08` and trialling on one TV.

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

## Result 3: the mic chain is not the bottleneck (P1 source attribution)

The same session-2 takes, scored on the satellite's own raw wake stream, and on filtered copies of that
stream (`ha_filter_mirror.py`, the trees extended with today's sessions: 3 files written, 50 skipped):

| Path (identical takes) | v4 (deployed) at 0.9437 | `rn_rp_h` at 0.97125 |
| --- | --- | --- |
| satellite, as recorded | 10/40 = 25 % | 28/40 = 70 % |
| + RNNoise | 12/40 = 30 % | 26/40 = 65 % |
| + second WebRTC NS pass | 7/40 = 18 % | 28/40 = 70 % |
| desk mic (clean reference, no satellite) | 11/40 = 28 % | 27/40 = 68 % |

Every difference is inside the noise of 40 takes (+-2 to +-3 cues, intervals +-15 points wide), and the
clean desk mic is no better than the satellite. **No front-end profile change is indicated**: the plan's
"no signal-processing change without a matched-model replay result" holds — the matched replay shows the
model explains the gap, the chain does not. Not re-run here: the `speex` mirror (its Nov 7 result stands);
`pyspeex_noise` is not installed in either workstation venv anymore.

## Result 4: recipe knobs on the same 40 cues do not help

Both knobs were tried with session 2 still held out (4 arms, `vnext/runs4.sh`):

| Arm | Real windows per take | Real share of positives/step | Holdout accepted | Train accepted |
| --- | --- | --- | --- | --- |
| `vn_h_c40_w32` (= `rn_rp_h`, re-run) | 4,760 | 64/256 | 79/113 | 114/119 |
| `vn_h_c40_w64` | 4,760 | 128/256 | 78/113 | 116/119 |
| `vn_h_c120_w32` | 14,280 | 64/256 | 78/113 | 105/119 |
| `vn_h_c120_w64` | 14,280 | 128/256 | 80/113 | 117/119 |

The re-run reproduced the original arm exactly (same threshold 0.97125, same 79/113), so the pipeline is
deterministic — and tripling the augmentation variety or doubling the real sampling weight moves the
holdout by one or two takes out of 113. **The limit is the diversity of the 40 cues, not the recipe.**
More recordings, not more tuning.

## Result 5: the confusers are fixed on real speech

All 54 consented real near-word takes (sessions 1 and 3, satellite, each model at its own selection
threshold):

| Near-word | v4 (deployed) | control (v4's real negatives) | `rn_rp_h` |
| --- | --- | --- | --- |
| oye Fifi | 4/6 | 0/6 | 0/6 |
| oye kiri | 3/6 | 2/6 | 0/6 |
| oye kili | 2/6 | 1/6 | 0/6 |
| oye tivi | 1/6 | 1/6 | 0/6 |
| **kivi (alone)** | 0/6 | 0/6 | **0/6** |
| oye mira / oye tú / o sea / hoy vi | 0/6 each | 0/6 each | 0/6 each |
| **ALL** | **10/54 = 18.5 %** | 4/54 = 7.4 % | **0/54** |

The core phrase goal still holds ("kivi" alone never triggers), and the candidate's confuser acceptance
is zero in 54 trials — with 0 events the 95 % upper bound is still ~5.6 %, so this is not a room
false-wake rate: that needs the room test replay (Next item 3), not 54 prompted takes.

## Result 6: the same candidate on the untouched room test part

`v3_room.py score --part test` (8.31 h: 4.11 h c3-a, 4.20 h c3-b; never used for selection, each model at
its own threshold, 2 s refractory, exact Poisson intervals):

| Model | c3-a | c3-b | pooled | per hour [95 % CI] |
| --- | --- | --- | --- | --- |
| v4 (deployed) at 0.9437 | 2 | 6 | 8 | **0.96/h [0.42, 1.90]** |
| control at 0.95435 | 1 | 2 | 3 | 0.36/h [0.07, 1.06] |
| `rn_rp_h` at 0.97125 | 1 | 2 | 3 | **0.36/h [0.07, 1.06]** |

The v4 row reproduces the published room figure exactly, which validates the replay chain. The candidate
is 3x under it in point estimate (**and 10 events at 0.9437, one event more than v4**: its score
distribution sits higher, so it is not a model you may leave at the old threshold — the threshold comes
with the model).

**This does not meet the gate yet.** 0.36/h [0.07, 1.06] cannot exclude the 0.25/h budget, and per device
c3-b is 0.48/h [0.06, 1.72]. Roughly 15 h per device with no events would be needed for the upper bound to
fall under 0.25/h, so the room exposure has to be collected over the coming days, not squeezed out of the
existing pieces.

## Result 7: the three heads side by side, and the one that shipped

Session 2 was held out of training for all three heads, so the satellite channel of that session is the
honest generalisation number. Each head is scored at its own selection threshold (`v4_select.py`,
<= 2 false accepts in 8.377 h of room selection part):

| Head | raw thr | TV off | TV on | ALL (40 takes) | Confuser takes (36) | Room test part (8.31 h) |
| --- | --- | --- | --- | --- | --- | --- |
| v4 (deployed) | 0.9437 | 7/20 = 35 % | 3/20 = 15 % | 10/40 = 25 % | 8/36 = 22 % | 8 = 0.96/h [0.42, 1.90] |
| `rn_rp_h` | 0.97125 | 14/20 = 70 % | 14/20 = 70 % | 28/40 = 70 % | 0/36 | 3 = 0.36/h [0.07, 1.06] |
| `rn_rp_h3` | 0.96408 | 15/20 = 75 % | 13/20 = 65 % | 28/40 = 70 % | 0/36 | 3 = 0.36/h [0.07, 1.06] |
| **`h_c120_w64`** | **0.9754** | **15/20 = 75 %** | **15/20 = 75 %** | **30/40 = 75 %** | **0/36** | **1 = 0.12/h [0.003, 0.67]** |

Per cell for the chosen head (10 takes each): 1.5 m TV off 10/10, 1.5 m TV on 7/10, **3 m TV off 5/10**,
3 m TV on 8/10. On the session's other microphones it is 32/40 (pc-mic) and 31/40 (webcam-mic).

**Why this one.** `rn_rp_h3` is marginally ahead on the window-level all-microphone measure (82/113
against 80/113) while `h_c120_w64` is ahead on the deployment channel (30/40 against 28/40): two takes out
of 40, which is noise. The tie-break was behaviour at the operating point that would actually be served —
one room event in 8.31 h against three. The heads are inside the margin that Result 4 measured for these
recipe knobs (one or two takes out of 113), so this is a choice among near-equals, not evidence that the
knobs matter; and the room part had already been inspected for v4 and `rn_rp_h`, so using it as the
tie-break costs a little of its independence.

**The threshold is not transferable.** At v4's raw 0.9437 this head accepts 8 events in the same room test
audio (and `rn_rp_h` accepts 10): their score distributions sit above v4's. The artefact is the pair
(model, threshold), which is why `calibrate` bakes 0.9754 into the final bias.

## Deployment: `oye_kivi_next` v5-oye-kivi-2026-11-08, one TV

`wake_release.py` ran the full flow for `h_c120_w64` as model id **`oye_kivi_next`**, version
`v5-oye-kivi-2026-11-08`, raw threshold **0.9754** = the server's default 0.5, served
`oye_kivi_next.tflite` sha256 `3101e61eb8d55faa37623c8fe7eda30cc1b9f3982c37bf393c85bce49fac93ae`, image
`git.grigri.cloud/agil/kivi-models:v5-oye-kivi-2026-11-08@sha256:3a198f7d4958` (anonymous pull refused, CI
run checked). The release state keeps the score and gate JSON:
`/work/workspaces/kivi-wakeword/releases/v5-oye-kivi-2026-11-08/`.

- **Gate: INCOMPLETE**, exactly as the data predicts — the room checks pass at the point estimate
  (0.12/h pooled, 0.0/h on c3-a, 0.238/h on c3-b) and the recall checks have no rows because all three
  real sessions are `train` split and there is only one speaker. Released with the owner's reason
  (`--accept-gate-fail`), not as a pass.
- **New id, not an overwrite.** v4's `oye_kivi` file is still served: the household pipeline "Kiwi" keeps
  `oye_kivi`, and the recovery plan's rule for a gate that does not pass — trial on one device — is only
  possible because the candidate has its own id. A trial pipeline "oye Kivi" (a copy of the preferred
  one, wake word `oye_kivi_next`) is live on **c3-a only** since 2026-11-08 11:56 UTC; c3-b is unchanged.
- **Rollback**: `wake_release.py rollback --version v5-oye-kivi-2026-11-08 --tv c3-a` (no approval needed).
  Promotion to both TVs (`promote`) is deliberately not done; it needs the evidence in "Next".
- **Model card** in kivi-models records the numbers, `notes.md` the caveats; licence notes were re-issued
  for this version because it is the first model here trained on a household member's **positives**.
- The three dataset sessions were adopted into the release state
  (`extract --adopt-session <session>`) instead of re-extracting already-aligned audio, which would have
  made a new revision and superseded the marks the training data refers to.

## What this does not prove

- **One speaker, one day.** Sessions 1 and 2 are 14 minutes apart, so the holdout measures continuity
  within a morning, not across-day or across-speaker robustness. spk02 is absent from the real data
  entirely (the gate needs >= 2 speakers).
- **20 takes per held-out condition** — intervals are +-19 points wide.
- **The 0/54 confuser result and the room replay are different evidence.** 54 prompted takes say nothing
  about rate per hour; 8.3 h of room audio cannot exclude 0.25/h (Result 6), and that part was already
  inspected for v4, so it is not a fresh unbiased test either.
- The `--holdout-session` windows still let session 2's 6 background segments per source into the negative
  pool (room noise, no speech); its takes and near-words do not (its near block is marked excluded).
- Selection thresholds come from the room *selection* parts at 2 events / 8.38 h. Session 2 has now
  informed a pick (Result 7), so it is a **holdout that has been looked at**, not a fresh test set: it
  estimates generalisation to an unseen session, but the *choice among* close heads used it, so the 75 %
  is mildly optimistic as a promise about tomorrow. The `test`-split session on a later day is the number
  that settles it.

## Reproduce

```sh
cd /work/workspaces/kivi-wakeword
H=$HOME/homelab/scripts/voice-assistant/wakeword
bash vnext/feats.sh && bash vnext/real.sh && bash vnext/runs.sh    # synthetic + real features, 5 arms, selection
bash vnext/real3.sh && bash vnext/runs3.sh                         # session 2 held out, two seeds + report
bash vnext/runs4.sh                                               # copies x weight ablation on the holdout

# the candidate heads, exported for the server's own runtime
for n in vn_ws4k_v4real vn_ws4k_rn_rp_h vn_ws4k_rn_rp_h3; do
  .venv-tf/bin/python $H/export_tflite.py vnext/runs/$n/kivi.onnx vnext/runs/$n/oye_kivi_uncalibrated.tflite \
    --check-features vnext/feat/pos_test.npy
done
M="--model oye_kivi=v4/final/oye_kivi_uncalibrated.tflite --model ctrl=vnext/runs/vn_ws4k_v4real/oye_kivi_uncalibrated.tflite --model h=vnext/runs/vn_ws4k_rn_rp_h/oye_kivi_uncalibrated.tflite"

# held-out positives (comparable to the P0 baseline) and the 54 real confuser takes
for s in 20261108T094310Z 20261108T092940Z 20261108T095130Z; do
  .venv/bin/python -I $H/kivi_score.py --session datasets/real/$s-spk01-tv-c3a $M \
    --thresholds 0.9437,0.95435,0.97125 --also-pc --jobs 4 --json vnext/score_$s_cand.json
done

# P1: extend the HA-side filter mirrors with the new sessions, then score the same session on each mirror
for f in rnnoise webrtc-ns; do
  systemd-run --user --scope -p MemoryMax=6G -p CPUQuota=400% nice -n 10 \
    .venv/bin/python -I $H/ha_filter_mirror.py build --src datasets/real --dst datasets/derived/$f --filter $f \
    --tool v2/fe_build/release/examples/aec_offline
  .venv/bin/python -I $H/kivi_score.py --session datasets/derived/$f/20261108T094310Z-spk01-tv-c3a $M \
    --thresholds 0.9437,0.95435 --jobs 3 --json vnext/p1_mirror_$f.json
done

# the untouched room test part (Result 6)
SO=emb_s_melrow.onnx,src/openWakeWord/openwakeword/resources/models/embedding_model.onnx
.venv/bin/python -I $H/v3_room.py embed --split v3/room_split.json --parts test --stream-onnx $SO --out-dir vnext/roomtest
.venv/bin/python -I $H/v3_room.py score --split v3/room_split.json --part test $M \
  --thresholds 0.9437,0.95435,0.97125 --jobs 4 --json vnext/room_test_cand.json

# Result 7: the other two heads on the same two measures (exports h_c120_w64; room replay ~20 min at 4 jobs)
bash vnext/cand2.sh        # v3_room.py score --part test for v4 / h / h3 / c120 at their own thresholds
bash vnext/cand3.sh        # kivi_score.py of the three real sessions for the same four models

# the release flow for the chosen head (v5-oye-kivi-2026-11-08, model id oye_kivi_next)
cd $H
R="python3 wake_release.py"
$R init   --version v5-oye-kivi-2026-11-08 --model-id oye_kivi_next --phrase "oye Kivi" \
  --candidate /work/workspaces/kivi-wakeword/vnext/runs/vn_h_c120_w64
$R pick-threshold --version v5-oye-kivi-2026-11-08 --max-fa 2          # raw 0.9754
$R calibrate --version v5-oye-kivi-2026-11-08 --check-features /work/workspaces/kivi-wakeword/vnext/feat/pos_test.npy
$R extract  --version v5-oye-kivi-2026-11-08 --adopt-session 20261108T092940Z-spk01-tv-c3a \
  --adopt-session 20261108T094310Z-spk01-tv-c3a --adopt-session 20261108T095130Z-spk01-tv-c3a
$R score    --version v5-oye-kivi-2026-11-08 --jobs 4
$R gate     --version v5-oye-kivi-2026-11-08                            # INCOMPLETE
$R package  --version v5-oye-kivi-2026-11-08 --license-notes <LICENSE-NOTES.md for this version>
$R release  --version v5-oye-kivi-2026-11-08 --yes-owner-approved --accept-gate-fail "<owner reason>"
$R deploy   --version v5-oye-kivi-2026-11-08
$R enable   --version v5-oye-kivi-2026-11-08 --tv c3-a --yes-owner-approved
```

## Next

1. **More real positives, more days** — recipe tuning is exhausted (Result 4), the 3 m TV-off cell (5/10)
   and the 80 % gate need data: a `--split test` session on a later day is what turns this into a gate run.
   Record it with `--split test` at recording time and adopt the older sessions with
   `extract --adopt-session` rather than re-extracting them.
2. **spk02 on c3-b** (gate needs >= 2 speakers; today's real data is one speaker).
3. **Fresh room exposure, as a new part.** The chosen head is at 0.12/h [0.003, 0.67] on the 8.31 h test
   part, but that part has now been inspected for v4, `rn_rp_h`, `rn_rp_h3` and `h_c120_w64` and was even
   the tie-break in Result 7, so it cannot also carry the 0.25/h proof. Extend `v3/room_split.json` with
   the days recorded *after* the trial started, keep the old test part as history, and require ~15 h per
   device before claiming the budget. Count only confirmed false wakes from the live event log.
4. ~~Re-score session 3's real confuser takes against the candidates, per word~~ — done (Results 5 and 7):
   0/54 for the candidate against 10/54 deployed. Still 4 takes per word: repeat the near-word block on a
   later day if the trial ever wakes on a real Spanish word.
5. **Watch the trial** (c3-a): daily glance at the satellite's wake events, and compare against c3-b, which
   still runs v4 in the same room. If the new model mispredicts in a way the offline numbers did not show,
   roll back — that is information, not a failure.
6. If a *confirmed* false wake ever appears on c3-a, mine it into the real negative pool at confuser weight
   (`--extra-neg <npy>:64:6`) and re-run the head: that is the loop that actually reduces false wakes per
   hour, and it needs no new recordings.
