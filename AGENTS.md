# AGENTS.md

Canonical instructions for AI coding agents in this repository.

This file is the single source of truth for agent behavior.
Tool-specific files should reference this file instead of duplicating rules.

## 0. Project Mission

Build Kafka patterns that are both:
- educational for learners
- professional for production-minded teams

Code must stay modular, testable, debuggable, maintainable, and explicit about tradeoffs.

## 1. Non-Negotiable Engineering Rules

- Do not implement non-trivial behavior in one giant script.
- Separate concerns into dedicated modules (core logic, clients/adapters, config, demo/entrypoint, types/models).
- Keep entrypoint files thin orchestration layers.
- Preserve stable public API when refactoring internals unless requested otherwise.
- Prefer small, reviewable diffs tied directly to the request.

## 2. Educational Code Requirements (Mandatory)

For any non-trivial workflow:
- Add a module-level ASCII flow diagram.
- Add stage/sub-stage markers in flow-critical paths:
- `Stage 1.0` for major steps
- `Stage 1.1`, `Stage 1.2` for sub-steps
- Add short stage descriptions so readers know:
- where they are in flow
- expected input/state at that point
- expected output/next transition

Comments/docstrings must explain:
- why this code exists
- what inputs are expected
- what outputs/contracts are produced
- production tradeoffs and failure behavior

Avoid comments that only restate syntax.

## 3. Explain Imports, Config, and Models

### Imports
- Explain non-obvious imports and optional dependencies.
- If fallback imports exist, document:
- why fallback is needed
- what behavior changes with fallback
- failure mode when neither dependency is available

### Configuration
- For each important config/default constant, explain:
- what it controls
- why this value is the default
- when/why to tune it for production

### Pydantic Models (when used)
- Explain why the model exists (validation boundary, schema contract, serialization contract).
- Explain key fields, required/optional behavior, and defaults.
- Explain validators and compatibility/versioning assumptions.

## 4. Simplicity and Scope Control

- Implement only what was requested.
- Do not add speculative abstractions.
- Do not refactor unrelated areas.
- If adjacent issues are found, mention them separately; do not silently fix unrelated code.

## 5. Verification and Testing

For non-trivial changes:
- Add/update focused tests.
- Cover happy path + failure path.
- Prefer scenario-based test names/docstrings.
- Keep unit tests independent of live infrastructure using fakes/mocks/protocols.

When integration checks are relevant:
- run them if feasible
- if not feasible, state limitation explicitly

### 5.1 Mandatory Pre-Commit Quality Gate

Before creating a commit or PR, run this sequence in order:

1. `ruff check <phased-scope> --fix --no-cache`
2. `ruff format <phased-scope>`
3. `ruff check <phased-scope> --no-cache`
4. `python -m unittest test.producer.test_simple_producer`

Default `<phased-scope>` for this repository:
- `config common producers consumers test`

Rules:
- Do not skip step 3 after auto-fix; lint must pass cleanly after changes.
- If unit tests fail, do not commit as "done".
- Use `python -m unittest` for full-suite runs only after package-import wiring is standardized.
- In final task summaries, report outcomes of each command explicitly.

## 6. Debuggability and Operations

Every important operation should make it easy to answer:
- what was attempted
- with which key parameters (sanitized)
- what succeeded/failed
- what to check next

Requirements:
- structured return/result objects for multi-step workflows
- actionable error messages
- no secret leakage in logs

## 7. Suggested Instruction Layering

Use this precedence:
1. User request
2. This `AGENTS.md`
3. Tool-specific wrapper files (`CLAUDE.md`, `.github/copilot-instructions.md`, etc.)
4. Nested `AGENTS.md` files for subprojects (if monorepo sections need overrides)

Closest relevant nested `AGENTS.md` can specialize local subproject behavior.
