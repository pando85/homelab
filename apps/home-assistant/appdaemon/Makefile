ENV_FILE ?= .env.list
LOG_LEVEL ?= INFO

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
	@pip install appdaemon > /dev/null
	@pip install -r requirements.txt > /dev/null

.PHONY: requirements_test
requirements_test: requirements
requirements_test:	## install requirements test
	@echo Install requirements test
	@pip install -r requirements_test.txt > /dev/null

.PHONY: run
run: DOCKER_EXEC_ARGS ?=
run:	## run in local docker
	@if [ ! -f $(ENV_FILE) ]; then \
        echo "Cannot find environment file at $(ENV_FILE)" >&2; \
        exit 1; \
    fi; \
	docker run --rm -it -p 5050:5050 --env-file $(ENV_FILE) -v "$${PWD}":/conf \
		acockburn/appdaemon:latest -D $(LOG_LEVEL) $(DOCKER_EXEC_ARGS)

.PHONY: test
test: requirements_test
test:	## run tests
	pytest apps
