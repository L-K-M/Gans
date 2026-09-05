"""Packaging tests: build the .deb with ``packaging/build-deb.sh`` into a temp dir, then
check its metadata, layout, modes, and the version stamping, and run the distro's own
validators (lintian, desktop-file-validate, appstreamcli, man) where they are installed.
Everything skips gracefully on a box without ``dpkg-deb``.
"""

from __future__ import annotations

import configparser
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Dict, List, Optional

LINUX_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = LINUX_DIR.parent
BUILD_SCRIPT = LINUX_DIR / "packaging" / "build-deb.sh"
TEST_VERSION = "9.9.9"
APP_ID = "ch.lkmc.Gans"
ICON_SIZES = (16, 32, 64, 128, 256, 512)


def _run(args: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
         timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _requires(tool: str):
    return unittest.skipUnless(shutil.which(tool), f"{tool} is not installed")


def _control_fields(deb: Path) -> Dict[str, str]:
    """``dpkg-deb -f`` output parsed into a field map (continuation lines joined)."""
    fields: Dict[str, str] = {}
    current = None
    for line in _run(["dpkg-deb", "-f", str(deb)]).stdout.splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += "\n" + line[1:]
        elif ":" in line:
            current, value = line.split(":", 1)
            fields[current] = value.strip()
    return fields


# MARK: Build once

@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb is not installed")
class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="gans-deb-")
        cls.tmp_dir = Path(cls.tmp.name)
        cls.output = cls.tmp_dir / "out"
        cls.stray_version_existed = (LINUX_DIR / "gans" / "VERSION").exists()
        # --version must win over $VERSION; run from an unrelated cwd on purpose.
        env = dict(os.environ, VERSION="0.0.1")
        result = _run([str(BUILD_SCRIPT), "--version", TEST_VERSION, "--output", str(cls.output), "--no-lintian"],
                      cwd=cls.tmp_dir, env=env)
        if result.returncode != 0:
            cls.tmp.cleanup()
            raise AssertionError(f"build-deb.sh failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        cls.build_output = result.stdout
        cls.deb = cls.output / f"gans_{TEST_VERSION}_all.deb"
        cls.extracted = cls.tmp_dir / "extracted"
        _run(["dpkg-deb", "-x", str(cls.deb), str(cls.extracted)])
        _run(["dpkg-deb", "-e", str(cls.deb), str(cls.extracted / "DEBIAN")])
        cls.contents = _run(["dpkg-deb", "--contents", str(cls.deb)]).stdout
        cls.fields = _control_fields(cls.deb)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _installed(self, path: str) -> Path:
        return self.extracted / path.lstrip("/")

    # MARK: Script

    def test_script_is_executable_and_prints_a_summary(self) -> None:
        self.assertTrue(os.access(BUILD_SCRIPT, os.X_OK))
        self.assertTrue(self.deb.is_file())
        self.assertIn("Package: gans", self.build_output)       # dpkg-deb --info
        self.assertIn("./usr/bin/gans", self.build_output)      # dpkg-deb --contents
        self.assertIn(f"built {self.deb}", self.build_output)
        self.assertIn("lintian skipped", self.build_output)
        # The build stamps VERSION into the staging tree only, never into the source tree.
        self.assertEqual((LINUX_DIR / "gans" / "VERSION").exists(), self.stray_version_existed)

    def test_version_falls_back_to_env_then_pbxproj(self) -> None:
        out = self.tmp_dir / "fallback"
        env = dict(os.environ, VERSION="8.8.8")
        result = _run([str(BUILD_SCRIPT), "--output", str(out), "--no-lintian"], cwd=REPO_DIR, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((out / "gans_8.8.8_all.deb").is_file())

        pbxproj = (REPO_DIR / "Gans.xcodeproj" / "project.pbxproj").read_text(encoding="utf-8")
        marketing = re.search(r"MARKETING_VERSION = ([0-9][0-9A-Za-z.]*);", pbxproj)
        self.assertIsNotNone(marketing)
        env.pop("VERSION")
        result = _run([str(BUILD_SCRIPT), "--output", str(out), "--no-lintian"], cwd=self.tmp_dir, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((out / f"gans_{marketing.group(1)}_all.deb").is_file())

    def test_rejects_bad_versions_and_arguments(self) -> None:
        for args in (["--version", "v-not-a-version"], ["--version", "1.0 beta"], ["--bogus"], ["--version"]):
            result = _run([str(BUILD_SCRIPT), *args, "--output", str(self.tmp_dir / "never"), "--no-lintian"],
                          cwd=self.tmp_dir)
            self.assertNotEqual(result.returncode, 0, args)
        self.assertFalse((self.tmp_dir / "never").exists())

    # MARK: Control

    def test_control_fields(self) -> None:
        fields = self.fields
        self.assertEqual(fields["Package"], "gans")
        self.assertEqual(fields["Version"], TEST_VERSION)
        self.assertEqual(fields["Architecture"], "all")
        self.assertEqual(fields["Section"], "utils")
        self.assertEqual(fields["Priority"], "optional")
        self.assertEqual(fields["Maintainer"], "L-K-M <claudecode@lkmc.ch>")
        self.assertEqual(fields["Homepage"], "https://github.com/L-K-M/Gans")
        for package in ("python3 (>= 3.10)", "python3-gi (>= 3.42)", "python3-gi-cairo", "gir1.2-gtk-3.0",
                        "gir1.2-glib-2.0", "python3-nacl", "python3-xlib", "python3-secretstorage",
                        "gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1"):
            self.assertIn(package, fields["Depends"])
        for package in ("gnome-keyring | kwalletmanager | keepassxc", "polkitd | policykit-1",
                        "gir1.2-gstreamer-1.0", "gstreamer1.0-plugins-base"):
            self.assertIn(package, fields["Recommends"])
        self.assertEqual(fields["Suggests"], "gnome-shell-extension-appindicator")
        synopsis, _, extended = fields["Description"].partition("\n")
        self.assertEqual(synopsis, "Ente Auth 2FA codes, one keystroke away (tray agent)")
        self.assertIn("end-to-end encrypted", extended)
        self.assertIn("Secret Service", extended)
        self.assertIn("gans toggle", extended)
        for line in extended.splitlines():
            self.assertLessEqual(len(line), 79, line)

    def test_installed_size_matches_dpkg_algorithm(self) -> None:
        kib = 0
        for path in self.extracted.rglob("*"):
            if "DEBIAN" in path.relative_to(self.extracted).parts:
                continue
            if path.is_symlink() or path.is_dir():
                kib += 1
            else:
                kib += (path.stat().st_size + 1023) // 1024
        self.assertEqual(int(self.fields["Installed-Size"]), kib)

    def test_maintainer_scripts(self) -> None:
        for name, tool in (("postinst", "py3compile"), ("prerm", "py3clean")):
            script = self.extracted / "DEBIAN" / name
            self.assertTrue(os.access(script, os.X_OK), name)
            text = script.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n"), name)
            self.assertIn("set -e", text)
            self.assertIn(f"command -v {tool}", text)
            self.assertIn(f"{tool} -p gans /usr/share/gans", text)
            self.assertNotIn("gtk-update-icon-cache", text)   # dpkg triggers do that
            self.assertNotIn("update-desktop-database", text)
            if shutil.which("sh"):
                self.assertEqual(_run(["sh", "-n", str(script)]).returncode, 0, name)

    def test_md5sums_cover_every_file(self) -> None:
        listed = {}
        for line in (self.extracted / "DEBIAN" / "md5sums").read_text(encoding="utf-8").splitlines():
            digest, _, path = line.partition("  ")
            listed[path] = digest
        on_disk = {}
        for path in self.extracted.rglob("*"):
            relative = path.relative_to(self.extracted)
            if path.is_file() and relative.parts[0] != "DEBIAN":
                on_disk[relative.as_posix()] = hashlib.md5(path.read_bytes()).hexdigest()
        self.assertEqual(listed, on_disk)

    # MARK: Layout

    def test_layout(self) -> None:
        expected = [
            "/usr/bin/gans",
            "/usr/share/gans/gans/__init__.py",
            "/usr/share/gans/gans/cli.py",
            "/usr/share/gans/gans/VERSION",
            "/usr/share/gans/gans/ente/vault.py",
            "/usr/share/gans/gans/ui/app.py",
            f"/usr/share/applications/{APP_ID}.desktop",
            "/usr/share/polkit-1/actions/ch.lkmc.gans.policy",
            f"/usr/share/metainfo/{APP_ID}.metainfo.xml",
            "/usr/share/man/man1/gans.1.gz",
            "/usr/share/doc/gans/copyright",
            "/usr/share/doc/gans/changelog.gz",
        ]
        expected += [f"/usr/share/icons/hicolor/{size}x{size}/apps/{APP_ID}.png" for size in ICON_SIZES]
        for path in expected:
            self.assertTrue(self._installed(path).is_file(), path)

        source_svgs = sorted(p.name for p in (LINUX_DIR / "gans" / "data" / "icons").glob("*.svg"))
        self.assertTrue(source_svgs)
        for name in source_svgs:
            self.assertTrue(self._installed(f"/usr/share/icons/hicolor/symbolic/apps/{name}").is_file(), name)
            # The tray points the indicator at the package's own icon dir (ui/tray.py).
            self.assertTrue(self._installed(f"/usr/share/gans/gans/data/icons/{name}").is_file(), name)

        # Every module in the source tree ships; nothing else from gans/ does.
        for source in (LINUX_DIR / "gans").rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            self.assertTrue(self._installed(f"/usr/share/gans/{source.relative_to(LINUX_DIR)}").is_file(), source)
        shipped = {p.relative_to(self.extracted / "usr/share/gans").as_posix()
                   for p in (self.extracted / "usr/share/gans").rglob("*") if p.is_file()}
        unexpected = {p for p in shipped if not (p.endswith(".py") or p == "gans/VERSION" or
                                                 (p.startswith("gans/data/icons/") and p.endswith(".svg")))}
        self.assertEqual(unexpected, set())
        for forbidden in ("__pycache__", ".pyc", "/tests/", "/DEBIAN/DEBIAN"):
            self.assertNotIn(forbidden, self.contents)
        self.assertNotIn(f"gans/data/{APP_ID}.desktop", self.contents)   # only in /usr/share/applications
        self.assertNotIn("gans/data/gans.1", self.contents)

    def test_app_icons_come_from_the_xcode_asset_catalogue(self) -> None:
        catalogue = REPO_DIR / "Gans/Resources/Assets.xcassets/AppIcon.appiconset"
        for size in ICON_SIZES:
            installed = self._installed(f"/usr/share/icons/hicolor/{size}x{size}/apps/{APP_ID}.png")
            self.assertEqual(installed.read_bytes(), (catalogue / f"AppIcon-{size}.png").read_bytes(), size)
            width, height = int.from_bytes(installed.read_bytes()[16:20], "big"), \
                int.from_bytes(installed.read_bytes()[20:24], "big")
            self.assertEqual((width, height), (size, size))

    def test_modes_and_ownership(self) -> None:
        entries = re.findall(r"^([-d][rwx-]{9}) (\S+/\S+)\s+\d+ \S+ \S+ (\./\S+)$", self.contents, re.MULTILINE)
        self.assertGreater(len(entries), 20)
        for mode, owner, path in entries:
            self.assertEqual(owner, "root/root", path)
            if mode.startswith("d"):
                self.assertEqual(mode, "drwxr-xr-x", path)
            elif path == "./usr/bin/gans":
                self.assertEqual(mode, "-rwxr-xr-x", path)
            else:
                self.assertEqual(mode, "-rw-r--r--", path)
        for name in ("control", "md5sums"):
            self.assertEqual((self.extracted / "DEBIAN" / name).stat().st_mode & 0o777, 0o644, name)

    # MARK: Version stamping

    def test_version_is_stamped_everywhere(self) -> None:
        self.assertEqual(self._installed("/usr/share/gans/gans/VERSION").read_text(encoding="utf-8"),
                         f"{TEST_VERSION}\n")
        # The real resolution path: gans.version reads VERSION next to the package.
        result = _run([sys.executable, "-c", "import gans; print(gans.__version__)"],
                      cwd=self._installed("/usr/share/gans"), env={"PYTHONPATH": str(self._installed("/usr/share/gans"))})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), TEST_VERSION)

        metainfo = self._installed(f"/usr/share/metainfo/{APP_ID}.metainfo.xml").read_text(encoding="utf-8")
        self.assertNotIn("@VERSION@", metainfo)
        self.assertNotIn("@DATE@", metainfo)
        release = ElementTree.fromstring(metainfo).find("./releases/release")
        self.assertIsNotNone(release)
        self.assertEqual(release.get("version"), TEST_VERSION)
        self.assertRegex(release.get("date", ""), r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(release.findtext("url"), f"https://github.com/L-K-M/Gans/releases/tag/v{TEST_VERSION}")

        with gzip.open(self._installed("/usr/share/doc/gans/changelog.gz"), "rt", encoding="utf-8") as handle:
            changelog = handle.read()
        self.assertTrue(changelog.startswith(f"gans ({TEST_VERSION}) unstable; urgency=medium\n"), changelog)
        self.assertIn("https://github.com/L-K-M/Gans/releases", changelog)
        self.assertRegex(changelog, r"\n -- L-K-M <claudecode@lkmc\.ch>  \w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} \+0000\n$")

        self.assertEqual(self._installed("/usr/bin/gans").read_bytes(), (LINUX_DIR / "bin" / "gans").read_bytes())

    # MARK: Data files

    def test_desktop_file_keys(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # type: ignore[assignment]  # desktop keys are case-sensitive
        parser.read(self._installed(f"/usr/share/applications/{APP_ID}.desktop"), encoding="utf-8")
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "Gans")
        self.assertEqual(entry["GenericName"], "Two-factor codes")
        self.assertEqual(entry["Comment"], "Ente Auth codes, one keystroke away")
        self.assertEqual(entry["Exec"], "gans %u")
        self.assertEqual(entry["Icon"], APP_ID)
        self.assertEqual(entry["Terminal"], "false")
        self.assertEqual(entry["Categories"], "Utility;Security;")
        self.assertEqual(entry["Keywords"], "2FA;TOTP;OTP;Ente;authenticator;")
        self.assertEqual(entry["StartupNotify"], "false")
        self.assertEqual(entry["MimeType"], "x-scheme-handler/ente-cli;")
        self.assertEqual(entry["X-GNOME-UsesNotifications"], "true")

    def test_polkit_policy(self) -> None:
        root = ElementTree.parse(self._installed("/usr/share/polkit-1/actions/ch.lkmc.gans.policy")).getroot()
        self.assertEqual(root.tag, "policyconfig")
        self.assertEqual(root.findtext("vendor"), "Gans")
        self.assertEqual(root.findtext("icon_name"), APP_ID)
        action = root.find("action")
        self.assertEqual(action.get("id"), "ch.lkmc.gans.unlock")   # AppLock.ACTION_ID
        self.assertEqual(action.findtext("description"), "Unlock Gans")
        self.assertEqual(action.findtext("message"), "Unlock Gans to access your codes")
        self.assertEqual(action.findtext("icon_name"), APP_ID)
        for key in ("allow_any", "allow_inactive", "allow_active"):
            self.assertEqual(action.findtext(f"defaults/{key}"), "auth_self", key)

    def test_metainfo_content(self) -> None:
        root = ElementTree.parse(self._installed(f"/usr/share/metainfo/{APP_ID}.metainfo.xml")).getroot()
        self.assertEqual(root.get("type"), "desktop-application")
        self.assertEqual(root.findtext("id"), APP_ID)
        self.assertEqual(root.findtext("metadata_license"), "CC0-1.0")
        self.assertEqual(root.findtext("project_license"), "Unlicense")
        self.assertEqual(root.findtext("name"), "Gans")
        self.assertEqual(root.findtext("developer/name"), "L-K-M")
        self.assertEqual(root.find("launchable").text, f"{APP_ID}.desktop")
        self.assertEqual(root.findtext("provides/binary"), "gans")
        urls = {url.get("type"): url.text for url in root.findall("url")}
        self.assertEqual(urls["homepage"], "https://github.com/L-K-M/Gans")
        self.assertEqual(urls["bugtracker"], "https://github.com/L-K-M/Gans/issues")
        self.assertEqual([c.text for c in root.findall("categories/category")], ["Utility", "Security"])
        self.assertEqual(root.find("content_rating").get("type"), "oars-1.1")
        self.assertEqual(len(root.find("content_rating")), 0)   # "none": no content attributes

    def test_copyright_is_dep5_with_the_unlicense_text(self) -> None:
        text = self._installed("/usr/share/doc/gans/copyright").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"))
        self.assertIn("License: Unlicense", text)
        for line in (REPO_DIR / "LICENSE").read_text(encoding="utf-8").splitlines():
            self.assertIn(line or ".", text)

    # MARK: Distro validators

    @_requires("desktop-file-validate")
    def test_desktop_file_validate(self) -> None:
        result = _run(["desktop-file-validate", str(self._installed(f"/usr/share/applications/{APP_ID}.desktop"))])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("error", result.stdout + result.stderr)

    @_requires("appstreamcli")
    def test_appstream_validate(self) -> None:
        result = _run(["appstreamcli", "validate", "--no-net",
                       str(self._installed(f"/usr/share/metainfo/{APP_ID}.metainfo.xml"))])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @_requires("man")
    def test_manpage_renders_without_warnings(self) -> None:
        env = dict(os.environ, MANROFFSEQ="", MANWIDTH="80", LC_ALL="C.UTF-8")
        result = _run(["man", "--warnings", "-E", "UTF-8", "-l", "-Tutf8", "-Z",
                       str(self._installed("/usr/share/man/man1/gans.1.gz"))], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotRegex(result.stderr, r"(?i)warning|error")
        with gzip.open(self._installed("/usr/share/man/man1/gans.1.gz"), "rt", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn(".TH GANS 1", source)
        for command in ("toggle", "search", "settings", "quit", "GANS_GDK_BACKEND", "GANS_DEBUG"):
            self.assertIn(command, source)

    @_requires("lintian")
    def test_lintian_reports_no_errors_or_warnings(self) -> None:
        result = _run(["lintian", "--fail-on", "error,warning", str(self.deb)], timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
