PYTHON ?= python3
BIN_DIR ?= $(HOME)/.local/bin
.DEFAULT_GOAL := help

.PHONY: help configure test install reload
help:
	@printf '%s\n' 'make configure  Compile config.yml (no runtime changes)' 'make test       Run isolated tests; no model or weather requests' 'make install    Safely link tmx into ~/.local/bin (no sudo)' 'make reload     Apply colors/plugins to the running private session' 'Override BIN_DIR to choose another user-owned PATH directory.'

configure:
	$(PYTHON) scripts/configure.py

test: configure
	sh -n tmx.sh
	bash -n tmx.sh
	$(PYTHON) -m unittest discover -s tests -v

install: configure
	$(PYTHON) scripts/install.py --bin-dir "$(BIN_DIR)"

reload: configure
	$(PYTHON) scripts/reload.py
