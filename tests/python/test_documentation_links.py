"""Documentation link checks for GitHub-rendered Markdown."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_FILES = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
HTML_ID_RE = re.compile(r"""<a\s+(?:[^>]*?\s+)?(?:id|name)=["']([^"']+)["']""", re.IGNORECASE)


def _without_fenced_blocks(markdown: str) -> str:
    """Return Markdown text with fenced code blocks removed before link parsing."""
    lines: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append("")
            continue
        lines.append("" if in_fence else line)
    return "\n".join(lines)


def _extract_link_target(raw_target: str) -> str:
    """Extract the URL part from a Markdown link target that may include a title."""
    target = raw_target.strip()
    if target.startswith("<"):
        end = target.find(">")
        return target[1:end] if end != -1 else target.strip("<>")
    return target.split(maxsplit=1)[0]


def _is_external_or_special(target: str) -> bool:
    parsed = urlparse(target)
    return bool(parsed.scheme) or target.startswith("mailto:")


def _github_heading_slug(heading_text: str) -> str:
    """Approximate GitHub's generated heading ids for repo Markdown files."""
    text = re.sub(r"<[^>]+>", "", heading_text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text)
    return text.strip("-")


def _anchor_ids(markdown_path: Path) -> set[str]:
    """Collect explicit and GitHub-style heading anchors for a Markdown file."""
    text = markdown_path.read_text(encoding="utf-8")
    anchors = set(HTML_ID_RE.findall(text))
    seen: dict[str, int] = {}
    for line in _without_fenced_blocks(text).splitlines():
        match = HEADING_RE.match(line)
        if not match:
            continue
        slug = _github_heading_slug(match.group(2))
        if not slug:
            continue
        count = seen.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return anchors


def _resolve_local_target(source: Path, target: str) -> tuple[Path, str]:
    path_part, _, fragment = target.partition("#")
    if not path_part:
        return source, unquote(fragment)
    if path_part.startswith("/"):
        resolved = REPO_ROOT / unquote(path_part.lstrip("/"))
    else:
        resolved = (source.parent / unquote(path_part)).resolve()
    return resolved, unquote(fragment)


def test_documentation_local_links_resolve() -> None:
    """Every local Markdown/image link should resolve when viewed on GitHub."""
    failures: list[str] = []

    for source in DOC_FILES:
        text = source.read_text(encoding="utf-8")
        stripped = _without_fenced_blocks(text)
        for regex in (LINK_RE, IMAGE_RE):
            for match in regex.finditer(stripped):
                target = _extract_link_target(match.group(1))
                if not target or _is_external_or_special(target):
                    continue

                resolved, fragment = _resolve_local_target(source, target)
                if not resolved.exists():
                    failures.append(f"{source.relative_to(REPO_ROOT)}: missing target {target!r}")
                    continue
                if fragment and resolved.suffix.lower() == ".md":
                    anchors = _anchor_ids(resolved)
                    if fragment not in anchors:
                        failures.append(
                            f"{source.relative_to(REPO_ROOT)}: missing anchor #{fragment} in "
                            f"{resolved.relative_to(REPO_ROOT)}"
                        )

    assert failures == []
