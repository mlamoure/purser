"""The hand-authored docs' curl examples work as pasted (issue #390 fix
round): a request with a JSON body needs `Content-Type: application/json`,
or curl's `-d` sends it form-encoded and the API answers 422.

Pure: reads the docs pages, no server."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs" / "src" / "content" / "docs"


def _shell_commands(text: str) -> list[str]:
    """Each command of the page's shell code blocks, continuation lines
    joined."""
    commands: list[str] = []
    in_block = False
    shell = False
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_block:
                in_block = True
                shell = stripped[3:].strip() in ("bash", "sh", "shell", "console")
            else:
                in_block = False
                if current:
                    commands.append(current)
                current = ""
            continue
        if not (in_block and shell):
            continue
        current += " " + stripped.rstrip("\\").strip()
        if not stripped.endswith("\\"):
            commands.append(current.strip())
            current = ""
    return commands


def test_every_curl_with_a_body_says_it_is_json() -> None:
    missing = []
    for page in sorted(DOCS.rglob("*.md")):
        for command in _shell_commands(page.read_text()):
            if not command.startswith("curl "):
                continue
            if re.search(r"\s(-d|--data(-raw)?)\s", command) and not re.search(
                r"Content-Type:\s*application/json", command, re.IGNORECASE
            ):
                missing.append(f"{page.relative_to(DOCS)}: {command[:90]}")
    assert missing == []


def test_the_scanner_sees_the_holdings_examples() -> None:
    """Guard the guard: the manual-holdings how-to's examples are found."""
    commands = _shell_commands((DOCS / "how-to" / "manual-holdings.md").read_text())
    assert sum(c.startswith("curl ") for c in commands) >= 3
