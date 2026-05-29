"""Public documentation hygiene checks."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HygienePattern:
    name: str
    pattern: re.Pattern[str]


TRACKED_MARKDOWN_FILES = [
    REPO_ROOT / path
    for path in subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO_ROOT,
        text=True,
    ).splitlines()
]

# Keep these patterns generic. Do not encode project-private names, personal
# names, private hostnames, or tool/vendor provenance names here.
PUBLIC_DOC_HYGIENE_PATTERNS = (
    HygienePattern(
        "private_repository_url",
        re.compile(r"https?://(?:www\.)?github\.com/[^\s)`]+/[^\s)`]*(?:private|internal)[^\s)`]*", re.I),
    ),
    HygienePattern(
        "windows_host_local_path",
        re.compile(r"\b[A-Z]:\\(?:Users|hyper-v|media|Windows|ProgramData|Program Files)\\[^\s)`]+", re.I),
    ),
    HygienePattern(
        "unc_or_wsl_host_path",
        re.compile(r"\\\\(?:wsl\$|wsl\.localhost)\\[^\s)`]+", re.I),
    ),
    HygienePattern(
        "unix_user_home_path",
        re.compile(r"(?<![\w.-])/home/(?!gatherlink(?:-user)?(?:/|$))[A-Za-z0-9_.-]+/[^\s)`]+"),
    ),
    HygienePattern(
        "secret_material_block",
        re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----", re.I),
    ),
    HygienePattern(
        "assigned_secret_value",
        re.compile(r"\b(?:password|token|secret|private_key)\s*[:=]\s*['\"][^'\"]{8,}['\"]", re.I),
    ),
    HygienePattern(
        "generated_private_state_path",
        re.compile(r"\.gatherlink/[^\s)`]*\.(?:secret|sealed)\.json", re.I),
    ),
    HygienePattern(
        "private_inventory_path",
        re.compile(r"(?:^|[/'\"`])inventory\.(?:env|json|yaml|yml)(?:$|['\"`\s)])", re.I),
    ),
)

# These are policy/example mentions, not leaked private data. Keep this list
# exact so new sensitive-looking text still has to justify itself.
PUBLIC_DOC_HYGIENE_ALLOWLIST: dict[str, tuple[re.Pattern[str], ...]] = {
    "docs/operations/release-checklist.md": (
        re.compile(r"private repository names"),
        re.compile(r"private remote URLs"),
        re.compile(r"host-local paths"),
        re.compile(r"keys, tokens"),
        re.compile(r"generated local state"),
        re.compile(r"host-local credentials"),
    ),
    "docs/operations/release-artifacts.md": (re.compile(r"inventory\.env"),),
    "docs/operations/documentation-maintenance.md": (
        re.compile(r"hostnames, keys, or secrets"),
        re.compile(r"Do not publish secrets"),
    ),
    "docs/operations/user-documentation.md": (
        re.compile(r"do not publish secrets"),
        re.compile(r"local VM hostnames"),
        re.compile(r"private IPs"),
    ),
    "docs/user/troubleshooting.md": (re.compile(r"config with secrets removed"),),
    "docs/labs/real-vm-acceptance.md": (
        re.compile(r"configs with secrets removed"),
        re.compile(r"redact secrets in reports"),
    ),
    "docs/labs/hyperv-vm-lab.md": (
        re.compile(r"inventory\.example\.env"),
        re.compile(r"\.gatherlink/hyperv-vm-acceptance/inventory\.env"),
    ),
    "tools/vm_acceptance/README.md": (
        re.compile(r"inventory\.example\.env"),
        re.compile(r"\.gatherlink/vm-acceptance/inventory\.env"),
    ),
}


def _is_allowlisted(source: Path, line: str) -> bool:
    rel = source.relative_to(REPO_ROOT).as_posix()
    return any(pattern.search(line) for pattern in PUBLIC_DOC_HYGIENE_ALLOWLIST.get(rel, ()))


def test_tracked_markdown_has_no_public_hygiene_leaks() -> None:
    """Tracked Markdown should not contain private locations, inventories, or secret material."""
    failures: list[str] = []
    checked_files = len(TRACKED_MARKDOWN_FILES)

    for source in TRACKED_MARKDOWN_FILES:
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            if _is_allowlisted(source, line):
                continue
            for hygiene in PUBLIC_DOC_HYGIENE_PATTERNS:
                if hygiene.pattern.search(line):
                    failures.append(f"{source.relative_to(REPO_ROOT)}:{line_number}: {hygiene.name}")

    print(f"checked {checked_files} project Markdown files for public hygiene leaks")
    assert failures == [], f"checked {checked_files} project Markdown files\n" + "\n".join(failures)
