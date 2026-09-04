import unittest

from gans.updates.github import ClientError, GitHubRelease, GitHubReleaseClient


class GitHubReleaseTests(unittest.TestCase):
    def test_from_json_and_notes(self):
        release = GitHubRelease.from_json({"tag_name": "v1.2.0", "name": "Gans 1.2.0", "body": "  notes  ",
                                           "html_url": "https://github.com/L-K-M/Gans/releases/tag/v1.2.0",
                                           "prerelease": False, "draft": False, "published_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(release.tag_name, "v1.2.0")
        self.assertEqual(release.release_notes(), "notes")
        self.assertIsNone(GitHubRelease.from_json({"tag_name": "v1", "html_url": "u"}).release_notes())
        long = GitHubRelease.from_json({"tag_name": "v1", "html_url": "u", "body": "x" * 700})
        self.assertEqual(len(long.release_notes(600)), 601)
        self.assertTrue(long.release_notes(600).endswith("…"))

    def test_rejects_malformed(self):
        with self.assertRaises(ClientError):
            GitHubRelease.from_json({"name": "no tag"})

    def test_prerelease_scan_picks_highest_non_draft(self):
        client = GitHubReleaseClient("L-K-M", "Gans")
        releases = [
            {"tag_name": "v1.4.0", "html_url": "a"},
            {"tag_name": "v2.0.0", "html_url": "b", "draft": True},
            {"tag_name": "v1.5.0-beta.1", "html_url": "c", "prerelease": True},
            {"tag_name": "garbage", "html_url": "d"},
        ]
        # GitHub's `releases/latest` already excludes drafts and pre-releases.
        client._fetch = lambda path: releases[0] if path == "releases/latest" else releases
        self.assertEqual(client.latest_release(include_prereleases=True).tag_name, "v1.5.0-beta.1")
        self.assertEqual(client.latest_release(include_prereleases=False).tag_name, "v1.4.0")
        client._fetch = lambda path: []
        with self.assertRaises(ClientError):
            client.latest_release(include_prereleases=True)


if __name__ == "__main__":
    unittest.main()
