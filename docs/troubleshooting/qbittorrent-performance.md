# qBittorrent Performance Tuning

## Symptoms of Connection Starvation

- Many torrents stuck in "Stalled Uploading" state (not getting peers)
- Low upload speed despite many seeding torrents
- High SYN_SENT:CLOSED connections on router (peers trying to connect but failing)
- Router state churn (high insert/removal rate, 500+/s)
- Only few dozen active torrents despite hundreds/thousands seeding

## Root Cause Analysis

When `max_connec` (global connection limit) is too low relative to seeding torrent count:
- Torrents compete for limited connection slots
- Connection churn increases (peers constantly connect/disconnect)
- Libtorrent can't accept new connections, leaving them stuck at SYN_SENT
- Upload throughput drops dramatically

**Key ratio:** `max_connec` should be >= `seeding_torrents × max_connec_per_torrent`

With 2291 seeding torrents and 30 connections per torrent max:
- Minimum needed: 2291 × 15 = ~34k theoretical max
- Practical: 2500-3000 is sufficient (not all torrents get max connections)

## Quick Diagnostics

### qBittorrent Status (WebUI or API)

```bash
# Get current preferences
curl -s http://qbittorrent.internal.grigri.cloud/api/v2/app/preferences | jq '{
  max_connec, max_connec_per_torrent, max_active_uploads,
  max_active_torrents, max_uploads, max_uploads_per_torrent
}'

# Get transfer info
curl -s http://qbittorrent.internal.grigri.cloud/api/v2/transfer/info | jq '{
  dl_info_speed, up_info_speed, dht_nodes, connection_status
}'

# Get sync data for active counts
curl -s http://qbittorrent.internal.grigri.cloud/api/v2/sync/maindata?rid=0 | jq '.server_state | {
  total_peer_connections, dl_info_speed, up_info_speed
}'
```

### Router State Table (pfSense)

```bash
ssh pfsense.grigri "pfctl -s info | grep -E 'current entries|inserts|removals|state-limit'"
ssh pfsense.grigri "pfctl -s states | grep 50413 | grep -c ESTABLISHED"
ssh pfsense.grigri "pfctl -s states | grep -c 'SYN_SENT:CLOSED'"
```

### Key Metrics to Monitor

| Metric | Healthy Range | Warning | Critical |
|--------|---------------|---------|----------|
| Stalled Uploading | < 20% of seeding | 20-40% | > 40% |
| Active torrents | > 5% of total | < 5% | < 1% |
| Total peer connections | 500-2000 | < 200 | < 50 |
| Upload speed | Near limit (8-9 MB/s) | < 1 MB/s | < 100 KB/s |
| Router SYN_SENT:CLOSED | < 100 | 100-500 | > 500 |
| Router state entries | < 20% of limit | 20-50% | > 80% |
| state-limit hits | 0 | > 0 | > 10/s |

## Recommended Settings for High-Seeding Scenarios

For 2000+ seeding torrents with 9 MB/s upload limit:

| Setting | Value | Reason |
|---------|-------|--------|
| `max_connec` | 2500-3000 | Allows multiple torrents to have concurrent connections |
| `max_connec_per_torrent` | 12-15 | Lower per-torrent = better distribution across all torrents |
| `max_active_uploads` | 500-800 | More active seeding slots |
| `max_active_torrents` | 800-1000 | Limits memory overhead from tracking stalled torrents |
| `max_uploads` | 100-150 | Global upload slots (9 MB/s ÷ 100 = 90 KB/s per slot) |
| `max_uploads_per_torrent` | 4-6 | Prevents bandwidth hogging by single torrent |

## Tuning Workflow

1. **Check current state:**
   - Note Stalled Uploading count
   - Check router SYN_SENT:CLOSED count
   - Get upload speed

2. **Calculate needed connections:**
   - `seeding_count × desired_connections_per_torrent`
   - Aim for 15 connections average per seeding torrent

3. **Adjust settings via API:**
   ```bash
   curl -X POST http://qbittorrent.internal.grigri.cloud/api/v2/app/setPreferences \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "json=$(jq -c '{max_connec:2500,max_connec_per_torrent:15}' <<< '{}')"
   ```

4. **Monitor for 10-30 minutes:**
   - Stalled Uploading should decrease
   - Upload speed should increase
   - SYN_SENT:CLOSED should drop

5. **Iterate if needed:**
   - If still high stalled, increase `max_connec` further
   - If upload speed low, increase `max_uploads`

## Common Pitfalls

- **Setting max_connec too high (10k+):** Can overwhelm router NAT table
- **Setting max_connec_per_torrent too high (50+):** Causes churn, fewer torrents active
- **max_active_torrents = 3000:** No real limit, wastes memory tracking inactive torrents
- **max_uploads too low (< 50):** Bandwidth bottleneck even with good connections

## Router Capacity Check

pfSense default state table: 402,000 entries

```bash
# Check current limit
ssh pfsense.grigri "pfctl -s memory"

# If approaching limit, increase via pfSense UI:
# System > Advanced > Firewall & NAT > Firewall Maximum Table Entries
```

## Historical Analysis

### 2026-04-12: Connection Starvation Fix

**Initial state:**
- 2291 seeding torrents
- `max_connec`: 500 (too low)
- Stalled Uploading: 2220 (97% of seeding!)
- Upload speed: 43 KB/s (near zero)
- SYN_SENT:CLOSED: ~30 connections stuck

**Fix applied:**
- `max_connec`: 500 → 2500
- `max_connec_per_torrent`: 30 → 15
- `max_active_uploads`: 200 → 600
- `max_active_torrents`: 3000 → 800
- `max_uploads`: 50 → 120

**Result after 10 minutes:**
- Stalled Uploading: 2220 → 679 (-70%)
- Active torrents: 32 → 128 (+300%)
- Upload speed: 43 KB/s → 896 KB/s (+20x)
- Peer connections: 288 → 703 (+144%)
- Router states: 12% of limit (healthy)

**Root cause:** 500 global connections couldn't serve 2291 seeding torrents. Only ~17 torrents (500÷30) could be fully active at once.
