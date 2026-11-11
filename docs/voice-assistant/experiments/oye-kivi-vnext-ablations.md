# vnext ablations: real voice is what moves the needle (2026-11-08)

Source: [recovery plan](oye-kivi-recovery.md) P1 (source attribution) and P2 (candidates), run ahead of
schedule because the real recordings arrived the same morning, plus the first room evidence for P3.
Follows the [P0 real-voice baseline](oye-kivi-p0-baseline.md). Everything ran offline on the workstation
(`/work/workspaces/kivi-wakeword/vnext/`). The result of this note **is deployed**: the
chosen head is `h_c120_w64`, released as `oye_kivi_next` `v5-oye-kivi-2026-11-08`, trialled on c3-a from
2026-11-08 and, by the owner's decision of 2026-11-11, the wake word of the household pipeline "Kiwi" for
every satellite ([Deployment](#deployment-oye_kivi_next-v5-oye-kivi-2026-11-08-one-tv)).

**Bottom line:** consented real positive takes move recall on *unseen* real takes from 25 % to 75 % on the
satellite at a *lower* measured room false-wake rate (0.12/h against 0.96/h), and the real confuser takes
drop from 10/54 to 1/54 (0/54 for `rn_rp_h`, the head that got closest on synthetic data). No front-end
filter helps (the model was the problem), and more augmentation of the
same 40 cues does nothing — the next improvement has to come from new recordings: a `test`-split session on
a later day, spk02, and ~15 h per device of fresh room exposure. The head that shows this (`h_c120_w64`)
is released as `oye_kivi_next` `v5-oye-kivi-2026-11-08`, the household wake word since 2026-11-11.

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

| Head | raw thr | TV off | TV on | ALL (40 takes) | Confuser takes, satellite (54) | Room test part (8.31 h) |
| --- | --- | --- | --- | --- | --- | --- |
| v4 (deployed) | 0.9437 | 7/20 = 35 % | 3/20 = 15 % | 10/40 = 25 % | 10/54 = 19 % | 8 = 0.96/h [0.42, 1.90] |
| `rn_rp_h` | 0.97125 | 14/20 = 70 % | 14/20 = 70 % | 28/40 = 70 % | 0/54 | 3 = 0.36/h [0.07, 1.06] |
| `rn_rp_h3` | 0.96408 | 15/20 = 75 % | 13/20 = 65 % | 28/40 = 70 % | 1/54 | 3 = 0.36/h [0.07, 1.06] |
| **`h_c120_w64`** | **0.9754** | **15/20 = 75 %** | **15/20 = 75 %** | **30/40 = 75 %** | **1/54** | **1 = 0.12/h [0.003, 0.67]** |

Per cell for the chosen head (10 takes each): 1.5 m TV off 10/10, 1.5 m TV on 7/10, **3 m TV off 5/10**,
3 m TV on 8/10. On the session's other microphones it is 32/40 (pc-mic) and 31/40 (webcam-mic).

The confuser column pools both near-word sessions on the satellite (session 1: 18 cues, session 3: 36), as
Result 5 does. In the dedicated confuser session alone (session 3, 36 takes) the new heads are **0/36**
against v4's 8/36; the single acceptance of `h_c120_w64` in the pooled 54 is **"oye kili"** once on session
1's near block, which was in its own training data — a fitting gap rather than a generalisation failure.
The same word also reaches the trigger once on each of the two PC reference microphones of session 3, so
"oye kili" stays in the negative pool at confuser weight. The held-out session's own near block is 0/9 on
every channel and every model.

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
  one, wake word `oye_kivi_next`) ran on **c3-a only** from 2026-11-08 11:56 UTC.
- **Household wake word since 2026-11-11 (owner decision).** The owner asked for the latest model on all
  satellites through the global pipeline and no per-satellite overrides: "Kiwi" (preferred) uses
  `oye_kivi_next`, both satellites' pipeline selects (both the first and the second select of each) are
  `preferred`, and the trial pipeline "oye Kivi" no longer exists. The gate stays **incomplete**; the owner's
  reason is in the release state's `promotion.owner_override`. The change was found live on 2026-11-11 and was
  recorded afterwards by `promote --previous-wake-word oye_kivi` (no Home Assistant write by the driver). With
  nothing pointing at a non-preferred pipeline, a Home Assistant restart can no longer restore a select to a
  trial pipeline (it can only fall back to `preferred`, which is the intended state).
- **Rollback**: `wake_release.py demote --version v5-oye-kivi-2026-11-08` (no approval needed): "Kiwi" goes
  back to v4's `oye_kivi`, the previous household wake word. The evidence in "Next" is still missing.
- **Model card** in kivi-models records the numbers, `notes.md` the caveats; licence notes were re-issued
  for this version because it is the first model here trained on a household member's **positives**.
- The three dataset sessions were adopted into the release state
  (`extract --adopt-session <session>`) instead of re-extracting already-aligned audio, which would have
  made a new revision and superseded the marks the training data refers to.

## Result 8: the first clean held-out session (2026-11-11, odd date, PC and webcam channels only)

Rule change, **owner decision 2026-11-11**: the model must work on any device, so on a test-split
(odd Europe/Madrid date) session the desk mic, the webcam and later phone or other devices are **evaluation**
channels too, reported per device class (`tv`, `pc-desk`, `pc-webcam`, `phone`) and **never pooled with the TV
gate**. The TV mic stays the gate for the deployed TV path. Day split unchanged: odd dates test, even dates train.
Old manifest rows are not rewritten: `eval_channel` is derived from (source, split) everywhere
(`kivi_real_common.is_eval_channel`).

Session `20261111T171918Z-spk01-tv-c3a` (spk01, c3-a, split test, 9 blocks, 101 cues, 9.2 min, cues every
3.5 +-0.5 s, later day than every training and selection input of v4 and v5). **The TV channel is missing**:
c3-a was on the "Kiwi Test" pipeline to keep the live wake word out of the session, and HA wrote no wake audio
for that pipeline (empty run directories: the pipeline pointed at the unavailable `conversation.chatgpt` (the OpenAI
integration returned 403) and the non-served wake word id `alexa_v0.1`, so every run ended run-start, error
`intent-not-supported`, run-end; the old run check counted any run, errored or another satellite's, as a new run);
HA also restarted twice around the session (a homelab push touching the HA config: Argo plus Reloader). Extracted from the PC
sources only (`extract -- --no-tv`, `tv_channel: missing` in the session record and the release state; no
`tv-c3a` rows, so the satellite gate has no new input). The webcam aligned (28/29 windows, drift +132 ppm); all
101 cues have full audio on both sources. v5 was not trained on this session: it is held out.

Recall on "oye kivi", each model at its own raw threshold (v4 0.9437, v5 0.9754), Wilson 95 % intervals,
`cue_hits` (every cue is full audio, so no cue counts as lost):

| Cell (10 cues; 3 m TV off = 36 pooled over 3 blocks) | pc-mic v4 | pc-mic v5 | webcam v4 | webcam v5 |
| --- | --- | --- | --- | --- |
| 3 m TV off | 24/36 = 67 % [50, 80] | 33/36 = 92 % [78, 97] | 22/36 = 61 % [45, 75] | 32/36 = 89 % [75, 96] |
| 3 m TV on | 1/10 [2, 40] | 6/10 [31, 83] | 1/10 [2, 40] | 5/10 [24, 76] |
| 1.5 m TV on | 4/10 [17, 69] | 6/10 [31, 83] | 1/10 [2, 40] | 8/10 [49, 94] |
| 1.5 m TV off | 8/10 [49, 94] | 10/10 [72, 100] | 9/10 [60, 98] | 8/10 [49, 94] |
| all 66 | 37/66 = 56 % [44, 67] | 55/66 = 83 % [73, 90] | 33/66 = 50 % [38, 62] | 53/66 = 80 % [69, 88] |

Near-phrase false accepts (35 near cues, 0 or 1 of 3 to 5 per word): v4 pc-mic 7/35 (oye kili 3/5, oye kiri
4/5), v4 webcam 13/35 (kili 4/5, kiri 5/5, Fifi 2/5, tivi 2/5); **v5 pc-mic 0/35, v5 webcam 1/35** (oye kili 1/5).
"kivi" alone, oye mira, oye tu, hoy vi, o sea: 0 for both models. v4's near-word problem on real speech reproduces
on a later day; v5 fixes it. False accepts in the time outside the cue windows: 0 for both models, but that time is
only 53 s per source (the 3.5 s cue rhythm leaves almost no gap; 1 event per model and source at most with narrower
windows), so it says nothing about a rate per hour.

Operating-point view (pc-mic / webcam, all 66 "oye kivi" cues; near-phrase false accepts of 35 in brackets):

| Model, raw threshold | pc-mic | webcam |
| --- | --- | --- |
| v4 0.9 | 44/66 = 67 % (10/35) | 43/66 = 65 % (17/35) |
| v4 0.9437 | 37/66 = 56 % (7/35) | 33/66 = 50 % (13/35) |
| v5 0.95 | 58/66 = 88 % (0/35) | 60/66 = 91 % (2/35) |
| v5 0.9754 | 55/66 = 83 % (0/35) | 53/66 = 80 % (1/35) |
| v5 0.98 | 55/66 = 83 % (0/35) | 52/66 = 79 % (1/35) |

What it means: v5 beats v4 on a day neither saw, at every condition except webcam at 1.5 m with the TV off (8/10
against 9/10, within noise); TV-on cells stay the weak ones (3 m 5-6/10 for v5). Caveats: one speaker, one session,
PC mics (not what the satellite hears; the desk mic and the TV mic differ), 10 cues per TV-on cell, and no
`tv-c3a` figure at all: the TV-path number and the gate still need a re-recorded session with the satellite
channel. The release gate stays INCOMPLETE (satellite rows only; the PC and webcam figures are listed apart as
"other devices, informational"). A cue-timing check found no cue without a speech burst on the desk mic; two cues
are worth the owner's confirmation as possible flubs or cut words: block 4 (1.5 m TV on) cue 10 (burst 0.36 s, shorter than
the usual 0.7 to 1.0 s) and cue 6 (burst starts 1.7 s after the cue).

### Session procedure after the pipeline fix (2026-11-11)

With the owner's approval "Kiwi Test" was edited: `conversation_engine` `conversation.fast` (was
`conversation.chatgpt`) and `wake_word_id` `alexa` (was `alexa_v0.1`); every other field is unchanged. Put c3-a on it
with `wake_release.py rollback --version V --tv c3-a --to "Kiwi Test"`: the tool refuses an unavailable engine or an
unserved wake word id, then checks the newest run of that satellite (`wake_word-start`, no `error`, 30 s) and prints
`FAILED: <code>` otherwise. Don't say "alexa" during sessions. Rules: (a) the run check must pass and `00_wake` must
grow (about 32 kB/s) before the first take; (b) no homelab push touching `apps/home-assistant` config during a
session. Verified on c3-a the same day: check OK; the newest `00_wake` file under "Kiwi Test" went from 113644 to
433964 bytes in 10 s (32.0 kB/s); back on `preferred` (oye_kivi_next) with a passing check.

Naming note: the owner briefly renamed the household pipeline to "Kivi" on 2026-11-11 and then back. Runs of that
window sit under `/voice-recordings/<device>/Kivi/` (one run directory per TV, created about 17:41 UTC). The tooling
finds the household pipeline by the preferred flag or id, never by its name.

## What this does not prove

- **One speaker, one day.** Sessions 1 and 2 are 14 minutes apart, so the holdout measures continuity
  within a morning, not across-day or across-speaker robustness. spk02 is absent from the real data
  entirely (the gate needs >= 2 speakers).
- **20 takes per held-out condition** — intervals are +-19 points wide.
- **The confuser result (0-1/54) and the room replay are different evidence.** 54 prompted takes say nothing
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

0. **Re-record the satellite channel** (Result 8 has PC and webcam only): a `--split test` session on c3-a on a
   pipeline that records wake audio, so the TV-path gate gets its first held-out rows.
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
   1/54 satellite for the shipped head against 10/54 deployed (0/54 for `rn_rp_h`). Still 2 to 4 takes per
   word per session: repeat the near-word block on a later day, and give "oye kili" more takes in the
   negative pool, since it is the one word that still reaches the trigger.
5. **Watch the household deployment** (both satellites since 2026-11-11, so there is no v4 satellite to compare
   with any more): a daily glance at the wake events of both. If the new model mispredicts in a way the offline
   numbers did not show, `demote` — that is information, not a failure.
6. If a *confirmed* false wake ever appears on c3-a, mine it into the real negative pool at confuser weight
   (`--extra-neg <npy>:64:6`) and re-run the head: that is the loop that actually reduces false wakes per
   hour, and it needs no new recordings.

## Next: v6 plan

Next: v6 plan -> kivi-models `docs/plan/2026-11-v6-plan.md` (private repository; owner decisions of
2026-11-11 and the v6 training plan).
