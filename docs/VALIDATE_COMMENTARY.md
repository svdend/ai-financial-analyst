# Validating commentary drafts offline

`src/validate_commentary.py` exposes the same hallucination guard used by
`src/generate_commentary.py` as a standalone CLI — useful for iterating on
prompts or validating a draft written from a dry-run payload, with no DuckDB
warehouse and no Anthropic API key.

```bash
# 1. Dump the structured payload + a dry-run prompt to disk:
python -m src.generate_commentary --dump-payload /tmp/payload.json > /tmp/dry_run.txt

# 2. Hand /tmp/dry_run.txt to Claude (claude.ai, Claude Code, anything),
#    write the response to /tmp/draft.md, then validate it offline:
python -m src.validate_commentary --payload /tmp/payload.json --commentary /tmp/draft.md
```

Exit code is `0` on a clean draft, `1` on a guard violation (the bad token is
named in stderr), and `2` on bad input. `--payload` accepts either the raw
JSON written by `--dump-payload` or the full dry-run output text — the loader
auto-detects the format.
