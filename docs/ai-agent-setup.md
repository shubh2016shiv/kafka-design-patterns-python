# AI Agent + Quality Setup Guide

This guide explains the repository-standard setup for:
- cross-platform agent instructions
- coding quality enforcement with Ruff
- required validation commands before commit/PR

## 1. Canonical Instruction File Strategy

Use this pattern:
- `AGENTS.md` = canonical instruction source
- `AGENT.md` = legacy compatibility wrapper to `AGENTS.md`
- `CLAUDE.md` = Claude compatibility wrapper to `AGENTS.md`
- `.github/copilot-instructions.md` = Copilot wrapper to `AGENTS.md`
- `.cursor/rules/*.mdc` = Cursor wrapper to `AGENTS.md`

Why:
- avoids duplicated policy drift
- works across major agentic coding platforms
- keeps one source of truth for standards

## 2. Mandatory Local Quality Gate

Run this sequence before commit/PR:

1. `ruff check config common producers consumers test --fix --no-cache`
2. `ruff format config common producers consumers test`
3. `ruff check config common producers consumers test --no-cache`
4. `python -m unittest test.producer.test_simple_producer`

If any step fails, the change is not complete.

Note:
- Full-suite `python -m unittest` is reserved for later once package-import wiring is standardized.

## 3. Ruff Enforcement Model (Phased)

Current model:
- **Blocking quality gate** on phased maintained scope:
- `config common producers consumers test`
- **Non-blocking visibility report** on full repository:
- `ruff check . --no-cache`

Why phased:
- enforces quality where active development happens
- avoids blocking all delivery on historical lint debt
- supports incremental tightening in follow-up cleanup PRs

## 4. Automation Files

- `pyproject.toml`
- Ruff configuration and legacy per-file ignores for phased adoption
- `.pre-commit-config.yaml`
- local hook automation (`ruff-check --fix`, `ruff-format`)
- `.github/workflows/quality.yml`
- CI quality gate + non-blocking debt visibility job

## 5. Educational + Professional Coding Expectations

For non-trivial modules:
- include ASCII flow diagrams
- include stage/sub-stage flow markers (`Stage 1.0`, `1.1`, ...)
- explain non-obvious imports and fallback behavior
- explain config defaults and tuning direction
- explain Pydantic model intent/fields/validators when used

## 6. Reuse Checklist for Future Repositories

1. Copy `AGENTS.md` as canonical source.
2. Add thin wrappers for target tools (`AGENT.md`, `CLAUDE.md`, Copilot, Cursor).
3. Add `pyproject.toml` Ruff config with phased-scope strategy.
4. Add `.pre-commit-config.yaml`.
5. Add CI workflow with blocking phased checks and non-blocking full-debt report.
6. Add this guide (or equivalent) and link it from root `README.md`.
