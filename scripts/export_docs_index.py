"""Generate the search_docs index from the docs-site markdown source.

Walks docs/src/content/docs and emits purser/mcp/docs_index.json — the
checked-in, build-time corpus the search_docs MCP tool answers from (DESIGN
decision 41). Checked in (not generated at image build) so wheel installs and
air-gapped instances get doc search too; CI regenerates and fails on drift,
exactly like frontend/openapi.json:

  uv run python scripts/export_docs_index.py

stdlib only, deterministic output. Hard rules enforced here so the anchors
Python computes can never drift from the ones Starlight renders:
  - every page needs a frontmatter title
  - headings must be ASCII (github-slugger and this script then agree)
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_CONTENT = REPO_ROOT / "docs" / "src" / "content" / "docs"
OUTPUT = REPO_ROOT / "purser" / "mcp" / "docs_index.json"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"^(##|###)\s+(.*)$")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _frontmatter_field(block: str, field: str) -> str | None:
    # Minimal YAML: our pages keep title/description as single-line scalars
    # (plain or quoted); anything fancier should fail loudly, not parse wrong.
    match = re.search(rf"^{field}:\s*(.+)$", block, re.MULTILINE)
    if match is None:
        return None
    value = match.group(1).strip()
    if value and value[0] in "'\"" and value[-1] == value[0] and len(value) >= 2:
        value = value[1:-1]
    return value


def _slugify(heading: str, path: Path) -> str:
    """github-slugger-compatible for the subset we allow (ASCII)."""
    if not heading.isascii():
        sys.exit(
            f"{path}: non-ASCII heading {heading!r} — ASCII-only headings are "
            "required so index anchors match Starlight's."
        )
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^a-z0-9_\- ]", "", text)
    return text.replace(" ", "-")


def _clean_text(lines: list[str]) -> str:
    text = "\n".join(lines)
    text = _IMAGE_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = text.replace("|", " ")  # table pipes
    text = re.sub(r"[*_>#]+", " ", text)
    text = text.replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _split_sections(body: str, path: Path) -> list[dict[str, str]]:
    """Split on ##/### headings, fence-aware; chunk 0 is the intro."""
    sections: list[dict[str, str]] = []
    seen_anchors: dict[str, int] = {}
    heading = ""
    anchor = ""
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        text = _clean_text(buffer)
        if text or heading:
            sections.append({"anchor": anchor, "heading": heading, "text": text})

    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue  # keep fence contents, drop the fence markers
        match = None if in_fence else _HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2).strip()
            slug = _slugify(heading, path)
            bump = seen_anchors.get(slug, 0)
            seen_anchors[slug] = bump + 1
            anchor = slug if bump == 0 else f"{slug}-{bump}"
            buffer = []
        else:
            buffer.append(line)
    flush()
    return sections


def _page_record(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        sys.exit(f"{path}: missing frontmatter block")
    front = match.group(1)
    if _frontmatter_field(front, "draft") == "true":
        return {}
    title = _frontmatter_field(front, "title")
    if not title:
        sys.exit(f"{path}: missing frontmatter title")
    if re.search(r"^slug:", front, re.MULTILINE):
        sys.exit(f"{path}: custom slug frontmatter is banned (index paths must be 1:1)")
    description = _frontmatter_field(front, "description") or ""

    rel = path.relative_to(DOCS_CONTENT)
    slug_path = str(rel.with_suffix("")).replace("\\", "/")
    if slug_path == "index":
        slug_path = ""
    elif slug_path.endswith("/index"):
        slug_path = slug_path[: -len("/index")]

    # .mdx pages (the splash) contribute title/description only — component
    # bodies aren't meaningful search text.
    sections = [] if path.suffix == ".mdx" else _split_sections(raw[match.end() :], path)
    return {
        "path": slug_path,
        "title": title,
        "description": description,
        "sections": sections,
    }


def main() -> None:
    if not DOCS_CONTENT.is_dir():
        sys.exit(f"docs content dir not found: {DOCS_CONTENT}")
    pages = []
    for path in sorted(DOCS_CONTENT.rglob("*")):
        if path.suffix not in (".md", ".mdx"):
            continue
        record = _page_record(path)
        if record:
            pages.append(record)
    pages.sort(key=lambda p: p["path"])
    index = {"version": 1, "pages": pages}
    OUTPUT.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total_sections = sum(len(p["sections"]) for p in pages)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}: {len(pages)} pages, {total_sections} sections")


if __name__ == "__main__":
    main()
