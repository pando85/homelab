# qBittorrent Performance Tuning

## Understanding the Settings

qBittorrent has two types of limits that are often confused:

### Queue Limits (which torrents can seek peers)
- `max_active_uploads`: How many seeding torrents can be "active" (allowed to find/connect peers)
- `max_active_downloads`: How many downloading torrents can be "active"
- `max_active_torrents`: Total active torrents (uploads + downloads)

**Critical:** If `max_active_uploads` is too low, torrents get stuck in `queuedUP` state and cannot even attempt to connect to peers. This is the #1 cause of slow speeds with many seeding torrents.

### Bandwidth/Slot Limits (how many can transfer data)
- `max_uploads`: How many torrents can upload data simultaneously
- `max_uploads_per_torrent`: Upload slots per torrent
- `max_connec`: Total peer connections across all torrents
- `max_connec_per_torrent`: Peer connections per torrent

### Memory Limits
- Container memory limit: Too low causes OOMKilled restarts, disrupting all connections
- Each torrent + peers consumes memory; 2000+ torrents need 6-8 GiB

## Symptoms and Root Causes

### Symptom: Many torrents "Stalled Uploading" or "queuedUP"

**Root cause:** `max_active_uploads` too low

Torrents in `queuedUP` state cannot connect to peers at all. They're waiting in a queue for an "active" slot.

**Example:** With 2000 seeding torrents and `max_active_uploads=100`:
- Only 100 torrents can seek peers
- 1900 torrents stuck in queue, never connecting
- Total peers: ~100-300 (very low)

**Fix:** Increase `max_active_uploads` to cover 40-50% of seeding count (see Lessons Learned)

### Symptom: Slow downloads despite many seeds

**Root causes:**
1. `max_active_downloads` too low (torrents queued, not connecting)
2. Upload blocked → BitTorrent tit-for-tat penalizes you
3. Container restarting (OOMKilled) disrupts connections

**BitTorrent protocol:** Peers prioritize uploaders. If you're not uploading, you get slower download slots.

### Symptom: Container restarting frequently

**Root cause:** Memory limit too low

Check with:
```bash
kubectl describe pod qbittorrent-0 -n qbittorrent | grep -A5 "Last State"
```

If "OOMKilled", increase memory limit in values.yaml.

### Symptom: High SYN_SENT:CLOSED on router (>500)

**Root cause:** `max_connec` too low

Peers try to connect but qbittorrent can't accept them (connection slots full). Connections stay stuck at SYN_SENT then timeout.

**Fix:** Increase `max_connec` based on router capacity (see below).

## Router Capacity Analysis

pfSense state table stores each network connection. Each torrent peer creates ~19 state entries over its lifecycle (SYN → ESTABLISHED → FIN_WAIT → TIME_WAIT).

### Check Capacity

```bash
ssh pfsense.grigri "pfctl -s info | grep -E 'current entries'"
ssh pfsense.grigri "pfctl -s memory"  # shows limit
```

### Calculate Safe Limits

| Router State Limit | Safe Ceiling (50%) | Max Connections |
|--------------------|--------------------|-----------------|
| 402,000 (default) | 201,000 states | ~10,500 connections |
| 200,000 | 100,000 states | ~5,250 connections |

**Formula:** `max_connec = (safe_ceiling_states - baseline_states) ÷ 19`

Where baseline_states = states from other services (~2-5k)

### Example Calculation

```
Router limit: 402,000
Safe ceiling: 201,000 (50%)
Current non-torrent states: 5,000
Available for torrents: 196,000
Max connections safe: 196,000 ÷ 19 = ~10,000
Recommended: 8,000 (leaving buffer)
```

## Quick Diagnostics

### Check Queue State

```bash
curl -s http://qbittorrent.internal.grigri.cloud/api/v2/sync/maindata | jq '
  .torrents | to_entries |
  group_by(.value.state) |
  map({state: .[0].value.state, count: length})
'
```

Key states to watch:
- `queuedUP`: Stuck waiting for active slot (BAD)
- `stalledUP`: Active but no peers connected (OK if low)
- `uploading`: Actively transferring (GOOD)

### Check Container Health

```bash
kubectl --context=grigri describe pod qbittorrent-0 -n qbittorrent | grep -A3 "Last State"
kubectl --context=grigri top pod qbittorrent-0 -n qbittorrent --containers
```

### Check Router State

```bash
ssh pfsense.grigri "pfctl -s states | grep 50413 | grep -c ESTABLISHED"
ssh pfsense.grigri "pfctl -s states | grep -c 'SYN_SENT:CLOSED'"
```

## Key Metrics

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| `queuedUP` torrents | < 5% | 5-20% | > 20% |
| `stalledUP` torrents | < 20% | 20-40% | > 40% |
| Total peer connections | 500+ | < 200 | < 50 |
| SYN_SENT:CLOSED | < 100 | 100-500 | > 500 |
| Router state % | < 25% | 25-50% | > 80% |
| Container restarts/day | 0 | 1-3 | > 5 |

## Recommended Settings

### For High-Seeding Scenarios (2000+ torrents)

| Setting | Recommended | Purpose |
|---------|-------------|---------|
| `max_active_uploads` | 600-1000 | Queue slots - allows torrents to seek peers |
| `max_active_downloads` | 10-20 | Queue slots for downloads |
| `max_active_torrents` | 800-1200 | Total queue limit |
| `max_uploads` | 60-100 | Bandwidth slots - actual upload transfers |
| `max_uploads_per_torrent` | 4-6 | Per-torrent upload slots |
| `max_connec` | 5000-8000 | Total peer connections (router dependent) |
| `max_connec_per_torrent` | 20-25 | Per-torrent peers |
| Memory limit | 10 GiB | Prevent OOMKilled |

### Relationship Summary

```
seeding_torrents ≈ 2000
max_active_uploads ≈ 40-50% of seeding = 800-1000
max_uploads ≈ bandwidth_based (9 MB/s ÷ 90 KB/s/slot = 100)
max_connec ≈ router_safe (8000 with 400k limit)
```

## Tuning Workflow

1. **Diagnose the bottleneck:**
   - High `queuedUP` → increase `max_active_uploads`
   - High SYN_SENT:CLOSED → increase `max_connec`
   - OOMKilled → increase memory limit
   - Slow downloads → check upload is working

2. **Adjust via API:**
   ```bash
   curl -X POST http://qbittorrent.internal.grigri.cloud/api/v2/app/setPreferences \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "json={'max_active_uploads':600,'max_connec':8000}"
   ```

3. **Verify router capacity:**
   ```bash
   ssh pfsense.grigri "pfctl -s info | grep 'current entries'"
   ```

4. **Monitor 10-30 minutes:**
   - `queuedUP` should drop to < 5%
   - Peer connections should increase
   - Upload/download speeds should improve

## Lessons Learned

### Memory does NOT scale with active upload slots

**Finding (May 2026):** Increasing `max_active_uploads` from 600 → 1000 had negligible
memory impact. Memory settled at ~6 Gi regardless of the active upload count.

**Reason:** Memory is driven by the total torrent count (session metadata, piece maps),
not by how many torrents are in the active upload set. Moving a torrent from `queuedUP`
to `stalledUP` (active but 0 peers) adds almost no memory since no peer state is allocated.

**Implication:** It is safe to increase `max_active_uploads` aggressively as long as the
container memory limit has headroom for the total torrent count. The memory floor is
~2.5-3 Gi per 1000 torrents regardless of queue settings.

### stalledUP is the dominant state with DHT/PeX/LSD disabled

**Finding:** With anonymous mode enabled and DHT/PeX/LSD disabled, peer discovery relies
solely on tracker HTTP announces. This causes ~50% of active torrents to be `stalledUP`
(active in the seeding pool but 0 connected peers) even with ample queue slots.

**Implication:** Increasing `max_active_uploads` moves torrents from `queuedUP` → `stalledUP`,
which still improves seeding because stalled torrents can respond to incoming connections.
But to significantly increase `uploading` counts, peer discovery must improve (enable DHT/PeX
or add more trackers).

### Incremental tuning workflow works well

**Validated approach (May 2026, 2396 torrents):**

1. Start with conservative increase (e.g. +50% of current `max_active_uploads`)
2. Monitor at 10-minute intervals: queuedUP, stalledUP, uploading, peers, memory
3. Expect: queuedUP drops, stalledUP absorbs, uploading ramps over 20-30 min
4. Memory stabilizes in ~60 min post-restart; compare against pre-change baseline
5. If queuedUP plateaus above 5%, another round of increases is safe (memory permitting)

**Results from 600 → 1000 increase:**

| Metric | Before | After (30 min) | Change |
|--------|--------|----------------|--------|
| queuedUP | 1,586 (66%) | 1,187 (50%) | -25% |
| uploading | 26 | 37 | +42% |
| Peer connections | 135 | 304 | +125% |
| Memory | 6.2 Gi | 6.1 Gi | Negligible |
| Restarts | 0 | 0 | Stable |

### Queue plateaus at MaxActiveUploads limit

When `queuedUP` stops decreasing after a config change, it means the active upload pool
(`stalledUP` + `uploading` + `forcedUP`) has saturated `max_active_uploads`. The remaining
queuedUP count equals `total_torrents - max_active_uploads - forcedUP`. To eliminate the
queue, `max_active_uploads` must exceed the total torrent count.

## Common Pitfalls

| Pitfall | Result | Fix |
|---------|--------|-----|
| `max_active_uploads` = 100 with 2000 seeding | 1900 torrents queued | Increase to 500+ |
| `max_connec` = 500 with 2000 seeding | SYN_SENT overflow | Increase to 5000+ |
| Memory = 3.5 GiB with 2000 torrents | OOMKilled restarts | Increase to 10 GiB |
| `max_uploads_per_torrent` = 1 | Single torrent hogs bandwidth | Use 4-6 |

## Quick Fixes

### All torrents stuck in queue
```bash
# Increase active slots
curl -X POST http://qbittorrent.internal.grigri.cloud/api/v2/app/setPreferences \
  -d "json={'max_active_uploads':600,'max_active_torrents':800}"
```

### Slow downloads
```bash
# Ensure uploads work (tit-for-tat)
curl -s http://qbittorrent.internal.grigri.cloud/api/v2/transfer/info | jq '.up_info_speed'
# If 0 or very low, check max_uploads_per_torrent isn't too low
```

### Router SYN_SENT high
```bash
# Increase connection limit
curl -X POST http://qbittorrent.internal.grigri.cloud/api/v2/app/setPreferences \
  -d "json={'max_connec':8000}"
```

### Container restarting
```bash
# Check OOMKilled
kubectl describe pod qbittorrent-0 -n qbittorrent | grep OOMKilled
# If present, increase memory limit in values.yaml and redeploy
```
