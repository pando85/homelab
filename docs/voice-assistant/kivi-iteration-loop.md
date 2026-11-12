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

**Truth audit of the false-wake labels (2026-11-12).** Almost every live false wake counted so far carries an
autolabel (`auto-v1`) whose accuracy was never measured. `kivi_false_wake_audit.py table | sample | analyze` builds the
per-clip audit table, a blind validation sample of about 40 live clips (offered first by `queue`, auto label hidden) and,
once labelled, the confusion matrix, the precision and recall of auto-false and the corrected false wakes per TV and
evening with 95 % intervals. Measurement only: no choice is made from the sample. The plan that uses it (Phase 0 truth,
cheap fixes, v6.3) is `docs/plan/2026-11-false-wake-plan.md` in the kivi-models checkout (private).

Each step is owner-triggered, one at a time. The method sections of the [release runbook](https://git.grigri.cloud/agil/kivi-models)
(`docs/runbook/wake-word-release.md`) hold the exact commands.

## Day rule

By Europe/Madrid date and the **day of the month**: odd = TEST, even = train / selection. `kivi_real_common.day_role` is the
single definition (the older day-count alternation went wrong after Dec 31 to Jan 1, both odd; fixed 2026-11-12, tested).
The role of each collected day is in its `daily/YYYY-MM-DD.json` and in `daily/summary.md`.

## Disk and retention (owner decision 2026-11-12)

The raw room audio is the big item: about 31 hours a day for both TVs, roughly 4 GB a day. `/home` (the work pool) is 85 %
full, so the raw room audio lives on the second ZFS pool, `datasets`, in the plain directory `/datasets/kivi-room-audio/`
(mode 700; about 204 GB free when it was set up; no zfs datasets, nothing else under `/datasets` is touched). The old
paths keep working: `datasets/real/negatives`, `negatives-v5era`, `negatives-1111eve`, `negatives-daily` and the filtered room
copies `datasets/derived/*/negatives` are symlinks into it. New daily copies land there through the `negatives-daily` symlink
(or `$KIVI_ROOM_DIR`), and the 50 GB guard checks the pool that receives the copy. Scores, queue tables, window clips, labels
and manifests stay in `~/kivi-models/data` (small). `data/negatives/scores` travelled with its directory (it mixes the two).

Retention: raw room audio is kept about **45 days** (even and odd days alike), which at 4 GB a day is about 180 GB at steady
state on the 204 GB pool, with headroom. Nothing is deleted automatically. The owner triggers
`kivi_collect.py prune` (skill step `prune`): it LISTS the days and GB it would delete and deletes only with `--yes`. It never
deletes labels, scores, queue tables, window clips or manifests, nor pieces of a day or part in `daily/protect.json` (an active
pre-registration: 2026-11-12 and 2026-11-13 until the v6.1 stage 3 has run, and `test2-v5era`; edit by hand), nor pieces of a
day that still has an open review item. Deleted pieces are logged in `daily/pruned.jsonl`; manifests keep their rows (the tools
skip pieces that are gone). Kept indefinitely: score chunks, queue tables, window clips of false wakes and candidates, labelled
items, and the odd-day test windows. The filtered copies under `datasets/derived` are not pruned by the tool. Source recordings
in Home Assistant are deleted by Home Assistant after 90 days (data register row 5).

## Where each part lives

| Part | Where |
| --- | --- |
| Skill (steps and hard rules) | `~/kivi-models/.claude/skills/kivi-wakeword-loop/SKILL.md` |
| Collector | `scripts/voice-assistant/wakeword/kivi_collect.py` (tests `test_kivi_collect.py`) |
| Labelling queue | `kivi_live_wakes.py queue` (tests in `test_kivi_live_wakes.py`) |
| False-wake truth audit, blind autolabel validation sample, analysis | `kivi_false_wake_audit.py` (tests `test_kivi_false_wake_audit.py`); sample and frozen key in `live/autolabel_validation*.csv` |
| Per-day room copy | `kivi_negatives.py --local-date D --part d-YYYYMMDD --out-name negatives-daily` |
| Daily counts | `~/kivi-models/data/daily/` (`state.json`, `YYYY-MM-DD.json`, `summary.md`, `protect.json`, `pruned.jsonl`) |
| Raw room audio | `/datasets/kivi-room-audio/` (second pool), reached through the `negatives*` symlinks in `datasets/real/` |
| Queue tables | `~/kivi-models/data/datasets/real/live-misses/` (`daily_false_wakes.csv`, `daily_miss_candidates.csv`, ...) and `live/live_wakes.csv` |
| Release driver | `scripts/voice-assistant/wakeword/wake_release.py` |
