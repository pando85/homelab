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

.PHONY: local-dashboard
local-dashboard:	## run ESPHome dashboard in local
	docker run --rm -v "$${PWD}":/config -p 6052:6052 -e ESPHOME_DASHBOARD_USE_PING=true -it \
		ghcr.io/esphome/esphome
