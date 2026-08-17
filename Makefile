.PHONY: help setup bootstrap schema seed test test-schema test-wrapper test-memory \
        pg-up pg-down pg-portable pg-portable-stop verify gate gate-pg gate-crdb clean

PY ?= python

help:
	@echo "RaceLab targets:"
	@echo "  make setup             install Python dependencies"
	@echo "  make bootstrap         schema + seed + full test suite, from nothing"
	@echo "  make schema            create the schema on both backends"
	@echo "  make seed              seed accounts and the memory corpus (needs Bedrock)"
	@echo "  make test              run every test"
	@echo "  make test-schema       verify a clean clone gets a usable vector index"
	@echo "  make test-wrapper      verify the two arms differ by exactly one thing"
	@echo "  make test-memory       verify retrieval is causal and index-backed"
	@echo "  make test-arms         verify the four arms, and decompose the ablation"
	@echo "  make sweep             the swept experiment -> results/sweep.md"
	@echo "  make sweep-smoke       2 runs per cell, to check it works"
	@echo "  make pg-up             start the PostgreSQL 16 control arm (Docker)"
	@echo "  make pg-down           stop and remove it"
	@echo "  make pg-portable       same arm without Docker, from portable binaries"
	@echo "  make pg-portable-stop  stop the portable server"
	@echo "  make verify            Phase 0: probe the live clusters, write docs/VERIFIED.md"
	@echo "  make gate              Phase 1: run the gate against both backends"
	@echo "  make clean             remove generated result files"

setup:
	$(PY) -m pip install -r requirements.txt

# One command from a fresh clone to a working, verified install.
bootstrap: schema seed test

schema:
	$(PY) -m racelab.schema --backend crdb
	$(PY) -m racelab.schema --backend pg

seed:
	$(PY) scripts/seed.py --reset

# test-schema runs first and against its own throwaway database, so it is the
# one test that still means something if seeding is broken or Bedrock is
# unavailable.
test: test-schema test-wrapper test-memory test-arms

test-schema:
	$(PY) scripts/verify_clean_clone.py

test-wrapper:
	$(PY) scripts/test_wrapper.py

test-memory:
	$(PY) scripts/test_memory_causality.py

test-arms:
	$(PY) scripts/test_arms.py

sweep:
	$(PY) scripts/run_sweep.py --runs 20

sweep-smoke:
	$(PY) scripts/run_sweep.py --smoke --agents 8

pg-up:
	docker compose up -d postgres
	@echo "waiting for postgres to accept connections..."
	@docker compose exec -T postgres sh -c 'until pg_isready -U racelab -d racelab; do sleep 1; done'

pg-down:
	docker compose down -v

# Docker-free path to the same stock PostgreSQL 16. Needs vendor/pg16.zip,
# the EDB binaries-only distribution.
pg-portable:
	$(PY) scripts/pg_portable.py init

pg-portable-stop:
	$(PY) scripts/pg_portable.py stop

verify:
	$(PY) spike/verify_env.py

gate: gate-pg gate-crdb

gate-pg:
	$(PY) spike/gate.py sweep --backend pg
	$(PY) spike/gate.py scale --backend pg

gate-crdb:
	$(PY) spike/gate.py sweep --backend crdb
	$(PY) spike/gate.py scale --backend crdb

clean:
	rm -rf results/gate/*.json
