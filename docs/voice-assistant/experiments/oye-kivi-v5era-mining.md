# v5-era room audio: replay false wakes and missed-wake mining (2026-11-11)

Source: owner approval of 2026-11-11 (copy and score once). Follows the
[vnext ablations](oye-kivi-vnext-ablations.md) and its Next item 3 (fresh room exposure). Everything ran offline on
the workstation; audio, scores and candidate windows stay in the private work directory
(`~/kivi-models/data/datasets/real/`, data register entries 20 and 21 in
[the register](../data-register.md)). Nobody listened to, transcribed or printed any audio; this note holds
counts, rates and scores only.
Continued every day by the [iteration loop](../kivi-iteration-loop.md) (`kivi_collect.py`, from 2026-11-12).

## Day split (fixed by the owner, 2026-11-11)

By **Europe/Madrid local date**, alternating: **odd dates = TEST, even dates = MINE/TRAIN** (2026-11-08 mine,
11-09 test, 11-10 mine, 11-11 test, and on alternately; pieces are cut at local midnight). The manifest `split` of
the copied pieces is `mine-v5era` or `test2-v5era`. **Test-day audio may be scored and labelled for evaluation, but is
never used to choose models or thresholds.** The rule lives in `kivi_real_common.day_role`; the live-wake `split`
command follows the same rule.

## What was copied and scored

- Copy: `kivi_negatives.py --since 2026-11-08T13:28:10Z --until 2026-11-11T16:52:39Z --exclude-pipeline "Kiwi Test"
  --out-name negatives-v5era --part-test-day test2-v5era --part-mine-day mine-v5era`. The period starts when both TVs
  ran the v5 wake word. The tool also drops each file's first 0.3 s and last 7 s, windows around presses and sessions,
  and 7 s after run starts that follow a detection within 60 s.
- 47 pieces, **146.19 h, 16.84 GB**: c3-a 72.80 h, c3-b 73.39 h; `mine-v5era` 66.70 h (7.68 GB), `test2-v5era` 79.49 h
  (9.16 GB). Hours per device and day (h): 11-08 mine 9.49 / 9.43, 11-09 test 23.00 / 23.81, 11-10 mine 23.94 / 23.84,
  11-11 test 16.37 / 16.31 (c3-a / c3-b). `negatives-v5era/COMPLETE.json` marks the finished copy.
- Scoring, once: `kivi_replay_mine.py score`, the server's streaming runtime (pyopen-wakeword, 1280-sample chunks,
  never batched) running v1 `kivi`, v4 `oye_kivi` and v5 `oye_kivi_next` together, uncalibrated files from
  `kivi-models` (sha256 in `scores/v5era-replay/score_meta.json`). Raw points: v4 0.9437, v5 0.9754, v1 0.98.
  Benchmark 300 s per worker with 6 workers: 66x real time (45.9x with one worker), estimate 2.33 h; the full pass took
  **124.3 min wall** at 6 workers, nice 10, CPUQuota 600 %.
- A piece is cut into 20 min ranges that run in parallel, each started 60 s early with its warm-up dropped. This is
  tested equal to the continuous stream (unit test on noise, and on a real 20 min range: 52 of 52 steps above 0.3 with
  identical times and a score difference of 0.0).
- Per range only the steps where some model scored above 0.3 are kept. Events merge such steps within 1.5 s (33,253
  events, `events.csv`). Detections use the server's rule (strictly above the raw point, 2 s refractory).
- `eval_stream.stream` drops the remainder of the last 1280-sample chunk of a stream; this scorer has the same
  semantics (floor of whole chunks). On room audio that is a 1-chunk end effect per piece: noise, no bias.

## (a) Replay false wakes

Per device, per part, per hour and per evening (18-24 Madrid), exact Poisson 95 % intervals. **These are upper
bounds on the false-wake rate**: any genuine "oye Kivi" that was said and missed live but caught in replay counts as a
"false" wake here (the files hold no labels).

| scope | hours | evening h | v4 at 0.9437 | v5 at 0.9754 | v5 at 0.95 | v5 at 0.98 |
| --- | --- | --- | --- | --- | --- | --- |
| all | 146.19 | 34.86 | 48 (0.328/h) | 11 (0.075/h [0.038, 0.135]) | 47 (0.322/h) | 7 (0.048/h [0.019, 0.099]) |
| c3-a, all | 72.80 | 17.03 | 8 (0.110/h) | 2 (0.027/h [0.003, 0.099]) | 10 (0.137/h) | 2 (0.027/h) |
| c3-b, all | 73.39 | 17.82 | 40 (0.545/h) | 9 (0.123/h [0.056, 0.233]) | 37 (0.504/h) | 5 (0.068/h) |
| mine, all | 66.70 | 23.82 | 22 (0.330/h) | 6 (0.090/h [0.033, 0.196]) | 24 (0.360/h) | 3 (0.045/h) |
| test, all | 79.49 | 11.04 | 26 (0.327/h) | 5 (0.063/h [0.020, 0.147]) | 23 (0.289/h) | 4 (0.050/h) |
| c3-b, mine | 33.27 | 11.85 | 19 (0.571/h) | 5 (0.150/h [0.049, 0.351]) | 20 (0.601/h) | 2 (0.060/h) |
| c3-b, test | 40.12 | 5.98 | 21 (0.523/h) | 4 (0.100/h [0.027, 0.255]) | 17 (0.424/h) | 3 (0.075/h) |
| c3-a, mine | 33.43 | 11.98 | 3 (0.090/h) | 1 (0.030/h) | 4 (0.120/h) | 1 (0.030/h) |
| c3-a, test | 39.37 | 5.06 | 5 (0.127/h) | 1 (0.025/h) | 6 (0.152/h) | 1 (0.025/h) |

Evening (18-24 Madrid) false wakes, all devices: v5 at 0.9754 4 in 34.86 h (0.115/h [0.031, 0.294]), v4 at 0.9437 10 in
34.86 h (0.287/h). The full table with every device x part x model cell, the evening columns and v1 is in
`scores/v5era-replay/fa_table.md` and `fa_summary.json` (also v5 at 0.99: 0, v4 at 0.99: 0).

Reading: v5 fires 4 to 7 times less often than v4 on the same audio (the model change did what the ablations said),
and c3-b is the noisier room. The per-cell counts are small (0 to 9), so the intervals overlap; the c3-b vs c3-a gap
at v5 is not significant at 0.9754 (9 in 73.4 h against 2 in 72.8 h).

**Cross-check against live.** The 27 live v5 detections (16 on c3-b, 11 on c3-a, `live_wakes.csv`) were in the dropped
file tails, so the replay is not expected to re-find them. The replay fires 11 times in 146 h (0.075/h) against about
0.18/h live (27 in the same 146 h): lower, not higher, so no feature or level mismatch is indicated. A direct check
scored the 6 s tails of the live v5 detections with the same runtime: the live model's peak is above its raw point in
13 of 27 tails and above 0.9 in 22 of 27 (median 0.971); a 6 s tail starts cold (no 8 s of context), so a lower peak
than live is expected. Not conclusive per clip, and the v4-era tails (4 of 37 above the raw point) include runs that
closed for other reasons, so they are not a check.

## (b) Missed-wake candidates

Pool: events where v5 did not fire in the replay (peak at or below 0.9754) and v4 or v5 peaks at 0.5 or more (v1 alone
is excluded: it has 12,372 events above 0.5). The pool has **1,734 events** (mine c3-a 220, mine c3-b 613, test c3-a
267, test c3-b 634).

Ranking (`rank_score`): v5 peak + 0.25 x v4 peak + 0.10 x v1 peak + retry evidence (a live detection or an
stt-only (push-to-talk / mic-button) run on the same device within 30 s after the event +0.5; another pool event
5 to 30 s later +0.1). Retry evidence in the pool: live 23, stt-only 0 within the window, another event 574, none 1,137
(7 stt-only runs on c3-a and 32 on c3-b exist since the v5 era; one lies in the window of a selected event).

Selection: at most 40, round-robin over part x device (10 each), highest rank first, at least 10 s apart on a device.
The first round is **40 candidates: mine c3-a 10, mine c3-b 10, test c3-a 10, test c3-b 10**; by retry evidence live 8,
another event 28, none 4. v5 peaks of the 40 lie between 0.663 and 0.973; 12 have v4 at or above 0.9437.
**1,694 more exist**: left out with a v5 peak of 0.9 or more 93, of 0.8 or more 315, of 0.6 or more 881; the lowest rank
taken is 1.195 (the best left out, 1.636, sits in a cell that already had its 10).

Each candidate is a 6 s WAV window centred on the peak in
`datasets/real/live-misses/<part>/<device>/<UTC>/window.wav`, listed in `miss_candidates.csv` (id, device, part, UTC,
three peaks, retry evidence and detail, rank, empty label and speaker, level and peak dBFS). The owner labels them by
listening, scores hidden:

```
cd ~/homelab/scripts/voice-assistant/wakeword
~/kivi-models/data/.venv/bin/python kivi_live_wakes.py review --table miss_candidates.csv
```

Labels: attempt (someone said "oye Kivi" / "Kivi" to it), other-speech, tv-media, noise, unclear; speaker spk01, spk02,
spk-other, unknown (asked for attempt and other-speech). A window of a test day is labelled for evaluation only.

The 11 replay detections of v5 at 0.9754 are not in this round (the pool excludes them by definition); they are the
numerator of table (a) and could be a cheap second round if the owner wants them labelled.

## Reproduce

```
kivi_negatives.py ... (command above)
kivi_replay_mine.py score --jobs 6 && kivi_replay_mine.py report && kivi_replay_mine.py selfcheck
kivi_replay_mine.py candidates          # refuses to overwrite a table that may hold labels
```
