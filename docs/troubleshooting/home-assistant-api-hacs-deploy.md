# Home Assistant API Access and HACS Deploys for Agents

## Overview

Agents can drive Home Assistant headlessly (no browser/Playwright needed) through the REST and
websocket APIs, using a short-lived token obtained with the normal login flow. This is also how to
deploy a change to a HACS-installed custom integration without writing into the pod.

## Getting a Token

Credentials live in `pass` as `web/hass.grigri.cloud` (first line is the password, user `agil`; no
MFA). Never print the password or token; write the token to a `0600` file in the scratchpad and
delete it afterwards.

```bash
H=https://hass.grigri.cloud; CID=https://hass.grigri.cloud/
FID=$(curl -s -X POST $H/auth/login_flow -H 'Content-Type: application/json' \
  -d "{\"client_id\":\"$CID\",\"handler\":[\"homeassistant\",null],\"redirect_uri\":\"${CID}?auth_callback=1\"}" \
  | jq -r .flow_id)
CODE=$(pass show web/hass.grigri.cloud | head -1 \
  | jq -R --arg u agil --arg c "$CID" '{username:$u,password:.,client_id:$c}' \
  | curl -s -X POST $H/auth/login_flow/$FID -H 'Content-Type: application/json' -d @- \
  | jq -r '.result // empty')
umask 077
curl -s -X POST $H/auth/token -d grant_type=authorization_code -d code=$CODE -d client_id=$CID \
  | jq -r .access_token > "$TOKEN_FILE"
```

REST examples (`Authorization: Bearer $(cat $TOKEN_FILE)`):

- `GET /api/config` — HA version
- `GET /api/states/<entity_id>` — state and attributes (e.g. `supported_color_modes`)
- `POST /api/services/light/turn_on` with `{"entity_id": "...", "color_temp_kelvin": 4000}`
- `POST /api/services/homeassistant/restart` — restart; `/api/` returns 503 for a few seconds
- `GET /api/history/period/<iso>?filter_entity_id=...` — check whether an entity ever reached a state

## Deploying a HACS Custom Integration Change

HACS-installed integrations (e.g. `pando85/localtuya`) are plain copies under
`/config/custom_components/<name>/` on the `home-assistant-config` PVC — not git checkouts, and
not managed by the chart. Repos without releases track the default branch commit. To deploy:

1. Merge/push the change to the integration repo's default branch.
2. Over the websocket API (`wss://hass.grigri.cloud/api/websocket`, send
   `{"type":"auth","access_token":...}` first), with the repo id from
   `/config/.storage/hacs.repositories`:
   - `{"type":"hacs/repository/refresh","repository":"<id>"}`
   - `{"type":"hacs/repository/info","repository_id":"<id>"}` — expect `available_version` =
     new commit, `pending_upgrade: true`
   - `{"type":"hacs/repository/download","repository":"<id>"}` — omit `version` to take the
     default branch
   Command names were checked against HACS 2.0.5 (`custom_components/hacs/websocket/repository.py`);
   re-check there if a call fails after a HACS upgrade.
3. Verify the files in the pod match the repo (read-only `kubectl exec ... cat` + `diff`).
4. Restart HA (`homeassistant.restart`) and re-test.

`hacs/repository/info` is the source of truth for the installed version: after a websocket
download, `.storage/hacs.repositories` keeps `last_commit` but may drop `version_installed`.

Do not copy files into the pod with `kubectl exec`/`cp` — HACS would not know about them and would
overwrite them on the next update.

## Driving the UI with Playwright

The Playwright MCP is configured for opencode, not Claude Code. For UI checks (e.g. what payload a
frontend control actually sends) use Python Playwright with the system browser, so no browser
download is needed:

```bash
python -m venv "$SCRATCH/pw-venv" && "$SCRATCH/pw-venv/bin/pip" install playwright
# chromium.launch(executable_path="/usr/bin/chromium", headless=True)
```

Pass the password through an environment variable, never screenshot the login form, and capture
`page.on("websocket")` → `framesent` to see the exact `call_service` payloads. Open a dialog
directly with `https://hass.grigri.cloud/?more-info-entity-id=<entity_id>`.
