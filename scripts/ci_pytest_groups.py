"""
scripts/ci_pytest_groups.py - the CI pytest groups (Stage 6.0.3). CI-only tooling: no trading, broker, strategy or server code.

  check               validate .github/ci/pytest_groups.json against tests/: every tests/test_*.py file in EXACTLY ONE group, every listed
                      file present, no test file hidden in a sub-directory, pytest's default discovery pattern in force. Exit 1 on any problem.
  list <group>        print the group's test files (one per line, repository-relative)
  run <group> [-- pytest args...]
                      `check`, then run the group's files with pytest in this process (exit code = pytest's). This is what each CI job runs.

The manifest is the single source of truth: tests/test_ci_groups_603.py runs `check` and also verifies that the workflow's matrix names
exactly these groups, so a new test file that belongs to no group fails CI instead of silently never running.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github" / "ci" / "pytest_groups.json"
TESTS = ROOT / "tests"
PYTEST_CONFIG_FILES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


def load() -> Dict[str, dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    groups = data.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise SystemExit(f"{MANIFEST}: no groups")
    return groups


def discovered() -> List[str]:
    """The files pytest collects from tests/ with its default `test_*.py` pattern (the repository defines no pytest configuration)."""
    return sorted(p.name for p in TESTS.glob("test_*.py"))


def nested_test_files() -> List[str]:
    return sorted(f"tests/{p.relative_to(TESTS).as_posix()}" for p in TESTS.rglob("test_*.py") if p.parent != TESTS)


def config_overrides() -> List[str]:
    """pytest configuration files that would change discovery (none are expected)."""
    out = []
    for name in PYTEST_CONFIG_FILES:
        p = ROOT / name
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            if "python_files" in text or "testpaths" in text or ("[pytest]" in text or "[tool.pytest" in text or "[tool:pytest]" in text):
                out.append(name)
    return out


def problems(groups: Dict[str, dict]) -> List[str]:
    out: List[str] = []
    seen: Dict[str, str] = {}
    for name, g in groups.items():
        files = g.get("files") if isinstance(g, dict) else None
        if not isinstance(files, list) or not files or not isinstance(g.get("job"), str) or not g["job"].strip():
            out.append(f"group {name!r}: needs a non-empty 'files' list and a 'job' name")
            continue
        for f in files:
            if not isinstance(f, str) or "/" in f or "\\" in f or not f.startswith("test_") or not f.endswith(".py"):
                out.append(f"group {name!r}: {f!r} is not a tests/test_*.py file name")
                continue
            if not (TESTS / f).is_file():
                out.append(f"group {name!r}: tests/{f} does not exist")
            if f in seen:
                out.append(f"tests/{f} is listed twice ({seen[f]} and {name})")
            seen[f] = name
    for f in discovered():
        if f not in seen:
            out.append(f"tests/{f} belongs to no CI group - add it to {MANIFEST.relative_to(ROOT).as_posix()}")
    for rel in nested_test_files():
        out.append(f"{rel}: test files must live directly in tests/ (CI groups list tests/test_*.py only)")
    for name in config_overrides():
        out.append(f"{name} configures pytest discovery; the CI groups assume the default test_*.py pattern under tests/")
    return out


def files_of(groups: Dict[str, dict], group: str) -> List[str]:
    if group not in groups:
        raise SystemExit(f"unknown group {group!r}; groups: {', '.join(sorted(groups))}")
    return [f"tests/{f}" for f in groups[group]["files"]]


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else "check"
    groups = load()
    if cmd == "check":
        bad = problems(groups)
        total = sum(len(g["files"]) for g in groups.values())
        for name, g in groups.items():
            print(f"{g['job']:26} ({name}): {len(g['files'])} file(s)")
        print(f"{total} test file(s) in {len(groups)} group(s); tests/ has {len(discovered())} test file(s)")
        for b in bad:
            print("PROBLEM:", b)
        return 1 if bad else 0
    if cmd == "list":
        for f in files_of(groups, argv[1]):
            print(f)
        return 0
    if cmd == "run":
        bad = problems(groups)
        if bad:
            for b in bad:
                print("PROBLEM:", b)
            return 1
        files = files_of(groups, argv[1])
        extra = argv[3:] if len(argv) > 2 and argv[2] == "--" else argv[2:]
        os.chdir(ROOT)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import pytest
        print(f"pytest group {argv[1]!r}: {len(files)} file(s)")
        return int(pytest.main(files + list(extra)))
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
