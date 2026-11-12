# Kivi wake word: the iteration loop

How the "oye Kivi" model improves from real household use, with the least owner time. Triggered **by hand**, no timer:
the owner runs the `kivi-wakeword-loop` skill from Claude Code (`/kivi-wakeword-loop collect`) or OpenCode (ask the agent
to load it). The skill lives in the kivi-models checkout (`~/kivi-models/.claude/skills/kivi-wakeword-loop/SKILL.md`; both
Claude Code and OpenCode read `.claude/skills/<name>/SKILL.md` of the project, sources below) and carries the hard rules
for any agent that runs it. Data, models and the venv sit in `~/kivi-models/data/` (`$KIVI_HOME`, gitignored, household
audio: see [data register](data-register.md) rows 25 and 26).

Skill discovery (checked against the vendor docs, 2026-11-12): Claude Code loads project skills from
`.claude/skills/<name>/SKILL.md` of the directory it starts in and every parent up to the repository root
(<https://code.claude.com/docs/en/skills>); OpenCode searches `.opencode/skills`, `.claude/skills` and `.agents/skills`
from the current directory up (<https://opencode.ai/docs/skills/>). One copy in `.claude/skills` therefore serves both; no
second path or symlink is needed. `disable-model-invocation: true` keeps Claude Code from starting it on its own.

## The cycle

```
 collect (owner starts)  ->  label (owner, ~15 min/week)  ->  retrain (enough new labelled data, or weekly)
      ^                                                              |
      |                                                              v
 rollback with demote  <-  promote (owner go)  <-  independent review  <-  gate on fresh odd days  <-  threshold on even days
```

1. **Collect** (`kivi_collect.py run`, one command, every Madrid day not yet processed up to yesterday; state in
   `daily/state.json`): per day the live wakes are extracted with their trigger class (wake or mic button), autolabelled
   locally (owner labels kept, `label_source` tells them apart), split by date parity; the room audio of the day is copied
   to `negatives-daily` (part `d-YYYYMMDD`); the live model (read from the pipeline "Kiwi", read only) replays it; its false
   wakes and the v4 / live near-misses are appended to the labelling queue tables; counts go to `daily/YYYY-MM-DD.json` and
   `daily/summary.md`. Idempotent: a done day is skipped, `--redo` repeats it, rows already in a table are never changed.
   A day collected less than 6 h after midnight is marked `early` and runs again later (files that span midnight are copied
   once closed). Guards: one lock, at most two heavy jobs in total, 50 GB free disk, resource scope (12 GB, 800 % CPU, nice
   10, at most 8 threads, 6 GB VRAM). Nothing is deleted.
2. **Label** (owner, own terminal, about 15 minutes a week):
   `~/kivi-models/data/.venv/bin/python ~/homelab/scripts/voice-assistant/wakeword/kivi_live_wakes.py queue --minutes 15`.
   It walks every pending item in priority order (false wakes of the live model, uncertain live clips, missed-wake
   candidates), scores hidden, each label saved at once, stop any time, counts per table at start and end. The existing
   vocabulary (attempt / other-speech / tv-media / noise / unclear + speaker; true / false / not-a-wake / unclear for live
   clips) is unchanged. Nobody but the owner listens to the audio.
3. **Retrain** when enough new labelled data exists or weekly, from EVEN days only (odd = TEST, never used for training,
   threshold, selection or mining). Plan and pre-register in the experiment doc first.
4. **Threshold** from held-out even days (`wake_release.py pick-threshold`, room selection part only).
5. **Gate**, pre-registered before computing anything, on fresh odd days: C1-C6 as in the
   [v6 experiment](experiments/oye-kivi-v6.md) (C1 session recall, C2 near-phrase accepts, **C3 room false wakes in a TFLite
   replay, strict against the live model**: per TV in the evening and in total no worse than the live model's, C4 live clips
   of the test days, C5 synthetic sets, C6 no odd-day data in training or selection and calibration checked). A failed
   criterion stays failed; only the owner can override and must say so.
6. **Independent review** by a second agent or the owner: re-derive the gate numbers from the tables, check C6.
7. **Deploy** with `wake_release.py package`, `release`, `deploy`, then `promote` (owner gate); the previous model stays
   in `models.lock`. **Rollback** is `demote` (always allowed). After a promote the next `collect` replays with the new live
   model automatically.

Each step is owner-triggered, one at a time. The method sections of the [release runbook](https://git.grigri.cloud/agil/kivi-models)
(`docs/runbook/wake-word-release.md`) hold the exact commands.

## Day rule

By Europe/Madrid date and the **day of the month**: odd = TEST, even = train / selection. `kivi_real_common.day_role` is the
single definition (the older day-count alternation went wrong after Dec 31 to Jan 1, both odd; fixed 2026-11-12, tested).
The role of each collected day is in its `daily/YYYY-MM-DD.json` and in `daily/summary.md`.

## Disk and retention proposal (the owner decides; nothing is deleted automatically)

The room audio is the big item: about 31 hours a day for both TVs, roughly 3.5 GB a day (the v5-era copy is 14 GB for 146 h;
the old sets `negatives`, `negatives-v5era`, `negatives-1111eve` hold about 17.5 GB). The pool had about 83 GB free when this
loop was built; the collector refuses to copy below 50 GB free.

Proposal for `negatives-daily` and its replay chunks:

- Keep the **score chunks** (`scores/daily-replay/chunks`, a few MB a day: times and scores, no audio), the **queue tables**,
  the **window clips of false wakes and candidates** (6 s each, about 200 KB) and everything labelled: indefinitely.
- Keep the raw room audio of a day **14 days**, then delete it once the day is collected, replayed and its queue items are
  labelled. If a retrain needs room negatives from older even days, build the embeddings (96 floats per step, no audio)
  first, as v6 did.
- Odd (TEST) days: keep **60 days** raw (a frozen test needs re-scoring when a new candidate appears; the score chunks are
  not enough for a new model), then delete; the tables and windows stay.
- Source recordings in Home Assistant are deleted after 90 days by Home Assistant itself (data register row 5).
- At 3.5 GB a day this is about 50 GB at steady state plus the old sets; if that is too much, shorten the odd-day period
  before the even one.
This is a proposal: delete nothing until the owner agrees, and do it by hand per directory.

## Where each part lives

| Part | Where |
| --- | --- |
| Skill (steps and hard rules) | `~/kivi-models/.claude/skills/kivi-wakeword-loop/SKILL.md` |
| Collector | `scripts/voice-assistant/wakeword/kivi_collect.py` (tests `test_kivi_collect.py`) |
| Labelling queue | `kivi_live_wakes.py queue` (tests in `test_kivi_live_wakes.py`) |
| Per-day room copy | `kivi_negatives.py --local-date D --part d-YYYYMMDD --out-name negatives-daily` |
| Daily counts | `~/kivi-models/data/daily/` (`state.json`, `YYYY-MM-DD.json`, `summary.md`) |
| Queue tables | `~/kivi-models/data/datasets/real/live-misses/` (`daily_false_wakes.csv`, `daily_miss_candidates.csv`, ...) and `live/live_wakes.csv` |
| Release driver | `scripts/voice-assistant/wakeword/wake_release.py` |
