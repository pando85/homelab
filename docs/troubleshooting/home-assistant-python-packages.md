# Home Assistant Python Package Management

## Overview

The `home-operations/home-assistant` image uses a virtual environment at `/config/.venv` for
user-installed packages, while system packages live in `/usr/local/lib/python3.14/site-packages/`.

## Architecture

```
/usr/local/lib/python3.14/site-packages/  # System packages (read-only, from image)
/config/.venv/lib/python3.14/site-packages/  # Venv packages (writable, persistent)
```

The entrypoint script:
1. Creates the venv with `uv venv --system-site-packages`
2. Activates it via `source /config/.venv/bin/activate`
3. Runs `python3 -m homeassistant`

## Package Resolution

Python loads packages in this order:
1. Venv site-packages (`/config/.venv/lib/python3.14/site-packages/`)
2. System site-packages (`/usr/local/lib/python3.14/site-packages/`)

When using `/config/.venv/bin/python3`, the venv takes precedence. When using system `python3`,
only system packages are loaded.

## Managing Packages

### Install a package

```bash
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -- \
  /config/.venv/bin/uv pip install <package>==<version>
```

### Check installed version

```bash
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -- \
  /config/.venv/bin/python3 -c "import <package>; print(<package>.__version__)"
```

### List installed packages

```bash
kubectl --context=grigri exec -n home-assistant home-assistant-hass-0 -- \
  /config/.venv/bin/uv pip list
```

## Important Notes

- The venv persists across pod restarts (stored on PVC at `/config`)
- System packages cannot be modified (read-only image layers)
- Use `--target /config/.venv/lib/python3.14/site-packages` if `uv pip install` fails with
  permission errors on system directories
- Restart the pod after installing packages: `kubectl delete pod home-assistant-hass-0 -n home-assistant`
