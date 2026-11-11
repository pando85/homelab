# "oye Kivi" training-data quality: phrase check, out-of-fold labels, coverage (2026-11-11)

Analysis only: no training set, model or pre-registration was changed and nothing was deployed. Aggregates only;
speakers are spk01 and spk02. Work directory `/work/workspaces/kivi-wakeword/quality/` (`report.md` there has every
table; ids and flag files are private, see [data register](../data-register.md) row 23). Follows the
[v6 experiment](oye-kivi-v6.md) and its data audit.

**Question:** does the v6 training data (and the v6.1 stage-1 hard negatives) hold label errors (real "oye Kivi"
attempts used as negatives, positives without the phrase), and which real-use conditions does it not cover?

**Privacy:** local faster-whisper large-v3-turbo only (the cluster Whisper logs transcripts to Loki), transcripts kept in
process memory; only booleans, word-end offsets, word probabilities and word counts were written. No audio was played
or saved, no speaker embedding was computed. Training checks read even days (2026-11-06, -08, -10) and grandfathered
training data only; 2026-11-12 and -13 were not read; odd-day live clips appear as counts only.

## Result

No mass labelling defect. Two independent checks (ASR phrase check and a grouped 5-fold out-of-fold score, i.e.
confident learning) agree on a handful of errors, all negatives that are real attempts:

| Finding | Size | Evidence |
| --- | --- | --- |
| Room negatives holding "oye Kivi" / "Kivi" | 4 events, about 20 of 252,624 room rows | ASR phrase or kivi word, OOF 0.88 to 0.9999; v5 scored them 0.95 to 0.98, v6 0.01 to 0.07 (v6 learned to reject them); one is miss candidate mc001 |
| Live false-wake clips (hard negatives, weight 6) ending in a "Kivi"-like word | 3 of 20 clips (540 of 3,600 windows), 2 more hotword-only | ASR kivi word near the end, OOF median 0.27 to 0.44 |
| v6.1 stage-1 hard negatives | 0 of 126 v6 events, 5 of 1,722 v5 events | all 5 already dropped by the v6.1 retry filter (which drops 56 % of events) |
| Weak synthetic voice `vctk_tr` | 8.7 % of isolated synthetic positives | OOF median 0.11, hotword-phrase rate 0.45 (context 0.40); `siwis`, `upmc` low on OOF in both sets |
| Real 2026-11-08 takes | 1 take mis-cut by 0.5 s (tv and pc), 1 webcam dropout | 231/232 hold the phrase; phrase end within ±0.2 s on 96 % (tv), 92.5 % (pc) |

Not errors: the OOF-flagged real "Kivi alone" and near-word takes (the hotword ASR reads "Kivi" alone in 111 of 123),
the synthetic confusers "oye kiri / kili / Kibo / Gibi / chivi" (genuine near-misses), and the low-OOF real desk takes of
the TV-on blocks (phrase present: hard positives).

**Coverage (the larger issue):** real TV-mic wakes (even days: 9 true, 20 false) are nearest to the real session takes
(median embedding distance 258 to 266) and farthest from the synthetic "...oye Kivi, <command>" positives (318). None of
the 9 true wakes has the TV on (audio proxy), against 7 of 20 false wakes; spk02 has 3 live clips; 2 of 9 true wakes are
far from every positive source and 3 of 9 are nearer to a negative. Level and noise floor are covered.

## Recommended for v6.2

1. Drop the room rows of the 4 events and the 3 (optionally 5) kivi-word `live_hard` clips; apply the retry filter to
   all room negatives, not only the newly mined ones.
2. Drop `vctk_tr`; drop or down-weight `siwis` and `upmc`.
3. Weight real TV-mic phrase+command positives up; cap synthetic context positives (they did not add coverage; the
   confuser-in-sentence negatives are what helped v6).
4. Collect a TV-on TV-mic session for spk01 and spk02 and more spk02 positives (no training change replaces this).
5. Hygiene: fix take B1c9's cut; `room_train_w8.npy` carries 5 all-zero trailing rows (`v6_room.py windows`
   over-allocates one row per piece when `(steps - 24) % 8 == 0`).

No new deploy blocker for v6 from the data: the errors found are small and do not explain its C3 regression.

## Method notes

- 1a/1c: hard-negative and room events re-derived exactly (433 v6 and 7,382 v5 windows reproduce stage 1); each event
  cut to [t - 4 s, t + 3 s], transcribed unprompted and with the hotword "oye Kivi" (the hotword run is biased: low
  confidence). Positive control: the unprompted check finds the phrase in 8 of 10 even-day true live wakes.
- 2: groups = TTS voice, recording session, live clip, room date-hour block; v6m's per-step mix and weights, trained
  from scratch (a v5 warm start would leak), 8,000 steps, about 2.5 min per fold; thresholds matched to v6's recall on
  the synthetic held-out set; confident-learning class thresholds t_pos 0.738, t_neg 0.973.
- 3: L2 nearest-neighbour distance of the server-exact detection window to every training source; level, floor and a
  loud-background proxy from the audio (no media-state metadata offline).
