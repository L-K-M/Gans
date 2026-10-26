#!/usr/bin/env python3
"""Collect the license texts of the Rust crates compiled into a PyPI wheel.

Wheels built with maturin or setuptools-rust ship their own LICENSE but not
the texts of the crates (and never the Rust standard library) linked into
their extension module. This script downloads the matching sdist from PyPI
(or takes a local one), runs `cargo metadata --locked` on the Cargo.lock it
contains, walks the resolve graph from the workspace members over normal
and build dependencies (all targets, so one file serves every platform),
and concatenates each crate's LICENSE*/LICENCE*/COPYING*/NOTICE* files.
It fails when a crate has no license text; known gaps get explicit,
commented [crate.<name>] overrides in the shared overrides TOML.

Run it once per wheel version, commit the output, and list it in that
distribution's python-notices override. Needs cargo and network access
(PyPI and crates.io); Python 3.11+ standard library otherwise.

  wheel-crates.py rpds-py==2026.9.1 --out packaging/licenses/rpds-py-2026.9.1-crates.txt
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

RULE = "-" * 72  # nested inside a python-notices entry, which uses "="
PATTERNS = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "UNLICENSE*", "COPYRIGHT*",
            "license*", "licenses/*")


def fetch_sdist(name, version, work):
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        release = json.load(response)
    sdists = [u for u in release["urls"] if u["packagetype"] == "sdist"]
    if not sdists:
        sys.exit(f"wheel-crates: {name} {version} has no sdist on PyPI; pass --sdist")
    info = sdists[0]
    target = work / info["filename"]
    with urllib.request.urlopen(info["url"], timeout=300) as response:
        target.write_bytes(response.read())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != info["digests"]["sha256"]:
        sys.exit(f"wheel-crates: sha256 mismatch for {info['filename']}")
    return target


def extract(archive, work):
    root = work / "src"
    root.mkdir()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    sys.exit(f"wheel-crates: unsafe path in sdist: {member}")
            bundle.extractall(root)
    else:
        with tarfile.open(archive) as bundle:
            if hasattr(tarfile, "data_filter"):
                bundle.extractall(root, filter="data")  # rejects absolute paths, links out, devices
            else:  # Python before 3.11.4: same checks by hand, links skipped
                members = []
                for member in bundle.getmembers():
                    parts = Path(member.name).parts
                    if member.name.startswith("/") or ".." in parts:
                        sys.exit(f"wheel-crates: unsafe path in sdist: {member.name}")
                    if member.isfile() or member.isdir():
                        members.append(member)
                bundle.extractall(root, members=members)
    entries = list(root.iterdir())
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else root


def find_manifest(project, explicit):
    if explicit:
        return project / explicit
    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        manifest = config.get("tool", {}).get("maturin", {}).get("manifest-path")
        if manifest:
            return project / manifest
    if (project / "Cargo.toml").is_file():
        return project / "Cargo.toml"
    candidates = sorted(project.rglob("Cargo.toml"), key=lambda p: (len(p.parts), str(p)))
    if not candidates:
        sys.exit("wheel-crates: no Cargo.toml in the sdist; pass --manifest-path")
    return candidates[0]


def load_overrides(path):
    if path is None:
        return {}, None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data.get("crate", {}), path.resolve().parent


def reachable_packages(document):
    nodes = {node["id"]: node for node in document["resolve"]["nodes"]}
    seen, pending = set(), list(document["workspace_members"])
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        for dependency in nodes[identifier]["deps"]:
            kinds = {kind.get("kind") for kind in dependency.get("dep_kinds", [{"kind": None}])}
            if kinds - {"dev"}:  # normal or build dependency
                pending.append(dependency["pkg"])
    return [p for p in document["packages"] if p["id"] in seen]


def read_text(path):
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return text.replace("\r\n", "\n").rstrip() + "\n"


def crate_texts(package, overrides, base, errors):
    root = Path(package["manifest_path"]).parent
    paths = set()
    if package.get("license_file"):
        paths.add(root / package["license_file"])
    for pattern in PATTERNS:
        paths.update(p for p in root.glob(pattern) if p.is_file())
    override = overrides.get(package["name"])
    if override:
        if not any(fnmatch.fnmatchcase(package["version"], v) for v in override.get("versions", [])):
            errors.append(f"{package['name']} {package['version']}: the [crate] override pins "
                          f"{override.get('versions')}; re-check and update")
        for name in override.get("files", []):
            file = (base / name).resolve()
            if file.is_file():
                paths.add(file)
            else:
                errors.append(f"{package['name']}: override file {name} not found")
    if not paths:
        errors.append(f"{package['name']} {package['version']} ({package.get('license')}): no license text. "
                      f"Add [crate.{package['name']}] with versions and files")
    return sorted(paths, key=lambda p: p.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("requirement", help="NAME==VERSION of the wheel")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, help="local sdist instead of downloading from PyPI")
    parser.add_argument("--manifest-path", help="Cargo.toml inside the sdist, if not found automatically")
    parser.add_argument("--overrides", type=Path, help="TOML with [crate.<name>] versions/files overrides")
    args = parser.parse_args()
    name, _, version = args.requirement.partition("==")
    if not version:
        parser.error("requirement must be NAME==VERSION")
    overrides, base = load_overrides(args.overrides)

    with tempfile.TemporaryDirectory(prefix="wheel-crates-") as temp:
        work = Path(temp)
        archive = args.sdist.resolve() if args.sdist else fetch_sdist(name, version, work)
        project = extract(archive, work)
        manifest = find_manifest(project, args.manifest_path)
        result = subprocess.run(
            ["cargo", "metadata", "--format-version", "1", "--locked", "--manifest-path", str(manifest)],
            capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(f"wheel-crates: cargo metadata failed:\n{result.stderr}")
        document = json.loads(result.stdout)
        errors, sections, seen = [], [], {}
        sections.append(f"Rust crates compiled into the {name} {version} wheel, resolved from its sdist's "
                        "Cargo.lock\n(all targets; build dependencies included). The Rust standard "
                        "library's notices\nare in Rust-Licenses.html.\n")
        for package in sorted(reachable_packages(document), key=lambda p: (p["name"], p["version"])):
            if package["source"] is None:  # the wheel's own workspace crates
                continue
            paths = crate_texts(package, overrides, base, errors)
            source = package.get("repository") or package.get("homepage") or ""
            header = [RULE, f"{package['name']} {package['version']}",
                      f"License: {package.get('license') or 'see the texts below'}"]
            if source:
                header.append(f"Source: {source}")
            if (package["source"] or "").startswith("registry+"):
                header.append(f"Crate: https://crates.io/crates/{package['name']}/{package['version']}")
            sections.append("\n".join(header) + "\n")
            for path in paths:
                text = read_text(path)
                digest = hashlib.sha256(text.encode()).hexdigest()
                if len(text) >= 2000 and digest in seen:
                    text = f"(Same text as {seen[digest]} above.)\n"
                else:
                    seen.setdefault(digest, f"{path.name} of {package['name']} {package['version']}")
                sections.append(f"\n--- {path.name} ---\n\n{text}")
            sections.append("\n")
    if errors:
        for message in errors:
            print(f"wheel-crates: error: {message}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(sections), encoding="utf-8", newline="\n")
    print(f"wheel-crates: wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
