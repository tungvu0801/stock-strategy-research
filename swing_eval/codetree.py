"""swing_eval/codetree.py — which code is checked out, read from the .git files (Stage 7.8B-1). Product code never starts a process (the Stage
3.9 rule), so nothing here runs git: the index is parsed, every tracked file is hashed against its index blob, and the tree the index would
commit is computed (sha1 of git's tree objects; nothing is written).

What product code cannot read without git's object store is whether that tree is the checked-out commit's tree, i.e. whether a change is
staged but not committed. The operator-side preflight `swing_eval/preflight.ps1` asks git (status, diff, ls-files, rev-parse) and writes a
CLEAN record with the commit and its tree; the manifest CLI requires that record and re-checks here that it still describes this checkout
(the checked-out commit, the index's computed tree, the working tree against the index). The manifest then pins the commit and the tree, and
the evaluator compares both again before any run. The record is a procedural control: git's answers on the operator's machine, not signed."""
from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from strategy_validation.engine import git_commit_sha

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_KIND = "sip_eval_preflight"
PREFLIGHT_VERSION = 1
PREFLIGHT_TOOL = "swing_eval/preflight.ps1"
PREFLIGHT_CHECKS = ("staged", "unstaged", "untracked", "unmerged", "hidden_by_index_flags", "ignored_python", "status")   # each must be NONE
TREE_CHECK = ("the working tree against the git index (modified, deleted, unmerged, untracked .py beside tracked ones; an entry hidden by an "
              "index flag, a symlink or a submodule is refused) and the index's computed tree against the manifest's code_tree (a staged "
              "change); that code_tree is the commit's tree as git reported it in the operator-side preflight")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_HIDDEN = ((0x8000, 0, "assume-unchanged"), (0, 0x4000, "skip-worktree"), (0, 0x2000, "intent-to-add"))   # (flags bit, extended bit, name)


def _gitdir(root: Path) -> Path:
    g = root / ".git"
    if g.is_file():                                                              # a worktree: ".git" names its git directory
        d = Path(g.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
        return d if d.is_absolute() else (root / d).resolve()
    return g


def _index(root: Path) -> Dict[str, Tuple[bytes, int, int, Optional[str]]]:
    """{path: (blob sha1, stage, mode, index flag or None)} from the git index file (versions 2 and 3). A split or sparse index is refused:
    some of its entries live elsewhere or are collapsed into directories."""
    data = (_gitdir(root) / "index").read_bytes()
    if data[:4] != b"DIRC":
        raise ValueError("not a git index")
    if data[-20:] not in (hashlib.sha1(data[:-20]).digest(), bytes(20)):          # bytes(20): index.skipHash
        raise ValueError("the git index checksum does not match")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise ValueError(f"git index version {version} is not read here")
    out, pos = {}, 12
    for _ in range(count):
        mode = struct.unpack(">I", data[pos + 24:pos + 28])[0]
        sha, flags = data[pos + 40:pos + 60], struct.unpack(">H", data[pos + 60:pos + 62])[0]
        ext = struct.unpack(">H", data[pos + 62:pos + 64])[0] if flags & 0x4000 else 0   # version 3: extended flags
        head = 64 if flags & 0x4000 else 62
        end = data.index(b"\0", pos + head)
        hidden = next((name for bit, xbit, name in _HIDDEN if flags & bit or ext & xbit), None)
        out[data[pos + head:end].decode("utf-8")] = (sha, (flags >> 12) & 3, mode, hidden)
        pos += (end - pos) // 8 * 8 + 8                                           # 1 to 8 NUL bytes pad each entry to a multiple of 8
    while pos < len(data) - 20:                                                   # the extensions
        if data[pos:pos + 4] == b"link":
            raise ValueError("a split index (link extension): its shared entries are not read here")
        if data[pos:pos + 4] == b"sdir":
            raise ValueError("a sparse index (sdir extension): directories outside the cone are not read here")
        pos += 8 + struct.unpack(">I", data[pos + 4:pos + 8])[0]
    if not any(stage == 0 for _, stage, _, _ in out.values()):
        raise ValueError("the git index holds no file")
    return out


def _blob(content: bytes) -> bytes:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).digest()


def tree_problems(root: Optional[Path] = None) -> List[str]:
    """The working tree against the git index. A problem: an unreadable index, an unmerged entry, an entry hidden from `git status` by an
    index flag (assume-unchanged, skip-worktree, intent-to-add), a symlink or a submodule (not checked here), a tracked file deleted or changed
    (neither its content nor its content with CRLF line ends read as LF matches the index blob: a line-end-only difference is not a change
    here), an untracked .py file beside tracked ones. A staged change is index_tree()'s part."""
    root = Path(root or ROOT)
    try:
        idx = _index(root)
    except (OSError, ValueError, IndexError, struct.error) as exc:
        return [f"the git index cannot be read: {exc}"]
    problems = []
    for rel, (sha, stage, mode, hidden) in sorted(idx.items()):
        if stage:
            problems.append(f"unmerged: {rel}")
            continue
        if hidden:
            problems.append(f"{hidden}: {rel}")
            continue
        if mode >> 12 != 0o10:
            problems.append(f"not a regular file (a symlink or a submodule): {rel}")
            continue
        p = root / rel
        if not p.is_file():
            problems.append(f"deleted: {rel}")
            continue
        content = p.read_bytes()
        if sha not in (_blob(content), _blob(content.replace(b"\r\n", b"\n"))):
            problems.append(f"modified: {rel}")
    code_dirs = {str(Path(rel).parent.as_posix()) for rel in idx if rel.endswith(".py")}
    for d in sorted(code_dirs):
        for p in sorted((root / d).glob("*.py")):
            rel = p.relative_to(root).as_posix()
            if rel not in idx:
                problems.append(f"untracked: {rel}")
    return problems


def tree_clean(root: Optional[Path] = None) -> bool:
    return not tree_problems(root)


def _tree_sha(node: dict) -> bytes:
    entries = []
    for name, v in node.items():
        n = name.encode("utf-8")
        if isinstance(v, dict):                                                   # git orders a directory as if its name ended in "/"
            entries.append((n + b"/", b"40000 " + n + b"\0" + _tree_sha(v)))
        else:
            entries.append((n, b"%o " % v[0] + n + b"\0" + v[1]))
    body = b"".join(e for _, e in sorted(entries))
    return hashlib.sha1(b"tree %d\0" % len(body) + body).digest()


def index_tree(root: Optional[Path] = None) -> str:
    """The sha1 of the tree `git write-tree` would write from the index, computed (nothing is written). It is the checked-out commit's tree
    exactly when nothing is staged."""
    top: dict = {}
    for rel, (sha, stage, mode, hidden) in _index(Path(root or ROOT)).items():
        if stage or hidden == "intent-to-add":
            raise ValueError(f"{rel}: an unmerged or intent-to-add entry is not part of a tree")
        *dirs, name = rel.split("/")
        node = top
        for d in dirs:
            node = node.setdefault(d, {})
        node[name] = (mode, sha)
    return _tree_sha(top).hex()


def index_tree_or_none(root: Optional[Path] = None) -> Optional[str]:
    try:
        return index_tree(root)
    except (OSError, ValueError, IndexError, struct.error, TypeError):
        return None


def head_commit(root: Optional[Path] = None) -> Optional[str]:
    """The checked-out commit (HEAD, read from the .git files), or None."""
    return git_commit_sha(Path(root or ROOT))


def record_problems(rec) -> List[str]:
    """A preflight record's own form (what a manifest must carry): a CLEAN record of this kind and version, every check NONE, a sha1 commit
    and tree."""
    if not isinstance(rec, dict):
        return ["no preflight record"]
    p = []
    if (rec.get("kind"), rec.get("version"), rec.get("status")) != (PREFLIGHT_KIND, PREFLIGHT_VERSION, "CLEAN"):
        p.append(f"not a CLEAN {PREFLIGHT_KIND} v{PREFLIGHT_VERSION} record: {rec.get('kind')!r} v{rec.get('version')!r} {rec.get('status')!r}")
    if rec.get("checks") != {c: "NONE" for c in PREFLIGHT_CHECKS}:
        p.append(f"the preflight did not find every check NONE ({', '.join(PREFLIGHT_CHECKS)}): {rec.get('checks')}")
    if not all(isinstance(rec.get(k), str) and _SHA1.fullmatch(rec[k]) for k in ("commit", "tree")):
        p.append("the preflight's commit or tree is not a sha1")
    return p


def preflight_problems(rec, root: Optional[Path] = None) -> List[str]:
    """A preflight record against this checkout, before a manifest is built: its form, its repository, the checked-out commit, the index's
    computed tree, and the working tree against the index. A change made after the preflight is caught here (a new commit, a staged change,
    an unstaged change, an untracked .py file); that the record's tree is its commit's tree is git's answer in the preflight."""
    root = Path(root or ROOT)
    p = record_problems(rec)
    if not isinstance(rec, dict):
        return p
    try:
        same = os.path.samefile(str(rec.get("repository") or ""), root)
    except OSError:
        same = False
    if not same:
        p.append(f"the preflight was run on another repository: {rec.get('repository')!r}")
    head = head_commit(root)
    if not head or head != rec.get("commit"):
        p.append(f"the checked-out commit {head} is not the preflight's {rec.get('commit')}")
    tree = index_tree_or_none(root)
    if not tree or tree != rec.get("tree"):
        p.append("the git index's tree is not the preflight's tree (a change staged since the preflight, or another commit)")
    return p + [f"working tree: {x}" for x in tree_problems(root)]
