"""Guest-side verification inventory and narrowly scoped cache recovery.

Executed by the host after the native Verifier has settled. Never remove existing
candidate files, modified files, symlinks, or unknown additions. A cache directory
name alone is not authority to discard its contents.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


VERIFIER_CACHE_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPYCACHEPREFIX": "/tmp/verifier-scratch/python-cache",
    "PYTEST_ADDOPTS": "-o cache_dir=/tmp/verifier-scratch/pytest-cache",
    "XDG_CACHE_HOME": "/tmp/verifier-scratch/xdg-cache",
    "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/verifier-scratch/hypothesis",
    "CARGO_TARGET_DIR": "/tmp/verifier-scratch/cargo-target",
}
VERIFIER_WORKDIR = "/tmp/verifier-scratch/candidate"
PROTECTED_ROOTS = ("/home/user/evolution_project", "/home/user/.practice_original_project")


def inventory(roots):
    result = {}
    for root in roots:
        base = Path(root)
        if not base.exists() and not base.is_symlink():
            result[root] = None
            continue
        if base.is_symlink() or not base.is_dir():
            raise ValueError("Protected project root is not a real directory: " + root)
        tree = {}

        def visit(directory):
            for path in sorted(directory.iterdir()):
                mode = path.lstat().st_mode
                name = path.relative_to(base).as_posix()
                if stat.S_ISLNK(mode):
                    tree[name] = {"kind": "symlink", "target": str(path.readlink())}
                elif stat.S_ISREG(mode):
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    tree[name] = {"kind": "file", "sha256": digest.hexdigest(),
                                  "bytes": path.stat().st_size}
                    if path.name == "index" and path.parent.name == ".git" and path.stat().st_size <= 16 * 1024 * 1024:
                        tree[name]["git_index_snapshot"] = base64.b64encode(path.read_bytes()).decode("ascii")
                elif stat.S_ISDIR(mode):
                    tree[name] = {"kind": "directory"}
                    visit(path)
                else:
                    tree[name] = {"kind": "special"}
        visit(base)
        result[root] = tree
    return result


def index_metadata_only(path, entry, actual):
    """Compare index entries/flags and require a clean staged tree before repair.

    Git reads temporary index copies with optional writes and fsmonitor disabled.
    The enclosing inventory must also prove all refs, objects and worktree files
    unchanged. Missing snapshots or any Git error fail closed.
    """
    if entry.get("kind") != "file" or actual.get("kind") != "file":
        return False
    try:
        original = base64.b64decode(entry["git_index_snapshot"], validate=True)
        current = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != entry["sha256"]:
            return False
        if hashlib.sha256(current).hexdigest() != actual["sha256"]:
            return False
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_OPTIONAL_LOCKS"] = "0"
        command = ["git", "-c", "core.fsmonitor=false", "--git-dir=" + str(path.parent),
                   "--work-tree=" + str(path.parent.parent)]
        signatures = []
        for content in (original, current):
            with tempfile.NamedTemporaryFile(prefix="rsi-index-") as stream:
                stream.write(content)
                stream.flush()
                env["GIT_INDEX_FILE"] = stream.name
                entries = subprocess.run(command + ["ls-files", "--stage", "-v", "-z"],
                    env=env, capture_output=True, check=True, timeout=30)
                subprocess.run(command + ["diff", "--cached", "--quiet", "--no-ext-diff",
                    "--no-textconv", "HEAD", "--"], env=env, capture_output=True, check=True, timeout=30)
                signatures.append(entries.stdout)
        return signatures[0] == signatures[1]
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        return False


def recognized_cache(root, name, entry, before):
    if entry["kind"] != "file" or entry["bytes"] > 16 * 1024 * 1024:
        return False
    relative = Path(name)
    path = Path(root) / relative
    if relative.parent.name == "__pycache__":
        match = re.fullmatch(r"(.+)\.cpython-\d+(?:(?:\.opt-\d+)|(?:-pytest-[\w.]+))?\.pyc", relative.name)
        if not match:
            return False
        source = (relative.parent.parent / (match[1] + ".py")).as_posix()
        if before.get(source, {}).get("kind") != "file":
            return False
        with path.open("rb") as stream:
            header = stream.read(16)
        return len(header) == 16 and header[:4] == importlib.util.MAGIC_NUMBER
    if ".pytest_cache" not in relative.parts:
        return False
    index = relative.parts.index(".pytest_cache")
    parent = Path(*relative.parts[:index]).as_posix()
    if parent != "." and before.get(parent, {}).get("kind") != "directory":
        return False
    suffix = "/".join(relative.parts[index + 1:])
    raw = path.read_bytes()
    if suffix == ".gitignore":
        return raw == b"# Created by pytest automatically.\n*\n"
    if suffix == "CACHEDIR.TAG":
        return raw.startswith(b"Signature: 8a477f597d28d172789f06886806bc55\n")
    if suffix == "README.md":
        return raw.startswith(b"# pytest cache directory #\n")
    if suffix not in {"v/cache/nodeids", "v/cache/lastfailed", "v/cache/stepwise"}:
        return False
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return False
    if suffix.endswith("nodeids"):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if suffix.endswith("lastfailed"):
        return isinstance(value, dict) and all(isinstance(k, str) and type(v) is bool for k, v in value.items())
    # Pytest versions use either []/nodeid or a state object for stepwise.
    return value is None or isinstance(value, (list, str, dict))


def prepare_working_copy(before, destination=VERIFIER_WORKDIR):
    """Create a verified disposable copy, leaving every original byte protected."""
    destination = Path(destination)
    source = PROTECTED_ROOTS[0] if PROTECTED_ROOTS[0] in before else next(iter(before))
    if before[source] is None or inventory(before) != before:
        raise ValueError("Candidate changed before preparing verification workspace")
    original_root, resolved_destination = Path(source).resolve(), destination.resolve()
    if (destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents)
            or original_root == resolved_destination or original_root in resolved_destination.parents
            or resolved_destination in original_root.parents):
        raise ValueError("Invalid verification workspace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="prepare-") as staging:
        copied = Path(staging) / "candidate"
        shutil.copytree(source, copied, symlinks=True)
        expected = dict(before[source])
        # Absolute links into the candidate must point into the copy, so cache
        # writes through such links cannot modify the protected original tree.
        for name, entry in before[source].items():
            if entry["kind"] != "symlink":
                continue
            original = Path(source) / name
            target = (original.parent / entry["target"]).resolve()
            if target.is_relative_to(Path(source).resolve()):
                mapped = str(destination / target.relative_to(Path(source).resolve()))
                link = copied / name
                link.unlink()
                link.symlink_to(mapped)
                expected[name] = {"kind": "symlink", "target": mapped}
        if inventory([str(copied)])[str(copied)] != expected or inventory(before) != before:
            raise ValueError("Verification copy does not match the candidate")
        if destination.exists():
            shutil.rmtree(destination)
        copied.rename(destination)
    return {"status": "prepared", "source": source, "working_copy": str(destination),
            "files": sum(entry["kind"] == "file" for entry in before[source].values()),
            "original_unchanged": True, "inventory": expected,
            "result_caches": [name for name, entry in expected.items()
                              if vitest_result_cache(destination, name, entry)]}


def vitest_result_cache(root, name, entry):
    """Only typed Vitest metadata in the disposable copy may be rewritten."""
    if (entry.get("kind") != "file" or entry["bytes"] > 16 * 1024 * 1024
            or not re.fullmatch(r"(?:[^/]+/)*node_modules/\.vite/vitest/(?:[0-9a-f]{40}/)?results\.json", name)):
        return False
    try:
        value = json.loads((Path(root) / name).read_text())
    except (ValueError, UnicodeError, OSError):
        return False
    if (not isinstance(value, dict) or set(value) != {"version", "results"}
            or not isinstance(value["version"], str) or not value["version"]
            or not isinstance(value["results"], list)):
        return False
    for item in value["results"]:
        if (not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                or not isinstance(item[1], dict) or set(item[1]) != {"duration", "failed"}):
            return False
        duration = item[1]["duration"]
        if type(item[1]["failed"]) is not bool or type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
            return False
    return True


def verify_working_copy(workspace):
    root, expected = workspace["working_copy"], workspace["inventory"]
    actual = inventory([root])[root]
    rejected, caches, indexes = [], [], []
    if actual is None:
        return {"status": "rejected", "changed": [root], "result_caches": []}
    for name, entry in expected.items():
        if actual.get(name) == entry:
            continue
        path = Path(root) / name
        if path.name == "index" and path.parent.name == ".git" and name in actual:
            indexes.append((path, entry, actual[name]))
            continue
        if (name in workspace["result_caches"] and name in actual
                and vitest_result_cache(root, name, actual[name])):
            caches.append(name)
        else:
            rejected.append(name)
    allowed_dirs = set()
    for name, entry in actual.items():
        if name in expected or entry["kind"] == "directory":
            continue
        if vitest_result_cache(root, name, entry) or recognized_cache(root, name, entry, expected):
            caches.append(name)
            allowed_dirs.update(parent.as_posix() for parent in Path(name).parents)
        else:
            rejected.append(name)
    rejected.extend(name for name, entry in actual.items()
                    if name not in expected and entry["kind"] == "directory" and name not in allowed_dirs)
    refreshed = []
    for path, entry, current in indexes:
        if rejected or not index_metadata_only(path, entry, current):
            rejected.append(path.relative_to(root).as_posix())
        else:
            refreshed.append(path.relative_to(root).as_posix())
    return {"status": "rejected" if rejected else "verified", "changed": sorted(rejected),
            "result_caches": sorted(caches), "git_index_metadata_drift": refreshed}


def recover(before):
    after = inventory(before)
    report = {"status": "unchanged", "changed": [], "missing": [], "unexpected": [],
              "new_cache_files": [], "removed": [], "git_index_metadata_drift": [],
              "restored_git_indexes": [], "before": before, "after": after}
    cleanup = []
    directories = []
    indexes = []
    for root, expected in before.items():
        actual = after[root]
        if expected is None or actual is None:
            if expected != actual:
                report["unexpected"].append(root + " (root presence changed)")
            continue
        for name, entry in expected.items():
            if name not in actual:
                report["missing"].append(str(Path(root) / name))
            elif actual[name] != entry:
                path = Path(root) / name
                if path.name == "index" and path.parent.name == ".git" and "git_index_snapshot" in entry:
                    indexes.append((path, entry, actual[name]))
                else:
                    report["changed"].append(str(path))
        extras = {name: entry for name, entry in actual.items() if name not in expected}
        allowed_dirs = set()
        for name, entry in extras.items():
            path = Path(root) / name
            if entry["kind"] == "directory":
                directories.append(path)
            elif recognized_cache(root, name, entry, expected):
                cleanup.append(path)
                report["new_cache_files"].append(str(path))
                parent = Path(name).parent
                while parent.as_posix() != ".":
                    allowed_dirs.add(parent.as_posix())
                    parent = parent.parent
            else:
                report["unexpected"].append(str(path))
        for name, entry in extras.items():
            if entry["kind"] == "directory" and name not in allowed_dirs:
                report["unexpected"].append(str(Path(root) / name))
    other_drift = bool(report["changed"] or report["missing"] or report["unexpected"])
    for path, entry, actual in indexes:
        if other_drift or not index_metadata_only(path, entry, actual):
            report["changed"].append(str(path))
        else:
            report["git_index_metadata_drift"].append(str(path))
    if report["changed"] or report["missing"] or report["unexpected"]:
        report["status"] = "rejected"
        return report  # Preserve all evidence, including caches, on a real drift.
    # Re-inventory before removal. No unchecked cache wildcard deletion.
    if inventory(before) != after:
        report.update(status="rejected", error="Protected tree changed during recovery")
        return report
    for path in cleanup:
        path.unlink()
        report["removed"].append(str(path))
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        path.rmdir()
    for path, entry, _ in indexes:
        # Restore the exact Actor bytes so the subsequent strict snapshot check
        # remains authoritative. Never run reset/checkout or rewrite Git history.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="rsi-index-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(base64.b64decode(entry["git_index_snapshot"]))
        try:
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        report["restored_git_indexes"].append(str(path))
    final = inventory(before)
    report["final_matches"] = final == before
    report["status"] = ("cache_recovered" if cleanup else "metadata_recovered" if indexes else "unchanged") if final == before else "rejected"
    return report


def main():
    if sys.argv[1] == "inventory":
        result = inventory(PROTECTED_ROOTS)
        if result[PROTECTED_ROOTS[0]] is None:
            raise ValueError("Missing candidate before Verifier inspection")
    elif sys.argv[1] == "recover":
        result = recover(json.loads(Path(sys.argv[2]).read_text()))
    elif sys.argv[1] == "prepare":
        result = prepare_working_copy(json.loads(Path(sys.argv[2]).read_text()))
    elif sys.argv[1] == "verify-copy":
        result = verify_working_copy(json.loads(Path(sys.argv[2]).read_text()))
    else:
        raise ValueError("Unknown integrity operation")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
