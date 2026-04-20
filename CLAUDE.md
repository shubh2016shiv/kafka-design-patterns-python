# CLAUDE.md

@AGENTS.md

Claude-specific notes:
- Use `AGENTS.md` as the canonical behavior and coding standard.
- Keep repository memory concise and avoid duplicating policy text already defined in `AGENTS.md`.
- If Claude-specific overrides are needed in future, add them below this section and keep them minimal.

Mandatory inherited checks from `AGENTS.md` before commit/PR:
1. `ruff check <phased-scope> --fix --no-cache`
2. `ruff format <phased-scope>`
3. `ruff check <phased-scope> --no-cache`
4. `python -m unittest test.producer.test_simple_producer`
