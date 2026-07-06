# Home Assistant aiohttp WebSocket Incompatibility

## Problem

Home Assistant 2026.7.1 logs show repeated WebSocket errors:

```
TypeError: WebSocketResponse.__init__() got an unexpected keyword argument 'decode_text'
```

The WebSocket API is broken, preventing frontend and mobile app connections.

## Root Cause

The `home-operations/home-assistant:2026.7.1` image ships with incompatible dependency versions:

- **Home Assistant 2026.7.1** uses `decode_text=False` in `websocket_api/http.py:98`
- **aiohttp 3.14.1** (system package) removed the `decode_text` parameter
- **aiohttp 3.14.0** has the `decode_text` parameter
- **aiohttp 3.13.3** (venv package) also has it

The image's entrypoint creates a venv with `--system-site-packages`, but the running process
uses system Python which loads aiohttp 3.14.1 from system site-packages, not the venv's 3.13.3.

This is a bug in the `home-operations/home-assistant` image packaging.

## How to Diagnose

```bash
# Check for the error in logs
kubectl --context=grigri logs home-assistant-hass-0 -n home-assistant | grep decode_text

# Check which aiohttp version is loaded
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -- \
  python3 -c "import aiohttp; print(aiohttp.__version__); print(aiohttp.__file__)"

# Check venv aiohttp version
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -- \
  /config/.venv/bin/python3 -c "import aiohttp; print(aiohttp.__version__)"

# Check if decode_text parameter exists
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -- \
  /config/.venv/bin/python3 -c "from aiohttp import web; import inspect; print('decode_text' in inspect.signature(web.WebSocketResponse.__init__).parameters)"
```

## Fix

Install aiohttp 3.14.0 in the venv (the version that has `decode_text`):

```bash
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -- \
  /config/.venv/bin/uv pip install --target /config/.venv/lib/python3.14/site-packages 'aiohttp==3.14.0'

kubectl --context=grigri delete pod home-assistant-hass-0 -n home-assistant
```

The fix persists across pod restarts because `/config` is on a PVC.

## Prevention

This issue will recur when the image is updated to a new HA version. Check if the upstream image
fixes the dependency mismatch. If not, repeat the fix after updating the image tag.

See [Home Assistant Python Package Management](home-assistant-python-packages.md) for details on
managing packages in the HA container.
