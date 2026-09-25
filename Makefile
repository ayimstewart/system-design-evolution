# make up STAGE=3   start stage 3 (remembers it for the other commands)
# make next         go up one stage
STAGE ?= $(shell cat .stage 2>/dev/null || echo 0)
RUN := bash scripts/stage.sh $(STAGE)

.PHONY: up next down reset ps logs load chaos watch stats test

up:
	@echo $(STAGE) > .stage
	@echo "==> stage $(STAGE): $$(ls compose | grep '^0$(STAGE)-' | sed 's/^0.-//; s/.yml//')"
	$(RUN) up -d --build --remove-orphans --wait
	@echo "==> http://localhost:8000"

next:
	@$(MAKE) --no-print-directory up STAGE=$$(( $(STAGE) + 1 ))

down:
	$(RUN) down --remove-orphans

reset:  ## wipe all data (volumes) and go back to stage 0
	bash scripts/stage.sh 9 down -v --remove-orphans
	@rm -f .stage

ps:
	$(RUN) ps

logs:
	$(RUN) logs -f --tail=50 app worker

load:   ## needs k6 (brew install k6), falls back to Docker
	@if command -v k6 >/dev/null; then k6 run loadtest/k6.js; \
	else docker run --rm -i --network host -e BASE_URL=http://localhost:8000 grafana/k6 run - < loadtest/k6.js; fi

chaos:  ## make chaos TARGET=db-replica
	bash scripts/chaos.sh $(or $(TARGET),app)

watch:
	bash scripts/watch.sh

stats:
	@curl -s localhost:8000/debug/stats | python3 -m json.tool

test:
	python3 -m pytest -q
