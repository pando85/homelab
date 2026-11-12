# Home Assistant voice and multiroom — implementation plan

**Status:** GitOps components implemented in branch
`feat/home-assistant-voice-multiroom`. Physical onboarding and network/TV
acceptance require access to the devices. This is not a claim of an already
working household deployment.

## Baseline (audited 2026-11-01)

Source: `apps/home-assistant/values.yaml`,
`apps/esphome/values.yaml`, `apps/navidrome/values.yaml` and the
`docs/network/configuration.md` VLAN inventory.

| Layer | Existing | Work in this PR |
|---|---|---|
| Home Assistant | 2026.10.4, PostgreSQL 17, persistent /config | 45s HA shutdown grace |
| STT | Wyoming Whisper large-v3-turbo, Spanish, CUDA | Reuse; latency benchmark later |
| TTS | Wyoming Piper es_ES-sharvard-medium | Reuse |
| Wake word | Wyoming OpenWakeWord (alexa) | Retain for other clients; ReSpeaker uses local micro_wake_word |
| ESPHome | 2026.10.0; GitOps packages and Vault | Kitchen XVF3800 config pinned to 2026.10.4 |
| Media library | Navidrome + Lidarr | Connect via Open Subsonic, no duplicate storage |
| Music controller | Not present in Git | New Music Assistant 2.10.9 |
| Player targets | LG C3 and office TV | Discover/pair interactively; no speculative entities |
| Network | LAN/DMZ/IoT VLANs, pfSense | Preflight multicast, routes, and client-reachable addresses |

## Architecture

```text
Kitchen XVF3800 (ESPHome micro_wake_word + XMOS DSP)
          | encrypted ESPHome API
          v
Home Assistant Assist <-----> Wyoming Whisper (Spanish, CUDA)
          |              \--> Wyoming Piper (Spanish TTS)
          | intents/search-and-play, room routing
          v
Music Assistant (hostNetwork on prusik, TCP 8095/8097 + LAN protocols)
       |                           |                 |
       | Open Subsonic              | AirPlay         | TV protocol TBD
       v                           v                 v
    Navidrome                  LG C3 salon        TV oficina
          \--> ReSpeaker Sendspin (experimental; separate validation)
```

**Separation of responsibilities:** Assist owns commands and home entities;
Music Assistant owns the catalog and playback queues; ESPHome owns the
satellite's microphone, local wake word and speaker. TV remote microphones
are not assumed accessible even on rooted webOS devices.

## Phases and gates

### Phase A — GitOps foundation (implemented)

- Music Assistant StatefulSet, pinned stable image, host networking on
  `prusik`, ClusterIP Service, internal OAuth2-protected UI Ingress.
- PVC and snapshots using existing ZFS/Argo patterns; no second library mount.
- ReSpeaker XVF3800 kitchen firmware file with five upstream remote packages
  and matching `respeaker_ref` tag; Vault-backed Wi-Fi/API/OTA credentials.
- Preserve existing /api/prometheus external ingress restriction on master.
- Longer termination grace for Home Assistant only; do not change
  AppDaemon/Whisper/Piper defaults.
- Static CI for rendered Helm resources and ESPHome config invariants.

**Merge gate:** Helm lint/template green, static checks green, review host
ports / direct 8095 access policy and ZFS volume placement. ArgoCD currently
auto-syncs `apps/*`; merging creates the service without extra approval.

### Phase B — audio discovery and onboarding (manual validation)

1. On `prusik`: check `ss -lntup` for 8095/8097 collisions;
   verify mDNS advertisements for LG C3 and office TV, with VLAN mapping
   and pfSense routing/firewall.
2. Set up Navidrome provider with a dedicated Open Subsonic account
   in the MA UI. Verify tracks are indexed.
3. Pair LG C3 using AirPlay; pair the second TV only after reading its
   model and capabilities. Confirm direct player-to-server TCP access.
4. Add Music Assistant integration using cluster service URL in HA and
   assign verified `media_player.*` entities to HA areas.
5. Test that screen-off audio on C3 works before automating it. Do not
   equate TV standby with screen-off.

**Gate:** per-device play, pause, resume, volume, reconnection and
Navidrome source quality tested. Record whether playback uses AirPlay,
Chromecast, DLNA or another protocol.

### Phase C — kitchen voice (firmware implemented, hardware pending)

1. Connect the XVF3800/XIAO ESP32-S3 by USB for first flash. The remote
   package updates XMOS DSP firmware if needed; do not interrupt power.
2. Verify `esphome config` and `esphome compile` with the deployed ESPHome
   version and secrets on the intended build host. Flash once over USB;
   later changes use OTA. No remote flash can be completed without hardware.
3. Add the device in HA's ESPHome integration; assign Kitchen area.
   Expose the speaker/MA players to Assist and select the Spanish pipeline
   (Whisper + Piper). Wake word runs on the device, so the central
   OpenWakeWord server is not required for this satellite.
4. Validate hardware mute, wake-up at different distances, timer,
   lights and voice-initiated kitchen music. Test with extractor and TV
   noise, and verify audio playback does not retrigger wake word.
5. Connect an appropriate active speaker to the ReSpeaker for actual music;
   the 5W speaker output is intended for modest near-field audio.

**Gate:** reliable wake, command understanding and TTS in Spanish with
measured end-to-end latency, and working hardware mute.

### Phase D — synced multiroom (not enabled by default)

The pinned upstream XVF3800 packages already include a Sendspin source and
player. Do not add a second Sendspin component to the firmware. Validate
compatibility with the **actual** Music Assistant server version and
ReSpeaker integration; a newer/beta MA may be needed for some features.
Do not migrate the stable MA database to a beta image in place.

- Independent room playback is required; synchronized groups are optional.
- Verify whether LG C3 (AirPlay) can group with Sendspin at acceptable drift.
- Add custom sentences only for unsupported actions (group, shuffle, move).
- Test announcements during music, ducking, stop words and recovery.
- If incompatibility persists, preserve independent playback rather than
  adding brittle transcoding or forwarding hacks.

### Phase E — operations and SLOs

- Continue using ZFS snapshots and existing HA/ESPHome monitoring.
- Add instrumentation only after confirming available upstream metrics:
  connected players, audio availability, STT/TTS latency, wake errors,
  disconnect/reconnect counts. Avoid publishing metrics through external
  endpoints.
- Optional LLM agent is outside the critical path. Native Assist intents
  should handle routine commands even during an Ollama outage.

## Network and security decisions

| Concern | Decision |
|---|---|
| MA in Kubernetes | Best-effort, hostNetwork on `prusik`; not upstream-supported |
| UI access | Internal TLS + oauth2-proxy; same existing Navidrome user group |
| Audio transports | Direct LAN IP/ports; never the Ingress or Kubernetes ClusterIP |
| Discovery | mDNS/SSDP works only inside its L2 domain by default |
| Cross-VLAN | Reflect selectively on pfSense *if needed*, allow stateful return traffic |
| Direct host UI | Restrict via pfSense/host firewall: bypasses the Ingress auth |
| Secrets | Dedicated Navidrome account, configured in MA UI; ESPHome secrets in Vault |
| Storage | Dedicated RWO PVC, host pinned; no duplicating Navidrome music |
| TV microphones | Not supported by assumption; separately researched experiment |

## Verification checklist

- [ ] ArgoCD sync succeeded and PVC is bound to `prusik`
- [ ] `GET /` from the Service and the protected UI Ingress is healthy
- [ ] No unrelated process occupied TCP 8095/8097; correct advertised host IP
- [ ] Navidrome Open Subsonic search works; credentials stored outside Git
- [ ] LG C3 plays from Music Assistant and handles screen-off test
- [ ] Office TV model/protocol discovered and playback validated
- [ ] HA Music Assistant integration installed and players assigned to areas
- [ ] ESPHome config/compile passed for kitchen and firmware flashed via USB
- [ ] Hardware mute, Spanish Assist commands, timer, music and TTS pass
- [ ] No unintended LAN/WAN exposure; 8095/8097 firewall reviewed
- [ ] Volume persistence and media playback recover after pod restart
- [ ] Optional multiroom grouping meets measured drift requirements

## References

- [Music Assistant installation and host networking](https://www.music-assistant.io/installation/)
- [Music Assistant + Home Assistant](https://www.music-assistant.io/integration/)
- [Music Assistant voice control](https://www.music-assistant.io/integration/voice/)
- [XVF3800 ESPHome upstream pinned integration](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration/tree/2026.10.4)
- [Existing ESPHome configuration](../../apps/esphome/config/README.md)
