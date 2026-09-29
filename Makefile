# gnomon -- pure standard library, no dependencies, no network.
#
# Every target is a thin wrapper around a plain python command. If make is not
# installed, run the command shown in the help text instead; the README lists
# them all.

PY ?= python

.PHONY: help run demo test samples check clean

help:
	@echo "make run      - build the samples and run the guided demonstration"
	@echo "                 (python main.py)"
	@echo "make test     - run the test suite"
	@echo "                 (python -m pytest tests -q)"
	@echo "make samples  - regenerate the synthetic sample databases"
	@echo "                 (python samples/build_samples.py)"
	@echo "make check    - samples, then tests, then the demo: the full gate"
	@echo "make clean    - remove generated samples and caches"

run demo:
	$(PY) main.py

test:
	$(PY) -m pytest tests -q

samples:
	$(PY) samples/build_samples.py

check: samples test run

clean:
	$(PY) tools/clean.py
