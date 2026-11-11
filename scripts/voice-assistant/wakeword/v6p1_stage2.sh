#!/bin/bash
# shellcheck disable=SC2086  # flag lists in variables are split on purpose
# Kivi v6.1 STAGE 2 (selection on 2026-11-12, even, fresh). Run after 2026-11-12 has ended in Europe/Madrid, best on the
# morning of 2026-11-13 (>= 08:00 local) so the files of the 11-12 evening are closed. The copy steps read the cluster
# (kubectl exec ... dd, read only) and are started by the coordinator on the owner's word. Never reads odd-day data:
# the 11-13 pieces this copy also writes are labelled test3-v6p1 and left alone until stage 3.
set -e
H=$HOME/homelab/scripts/voice-assistant/wakeword
W=/work/workspaces/kivi-wakeword
UNTIL=${UNTIL:?set UNTIL to an ISO UTC time about 15 min ago, e.g. 2026-11-13T07:00:00Z}
cd $H
# 1. room audio 2026-11-11 16:52:39Z .. UNTIL, cut at local midnight: 11-12 -> sel-v6p1, odd days -> test3-v6p1
python3 kivi_negatives.py --hours 1000 --since 2026-11-11T16:52:39Z --until "$UNTIL" --exclude-pipeline "Kiwi Test" \
  --out-name negatives-v6p1 --part-test-day test3-v6p1 --part-mine-day sel-v6p1 --dry-run
python3 kivi_negatives.py --hours 1000 --since 2026-11-11T16:52:39Z --until "$UNTIL" --exclude-pipeline "Kiwi Test" \
  --out-name negatives-v6p1 --part-test-day test3-v6p1 --part-mine-day sel-v6p1
# 2. live wake clips (new runs only; existing rows are never changed) and the local auto-labeller (no cluster Whisper)
python3 kivi_live_wakes.py extract
$W/.venv/bin/python -I kivi_live_wakes.py autolabel
cd $W
SCOPE="systemd-run --user --scope -p MemoryMax=12G -p CPUQuota=400% -- nice -n 10"
# 3. server-exact embeddings of the new pieces (appends to v6/room_emb/index.json; parts sel-v6p1 / test3-v6p1)
$SCOPE .venv/bin/python -I $H/v6_room.py embed --manifest datasets/real/negatives-v6p1/manifest.jsonl --out-dir v6/room_emb
# 4. the pre-registered rule: threshold per arm and the arm (selection.json); nothing else is read
$SCOPE .venv/bin/python -I $H/v6p1_select.py --index v6/room_emb/index.json --part sel-v6p1 --local-date 2026-11-12 \
  --live-date 2026-11-12 --arm p1a=v6p1/runs/p1a --arm p1b=v6p1/runs/p1b --arm p1c=v6p1/runs/p1c --out v6p1/selection.json
echo STAGE2_DONE
