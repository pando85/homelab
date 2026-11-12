# Single place that pins the ESPHome CLI used by flash-device/device-logs. Keep it in step with the
# dashboard image tag in ../values.yaml (Renovate bumps both in one PR, see .github/renovate.json5).
# renovate: datasource=docker depName=ghcr.io/esphome/esphome
ESPHOME_VERSION ?= 2026.10.2
ESPHOME ?= uvx --from 'esphome==$(ESPHOME_VERSION)' --with pillow esphome
DEVICE_DOMAIN ?= iot.grigri
FLASH_KUBE_CONTEXT ?= $(or $(KUBE_CONTEXT),grigri)

# Extra files a target creates and with_secrets removes on exit (set per target).
CLEANUP ?=

# Shell prologue: write secrets.yaml (mode 0600) and always shred it on exit, Ctrl-C included.
define with_secrets
set -eu; umask 077; \
if [ -z "$(DEVICE)" ] || [ ! -f "$(DEVICE).yaml" ]; then \
	echo "Usage: make $@ DEVICE=<name> (needs ./<name>.yaml)"; exit 1; \
fi; \
trap 'shred -u secrets.yaml 2>/dev/null || rm -f secrets.yaml; rm -f $(CLEANUP)' EXIT; \
trap 'exit 130' INT TERM HUP; \
kubectl --context=$(FLASH_KUBE_CONTEXT) -n esphome get secret esphome-secrets \
	-o jsonpath='{ .data.secrets\.yaml }' | base64 -d > secrets.yaml; \
[ -s secrets.yaml ] || { echo "Error: empty secrets.yaml"; exit 1; };
endef

.DEFAULT: help
.PHONY: help
help:	## Show this help menu.
	@echo "Usage: make [TARGET ...]"
	@echo ""
	@@egrep -h "#[#]" $(MAKEFILE_LIST) | sed -e 's/\\$$//' | awk 'BEGIN {FS = "[:=].*?#[#] "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""

.PHONY: requirements
requirements:	## install requirements
	@echo Install requirements
	@pip install --upgrade -r requirements.txt > /dev/null

.PHONY: secrets.yaml
secrets:	## update secrets file
secrets:
	@if [ -z "$(KUBE_CONTEXT)" ]; then \
		echo "Error: KUBE_CONTEXT is not set."; \
		exit 1; \
	fi
	kubectl --context=$(KUBE_CONTEXT) -n esphome get secret esphome-secrets -o jsonpath='{ .data.secrets\.yaml }' \
		| base64 -d > secrets.yaml

.PHONY: upload
%.yaml:	## upload target file
%.yaml: requirements secrets
	esphome run $@

.PHONY: logs
logs:	## read logs for FILE target. E.g.: make logs FILE='<my device>.yaml'
logs:	requirements secrets
	@if [ -z "$(FILE)" ]; then \
		echo "Usage: make logs FILE='<my device>.yaml'"; \
		exit 1; \
	fi
	esphome logs "$(FILE)"

.PHONY: flash-device
flash-device:	## OTA-flash one device with the pinned ESPHome, then shred secrets. E.g.: make flash-device DEVICE=mini-switch-office-a
flash-device:
	@$(with_secrets) \
	$(ESPHOME) run "$(DEVICE).yaml" --no-logs --device "$(DEVICE).$(DEVICE_DOMAIN)"

.PHONY: device-logs
device-logs:	## tail logs of one device over the API (non-interactive). E.g.: make device-logs DEVICE=mini-switch-office-a
device-logs:
	@$(with_secrets) \
	$(ESPHOME) logs "$(DEVICE).yaml" --device "$(DEVICE).$(DEVICE_DOMAIN)"

.PHONY: flash-device-migrate
flash-device-migrate: CLEANUP := .ota-migrate.yaml
flash-device-migrate:	## first OTA of a device whose running firmware cannot encrypt OTA yet (before 2026.9.0), see planning/esphome-security-hardening-todo.md. E.g.: make flash-device-migrate DEVICE=rack-controller
	@$(with_secrets) \
	printf '%s\n' 'packages:' '  device: !include $(DEVICE).yaml' '' 'ota:' '  - platform: esphome' \
		'    password: !secret ota_password' '    encryption:' '      allow_plaintext_upload: true' > .ota-migrate.yaml; \
	$(ESPHOME) run .ota-migrate.yaml --no-logs --device "$(DEVICE).$(DEVICE_DOMAIN)"

.PHONY: local-dashboard
local-dashboard:	## run ESPHome dashboard in local
	docker run --rm -v "$${PWD}":/config -p 6052:6052 -e ESPHOME_DASHBOARD_USE_PING=true -it \
		ghcr.io/esphome/esphome
