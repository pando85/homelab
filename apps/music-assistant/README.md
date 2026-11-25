# Music Assistant

GitOps deployment of [Music Assistant](https://www.music-assistant.io/installation/)
for Home Assistant voice control and three-room music playback.

## Scope and decisions

- Pin Music Assistant to the stable `2.10.9` container digest. Do not use
  `latest` or mix beta/stable databases on the same PVC.
- Use `hostNetwork: true`, `dnsPolicy: ClusterFirstWithHostNet`, and pin to
  `prusik`. AirPlay, DLNA and Chromecast discovery and playback cannot be
  reliably handled through normal Kubernetes pod networking and HTTP Ingress.
- Expose the **UI only** at
  `https://music-assistant.internal.grigri.cloud`, reusing the existing
  `navidrome-users@idm.grigri.cloud` OAuth2 group.
- Keep Music Assistant's state under `/data` on a non-prunable 4 GiB PVC
  with ZFS snapshot scheduling. Music files stay in Navidrome; do **not**
  mount the 200 GiB music library twice.
- Do not add a custom media proxy, a new LLM, a dedicated STT service, or
  mDNS reflectors before validating actual device/VLAN connectivity.

## Security and network prerequisites

> Kubernetes is **not** an officially supported deployment target for Music
> Assistant. If host networking fails to discover the TVs, the documented,
> supported fallback is Podman/Docker on the same physical host/network.

1. Verify no listener on `prusik` already binds TCP 8095 or 8097. Host
   networking intentionally shares these ports and dynamic UDP/TCP player
   ports. Server may choose a different stream port if 8097 is taken.
2. Confirm `prusik` can discover and reach both TVs over the appropriate
   VLANs (AirPlay/mDNS, DLNA/SSDP or Chromecast). mDNS packets do not normally
   cross VLANs; reflect only selected service advertisements at pfSense if
   needed, and allow return traffic. Do not blindly open IoT VLANs.
3. Allow player devices to reach the host's **LAN IP** on TCP 8095 and 8097
   (or the actual stream port reported in MA logs); allow the protocol-specific
   player sockets. The protected Ingress cannot act as an audio transport.
4. **Host-networking bypasses Kubernetes NetworkPolicy** and exposes the
   web server on `prusik:8095` to any network allowed by pfSense/host
   firewall. OAuth2 guards only the named Ingress. Restrict direct access to
   trusted LAN subnets; use Music Assistant's supported login configuration
   for interactive clients. Never expose 8095/8097 to WAN.
5. Confirm the local ZFS PVC is provisioned on `prusik` before treating
   the StatefulSet as ready. `ReadWriteOnce` is intentionally single-writer.

## Bootstrap once the pod is running

```sh
kubectl -n music-assistant get pods,pvc,svc,ingress
kubectl -n music-assistant logs music-assistant-main-0 -c main --tail=100
kubectl -n music-assistant port-forward svc/music-assistant-main 8095:8095
# Open http://127.0.0.1:8095 to diagnose the UI without DNS/mDNS assumptions
```

1. Open Music Assistant's UI. Add **Open Subsonic** as a music provider using
   `http://navidrome.navidrome.svc.cluster.local:4533` and a dedicated
   Navidrome user (created interactively; no passwords in Git).
   Verify library sync, searching and playback.
2. Let MA discover the LG C3 with the AirPlay provider. Discover the office
   TV using its **actual** supported protocol (model remains unverified).
   Do not commit speculative device IPs, credentials or entity IDs.
3. In Home Assistant, add the **Music Assistant** integration via
   Settings > Devices & Services and point it at
   `http://music-assistant-main.music-assistant.svc.cluster.local:8095`.
   Verify the players appear and can be controlled from HA.
4. Assign players to existing HA areas (Kitchen, Living room, Office as
   appropriate). Expose the relevant players to Assist; enable the
   built-in Search and Play intent with the local Spanish pipeline.
   For complex group/shuffle commands add custom sentences *after* baseline
   playback works.
5. Check advertised server URLs in Music Assistant > Settings > System >
   Webserver. Devices must receive a LAN-reachable address, not a
   ClusterIP, the OAuth2 Ingress hostname or a container loopback URL.

## Acceptance criteria (manual; do not claim until observed)

- UI responds internally, and the authenticated Ingress works.
- Navidrome tracks are discoverable with no duplicate library mount.
- Each TV works independently: play, stop, seek, volume; power/screen-off
  behavior tested separately (TV vendor integration dependent).
- `prusik` is reachable from the player VLAN for audio streaming.
- Home Assistant's Music Assistant integration discovers both MA players.
- Restarting the pod preserves accounts, library and queues on the PVC.
- No user credentials appear in the Git history or pod logs.

## Rollback

ArgoCD revert removes the workload/Ingress, not the persistent PVC
(`argocd.argoproj.io/sync-options: Prune=false`). Preserve snapshots
before changing Music Assistant version. Do **not** point another MA instance
at this PVC, nor reuse a beta database on stable. Removing the MA integration
from HA is a separate UI action.

See [voice and multiroom implementation plan](../../docs/deployment/home-assistant-voice-multiroom.md).
