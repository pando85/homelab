# P0 real-voice baseline: deployed v4 "oye Kivi" (2026-11-08)

Source: [recovery plan](oye-kivi-recovery.md) P0. This is the first measured real recall for
the household wake phrase. Trained and measured offline on the workstation; model files, audio
and features stay in the private work directory (`/work/workspaces/kivi-wakeword/vnext/`).

## What was captured

spk01 (consented, the owner) on c3-a, `kivi_session.py --phrase "oye kivi"`, three `train`-split
sessions on 2026-11-08:

| Session | Positives | Near-words | Notes |
| --- | --- | --- | --- |
| `20261108T092940Z-spk01-tv-c3a` | 40 (10 per condition) | 18 (2 x 9, real speech) | default 5 s protocol, ~9 min |
| `20261108T094310Z-spk01-tv-c3a` | 40 (10 per condition) | 9 cues not spoken | fast 3.5 s protocol; the near-word block is digital silence (RMS 0.0015-0.002 vs 0.039+ for speech) and is marked `excluded` with `kivi_dataset_marks.py exclude --block near-words`, so it never enters training or scoring |
| `20261108T095130Z-spk01-tv-c3a` | — | 36 (4 x 9, real speech, RMS 0.022-0.15 verified) | dedicated `--only near-words --repeat 2` session, ~3 min; also 20 background segments per source. Desk mic lost audio mid-session; `kivi_extract.py` aligned with a piecewise offset curve (residual ~47 ms, trusted, PSR 17-38) |

Sessions 1-2: conditions 1.5 m / 3 m x TV off / TV on, normal volume. Session 3: near-words only,
1.5 m TV off (the default near block). Extracted with `kivi_extract.py`; alignment trusted on all
files (sessions 1-2 residuals <= 8 ms; session 3 ~47 ms through its piecewise curve). Coverage:
every cue in the positive and near blocks has full audio, 0 partial, 0 lost, 0 no-audio.

## Result: the model does not work on real speech

Scored with the deployed v4 head (`oye_kivi_uncalibrated.tflite`, sha256 `4304cb85...80a6`,
calibration server 0.5 = raw **0.9437`) through `pyopen-wakeword` (the server's own runtime),
satellite source `tv-c3a`, 2 s refractory. Recall counts every cue as a trial.

| Condition | Session 1 | Session 2 | Pooled | Gate | Verdict |
| --- | --- | --- | --- | --- | --- |
| TV off (1.5 m + 3 m) | 5/20 | 7/20 | **12/40 = 30 %** [18, 45] | >= 80 % | **fail** |
| TV on (1.5 m + 3 m) | 3/20 | 3/20 | **6/40 = 15 %** [7, 29] | >= 60 % | **fail** |
| ALL positives | 8/40 | 10/40 | **18/80 = 22.5 %** [15, 33] | — | — |

(Wilson 95 % intervals.) Per cell (session 1 / session 2, satellite): 1.5 m TV off 5+4/20,
1.5 m TV on 1+1/20, 3 m TV off 0+3/20, 3 m TV on 2+2/20.

A threshold change cannot rescue this: at raw 0.9 (much more permissive than any room-false-wake
budget allows) pooled recall is only 27/80 = 34 %, and the room test already showed 0.96 false
wakes/h at 0.9437.

### Reference channels (same takes, cleaner audio)

| Source | Recall at 0.9437 |
| --- | --- |
| tv-c3a (satellite, what the server hears) | 18/80 = 22.5 % |
| pc-mic (desk, near field) | 10/40 = 25 % (session 1) |
| webcam-mic | 13/40 = 32.5 % (session 1) |

**The failure is not the satellite front end.** A clean near-field desk microphone scores the
same as the TV satellite (~25 %). The model's scores on real speech are simply low.

### Near-words (sessions 1 + 3, the ones with real confuser speech)

Pooled at 0.9437 on the satellite (session 1: 2 takes per word; session 3: 4 per word):

| Near-word | False accepts | vnext synthetic weight |
| --- | --- | --- |
| oye Fifi | 4/6 | 3 |
| oye kiri | 3/6 | 1.5 (kept low: Piper collapses it onto the target) |
| oye kili | 2/6 | 3 |
| oye tivi | 1/6 | 3 |
| **kivi (alone)** | **0/6** | 8 |
| oye mira / oye tú / o sea / hoy vi | 0/6 each | 2 / 2 / 2 / 3 |
| **ALL** | **10/54 = 18.5 %** | — |

Non-cue time in these sessions is a few hundredths of an hour — not a room-exposure test; the
room test sets remain the FA evidence (v4: 0.96/h, c3-a 0.49/h, c3-b 1.43/h).

The core phrase goal holds: **"kivi" alone 0/6 at every threshold**, and the ordinary-speech
near-phrases are clean. The confusers are specific: **"oye Fifi", "oye kiri", "oye kili"**.

On the desk mic the confusers invert harder: "oye kiri" 4/4 and "oye kili" 3/4 **above raw 0.95**,
while most true positives score below it — a confuser can outscore the target. This is the
real-speech confirmation of the synthetic DTW finding ("oye kiri" collapses onto the target in
11-14 % of Piper renderings, so the v4/vnext recipes keep its synthetic weight low).

**Recipe consequence:** the synthetic weight for "oye kiri" must stay low (rendering collapse), but
the real fix is its **6 consented real takes** (plus "oye Fifi" 6, "oye kili" 6), which
`v4_realneg.py` turns into real negative windows. Session 3 exists precisely for this.

## What the real data now contributes to training

The tools assumed the old phrase, so they could not use these sessions; both gaps are fixed (with
tests): `v3_real.py takes` labels a cue against the session's `--phrase` (`take_slot`) instead of a
hardcoded `"kivi"`, and `negative_rows_for_phrase` no longer skips a session's own `near:*` rows —
they are the confusers to push down.

| Derived set | Content (satellite source; every source is built) |
| --- | --- |
| real positive takes | 232 = 80 cues x 3 sources - 8 whose re-cut did not match the stored clip (`v3_real.py takes --phrase "oye kivi" --only-positive`) |
| real negative rows for "oye kivi" | per source: 31 old-"kivi" takes + 66 near-word takes (12 Nov 7 + 18 session 1 + 36 session 3) + 83 background segments = 180 rows |

`vnext/runs.sh` trains five arms that differ only in the real inputs, so each comparison isolates one
variable: v4's real negatives (as before the confuser session) -> refreshed real negatives -> plus
the real positive takes, with a fresh-head and a longer-step control. Selection reports the synthetic
and room sets (`v4/select.sh`) and the real takes (`v3_select.py --real`) separately.

## Live pipeline context

A `kivi_extract.py` pre-flight on 2026-11-08 was read as showing the preferred Assist pipeline on
`hey_jarvis`. **That was not reproduced**: read from Home Assistant on 2026-11-11 (`wake_release.py status`),
no pipeline uses `hey_jarvis` ("Kiwi", the preferred one, uses `oye_kivi_next` and "Kiwi Test" uses
`alexa_v0.1`), and the 2026-11-08 trial records show "Kiwi" on `oye_kivi`. Treat the pre-flight line as an
unverified reading, not as a state of the household pipeline. The v4 files remain served in the
openwakeword pod (`v4-oye-kivi-2026-11-07`), loaded and available.

## Conclusions (feeds P1/P2)

1. **Synthetic-only training overestimates real recall by ~3x.** v4 claimed 72.8 % clean on
   held-out *synthetic* voices at this operating point; real speech from the one speaker whose
   takes are in its training data (as negatives) is 22.5 %. The synthetic-to-real domain gap is
   the primary defect.
2. **Candidates must mix consented real positives with the synthetic set.** The vnext ablations
   train on spk01's 80 real takes per source (sessions 1 + 2, `train` split only) as well as the
   refreshed real negatives; see the table above.
3. **"oye kiri" / "oye kili" are first-class confusers on real speech**, with channel-dependent
   behaviour; they stay as negatives with real-take negatives added.
4. **P0 exit is partially met:** >= 10 takes per condition reached (10 + 10 per condition pooled,
   one session each), two sessions, but only one speaker. The test-split session (spk01, later
   day) and spk02 coverage are still required before any gate run.
5. Data hygiene: recordings live only in `datasets/real/` (mode 700) and the workstation vnext
   directory; aggregate numbers only in this file and the registry (data-register entries 13/15
   cover the derived feature windows; a new entry is needed for the vnext derived data).

## Reproduce

```sh
cd scripts/voice-assistant/wakeword
python3 kivi_extract.py --session-dir ~/kivi-sessions/<id>
/work/workspaces/kivi-wakeword/.venv/bin/python -I kivi_score.py \
  --session /work/workspaces/kivi-wakeword/datasets/real/<id> \
  --model oye_kivi=/work/workspaces/kivi-wakeword/v4/final/oye_kivi_uncalibrated.tflite \
  --thresholds 0.9,0.9437,0.95,0.97 --also-pc --jobs 4 --json <out>.json

# real takes into features (private work directory, nothing committed)
/work/workspaces/kivi-wakeword/.venv/bin/python -I $H/v3_real.py takes --phrase "oye kivi" --only-positive \
  --session 20261108T092940Z-spk01-tv-c3a --session 20261108T094310Z-spk01-tv-c3a --out vnext/real/takes.npz
bash vnext/real.sh   # real negative windows (v4_realneg.py) + augmented positive windows
```
