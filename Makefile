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

# MP_COVER_SUBPROCESS makes the suite run each script under coverage too. Most
# of it drives them as subprocesses, which a plain run cannot see -- without it
# the report understates the scripts by roughly two thirds. COVERAGE_FILE is
# absolute because those subprocesses start in throwaway repositories, and each
# writes its own data file beside it.
#
# The floor is per file, not per project: a project total hides a hole. Same
# script CI runs, so local and CI cannot drift apart.
coverage:
	rm -f .coverage .coverage.*
	MP_COVER_SUBPROCESS=1 COVERAGE_FILE=$(CURDIR)/.coverage $(PYTHON) -m pytest --cov --cov-report= -q
	COVERAGE_FILE=$(CURDIR)/.coverage $(PYTHON) -m coverage report
	COVERAGE_FILE=$(CURDIR)/.coverage $(PYTHON) tests/check_coverage.py --min 90

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
