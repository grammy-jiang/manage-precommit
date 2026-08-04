.PHONY: help test coverage lint format typecheck verify install uninstall clean

# Overridable so the targets work from a bare checkout, a venv, or CI without
# each caller having to know which interpreter is on PATH.
PYTHON ?= python3
SKILL := src/manage_precommit/skill

help:
	@echo "test        run the test suite"
	@echo "lint        ruff check + format check"
	@echo "format      ruff format (writes)"
	@echo "typecheck   mypy"
	@echo "verify      lint + typecheck + test  (run before shipping a change)"
	@echo "coverage    the same suite under coverage"
	@echo "install     symlink the skill from this checkout into ~/.claude/skills/"
	@echo "uninstall   remove that symlink again"

test:
	$(PYTHON) -m pytest

coverage:
	rm -f .coverage .coverage.*
	COVERAGE_FILE=$(CURDIR)/.coverage $(PYTHON) -m pytest --cov --cov-report= -q
	COVERAGE_FILE=$(CURDIR)/.coverage $(PYTHON) -m coverage report

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck:
	$(PYTHON) -m mypy

verify: lint typecheck test

# Points the link at the working tree, so edits take effect on the next Claude
# Code restart with no rebuild. The link targets the skill directory itself --
# the checkout and the installed tree are the same paths, so nothing is remapped.
install:
	ln -sfn "$(CURDIR)/$(SKILL)" "$(HOME)/.claude/skills/manage-precommit"
	@echo "linked $(HOME)/.claude/skills/manage-precommit -> $(CURDIR)/$(SKILL)"

uninstall:
	rm -f "$(HOME)/.claude/skills/manage-precommit"

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache .coverage .coverage.*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
