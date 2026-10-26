#!/usr/bin/env python3
"""Write Third-Party-Notices.txt for the Python code an artifact ships.

Scans the installed distributions of a pip --target tree, a site-packages
directory, or (with --files-from) exactly the files a PyInstaller build
collected. For each distribution it copies the license files its metadata
ships, adds the explicit overrides from a TOML file, and scans native files
for code compiled into them: the Rust standard library and crates, OpenSSL,
libsodium, libffi and other C libraries, plus bundled shared libraries,
fonts and vendored packages. The project's own distributions (--own) are
scanned the same way, without their own license. With --cpython it adds the
interpreter a frozen app or image carries.

It exits non-zero and lists every problem when something that ships has no
license text, when native content has no reviewed override, or when an
override pins a version other than the one installed. Overrides never
disappear silently: each one names the versions it was checked for.

Python 3.11+ standard library only. Usage and override format: RECIPE.md
and python-notices.example.toml next to this script.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import importlib.metadata as metadata
import json
import mmap
import os
import re
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

RULE = "=" * 72
# Files whose names mark license texts and notices, also after a prefix
# (MIT-LICENSE, ICON_LICENSE.md).
LICENSE_NAME = re.compile(r"(?:^|[-_. ])(?:LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|UNLICENSE|PATENTS)", re.I)
LICENSE_DIRS = ("licenses", "license_files", "LICENSES")
NOT_LICENSE_SUFFIXES = {".py", ".pyc", ".pyi", ".pyo", ".so", ".pyd", ".dll", ".dylib",
                        ".json", ".js", ".h", ".c", ".cpp", ".pyx", ".pxd"}
# Never native code; skipped without opening, which keeps big trees fast.
TEXT_SUFFIXES = {".py", ".pyi", ".pyc", ".pyo", ".txt", ".md", ".rst", ".json", ".toml",
                 ".cfg", ".ini", ".typed", ".html", ".htm", ".css", ".js", ".mjs", ".qml",
                 ".qmltypes", ".qm", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".ico",
                 ".pak", ".dat", ".xml", ".yaml", ".yml", ".h", ".hpp", ".c", ".cpp", ".pxd",
                 ".pyx", ".csv", ".pem", ".sh", ".pc", ".cmake", ".prl", ".metatypes"}
# Fonts usually carry their own license (OFL-1.1, Apache-2.0, ...), not the package's.
FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".pfa", ".pfb"}
NATIVE_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"MZ")
EXTENSION = re.compile(r"\.(?:cpython-[^.]+|abi3|pypy[^.]*|cp\d+[^.]*)\.(?:so|pyd|dylib)$|\.pyd$")
# Code compiled into a native file: (item, cheap literal anchor, evidence,
# evidence of dynamic linking that means the library is not compiled in).
# Heuristics: a hit asks for a reviewed override, a miss proves nothing.
_DYN = r"[\w.-]*?\.(?:so|dylib|dll)"
STATIC = [
    # OPENSSL_VERSION_TEXT ("OpenSSL 3.0.15 3 Sep 2024") exists only where libcrypto is compiled in.
    ("openssl", b"OpenSSL ", rb"OpenSSL \d+\.\d+\.\d+[a-z]?(?:-[\w.]+)? +\d{1,2} [A-Z][a-z]{2} \d{4}", None),
    ("boringssl", b"BoringSSL", rb"BoringSSL", None),
    ("libsodium", b"sodium_init", rb"sodium_init", ("libsodium" + _DYN).encode()),
    ("libffi", b"ffi_prep_cif", rb"ffi_prep_cif", ("libffi" + _DYN).encode()),
    ("zlib", b"flate ", rb"(?:deflate|inflate) \d+\.\d+[\w.-]* Copyright", None),
    ("sqlite", b"SQLite format 3", rb"SQLite format 3", ("libsqlite3" + _DYN).encode()),
    ("expat", b"expat_", rb"expat_\d+\.\d+\.\d+", ("libexpat" + _DYN).encode()),
    ("libxml2", b"xmlParseDocument", rb"xmlParseDocument", ("libxml2" + _DYN).encode()),
    ("libxslt", b"xsltApplyStylesheet", rb"xsltApplyStylesheet", ("libxslt" + _DYN).encode()),
    ("libyaml", b"yaml_parser_initialize", rb"yaml_parser_initialize", ("libyaml" + _DYN).encode()),
    ("liblzma", b"lzma_stream_decoder", rb"lzma_stream_decoder", ("liblzma" + _DYN).encode()),
    ("bzip2", b"libbzip2: internal error", rb"libbzip2: internal error", None),
    ("mpdecimal", b"MPD_MINALLOC", rb"MPD_MINALLOC", ("libmpdec" + _DYN).encode()),
    ("tcl-tk", b"tcl_patchLevel", rb"tcl_patchLevel", rb"lib(?:tcl|tk)[\w.-]*?\.(?:so|dylib|dll)|Tcl\.framework"),
    ("ncurses", b"ncurses ", rb"ncurses \d+\.\d+\.\d{8}", ("libncurses" + _DYN).encode()),
    ("berkeley-db", b"Berkeley DB ", rb"Berkeley DB \d+\.\d+", ("libdb" + _DYN).encode()),
    ("go-runtime", b"\xff Go buildinf:", rb"\xff Go buildinf:", None),
]
RUSTC = re.compile(rb"[/\\]rustc[/\\]([0-9a-f]{40})[/\\]")
CRATE = re.compile(rb"[/\\](?:registry[/\\]src[/\\][^/\\\x00]{1,80}|vendor)[/\\]"
                   rb"([A-Za-z0-9_][A-Za-z0-9_-]*?)-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)[/\\]")
VENDORED_DIR = re.compile(r"(?:^|/)_?vendor(?:ed)?/([^/]+)/")
VENDORED_MODULE = re.compile(r"(?:^|/)_?vendor(?:ed)?/([^/]+)\.py$")
COPYLEFT = re.compile(r"\b(?:A?GPL|LGPL|MPL|EPL|CDDL|EUPL)", re.I)
INCORPORATED = "Incorporated Software"  # heading of CPython's third-party section
INCORPORATED_FIRST = "Mersenne Twister"  # its first subsection in every CPython 3 release
# [cpython] items that are GPL-3.0 libraries and so need a source link.
GPL_CPYTHON_ITEMS = {"gnu-readline", "gnu-gdbm"}
SYSTEM_PREFIXES = {"/", "/usr", "/usr/local"}
CLASSIFIER_SPDX = {
    "MIT License": "MIT", "MIT No Attribution License (MIT-0)": "MIT-0",
    "Apache Software License": "Apache-2.0", "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0", "BSD License": "BSD",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0", "The Unlicense (Unlicense)": "Unlicense",
    "Zero-Clause BSD (0BSD)": "0BSD", "zlib/libpng License": "Zlib",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
}
# Common SPDX identifiers, to tell expressions from free-text License fields.
SPDX_IDS = {
    "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "PSF-2.0", "Python-2.0",
    "MPL-2.0", "LGPL-2.0-only", "LGPL-2.0-or-later", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0-only", "LGPL-3.0-or-later", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only",
    "GPL-3.0-or-later", "AGPL-3.0-only", "AGPL-3.0-or-later", "Unlicense", "0BSD", "Zlib",
    "CC0-1.0", "BSL-1.0", "Unicode-3.0", "Unicode-DFS-2016", "HPND", "EPL-2.0", "OFL-1.1",
    "BlueOak-1.0.0", "CNRI-Python", "X11", "curl", "OpenSSL", "SSLeay-standalone",
    "LLVM-exception", "Qt-GPL-exception-1.0", "Qt-LGPL-exception-1.1", "GCC-exception-3.1",
    "Classpath-exception-2.0", "AND", "OR", "WITH",
}
SOURCE_LABELS = ("source", "source code", "repository", "code", "github", "homepage", "home")
OVERRIDE_KEYS = {
    "distribution": {"versions", "files", "installed_files", "license", "source", "covers", "exclude"},
    "component": {"name", "version", "license", "source", "files", "paths"},
    "cpython": {"versions", "files", "covers", "source"},
}


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_text(path):
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def matches(patterns, value):
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


@dataclass
class Entry:
    name: str
    version: str
    license: str
    source: str
    notes: list = field(default_factory=list)
    texts: list = field(default_factory=list)  # (label, text)
    items: list = field(default_factory=list)  # detected content, for --inventory


class Problems:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)


def load_overrides(path, problems):
    empty = {"distribution": {}, "component": [], "cpython": None}
    if path is None:
        return empty, None
    if tomllib is None:
        sys.exit("python-notices: --overrides needs Python 3.11 or newer (tomllib)")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:  # missing file or invalid TOML
        problems.error(f"--overrides {path}: {error}")
        return empty, None
    base = path.resolve().parent
    for key in data:
        if key not in OVERRIDE_KEYS and key != "crate":  # crate tables belong to wheel-crates.py
            problems.error(f"{path}: unknown table [{key}]")
    distributions = {}
    for name, entry in data.get("distribution", {}).items():
        check_keys(path, f"distribution.{name}", entry, OVERRIDE_KEYS["distribution"], problems)
        if not entry.get("versions"):
            problems.error(f"{path}: [distribution.{name}] needs versions = [...] (the versions it was checked for)")
        distributions[normalize(name)] = entry
    components = data.get("component", [])
    for entry in components:
        check_keys(path, f"component {entry.get('name')}", entry, OVERRIDE_KEYS["component"], problems)
        if not entry.get("name") or not entry.get("files"):
            problems.error(f"{path}: every [[component]] needs name and files")
        # Copyleft components need the same exact source link as copyleft wheels.
        if COPYLEFT.search(entry.get("license", "")) and not entry.get("source"):
            problems.error(f"{path}: [[component]] {entry.get('name')} is copyleft "
                           f"({entry['license']}); set source to its exact source package")
    cpython = data.get("cpython")
    if cpython is not None:
        check_keys(path, "cpython", cpython, OVERRIDE_KEYS["cpython"], problems)
    return {"distribution": distributions, "component": components, "cpython": cpython}, base


def check_keys(path, where, entry, allowed, problems):
    for key in set(entry) - allowed:
        problems.error(f"{path}: [{where}] has unknown key {key!r}")


def override_texts(entry, base, problems, where):
    texts = []
    for name in entry.get("files", []):
        file = (base / name).resolve()
        if not file.is_file() or file.stat().st_size == 0:
            problems.error(f"{where}: override file {name} is missing or empty")
            continue
        texts.append((file.name, read_text(file)))
    return texts


@dataclass
class Dist:
    name: str
    key: str
    version: str
    meta: object
    info_dir: Path
    root: Path
    files: list | None  # absolute paths from RECORD, None without RECORD


def discover(paths, problems):
    found = {}
    search = [str(p) for p in paths] if paths else None
    for dist in metadata.distributions(path=search) if search else metadata.distributions():
        info_dir = getattr(dist, "_path", None)
        name = dist.metadata.get("Name")
        if info_dir is None or not name:
            continue
        info_dir = Path(info_dir)
        key = normalize(name)
        version = dist.metadata.get("Version") or "unknown"
        if key in found:
            if found[key].version == version:
                continue
            message = f"{name}: versions {found[key].version} and {version} both installed"
            if search:  # a shipped tree with two copies: unclear which files ship
                problems.error(message + "; remove the stale dist-info from the tree")
            else:  # sys.path order: the first one imports; shipped files of the other end up unowned
                problems.warn(message + "; using the first on sys.path")
            continue
        files = None
        if dist.files is not None:
            files = [Path(dist.locate_file(f)) for f in dist.files]
        found[key] = Dist(name, key, version, dist.metadata, info_dir, info_dir.parent, files)
    return found


def license_files(dist):
    """License files: License-File entries, dist-info license dirs and names, vendored copies."""
    found = {}
    info = dist.info_dir
    if not info.is_dir():  # single-file .egg-info: no license files beside it
        return []
    for declared in dist.meta.get_all("License-File") or []:
        for candidate in (info / "licenses" / declared, info / declared, info / "license_files" / declared):
            if candidate.is_file():
                found[candidate.resolve()] = candidate.relative_to(info).as_posix()
                break
    for sub in LICENSE_DIRS:
        if (info / sub).is_dir():
            for file in (info / sub).rglob("*"):
                if file.is_file():
                    found[file.resolve()] = file.relative_to(info).as_posix()
    for file in info.iterdir():
        if file.is_file() and LICENSE_NAME.search(file.name):
            found[file.resolve()] = file.name
    # License files elsewhere in the installed files, e.g. vendored packages' own.
    for file in dist.files or []:
        if (LICENSE_NAME.search(file.name) and file.suffix.lower() not in NOT_LICENSE_SUFFIXES
                and info not in file.parents and file.is_file()):
            found.setdefault(file.resolve(), relative(file, dist.root))
    return sorted(found.items(), key=lambda item: item[1])


def relative(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return Path(os.path.relpath(path, root)).as_posix()


def is_spdx(value):
    tokens = value.replace("(", " ").replace(")", " ").split()
    return bool(tokens) and all(t in SPDX_IDS or t.startswith("LicenseRef-") for t in tokens)


def license_expression(meta):
    """Return (license, is_spdx): License-Expression, an SPDX License field or classifiers."""
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip(), True
    value = (meta.get("License") or "").strip()
    short = value if value and "\n" not in value and len(value) <= 80 and value.upper() != "UNKNOWN" else ""
    if short and is_spdx(short):
        return short, True
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::") and c != "License :: OSI Approved"]
    mapped = " OR ".join(CLASSIFIER_SPDX.get(c, c) for c in classifiers)
    if mapped and is_spdx(mapped):
        return mapped, True
    return short or mapped, False


def source_url(meta, name, version):
    urls = {}
    for value in meta.get_all("Project-URL") or []:
        label, _, url = value.partition(",")
        urls[label.strip().lower()] = url.strip()
    for label in SOURCE_LABELS:
        if urls.get(label):
            return urls[label]
    home = meta.get("Home-page")
    if home and home.upper() != "UNKNOWN":
        return home.strip()
    return f"https://pypi.org/project/{name}/{version}/"


def is_native(path):
    if path.suffix.lower() in TEXT_SUFFIXES or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4).startswith(NATIVE_MAGIC)


def is_extension(path):
    name = path.name
    if EXTENSION.search(name):
        return True
    # Old-style "module.so": not lib-prefixed and a valid module name.
    return name.endswith(".so") and not name.startswith("lib") and name[:-3].isidentifier()


def scan_native(path):
    """Return (static library items, rustc commits, crates) compiled into a native file."""
    items, commits, crates = set(), set(), set()
    with path.open("rb") as handle:
        try:
            data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:  # empty file
            return items, commits, crates
    with data:
        for item, anchor, evidence, dynamic in STATIC:
            if data.find(anchor) >= 0 and re.search(evidence, data) and not (dynamic and re.search(dynamic, data)):
                items.add(item)
        if data.find(b"rustc") >= 0:
            commits = {match.decode() for match in RUSTC.findall(data)}
        if data.find(b"registry") >= 0 or data.find(b"vendor") >= 0:
            crates = {(n.decode(), v.decode()) for n, v in CRATE.findall(data)}
    return items, commits, crates


def read_sboms(dist, problems):
    """Components from PEP 770 SBOMs in dist-info/sboms (CycloneDX or SPDX JSON)."""
    crates, others = set(), set()
    own = {normalize(dist.name), normalize(dist.name).replace("-", "_")}

    def add(name, version, purl):
        if not name or normalize(name) in own:
            return
        if (purl or "").startswith("pkg:cargo/"):
            if "download_url=file" not in purl:  # local path crates are the wheel's own code
                crates.add((name, version or ""))
        else:
            others.add(name.lower())

    def walk(components):
        for component in components or []:
            add(component.get("name"), component.get("version"), component.get("purl"))
            walk(component.get("components"))

    sboms = dist.info_dir / "sboms"
    for file in sorted(sboms.glob("*.json")) if sboms.is_dir() else []:
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            problems.error(f"{dist.name} {dist.version}: unreadable SBOM {file.name}: {error}")
            continue
        walk(document.get("components"))
        for package in document.get("packages", []):  # SPDX
            purl = next((ref.get("referenceLocator") for ref in package.get("externalRefs", [])
                         if ref.get("referenceType") == "purl"), "")
            add(package.get("name"), package.get("versionInfo"), purl)
    return crates, others


def summarize(items, limit=15):
    shown = "; ".join(items[:limit])
    return shown + (f"; and {len(items) - limit} more (see --inventory)" if len(items) > limit else "")


def mentions_crate(text, name, version):
    """True when a crate notices file has a "<name> <version>" header line for the crate."""
    names = {name, name.replace("-", "_"), name.replace("_", "-")}
    return any(re.search(rf"(?m)^{re.escape(n)}[ @]v?{re.escape(version)}\s*$", text) for n in names)


def build_entry(dist, override, base, shipped, problems, state, own=False):
    """Entry for a third-party distribution. For the project's own distribution
    (own=True) only third-party code found inside it counts; returns None if none."""
    where = f"{dist.name} {dist.version}"
    texts = [(label, read_text(path)) for path, label in license_files(dist)
             if not (own and dist.info_dir.resolve() in path.parents)]  # own dist-info: the project's license
    covers = []
    license_value, spdx = license_expression(dist.meta)
    source = source_url(dist.meta, dist.name, dist.version)
    explicit_source = False
    if own:
        license_value, spdx, source = "see the texts below", True, ""
    if override:
        if not matches(override.get("versions", []), dist.version):
            problems.error(f"{where}: the override pins versions {override.get('versions')}; "
                           "re-check its native content and texts, then update versions")
        texts += override_texts(override, base, problems, where)
        for name in override.get("installed_files", []):
            file = dist.root / name
            if file.is_file():
                texts.append((name, read_text(file)))
            else:
                problems.error(f"{where}: installed file {name} not found")
        covers = override.get("covers", [])
        if override.get("license"):
            license_value, spdx = override["license"], True
        if override.get("source"):
            source, explicit_source = override["source"], True
    if not texts and not own:
        problems.error(f"{where}: no license text. Add [distribution.{dist.key}] with versions and files")
    if not spdx:
        problems.warn(f"{where}: license {license_value or '(none)'!r} is not an SPDX expression; "
                      f"set license in [distribution.{dist.key}]")
        license_value = license_value or "see the texts below"

    items, commits, crates, native_files = {}, set(), set(), 0
    if dist.files is None:
        items["unscanned"] = "no RECORD, so its files could not be checked"
    files = dist.files or []
    if shipped is not None:  # frozen app: only what the bundler collected ships
        files = [f for f in files if Path(os.path.realpath(f)) in shipped]
    for file in files:
        rel = relative(file, dist.root)
        for pattern in (VENDORED_DIR, VENDORED_MODULE):
            match = pattern.search(rel)
            name = match.group(1) if match else ""
            if name and name not in ("__pycache__", "__init__") and not name.endswith(".dist-info"):
                items[f"vendored:{name}"] = "vendored package"
        if file.suffix.lower() in FONT_SUFFIXES:
            items[f"font:{rel}"] = "bundled font"
            continue
        if not is_native(file):
            continue
        native_files += 1
        found, file_commits, file_crates = scan_native(file)
        for item in found:
            items[item] = f"compiled into {rel}"
        commits |= file_commits
        crates |= file_crates
        if not is_extension(file):
            items[f"native:{rel}"] = "bundled native file"
    sbom_crates, sbom_others = read_sboms(dist, problems)
    crates |= sbom_crates
    for name in sbom_others:
        items.setdefault(name, "listed in the wheel's SBOM")

    labels = " ".join(label.lower() for label, _ in texts)
    alltext = "\n".join(text for _, text in texts)
    used = set()
    uncovered = []
    for item in sorted(items):
        hits = {p for p in covers if fnmatch.fnmatchcase(item, p)}
        used |= hits
        name = (item.partition(":")[2] or item).lower()
        # Vendored packages and SBOM components count as covered by a license
        # file whose path names them, e.g. licenses/src/pip/_vendor/rich/LICENSE
        # or openssl-3.5.4-LICENSE.txt; the name must be a whole token there.
        token = re.escape(name) + r"(?![a-z0-9_])"  # tomli must not match tomli_w/
        named = (item.startswith("vendored:") and re.search("_vendor/" + token, labels)) or (
            items[item] == "listed in the wheel's SBOM" and re.search(r"(?<![a-z0-9])" + token, labels))
        if not hits and not named:
            uncovered.append(f"{item} ({items[item]})")
    own_crates = {normalize(dist.name), normalize(dist.name).replace("-", "_")}
    missing_crates = []
    for name, version in sorted(crates):
        if normalize(name) in own_crates or name in own_crates:
            continue
        item = f"crate:{name}@{version}"
        hits = {p for p in covers if fnmatch.fnmatchcase(item, p)}
        if hits:
            used |= hits
        elif not (version and mentions_crate(alltext, name, version)):
            missing_crates.append(f"{name} {version}".strip())
    if uncovered:
        problems.error(f"{where}: native or vendored content without a reviewed override: "
                       + summarize(uncovered)
                       + f". Ship its texts via [distribution.{dist.key}] files and list it in covers")
    if missing_crates:
        problems.error(f"{where}: Rust crates without license text: {', '.join(missing_crates)}. "
                       "Generate them with wheel-crates.py and add the file to the override")
    for pattern in sorted(set(covers) - used):
        problems.warn(f"{where}: covers pattern {pattern!r} matched nothing")
    if commits:
        state["rust"].setdefault(where, set()).update(commits)
    if native_files and COPYLEFT.search(license_value) and not explicit_source:
        problems.error(f"{where}: copyleft ({license_value}) native code needs an exact source link; "
                       f"set source in [distribution.{dist.key}]")
    if own:
        if not (items or crates or commits):
            return None
        if (items or crates) and not texts:
            problems.error(f"{where}: the project's own distribution contains third-party code without "
                           f"license text; add [distribution.{dist.key}] with files and covers")

    entry = Entry(f"Third-party code in {dist.name}" if own else dist.name, dist.version, license_value,
                  source, texts=texts,
                  items=sorted(items) + [f"crate:{n}@{v}" for n, v in sorted(crates)])
    static = sorted(i for i in items if not i.startswith(("native:", "vendored:")) and i != "unscanned")
    if commits:
        entry.notes.append("Statically links the Rust standard library (rustc "
                           + ", ".join(sorted(commits)) + "); its notices are in Rust-Licenses.html.")
    if static:
        entry.notes.append("Contains: " + ", ".join(static) + ".")
    if crates:
        entry.notes.append("Rust crates: " + ", ".join(f"{n} {v}".strip() for n, v in sorted(crates)) + ".")
    return entry


def cpython_entry(override, base, shipped_modules, scanned, args, problems):
    version = sys.version.split()[0]
    where = f"CPython {version}"
    candidates = [Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE.txt"]
    license_file = next((c for c in candidates if c.is_file()), None)
    texts = []
    if license_file is None:
        problems.error(f"{where}: LICENSE.txt not found next to the standard library ({candidates[0]})")
    else:
        texts.append(("LICENSE.txt", read_text(license_file)))
    covers = []
    if override is None:
        problems.error(f"{where}: --cpython needs a [cpython] override with CPython's "
                       "'Licenses and Acknowledgements for Incorporated Software' (fetch-cpython-notices.py)")
    else:
        if not matches(override.get("versions", []), version):
            problems.error(f"{where}: [cpython] pins versions {override.get('versions')}; "
                           "fetch the notices for this version and update versions")
        texts += override_texts(override, base, problems, where)
        covers = override.get("covers", [])
        if not any(INCORPORATED in text and INCORPORATED_FIRST in text for _, text in texts):
            problems.error(f"{where}: no complete '{INCORPORATED}' text among the [cpython] files "
                           "(fetch-cpython-notices.py)")
    items = dict(scanned)
    modules = shipped_modules | set(sys.builtin_module_names)
    if "readline" in modules:
        try:
            import readline
            if "editline" not in readline._READLINE_LIBRARY_VERSION.lower():
                items["gnu-readline"] = "the readline module links GNU readline (GPL-3.0)"
        except (ImportError, AttributeError):
            pass
    if "_gdbm" in modules:
        items["gnu-gdbm"] = "the _gdbm module links GNU gdbm (GPL-3.0)"
    used = set()
    uncovered = []
    for item in sorted(items):
        hits = {p for p in covers if fnmatch.fnmatchcase(item, p)}
        used |= hits
        if not hits:
            uncovered.append(f"{item} ({items[item]})")
    if uncovered:
        problems.error(f"{where}: interpreter content without a reviewed override: " + summarize(uncovered)
                       + ". Add the texts to [cpython] files and list the items in covers")
    for pattern in sorted(set(covers) - used):
        problems.warn(f"{where}: covers pattern {pattern!r} matched nothing")
    source = (override or {}).get("source", "")
    gpl = sorted(GPL_CPYTHON_ITEMS & set(items))
    if gpl and not source:
        problems.error(f"{where}: {', '.join(gpl)} (GPL-3.0) needs an exact source link; "
                       "set source in [cpython] to the library's source package")
    entry = Entry("CPython", version, "PSF-2.0", f"https://github.com/python/cpython/tree/v{version}",
                  texts=texts, items=sorted(items))
    if source:
        entry.notes.append(f"Source of the linked libraries: {source}")
    reported = runtime_versions(modules)
    if reported:
        entry.notes.append("Libraries reported by the interpreter: " + "; ".join(reported) + ".")
    static = sorted(i for i in items if not i.startswith("native:"))
    if static:
        entry.notes.append("Includes or links: " + ", ".join(static) + ".")
    return entry


def runtime_versions(modules):
    probes = [
        ("_ssl", lambda: __import__("ssl").OPENSSL_VERSION),
        ("_sqlite3", lambda: "SQLite " + __import__("sqlite3").sqlite_version),
        ("zlib", lambda: "zlib " + __import__("zlib").ZLIB_RUNTIME_VERSION),
        ("pyexpat", lambda: __import__("pyexpat").EXPAT_VERSION),
        ("_decimal", lambda: "libmpdec " + __import__("_decimal").__libmpdec_version__),
        ("_tkinter", lambda: "Tcl/Tk " + str(__import__("tkinter").TclVersion)),
    ]
    reported = []
    for module, probe in probes:
        if module in modules:
            with contextlib.suppress(Exception):  # absent or broken optional module: report nothing
                reported.append(probe())
    return reported


def stdlib_dirs():
    dirs = {Path(sysconfig.get_path(key)).resolve() for key in ("stdlib", "platstdlib")}
    return dirs


def under(path, roots):
    return any(root == path or root in path.parents for root in roots)


def classify_shipped(shipped, dists, own_paths, components, problems):
    """Map shipped files to distributions; return owners, CPython modules and items, unowned files."""
    owner = {}
    for dist in dists.values():
        for file in dist.files or []:
            owner.setdefault(Path(os.path.realpath(file)), dist.key)
    owners, modules, items = set(), set(), {}
    std = stdlib_dirs()
    base = Path(os.path.realpath(sys.base_prefix))
    private_prefix = str(base) not in SYSTEM_PREFIXES
    unowned = []
    for file in shipped:
        if file in owner:
            owners.add(owner[file])
            continue
        if under(file, own_paths):
            continue
        if any(matches(c.get("paths", []), file.as_posix()) for c in components):
            continue
        if under(file, std) or file.name.startswith("libpython") or (
                private_prefix and under(file, {base})):
            modules.add(file.name.split(".")[0])
            if is_native(file):
                found, _, _ = scan_native(file)
                for item in found:
                    items[item] = f"compiled into {file.name}"
                if not under(file, std) and not file.name.startswith("libpython"):
                    items[f"native:{file.name}"] = "shared library shipped with the interpreter"
            continue
        unowned.append(file)
    if unowned:
        listed = ", ".join(p.as_posix() for p in sorted(unowned)[:20])
        problems.error(f"{len(unowned)} shipped files belong to no distribution, the interpreter or --own-path: "
                       f"{listed}. Add a [[component]] with paths and files for them")
    return owners, modules, items


def all_stdlib_natives():
    """Without --files-from, the interpreter ships its whole lib-dynload and libpython."""
    modules, items = set(), {}
    candidates = list(Path(sysconfig.get_path("platstdlib")).glob("lib-dynload/*"))
    libdir, library = sysconfig.get_config_var("LIBDIR"), sysconfig.get_config_var("LDLIBRARY")
    if libdir and library:
        candidates.append(Path(libdir) / library)
    candidates.append(Path(os.path.realpath(sys.executable)))  # statically linked interpreters
    # Windows keeps extension modules and their DLLs (OpenSSL, SQLite, libffi) in DLLs/.
    candidates += [*Path(sys.base_prefix).glob("DLLs/*"), *Path(sys.base_prefix).glob("python3*.dll")]
    for file in candidates:
        modules.add(file.name.split(".")[0])
        if is_native(file):
            found, _, _ = scan_native(file)
            for item in found:
                items[item] = f"compiled into {file.name}"
    return modules, items


def render(entries, title, rust):
    out = ["Third-party notices", "", title, ""]
    if rust:
        out += ["Rust standard library notices are in Rust-Licenses.html, next to this file.", ""]
    seen = {}
    for entry in sorted(entries, key=lambda e: (e.name.casefold(), e.version)):
        out += [RULE, f"{entry.name} {entry.version}".rstrip(), f"License: {entry.license}"]
        if entry.source:
            out.append(f"Source: {entry.source}")
        out += entry.notes
        for label, text in entry.texts:
            digest = hashlib.sha256(text.encode()).hexdigest()
            out += ["", f"--- {label} ---", ""]
            if len(text) >= 2000 and digest in seen:
                out.append(f"(Same text as {seen[digest]} above.)")
            else:
                seen.setdefault(digest, f"{label} of {entry.name} {entry.version}")
                out.append(text.rstrip("\n"))
        out.append("")
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--path", action="append", type=Path, default=[],
                        help="pip --target dir or site-packages to scan (repeatable)")
    parser.add_argument("--files-from", type=Path,
                        help="list of the files a frozen app ships (one per line); only their owners count")
    parser.add_argument("--own", action="append", default=[], help="the project's own distribution (repeatable)")
    parser.add_argument("--own-path", action="append", type=Path, default=[],
                        help="directory of the project's own sources (with --files-from)")
    parser.add_argument("--overrides", type=Path, help="TOML overrides (see python-notices.example.toml)")
    parser.add_argument("--cpython", action="store_true", help="the artifact ships this interpreter")
    parser.add_argument("--rust-licenses", type=Path,
                        help="COPYRIGHT-library.html of the rustc that built the Rust wheels; "
                             "copied next to --out as Rust-Licenses.html")
    parser.add_argument("--title", default="Third-party software bundled with this artifact, with its licenses.")
    parser.add_argument("--out", type=Path, help="notices file to write")
    parser.add_argument("--inventory", action="store_true", help="print what was found as JSON and exit")
    args = parser.parse_args()
    if not args.path and not args.files_from:
        parser.error("pass --path or --files-from")
    if not args.out and not args.inventory:
        parser.error("pass --out or --inventory")

    problems = Problems()
    overrides, base = load_overrides(args.overrides, problems)
    for path in args.path:
        if not path.is_dir():
            problems.error(f"--path {path} is not a directory")
    dists = discover(args.path, problems)
    own = {normalize(name) for name in args.own}
    for name in own - set(dists):
        problems.warn(f"--own {name}: not installed")
    state = {"rust": {}}
    cpython_modules, cpython_items = set(), {}
    selected, shipped = set(dists), None
    if args.files_from:
        try:
            lines = [line.strip() for line in args.files_from.read_text(encoding="utf-8").splitlines()]
        except OSError as error:
            problems.error(f"--files-from {args.files_from}: {error}")
            lines = []
        shipped = {Path(os.path.realpath(line)) for line in lines if line and not line.startswith("#")}
        own_paths = {p.resolve() for p in args.own_path}
        selected, cpython_modules, cpython_items = classify_shipped(
            shipped, dists, own_paths, overrides["component"], problems)
        if (cpython_modules or cpython_items) and not args.cpython:
            problems.error("the artifact ships interpreter files; pass --cpython")
    elif args.cpython:
        cpython_modules, cpython_items = all_stdlib_natives()
    if not selected - own and not args.cpython:
        problems.error("no third-party distributions found; check --path")

    entries = []
    for key in sorted(selected):
        dist = dists[key]
        override = overrides["distribution"].get(key)
        if override and override.get("exclude"):
            if args.files_from:
                problems.warn(f"{dist.name}: excluded although its files ship")
            continue
        # The own distribution is scanned too: code vendored or compiled into it needs notices.
        entry = build_entry(dist, override, base, shipped, problems, state, own=key in own)
        if entry is not None:
            entries.append(entry)
    for key in sorted(set(overrides["distribution"]) - set(dists)):
        problems.warn(f"override [distribution.{key}] matches nothing installed here")
    if args.cpython:
        entries.append(cpython_entry(overrides["cpython"], base, cpython_modules, cpython_items, args, problems))
    for component in overrides["component"]:
        where = f"{component.get('name')} {component.get('version', '')}".strip()
        entries.append(Entry(component.get("name", "?"), component.get("version", ""),
                             component.get("license", "see the texts below"), component.get("source", ""),
                             texts=override_texts(component, base, problems, where)))

    if state["rust"]:
        if args.rust_licenses is None:
            commits = sorted(set().union(*state["rust"].values()))
            problems.error("the Rust standard library is statically linked into "
                           + ", ".join(sorted(state["rust"])) + "; pass --rust-licenses with the "
                           "COPYRIGHT-library.html of rustc " + ", ".join(commits)
                           + " (git ls-remote --tags https://github.com/rust-lang/rust | grep <commit>)")
    elif args.rust_licenses:
        problems.warn("--rust-licenses given but no Rust code found; the file is copied anyway")
    if args.rust_licenses and (not args.rust_licenses.is_file() or args.rust_licenses.stat().st_size == 0):
        problems.error(f"--rust-licenses {args.rust_licenses} is missing or empty")

    if args.inventory:
        print(json.dumps([{"name": e.name, "version": e.version, "license": e.license, "source": e.source,
                           "notes": e.notes, "texts": [label for label, _ in e.texts], "items": e.items}
                          for e in sorted(entries, key=lambda e: (e.name.casefold(), e.version))], indent=2))
        for message in problems.warnings + problems.errors:
            print(f"python-notices: {message}", file=sys.stderr)
        return 0
    for message in problems.warnings:
        print(f"python-notices: warning: {message}", file=sys.stderr)
    if problems.errors:
        for message in problems.errors:
            print(f"python-notices: error: {message}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(entries, args.title, bool(state["rust"])), encoding="utf-8", newline="\n")
    if args.rust_licenses:
        target = args.out.parent / "Rust-Licenses.html"
        if args.rust_licenses.resolve() != target.resolve():
            shutil.copyfile(args.rust_licenses, target)
    print(f"python-notices: wrote {args.out} ({len(entries)} components)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
