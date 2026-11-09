#!/usr/bin/env python3
"""Build the pinned BitFun patch separately; never replace the baseline binary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.deepswe.context_policy import validate_capabilities


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".runtime/bitfun-paper")
    parser.add_argument("--archive", type=Path, help="Previously downloaded pinned source archive")
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--profile", choices=["dev", "release-fast", "release"], default="dev")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--test", action="store_true", help="Run focused Rust tests before publishing the binary")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "patches/bitfun/context-policy.json").read_text())
    patch = ROOT / "patches/bitfun/context-policy.patch"
    if sha256(patch) != manifest["patch_sha256"]:
        raise SystemExit("Patch differs from its source manifest")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    archive = args.archive.resolve() if args.archive else work / "downloads/source.tar.gz"
    if not archive.exists():
        if args.offline:
            raise SystemExit("Offline source archive is missing; pass --archive")
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_suffix(".download")
        urllib.request.urlretrieve(manifest["source_archive_url"], temporary)
        if sha256(temporary) != manifest["source_archive_sha256"]:
            raise SystemExit("Downloaded source archive hash mismatch")
        temporary.replace(archive)
    if sha256(archive) != manifest["source_archive_sha256"]:
        raise SystemExit("Source archive hash mismatch")

    # A fresh source tree makes patch application independent of ignored local edits.
    source_root = Path(tempfile.mkdtemp(prefix="patched-source-", dir=work))
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(source_root, filter="data")
    source = source_root / ("OpenBitFun-" + manifest["source_commit"])
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=source, check=True)
    env = dict(os.environ)
    env.setdefault("CARGO_TARGET_DIR", str(work / "target"))
    env.setdefault("CARGO_PROFILE_DEV_DEBUG", "0")
    env.setdefault("CARGO_BUILD_JOBS", "4")
    # Rustup proxies dispatch by executable name; do not resolve the cargo symlink.
    cargo = os.path.abspath(args.cargo) if "/" in args.cargo else args.cargo
    flags = ["--locked"] + (["--offline"] if args.offline else [])
    if args.test:
        for package, features, match in [
            ("bitfun-agent-runtime", [], "context_policy"),
            ("bitfun-core", ["--no-default-features", "--features", "product-full"], "work_log"),
        ]:
            subprocess.run([cargo, "test", *flags, "-p", package, *features, match, "--lib"],
                           cwd=source, env=env, check=True)
    subprocess.run([cargo, "build", *flags, "-p", "bitfun-cli", "--profile", args.profile],
                   cwd=source, env=env, check=True)
    target = Path(env["CARGO_TARGET_DIR"])
    if not target.is_absolute():
        target = source / target
    built = target / ("debug" if args.profile == "dev" else args.profile) / "bitfun"
    capability_output = subprocess.check_output([str(built), "--context-policy-capabilities"], text=True)
    capabilities = validate_capabilities(capability_output)
    destination = work / "bin/bitfun"
    if destination.resolve() == (ROOT / ".runtime/deepswe/bin/bitfun").resolve():
        raise SystemExit("Refusing to replace the pinned baseline binary")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_suffix(".pending")
    shutil.copy2(built, pending)
    receipt = {
        "implementation": manifest["implementation"],
        "source_commit": manifest["source_commit"],
        "source_archive_sha256": manifest["source_archive_sha256"],
        "patch_sha256": sha256(patch), "binary_sha256": sha256(pending),
        "capabilities": capabilities, "profile": args.profile,
        "cargo": subprocess.check_output([cargo, "--version"], env=env, text=True).strip(),
        "source_directory": str(source), "focused_rust_tests": args.test,
    }
    pending.replace(destination)
    receipt_path = destination.with_name(destination.name + ".receipt.json")
    receipt_pending = receipt_path.with_suffix(".pending")
    receipt_pending.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_pending.replace(receipt_path)
    print(json.dumps({"binary": str(destination), "receipt": str(receipt_path)}, indent=2))


if __name__ == "__main__":
    main()
