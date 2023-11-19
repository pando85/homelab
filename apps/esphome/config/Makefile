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
	@pip install -r requirements.txt > /dev/null

.PHONY: secrets.yaml
secrets:	## update secrets file
secrets:
	kubectl -n esphome get secret esphome-secrets -o jsonpath='{ .data.secrets\.yaml }' \
		| base64 -d > secrets.yaml

.PHONY: upload
%.yaml:	## upload target file
%.yaml: requirements secrets
	esphome run $@

.PHONY: local-dashboard
local-dashboard:	## run ESPHome dashboard in local
	docker run --rm -v "$${PWD}":/config -p 6052:6052 -e ESPHOME_DASHBOARD_USE_PING=true -it \
		ghcr.io/esphome/esphome
