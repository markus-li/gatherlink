"""Documentation structure and release hygiene checks."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPO_ROOT / "docs"
DOCS_MAP = DOCS_ROOT / "README.md"

TRACKED_MARKDOWN_FILES = [
    REPO_ROOT / path
    for path in subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO_ROOT,
        text=True,
    ).splitlines()
]

DOCS_MARKDOWN_FILES = [path for path in TRACKED_MARKDOWN_FILES if path.is_relative_to(DOCS_ROOT)]

HISTORICAL_STALE_PHRASE_ALLOWLIST = {
    "docs/reports/README.md",
    "docs/reports/mvp-implementation-priorities-closed.md",
}

AUTO_REVIEWED_NEW_MARKDOWN_PATTERNS = (
    re.compile(r"docs/releases/v\d+\.\d+\.\d+\.md"),
    re.compile(r"docs/reports/.*roadmap.*\.md"),
)

REVIEWED_MARKDOWN_FILES = {
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "docs/README.md",
    "docs/architecture/README.md",
    "docs/architecture/api-surface.md",
    "docs/architecture/architecture-contract.md",
    "docs/architecture/architecture.md",
    "docs/architecture/design-principles.md",
    "docs/architecture/performance-philosophy.md",
    "docs/architecture/plugin-strategy.md",
    "docs/architecture/source-map.md",
    "docs/benchmarks/README.md",
    "docs/benchmarks/external-aggregation-comparison.md",
    "docs/benchmarks/hyperv-performance-history.md",
    "docs/benchmarks/hyperv-performance-log.md",
    "docs/benchmarks/thresholds.md",
    "docs/benchmarks/wireguard-over-gatherlink-status.md",
    "docs/future/README.md",
    "docs/future/access-policy.md",
    "docs/future/identity-and-topology.md",
    "docs/future/overlay-naming.md",
    "docs/future/overlay-routing.md",
    "docs/helpers/README.md",
    "docs/helpers/captive-portal-helper.md",
    "docs/helpers/dns-helper.md",
    "docs/helpers/helper-priorities.md",
    "docs/helpers/ipsec-helper.md",
    "docs/helpers/policy-advisor.md",
    "docs/helpers/relay-fabric.md",
    "docs/helpers/socks5-helper.md",
    "docs/helpers/tcp-forwarding-helper.md",
    "docs/helpers/time-sync.md",
    "docs/helpers/traffic-split-helper.md",
    "docs/helpers/wireguard-helper.md",
    "docs/labs/README.md",
    "docs/labs/http3-datagram-carrier.md",
    "docs/labs/hyperv-vm-lab.md",
    "docs/labs/lab-bundles.md",
    "docs/labs/lab-demo.md",
    "docs/labs/local-dual-path-lab.md",
    "docs/labs/quic-traefik-proxy.md",
    "docs/labs/real-vm-acceptance.md",
    "docs/labs/wsl-two-distro-lab.md",
    "docs/operations/README.md",
    "docs/operations/appliance-update-strategy.md",
    "docs/operations/deployment-archetypes.md",
    "docs/operations/development-discipline.md",
    "docs/operations/diagnostics-dictionary.md",
    "docs/operations/diagnostics-events.md",
    "docs/operations/diagnostics.md",
    "docs/operations/documentation-maintenance.md",
    "docs/operations/library-selection.md",
    "docs/operations/operator-runbook.md",
    "docs/operations/release-artifacts.md",
    "docs/operations/release-checklist.md",
    "docs/operations/release-development-process.md",
    "docs/operations/testing-strategy.md",
    "docs/operations/troubleshooting-guide.md",
    "docs/operations/user-documentation.md",
    "docs/project-living-assessment.md",
    "docs/project-story.md",
    "docs/protocol/README.md",
    "docs/protocol/capability-negotiation.md",
    "docs/protocol/control-context.md",
    "docs/protocol/plaintext-security-mode.md",
    "docs/protocol/protocol-notes.md",
    "docs/protocol/protocol.md",
    "docs/protocol/relay-session-lifecycle.md",
    "docs/protocol/relay-trust-model.md",
    "docs/protocol/runtime-session-model.md",
    "docs/protocol/secrets-age.md",
    "docs/protocol/security.md",
    "docs/public/README.md",
    "docs/public/cloudflare-pages.md",
    "docs/releases/README.md",
    "docs/reports/README.md",
    "docs/reports/mvp-implementation-priorities-closed.md",
    "docs/reports/three-path-scheduler-lab.md",
    "docs/reports/v0.9-code-audit-followups.md",
    "docs/research/study-and-evaluation-notes.md",
    "docs/runtime/README.md",
    "docs/runtime/config-runtime-state.md",
    "docs/runtime/configuration.md",
    "docs/runtime/failure-model.md",
    "docs/runtime/ipv6-strategy.md",
    "docs/runtime/nat-traversal.md",
    "docs/runtime/path-lifecycle.md",
    "docs/runtime/resource-guardrails.md",
    "docs/runtime/scheduler.md",
    "docs/runtime/service-priority.md",
    "docs/runtime/state-persistence.md",
    "docs/user/README.md",
    "docs/user/config-cookbook.md",
    "docs/user/core-service.md",
    "docs/user/quickstart.md",
    "docs/user/socks5.md",
    "docs/user/troubleshooting.md",
    "docs/user/wireguard-multipath.md",
    "docs/user/wireguard.md",
    "tools/vm_acceptance/README.md",
}


def _markdown_links(markdown: str) -> set[str]:
    return set(re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", markdown))


def _has_markdown_link_to(markdown: str, target_name: str) -> bool:
    for link in _markdown_links(markdown):
        target = link.split("#", 1)[0].split(maxsplit=1)[0]
        if target.endswith(target_name):
            return True
    return False


def _table_header_after_heading(markdown: str, heading: str) -> list[str]:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != heading:
            continue
        for candidate in lines[index + 1 :]:
            stripped = candidate.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                return [cell.strip() for cell in stripped.strip("|").split("|")]
    return []


def _paragraphs_with_line_numbers(markdown: str) -> list[tuple[int, str]]:
    paragraphs: list[tuple[int, str]] = []
    current: list[str] = []
    start_line = 1

    for line_number, line in enumerate(markdown.splitlines(), start=1):
        if line.strip():
            if not current:
                start_line = line_number
            current.append(line)
            continue
        if current:
            paragraphs.append((start_line, " ".join(current)))
            current = []

    if current:
        paragraphs.append((start_line, " ".join(current)))

    return paragraphs


def _sentences_with_line_numbers(markdown: str) -> list[tuple[int, str]]:
    sentences: list[tuple[int, str]] = []
    for line_number, paragraph in _paragraphs_with_line_numbers(markdown):
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            if sentence.strip():
                sentences.append((line_number, sentence))
    return sentences


def _is_auto_reviewed_new_markdown_file(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in AUTO_REVIEWED_NEW_MARKDOWN_PATTERNS)


def test_new_markdown_files_are_reviewed_before_becoming_standalone_docs() -> None:
    """New Markdown files should usually be merged into existing docs."""
    unexpected = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in TRACKED_MARKDOWN_FILES
        if path.relative_to(REPO_ROOT).as_posix() not in REVIEWED_MARKDOWN_FILES
        and not _is_auto_reviewed_new_markdown_file(path.relative_to(REPO_ROOT).as_posix())
    )

    assert unexpected == [], "New Markdown files need an explicit documentation-structure review:\n" + "\n".join(
        f"{path}: this file was added; is it really needed, or does the content belong in an existing file?"
        for path in unexpected
    )


def test_doc_directories_with_multiple_markdown_files_have_readme_indexes() -> None:
    """Every documentation directory with multiple Markdown files should have a README index."""
    failures: list[str] = []
    by_directory: dict[Path, list[Path]] = {}

    for path in DOCS_MARKDOWN_FILES:
        by_directory.setdefault(path.parent, []).append(path)

    for directory, files in sorted(by_directory.items()):
        if len(files) <= 1:
            continue
        if directory.name == "docs":
            continue
        if directory / "README.md" not in files:
            failures.append(f"{directory.relative_to(REPO_ROOT)}: missing README.md for {len(files)} docs")

    assert failures == [], "\n".join(failures)


def test_tracked_docs_are_reachable_from_docs_map_or_directory_index() -> None:
    """Tracked docs should be findable from the global map or their directory README."""
    failures: list[str] = []
    docs_map_text = DOCS_MAP.read_text(encoding="utf-8")

    for path in DOCS_MARKDOWN_FILES:
        if path == DOCS_MAP:
            continue
        if path.name == "README.md":
            if _has_markdown_link_to(docs_map_text, path.relative_to(DOCS_ROOT).as_posix()):
                continue
            if path.parent == DOCS_ROOT:
                continue
            failures.append(f"{path.relative_to(REPO_ROOT)}: directory README is not linked from docs/README.md")
            continue

        directory_readme = path.parent / "README.md"
        directory_text = directory_readme.read_text(encoding="utf-8") if directory_readme.exists() else ""
        rel_from_docs = path.relative_to(DOCS_ROOT).as_posix()
        if _has_markdown_link_to(docs_map_text, rel_from_docs) or _has_markdown_link_to(directory_text, path.name):
            continue
        failures.append(f"{path.relative_to(REPO_ROOT)}: not linked from docs map or directory README")

    assert failures == [], "\n".join(failures)


def test_tracked_docs_do_not_use_stale_release_status_phrasing() -> None:
    """Current docs should not drift back to old milestone or active-release wording."""
    patterns = (
        ("historical_milestone_label", re.compile(r"\bMVP\b")),
        ("old_release_marked_active", re.compile(r"\bv0\.9\.2\s+(?:is\s+)?active\b|\bactive\s+v0\.9\.2\b", re.I)),
        ("old_v1_release_label", re.compile(r"\bv1(?:\.0)?\s+(?:release|roadmap|production)\b", re.I)),
    )
    failures: list[str] = []

    for path in TRACKED_MARKDOWN_FILES:
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in HISTORICAL_STALE_PHRASE_ALLOWLIST:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name, pattern in patterns:
                if pattern.search(line):
                    failures.append(f"{rel}:{line_number}: {name}")

    assert failures == [], "\n".join(failures)


def test_canonical_ownership_claims_link_to_canonical_docs() -> None:
    """Ownership/source-of-truth claims outside the maintenance guide should link to their source."""
    claim_patterns = (
        re.compile(
            r"\bcanonical\b(?:\W+\w+){0,4}\W+\b(?:doc|docs|document|policy|rule|home)\b",
            re.I,
        ),
        re.compile(
            r"\bsource of truth\b(?:\W+\w+){0,6}\W+\b(?:doc|docs|document|policy|rule|roadmap|release|config|helper)\b",
            re.I,
        ),
    )
    allowed_claim_files = {
        "docs/README.md",
        "docs/operations/documentation-maintenance.md",
    }
    failures: list[str] = []

    for path in TRACKED_MARKDOWN_FILES:
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowed_claim_files:
            continue
        for line_number, sentence in _sentences_with_line_numbers(path.read_text(encoding="utf-8")):
            if not any(pattern.search(sentence) for pattern in claim_patterns):
                continue
            if "[" in sentence and "](" in sentence:
                continue
            failures.append(f"{rel}:{line_number}: canonical/source-of-truth claim should link to canonical doc")

    assert failures == [], "\n".join(failures)


def test_active_release_notes_exist_and_are_linked_from_docs_map() -> None:
    """The highest tracked release note should exist and be linked from the docs map."""
    release_files = sorted((DOCS_ROOT / "releases").glob("v*.md"))
    assert release_files

    def version_key(path: Path) -> tuple[int, int, int]:
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\.md", path.name)
        assert match is not None
        return tuple(int(part) for part in match.groups())

    active_release = max(release_files, key=version_key)
    docs_map_text = DOCS_MAP.read_text(encoding="utf-8")
    assert active_release.exists()
    assert _has_markdown_link_to(docs_map_text, active_release.relative_to(DOCS_ROOT).as_posix())


def test_benchmark_current_tables_keep_required_columns() -> None:
    """Benchmark evidence tables should keep comparison and gate columns visible."""
    performance_log = (DOCS_ROOT / "benchmarks" / "hyperv-performance-log.md").read_text(encoding="utf-8")
    required_by_heading = {
        "### Current Comparison Matrix": {
            "Date",
            "Profile",
            "Mode",
            "WG path-set TCP",
            "Raw GL guardrail",
            "% WG path-set TCP",
            "% raw GL total",
            "GL Gate",
            "WG Gate",
            "Reading",
            "Evidence",
        },
        "## Current v0.9.3 Proof Rows": {
            "Date",
            "Proof",
            "Shape",
            "Offered / baseline",
            "Result",
            "Reading",
            "Evidence",
        },
        "## Current Raw Guardrails": {
            "Date",
            "Shape",
            "Scheduler",
            "Offered",
            "Delivered",
            "Packet delta",
            "Evidence",
        },
    }
    failures: list[str] = []

    for heading, required in required_by_heading.items():
        header = set(_table_header_after_heading(performance_log, heading))
        missing = sorted(required - header)
        if missing:
            failures.append(f"{heading}: missing {', '.join(missing)}")

    assert failures == [], "\n".join(failures)
