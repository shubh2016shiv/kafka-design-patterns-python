# AGENT.md

Legacy compatibility wrapper.

Canonical agent instructions now live in:
- [AGENTS.md](./AGENTS.md)

Reason:
- `AGENTS.md` is the emerging cross-tool convention for agent instructions.
- Keeping this file avoids breaking tools/scripts that still look for `AGENT.md`.

If a tool reads this file, apply all rules from `AGENTS.md`.

Mandatory inherited checks from `AGENTS.md`:
1. `ruff check <phased-scope> --fix --no-cache`
2. `ruff format <phased-scope>`
3. `ruff check <phased-scope> --no-cache`
4. `python -m unittest test.producer.test_simple_producer`
