"""Documentation integrity tests.

The README is the project's front door, and it is easy to break it quietly: a
renamed file, a stale anchor, a screenshot that no longer exists, a Mermaid
block with a typo. None of that breaks the code, so nothing else would notice.

These checks are deliberately dependency free. Validating Mermaid properly
needs a JavaScript toolchain, which this project does not otherwise have; the
structural checks here catch the mistakes that actually happen in practice, and
`docs/architecture.md` diagrams are additionally verified by hand against
`mermaid.parse` during development.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = sorted([REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))])
#: Everything published that is not markdown and can still hold a placeholder.
NON_MARKDOWN_PUBLISHED = [
    REPO_ROOT / "pyproject.toml",
    *sorted((REPO_ROOT / "packaging" / "systemd").glob("*")),
]

#: Mermaid diagram types this project uses.
MERMAID_TYPES = {
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "classDiagram",
    "erDiagram",
    "journey",
    "gantt",
    "pie",
    "gitGraph",
}

_LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")
_IMAGE_RE = re.compile(r"\!\[([^\]]*)\]\(([^)]+)\)")
_FENCED_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_FENCE_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def _is_external(target: str) -> bool:
    """True for links that are not local repository paths."""
    return target.startswith(("http://", "https://", "mailto:", "#", "//"))


def _brackets_balanced(line: str) -> bool:
    """Check bracket balance for one Mermaid statement.

    Brackets inside quoted labels are ignored, so a label may contain them.
    Mermaid node shapes mix the two bracket kinds freely - ``A[label]`` and
    ``A{label}`` are both valid, and so are ``A((label))`` - so the check is
    simply that whatever is opened is closed in the same style.
    """
    without_labels = re.sub(r'"[^"]*"', '""', line)
    without_comments = re.sub(r"%%.*$", "", without_labels)
    stack: list[str] = []
    pairs = {"]": "[", "}": "{", ")": "("}
    # "((label))" and "((" nest, so track the opener for each closer.
    for char in without_comments:
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack[-1] != pairs[char]:
                return False
            stack.pop()
    return not stack


def _strip_fences(text: str) -> str:
    """Remove fenced code blocks.

    Shell transcripts and ``sha256sum`` output contain lines that look like
    markdown headings, so headings and anchors must be derived with the fences
    removed.
    """
    return _FENCED_RE.sub("", text)


def _resolve(base: Path, target: str) -> Path:
    """Resolve a link target relative to the file that contains it."""
    return (base.parent / target.split("#", 1)[0]).resolve()


def _slugify(heading: str) -> str:
    """Reproduce GitHub's heading anchor algorithm closely enough to test it."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)  # inline code
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links
    text = re.sub(r"[*_]", "", text)
    text = text.strip().lower()
    chars = []
    for char in text:
        if char.isalnum() or char in "-_ " or unicodedata.category(char).startswith("L"):
            chars.append(char)
        # Everything else is dropped, as GitHub does.
    return re.sub(r"\s+", "-", "".join(chars).strip())


def _anchors(text: str) -> set[str]:
    """Return every heading anchor in a markdown document."""
    found: dict[str, int] = {}
    anchors: set[str] = set()
    for match in _ATX_RE.finditer(_strip_fences(text)):
        slug = _slugify(match.group(2))
        if not slug:
            continue
        count = found.get(slug, 0)
        found[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def test_docs_exist() -> None:
    assert DOC_FILES, "no documentation found to check"


@pytest.mark.parametrize("doc", DOC_FILES, ids=lambda p: p.name)
def test_relative_links_resolve(doc: Path) -> None:
    """Every relative link and image must point at a file that exists."""
    text = doc.read_text(encoding="utf-8")
    broken: list[str] = []
    for pattern, group in ((_IMAGE_RE, 2), (_LINK_RE, 1)):
        for match in pattern.finditer(text):
            target = match.group(group)
            if _is_external(target) or not target.strip():
                continue
            if not _resolve(doc, target).exists():
                broken.append(target)
    assert not broken, f"{doc.name} links to missing paths: {sorted(set(broken))}"


def test_readme_table_of_contents_anchors_resolve() -> None:
    """A TOC entry pointing at a renamed heading is a silent README bug."""
    readme = REPO_ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    available = _anchors(text)

    toc = re.search(r"^## Table of Contents\s*$(.*?)^---\s*$", text, re.M | re.DOTALL)
    assert toc, "README has no Table of Contents section"

    listed: list[str] = []
    for match in re.finditer(r"^\s*[-*]\s+\[[^\]]+\]\((#[^)]+)\)", toc.group(1), re.M):
        listed.append(match.group(1).lstrip("#"))

    assert len(listed) >= 20, f"TOC looks truncated, only {len(listed)} entries"
    missing = [anchor for anchor in listed if anchor not in available]
    assert not missing, f"TOC anchors with no matching heading: {missing}"


def test_readme_has_exactly_one_h1() -> None:
    text = _strip_fences((REPO_ROOT / "README.md").read_text(encoding="utf-8"))
    h1 = [line for line in text.splitlines() if line.startswith("# ")]
    assert h1 == ["# log-archive"], f"expected a single H1, found {h1}"
    assert not text.rstrip().endswith("# log-archive"), "stray heading at end of README"


def test_no_unresolved_placeholders() -> None:
    """Placeholders would ship to a public repository."""
    for doc in [*DOC_FILES, *NON_MARKDOWN_PUBLISHED]:
        text = doc.read_text(encoding="utf-8")
        for needle in ("github.com/example", "example.com", "<your-repository-url>"):
            assert needle not in text, f"{doc.name} still contains {needle}"


@pytest.mark.parametrize("doc", DOC_FILES, ids=lambda p: p.name)
def test_mermaid_blocks_are_structurally_valid(doc: Path) -> None:
    """Check Mermaid blocks for the mistakes that actually occur.

    A full parse needs a JavaScript toolchain, so this covers the common
    failure modes: an unknown diagram type, an empty block, and unbalanced
    brackets or quotes inside node labels.
    """
    blocks = _FENCE_RE.findall(doc.read_text(encoding="utf-8"))
    for index, block in enumerate(blocks, start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        assert lines, f"{doc.name} mermaid block {index} is empty"
        first = lines[0].strip()
        assert first.split()[0] in MERMAID_TYPES, (
            f"{doc.name} mermaid block {index} starts with {first!r}, "
            f"expected one of {sorted(MERMAID_TYPES)}"
        )
        for line in lines:
            if line.count('"') % 2:
                assert line.strip().startswith("%%"), (
                    f"{doc.name} mermaid block {index} has an unbalanced quote: {line!r}"
                )
            assert _brackets_balanced(line), (
                f"{doc.name} mermaid block {index} has mismatched brackets, which Mermaid "
                f"rejects at render time: {line.strip()!r}"
            )


def test_readme_shows_the_screenshots() -> None:
    """The screenshots are part of the documentation, so keep them wired up."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    images = _IMAGE_RE.findall(text)
    screenshots = [target for _alt, target in images if "screenshots" in target]
    assert len(screenshots) >= 7, f"only {len(screenshots)} screenshots referenced"
    for target in screenshots:
        assert _resolve(REPO_ROOT / "README.md", target).is_file()


def test_architecture_doc_is_reachable_from_the_readme() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/architecture.md" in text
