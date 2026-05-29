"""Documentation link checks for GitHub-rendered Markdown."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_FILES = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]
PROJECT_MARKDOWN_FILES = [
    REPO_ROOT / path
    for path in subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO_ROOT,
        text=True,
    ).splitlines()
]
README_FILES = sorted((REPO_ROOT / "docs").rglob("README.md"))

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
HTML_ID_RE = re.compile(r"""<a\s+(?:[^>]*?\s+)?(?:id|name)=["']([^"']+)["']""", re.IGNORECASE)
MARKDOWN_FILENAME_RE = re.compile(r"(?<![\w])([A-Za-z0-9_./-]+\.md)(?![\w])")

# These are intentionally not links to committed documentation. Keep this list
# narrow so real doc references cannot drift back into bare code spans.
ALLOWED_LITERAL_MARKDOWN_REFERENCES: dict[str, tuple[re.Pattern[str], ...]] = {
    "docs/benchmarks/hyperv-performance-history.md": (re.compile(r"`report\.md`"),),
    "docs/labs/real-vm-acceptance.md": (
        re.compile(r"`report\.md`"),
        re.compile(r"`report\.json`"),
    ),
    "docs/operations/documentation-maintenance.md": (re.compile(r"`-full\.md`"),),
    "docs/releases/v0.9.1.md": (re.compile(r"`report\.md`"),),
    "docs/releases/v0.9.3.md": (re.compile(r"`\.gatherlink/[^`]+/report\.md`"),),
    "docs/reports/v0.9-code-audit-followups.md": (re.compile(r"`\.gatherlink/[^`]+/report\.md`"),),
    "tools/vm_acceptance/README.md": (re.compile(r"`report\.md`"),),
}


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


def _markdown_link_spans(line: str) -> list[range]:
    """Return ranges occupied by Markdown image/link expressions."""
    spans: list[range] = []
    for regex in (LINK_RE, IMAGE_RE):
        for match in regex.finditer(line):
            spans.append(range(match.start(), match.end()))
    return spans


def _is_allowed_literal_markdown_reference(source: Path, token: str, line: str) -> bool:
    rel = source.relative_to(REPO_ROOT).as_posix()
    allowed_patterns = ALLOWED_LITERAL_MARKDOWN_REFERENCES.get(rel, ())
    return any(pattern.search(line) for pattern in allowed_patterns)


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


def test_directory_readmes_link_markdown_files_directly() -> None:
    """Directory indexes should use GitHub-clickable links for Markdown files."""
    failures: list[str] = []
    inline_markdown_filename = re.compile(r"(?<!\[)`[^`]+\.md`(?!\]\()")

    for source in README_FILES:
        stripped = _without_fenced_blocks(source.read_text(encoding="utf-8"))
        for line_number, line in enumerate(stripped.splitlines(), start=1):
            if inline_markdown_filename.search(line):
                failures.append(f"{source.relative_to(REPO_ROOT)}:{line_number}: use a Markdown link, not a code span")

    assert failures == []


def test_markdown_document_references_are_links() -> None:
    """References from one project Markdown file to another should be clickable links."""
    failures: list[str] = []
    checked_files = len(PROJECT_MARKDOWN_FILES)

    for source in PROJECT_MARKDOWN_FILES:
        stripped = _without_fenced_blocks(source.read_text(encoding="utf-8"))
        for line_number, line in enumerate(stripped.splitlines(), start=1):
            link_spans = _markdown_link_spans(line)
            for match in MARKDOWN_FILENAME_RE.finditer(line):
                token = match.group(1)
                if any(match.start(1) in span for span in link_spans):
                    continue
                if _is_allowed_literal_markdown_reference(source, token, line):
                    continue
                failures.append(f"{source.relative_to(REPO_ROOT)}:{line_number}: " f"use a Markdown link for {token!r}")

    print(f"checked {checked_files} project Markdown files for clickable .md references")
    assert failures == [], f"checked {checked_files} project Markdown files"
