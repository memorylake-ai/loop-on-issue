import unittest

import _bootstrap  # noqa: F401

from loopkit import remotes


class ParseRemoteURL(unittest.TestCase):
    def test_scp_style_ssh(self):
        r = remotes.parse_remote_url("git@github.com:memorylake-ai/loop-on-issue.git")
        self.assertEqual((r.host, r.path), ("github.com", "memorylake-ai/loop-on-issue"))

    def test_https(self):
        r = remotes.parse_remote_url("https://github.com/owner/repo.git")
        self.assertEqual((r.host, r.path), ("github.com", "owner/repo"))

    def test_https_without_dot_git(self):
        r = remotes.parse_remote_url("https://github.com/owner/repo")
        self.assertEqual((r.host, r.path), ("github.com", "owner/repo"))

    def test_https_with_credentials_in_the_url(self):
        r = remotes.parse_remote_url("https://oauth2:tok@gitlab.example.com/g/p.git")
        self.assertEqual((r.host, r.path), ("gitlab.example.com", "g/p"))

    def test_nested_gitlab_subgroups_are_kept_whole(self):
        # GitLab project paths can be arbitrarily deep; truncating to the last two
        # segments would address the wrong project.
        r = remotes.parse_remote_url("git@gitlab.example.com:group/sub/deep/proj.git")
        self.assertEqual(r.path, "group/sub/deep/proj")

    def test_ssh_url_with_explicit_port(self):
        r = remotes.parse_remote_url("ssh://git@gitlab.example.com:2222/group/proj.git")
        self.assertEqual((r.host, r.path), ("gitlab.example.com", "group/proj"))

    def test_ssh_host_alias_keeps_the_alias_as_host(self):
        # `git@github-work:org/repo.git` resolves through ~/.ssh/config, so the
        # host is not a real domain. Detection has to fall through to a CLI probe.
        r = remotes.parse_remote_url("git@github-work:org/repo.git")
        self.assertEqual((r.host, r.path), ("github-work", "org/repo"))

    def test_trailing_slash_is_tolerated(self):
        r = remotes.parse_remote_url("https://github.com/owner/repo/")
        self.assertEqual(r.path, "owner/repo")

    def test_local_path_is_not_a_forge_remote(self):
        self.assertIsNone(remotes.parse_remote_url("/srv/git/repo.git"))
        self.assertIsNone(remotes.parse_remote_url("../sibling"))

    def test_empty_input(self):
        self.assertIsNone(remotes.parse_remote_url(""))
        self.assertIsNone(remotes.parse_remote_url(None))


class ForgeFromHost(unittest.TestCase):
    def test_github_dot_com(self):
        self.assertEqual(remotes.forge_from_host("github.com"), "github")

    def test_gitlab_dot_com(self):
        self.assertEqual(remotes.forge_from_host("gitlab.com"), "gitlab")

    def test_case_insensitive(self):
        self.assertEqual(remotes.forge_from_host("GitHub.COM"), "github")

    def test_self_hosted_gitlab(self):
        self.assertEqual(remotes.forge_from_host("gitlab.zbyte-inc.cloud"), "gitlab")

    def test_github_enterprise(self):
        self.assertEqual(remotes.forge_from_host("github.acme-corp.net"), "github")

    def test_unknown_host_is_undecidable(self):
        # Deliberately not a guess: a wrong forge means every API call 404s in a
        # confusing way. The caller probes the CLIs instead.
        self.assertIsNone(remotes.forge_from_host("git.acme.internal"))
        self.assertIsNone(remotes.forge_from_host("github-work"))
        self.assertIsNone(remotes.forge_from_host(""))


class RemoteOrder(unittest.TestCase):
    def test_upstream_wins_when_present(self):
        # The fork workflow: branches are pushed to origin, but issues and change
        # requests live upstream.
        self.assertEqual(remotes.remote_order(["origin", "upstream"]), ["upstream", "origin"])

    def test_origin_only(self):
        self.assertEqual(remotes.remote_order(["origin"]), ["origin"])

    def test_other_remotes_come_last_in_their_original_order(self):
        self.assertEqual(
            remotes.remote_order(["fork", "origin", "mirror"]),
            ["origin", "fork", "mirror"],
        )

    def test_explicit_preference_jumps_the_queue(self):
        self.assertEqual(
            remotes.remote_order(["origin", "upstream"], prefer="origin"),
            ["origin", "upstream"],
        )

    def test_explicit_preference_that_does_not_exist_is_ignored(self):
        self.assertEqual(
            remotes.remote_order(["origin", "upstream"], prefer="nope"),
            ["upstream", "origin"],
        )


if __name__ == "__main__":
    unittest.main()
