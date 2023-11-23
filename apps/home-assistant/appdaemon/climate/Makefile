.DEFAULT: help
.PHONY: help
help:	## Show this help menu.
	@echo "Usage: make [TARGET ...]"
	@echo ""
	@@egrep -h "#[#]" $(MAKEFILE_LIST) | sed -e 's/\\$$//' | awk 'BEGIN {FS = "[:=].*?#[#] "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo ""

ENV_FILE=.env.list

.PHONY: requirements
requirements:	## install requirements
	@echo Install requirements
	@pip install -r requirements.txt > /dev/null

.PHONY: run
run:	## run in local docker
	@if [ ! -f $(ENV_FILE) ]; then \
        echo "Cannot find environment file at $(ENV_FILE)" >&2; \
        exit 1; \
    fi; \
	docker run --rm -it -p 5050:5050 --env-file $(ENV_FILE) -v "$${PWD}":/conf \
		acockburn/appdaemon:latest -D DEBUG
