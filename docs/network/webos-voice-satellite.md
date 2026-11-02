# webOS voice satellite network requirement

Owner-specific notes for the [webOS voice satellite](https://github.com/pando85/webos-voice-satellite)
project: a local-first Home Assistant voice satellite on a rooted LG webOS TV.

## Device

- TV `lg-c3-42` on LAN (VLAN 1), 192.168.76.12 (static DHCP mapping)
- ESPHome native API on TCP 6053. Home Assistant always dials the device.

## Firewall requirement (PENDING)

Status: **not applied** as of 2026-11-02. Diagnosed from the pfSense backup
(`pass grigri/pfsense.config.xml`), which may be stale versus the live firewall.

- HA pod egress is SNATed by Cilium to a DMZ node IP (VLAN 101, 192.168.192.0/24).
- DMZ rule "DMZ to LANBRIDGE" (#26) rejects DMZ to LAN (192.168.76.0/24).
- Per-host passes #18-#21 allow only z800 (192.168.76.20) on 10300, 11434, 19444 and
  hall_tv (192.168.76.11) on 19444.
- ESPHome devices work because they sit on IOT_OFFLINE (VLAN 502), reachable via "Default allow DMZ".

Owner action (manual pfSense UI; pfSense is not managed by GitOps). Firewall > Rules > DMZ,
after #21 and before #26:

- Action Pass, IPv4, TCP
- Source: DMZ net
- Destination: `lg_c3_tv` (192.168.76.12), port `esphome_api_tcp` (6053)
- Description: ESPHome API from HA to LG TV satellite

Optional aliases: host `lg_c3_tv`, port `esphome_api_tcp`.
Rollback: disable or delete the rule. A second TV needs the same rule for its own IP.

## Home Assistant side

- Add the device manually in the ESPHome integration (host = TV, port 6053). The Noise
  encryption key is kept privately by the owner, not in git.
- Kiwi preferred pipeline: `wake_word_id` changed from `alexa_v0.1` to `alexa` on 2026-11-02,
  because openWakeWord 2.1.0 exposes `alexa`. Rollback: set it back to `alexa_v0.1`.
- Kiwi's conversation agent uses Ollama on z800:11434, currently stopped and disabled, so
  conversation fails until it runs.
