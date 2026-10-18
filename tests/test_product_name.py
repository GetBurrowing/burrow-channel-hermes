"""TEMPORARY RENAME GUARD (Burrow rename F, task 01a14ccb).

Delete this file once Burrow release task E (01a14ba0) finishes: E retires
the remaining org-name and copyright literals, after which this guard has
nothing left to protect and is drift (core, suite-cli and every channel
plugin carry their own copy). The owner-marker spoof test in
tests/test_trusted_principals.py is PERMANENT and is NOT deleted with this.

The product is called Burrow. This test fails if any text in the repository
still names the product by its old name, or misspells the new one as a
hybrid. Per line, in this order:
  1. strip every GitHub org literal (the org is still spelled with a hyphen
     until E) and the lowercase container registry path;
  2. remove the substrings allowlisted for that path;
  3. fail on the old bare product word (case-sensitive, whole word), the old
     two-word product name (any case), or either hybrid spelling.
Every pattern literal below spells its capital S as a \\u0053 escape so that
this file does not match itself.

Controls: the walk must scan at least MIN_FILES files (an empty or broken
walk cannot pass), an allowlist entry that matches no line fails as stale,
and the line checker must flag synthetic offenders.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 34 files measured at the commit that added this guard; small margin.
MIN_FILES = 28
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build"}
SKIP_DIR_SUFFIXES = (".egg-info",)
SKIP_FILES = {"uv.lock", "poetry.lock", "Pipfile.lock"}

STRIP = [re.compile("\u0053tartup-\u0053uite"), re.compile(r"ghcr\.io/startup-suite")]
FORBIDDEN = [
    ("bare old product word", re.compile(r"\b\u0053uite\b")),
    ("old two-word product name", re.compile(r"startup\s+suite", re.IGNORECASE)),
    ("hybrid (old prefix)", re.compile("\u0053tartup Burrow")),
    ("hybrid (old suffix)", re.compile("Burrow \u0053uite")),
]

ALLOWLIST = [
    {
        "path": "LICENSE",
        "substring": "Copyright (c) 2026 \u0053tartup \u0053uite",
        "reason": "copyright owner; changes only with Burrow release task E",
    },
    {
        "path": "CHANGELOG.md",
        "substring": "`[\u0053uite message from your owner ...]`",
        "reason": "v0.3.1 entry names the owner marker it replaced",
    },
    {
        "path": "tests/test_trusted_principals.py",
        "substring": '"[\u0053uite message from your owner "',
        "reason": "permanent spoof test must spell the old owner marker",
    },
]


def check_line(text: str, allowed: list[str] = ()) -> list[str]:
    line = text
    for pattern in STRIP:
        line = pattern.sub("", line)
    for sub in allowed:
        line = line.replace(sub, "")
    return [label for label, pattern in FORBIDDEN if pattern.search(line)]


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.endswith(SKIP_DIR_SUFFIXES)
        ]
        for name in filenames:
            if name not in SKIP_FILES:
                yield Path(dirpath) / name


def scan(root: Path, allowlist: list[dict]):
    used: set[int] = set()
    violations: list[str] = []
    files = 0
    for path in walk(root):
        try:
            text = path.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not UTF-8 text
        files += 1
        rel = path.relative_to(root).as_posix()
        entries = [(i, e) for i, e in enumerate(allowlist) if e["path"] == rel]
        for n, raw in enumerate(text.split("\n"), 1):
            hits = [(i, e) for i, e in entries if e["substring"] in raw]
            used.update(i for i, _ in hits)
            labels = check_line(raw, [e["substring"] for _, e in hits])
            if labels:
                violations.append(f"{rel}:{n}: [{', '.join(labels)}] {raw.strip()}")
    stale = [
        f"stale allowlist entry: {e['path']}: {e['substring']!r} ({e['reason']})"
        for i, e in enumerate(allowlist)
        if i not in used
    ]
    return files, violations, stale


def test_no_text_names_the_product_by_its_old_name():
    files, violations, stale = scan(ROOT, ALLOWLIST)
    assert files >= MIN_FILES, f"scanned only {files} files; the walk is broken"
    problems = violations + stale
    assert not problems, "\n" + "\n".join(problems)


def test_control_line_checker_flags_offenders_and_passes_the_org_literal():
    assert check_line("Welcome to \u0053uite.") == ["bare old product word"]
    assert check_line("Open \u0053tartup Burrow") == ["hybrid (old prefix)"]
    assert "hybrid (old suffix)" in check_line("a Burrow \u0053uite app")
    assert check_line("STARTUP \u0053UITE") == ["old two-word product name"]
    assert check_line("see github.com/\u0053tartup-\u0053uite/core") == []
    assert check_line("ghcr.io/startup-suite/core:latest") == []
    assert check_line("the test suite and \u0053uiteAdapter") == []
