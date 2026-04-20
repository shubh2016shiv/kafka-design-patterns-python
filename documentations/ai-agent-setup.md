# Agent Instruction Files Setup Guide (Platform Agnostic)

This is a general-purpose guide for configuring agent instruction files across modern
agentic coding platforms. It is intentionally not tied to any single repository,
language, or framework.

## Why This Guide Exists

AI coding tools can produce inconsistent behavior when instruction files are duplicated
across different wrappers. The fix is a strict Hub-and-Spoke model:

- One canonical instruction file (the Hub).
- Thin tool-specific wrappers (the Spokes) that only point to the Hub.
- Clear precedence rules for conflict resolution.
- A required local and CI quality gate for deterministic output quality.

## Architecture Overview

```text
Developer/Agent Session
        |
        v
Tool Wrapper File (Spoke)
        |
        v
Canonical AGENTS.md (Hub)
        |
        v
Nested AGENTS.md (optional, nearest scope wins)
        |
        v
Plan -> Implement -> Verify -> Commit/PR
```

## Stage 1.0 - Establish Governance

### Stage 1.1 - Choose the Hub (Single Source of Truth)

Use one canonical file at repository root:

- `AGENTS.md`

It should contain all behavioral rules, quality gates, collaboration norms, and
operational constraints.

### Stage 1.2 - Define Precedence

Document a strict precedence order in `AGENTS.md`:

1. User task/request.
2. Root `AGENTS.md` canonical rules.
3. Tool wrapper files (spokes).
4. Nested `AGENTS.md` files for subdirectories (if monorepo).

### Stage 1.3 - Decide Spoke Coverage

Create wrapper files only for tools your team actually uses. Typical wrappers:

- `CLAUDE.md`
- `.github/copilot-instructions.md`
- `.cursorrules` or `.cursor/rules/*.mdc`
- `AGENT.md` (optional legacy compatibility)
- Other tool-specific equivalents

## Stage 2.0 - Author the Hub (`AGENTS.md`)

A production-grade `AGENTS.md` should define the following sections.

### Stage 2.1 - Mission and Scope

Specify:

- Target code quality bar.
- Primary repository goals.
- In-scope and out-of-scope work.

### Stage 2.2 - Non-Negotiable Engineering Rules

Define baseline guardrails such as:

- Keep architecture modular and testable.
- Keep entrypoints thin.
- Preserve public contracts unless explicitly requested.
- Avoid unrelated refactors.
- Prefer small, reviewable diffs.

### Stage 2.3 - Documentation and Explainability Standards

Require for non-trivial workflows:

- ASCII flow diagram.
- Stage markers (`Stage 1.0`, `Stage 1.1`, etc.).
- Why/input/output/failure-tradeoff explanations.
- Detailed class/function docstrings (purpose, inputs, outputs, failure behavior).
- Import explanations so dependency purpose is clear.
- Configuration comments for defaults, tuning direction, and allowed ranges.
- Pydantic model docstrings/field explanations/validator rationale.

### Stage 2.4 - Quality Gates

Define exact required sequence before commit/PR, for example:

1. Lint auto-fix.
2. Format.
3. Lint verify (clean pass required).
4. Targeted tests.
5. Optional broader test pass.

Include:

- Which paths are in scope.
- Which failures are blocking.
- When integration tests are mandatory.

### Stage 2.5 - Safety and Operations

Define:

- No secret leakage in logs.
- Actionable errors.
- No destructive operations without explicit approval.
- Requirements for observability and debugging breadcrumbs.

### Stage 2.6 - Communication Expectations

Set behavioral norms:

- Short progress updates during long tasks.
- Explicit assumptions.
- Transparent limitation reporting.
- Clear completion summary with verification outcomes.

### Stage 2.7 - Naming, Typing, and Modeling Best Practices

Set explicit code readability and contract rules:

- Use intuitive, professional, descriptive names for variables, classes, and functions.
- Prefer clarity over brevity; avoid cryptic abbreviations.
- Require type hints for parameters, returns, and meaningful attributes.
- Use enums for finite option sets instead of magic strings/integers.
- Apply typing best practices (`Literal`, `TypedDict`, `Protocol`, generics, aliases) where useful.
- Avoid `Any` unless necessary; document why when used.
- Follow Pydantic best practices with explicit field metadata, validation rationale, and compatibility notes.

## Stage 3.0 - Convert Wrappers into Spokes

Spokes should not duplicate policy. They should only redirect to `AGENTS.md`.

### Stage 3.1 - Spoke Template (Markdown Wrappers)

Use this template for files like `CLAUDE.md` and
`.github/copilot-instructions.md`:

```markdown
# <Tool Name> Instructions

This repository uses a Single Source of Truth for AI agent behavior.
Please read and strictly follow the canonical repository instructions located in `AGENTS.md` before taking any actions, planning, or writing any code. Do not duplicate rules here.
```

### Stage 3.2 - Cursor Style Wrappers

If the platform expects plain text or custom schema, keep the same intent:

- One short pointer to `AGENTS.md`.
- No repeated rules.
- No tool-specific overrides unless absolutely required.

### Stage 3.3 - Legacy Compatibility (`AGENT.md`)

Only keep `AGENT.md` if a tool requires it. If not required:

- delete it to reduce confusion.

If retained, it must stay a thin pointer and must not diverge from Hub policy.

## Stage 4.0 - Monorepo and Nested Scope Design

Use nested `AGENTS.md` only when a subproject has legitimate local differences
(for example separate runtime, deployment, or compliance constraints).

Rules:

- Root `AGENTS.md` defines universal policy.
- Nested `AGENTS.md` only adds local specialization.
- Never contradict root safety or quality policy.
- Keep nested files short and domain specific.

Example layout:

```text
repo/
  AGENTS.md
  CLAUDE.md
  .github/copilot-instructions.md
  services/
    payments/
      AGENTS.md
    ml-inference/
      AGENTS.md
```

## Stage 5.0 - Platform Mapping Reference

Use this as a practical mapping checklist.

| Platform | Typical file(s) | Recommended content |
| --- | --- | --- |
| Claude-based tools | `CLAUDE.md` | Pointer-only spoke to `AGENTS.md` |
| GitHub Copilot coding agents | `.github/copilot-instructions.md` | Pointer-only spoke to `AGENTS.md` |
| Cursor | `.cursorrules` or `.cursor/rules/*.mdc` | Pointer-only spoke to `AGENTS.md` |
| Legacy/compat adapters | `AGENT.md` | Pointer-only, or remove if unused |
| Any other agent tool | tool-native wrapper file | Pointer-only spoke to `AGENTS.md` |

## Stage 6.0 - What to Include in Commit/PR Policy

Every repository should define:

- Required local command sequence.
- Required CI checks and pass/fail thresholds.
- Review checklist (tests, docs, security, migrations).
- Commit message style.
- Rules for changed-file scope control.

Recommended minimal review checklist:

1. Does change match user request exactly?
2. Are unrelated files untouched?
3. Are tests added/updated where behavior changed?
4. Does quality gate pass locally?
5. Is the summary explicit about what was and was not verified?

## Stage 7.0 - Drift Prevention and Governance

### Stage 7.1 - Ownership

Assign owners for:

- `AGENTS.md` policy updates.
- Wrapper consistency audits.
- Quality gate maintenance.

### Stage 7.2 - Change Control

For instruction policy changes:

- require review approval from code owners.
- include rationale in PR description.
- update all spokes in same PR if template changes.

### Stage 7.3 - Audit Cadence

Run a recurring audit (monthly or quarterly):

- ensure wrappers are pointer-only.
- confirm no orphan instruction files exist.
- verify CI quality gate still matches documented sequence.

## Stage 8.0 - Anti-Patterns to Avoid

- Copying full policy into every wrapper file.
- Allowing wrapper-specific behavior to override Hub silently.
- Mixing style guidance and enforcement rules across many files.
- Keeping obsolete instruction files after platform migration.
- Using vague quality gates without exact commands and pass criteria.

## Stage 9.0 - Quick Start Checklist

1. Create root `AGENTS.md` with full policy.
2. Define precedence order in `AGENTS.md`.
3. Create spoke wrappers for active platforms.
4. Ensure every wrapper only points to `AGENTS.md`.
5. Add local and CI quality gates with exact commands.
6. Add nested `AGENTS.md` only for true subproject specialization.
7. Add recurring audit process to prevent policy drift.

## Appendix A - Minimal Hub Skeleton

```markdown
# AGENTS.md

## Mission
<What quality and outcomes this repository targets>

## Scope and Constraints
<What agents should and should not do>

## Engineering Rules
<Architecture, modularity, contract stability, diff size>

## Documentation and Explainability
<When to require flow diagrams, stage markers, tradeoff notes, docstrings, import/config/model explanations>

## Naming and Typing Standards
<Descriptive names, required type hints, enum usage, typing-library best practices>

## Testing and Quality Gate
1. <lint autofix command>
2. <format command>
3. <lint verify command>
4. <targeted test command>

## Safety and Security
<No secret leakage, no destructive operations without approval>

## Communication
<Progress updates, assumption disclosure, final reporting format>

## Precedence
1. User request
2. This file
3. Tool wrapper files
4. Nested AGENTS.md (closest scope)
```

## Appendix B - Spoke Validation Script Idea

You can enforce pointer-only spokes via CI with checks like:

- wrapper file length threshold.
- required pointer sentence present.
- forbidden duplicated policy keywords.

This turns governance from convention into enforceable hygiene.
