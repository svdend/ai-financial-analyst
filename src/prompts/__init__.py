"""Prompt templates for the LLM commentary pipeline.

Versioned prompt strings live as ``.md`` files alongside this module. Each
file may begin with an HTML-comment header (purely a documentation aid for
PR reviewers); the header is stripped before the prompt is returned, so it
never reaches the model.

Use :func:`load_prompt` to retrieve a prompt by name (without the ``.md``
extension). Results are memoised, so repeated calls do not re-read from
disk.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["load_prompt"]

_PACKAGE_DIR = Path(__file__).resolve().parent

# Matches a leading HTML comment block followed by any blank lines.
# Non-greedy so it only consumes the first ``-->``.
_LEADING_HTML_COMMENT = re.compile(r"\A<!--.*?-->\s*", re.DOTALL)

_PROMPT_CACHE: dict[str, str] = {}


def load_prompt(name: str) -> str:
    """Load a versioned prompt template by name.

    The prompt file is expected at ``<package_dir>/<name>.md``. Any leading
    HTML-comment block (used for in-repo documentation) is stripped, along
    with any leading blank lines and trailing whitespace, so the returned
    string is byte-stable across editor newline conventions and matches the
    exact text the model should receive.

    Results are cached in a module-level dictionary, so repeated calls for
    the same ``name`` do not re-read the file from disk.

    Args:
        name: Prompt identifier (file basename without the ``.md`` suffix),
            for example ``"commentary_system_v1"``.

    Returns:
        The prompt body with any leading HTML-comment header stripped.

    Raises:
        FileNotFoundError: If no ``<name>.md`` file exists in the prompts
            package directory.
    """
    cached = _PROMPT_CACHE.get(name)
    if cached is not None:
        return cached

    path = _PACKAGE_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt template not found: {path} " f"(expected '{name}.md' in {_PACKAGE_DIR})"
        )

    raw = path.read_text(encoding="utf-8")
    body = _LEADING_HTML_COMMENT.sub("", raw, count=1).lstrip("\n").rstrip()

    _PROMPT_CACHE[name] = body
    return body
