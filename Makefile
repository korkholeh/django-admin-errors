SHELL := /bin/sh

READY_URL := http://127.0.0.1:8000/admin/login/
PIDFILE := .autodev/e2e-server.pid
ADMIN_ERRORS_TEST_PG_URL ?= postgres://postgres:postgres@localhost:5432/admin_errors_test

.PHONY: e2e-up e2e-down test test-pg pg-up pg-down lint build demo demo-pg

e2e-up:
	@test -f demo/manage.py || { echo "e2e: demo/ not present yet, skipping"; exit 0; }; \
	uv run python -c "import urllib.request; urllib.request.urlopen('$(READY_URL)', timeout=2)" >/dev/null 2>&1 && { echo "e2e: server already answering on $(READY_URL)"; exit 0; }; \
	uv run python demo/manage.py migrate --noinput || { echo "e2e: migrate failed"; exit 1; }; \
	uv run python demo/manage.py demo_seed --issues 40 --days 30 --reset || { echo "e2e: demo_seed failed"; exit 1; }; \
	uv run python -m playwright install chromium || { echo "e2e: playwright install failed"; exit 1; }; \
	(uv run python demo/manage.py runserver 127.0.0.1:8000 --noreload >/tmp/admin-errors-e2e-server.log 2>&1 & echo $$! > $(PIDFILE)); \
	i=0; \
	while [ $$i -lt 45 ]; do \
		uv run python -c "import urllib.request; urllib.request.urlopen('$(READY_URL)', timeout=2)" >/dev/null 2>&1 && exit 0; \
		i=`expr $$i + 1`; \
		sleep 1; \
	done; \
	echo "e2e: server never became ready on $(READY_URL)"; exit 1

e2e-down:
	@test -f $(PIDFILE) && kill `cat $(PIDFILE)` 2>/dev/null; rm -f $(PIDFILE); exit 0

test:
	uv run pytest -q

pg-up:
	docker compose -f demo/docker-compose.yml up -d --wait

pg-down:
	docker compose -f demo/docker-compose.yml down

test-pg:
	@$(MAKE) pg-up && { \
		DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=$(ADMIN_ERRORS_TEST_PG_URL) uv run pytest -q; \
		st=$$?; $(MAKE) pg-down; exit $$st; \
	}

lint:
	uv run ruff check . && uv run ruff format --check . \
		&& uv run python -m django check --settings=tests.settings \
		&& uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings

build:
	uv run python -m build && uv run twine check dist/*

demo:
	uv run python demo/manage.py migrate --noinput
	uv run python demo/manage.py demo_seed --issues 40 --days 30 --reset
	uv run python demo/manage.py runserver 127.0.0.1:8000

demo-pg:
	@$(MAKE) pg-up
	DEMO_DB=postgres uv run python demo/manage.py migrate --noinput
	DEMO_DB=postgres uv run python demo/manage.py demo_seed --issues 40 --days 30 --reset
	DEMO_DB=postgres uv run python demo/manage.py runserver 127.0.0.1:8000
