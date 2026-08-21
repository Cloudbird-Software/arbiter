"""GitHubRefBackend 请求形状回归测试（W1-C2 补充）。

背景：create_commit 原实现先 POST /git/trees 空 body 建"空树"，但 GitHub 对
空树创建一律 422 "Invalid tree info"（e2e .github#206 演习实测，
conductor run 32493680834）——该路径在真实 API 上从未成功，单测因走
LocalGitBackend 而未暴露。本测试在 HTTP 请求层（mock _request）锁定请求形状，
防再次引入"创建空树"调用。
"""

import unittest
from unittest.mock import patch

from arbiter.backend import GitHubRefBackend


def make_backend():
    return GitHubRefBackend(token="t-test", repo="o/r")


class TestCreateCommitWire(unittest.TestCase):
    def test_create_commit_uses_canonical_empty_tree_no_trees_call(self):
        """create_commit 不得调用 /git/trees；commit 请求体 tree=经典空树 SHA。"""
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            if path.endswith("/git/commits"):
                return 201, {"sha": "c0ffee"}
            raise AssertionError(f"意外调用: {method} {path}")

        b = make_backend()
        with patch.object(b, "_request", side_effect=fake_request):
            sha = b.create_commit("lease-meta-json", parent=None)
        self.assertEqual(sha, "c0ffee")
        self.assertEqual(len(calls), 1, "create_commit 应只发一次 POST /git/commits")
        method, path, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(path.endswith("/git/commits"))
        self.assertEqual(body["tree"], GitHubRefBackend.EMPTY_TREE_SHA)
        self.assertEqual(body["message"], "lease-meta-json")
        self.assertNotIn("parents", body)  # 无父提交（新租约）

    def test_create_commit_with_parent(self):
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            return 201, {"sha": "beef"}

        b = make_backend()
        with patch.object(b, "_request", side_effect=fake_request):
            sha = b.create_commit("renew", parent="aaa")
        self.assertEqual(sha, "beef")
        self.assertEqual(calls[0][2]["parents"], ["aaa"])  # 接管=以旧租约为父

    def test_empty_tree_sha_is_the_git_canonical_constant(self):
        # 4b825dc…是空树的恒定 SHA（git mktree </dev/null），任何 git 环境可复验；
        # 若此断言失败说明有人改动了常量——需附带真实 API 证据再改
        self.assertEqual(
            GitHubRefBackend.EMPTY_TREE_SHA,
            "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
        )


if __name__ == "__main__":
    unittest.main()


class TestQuoteRefPathWire(unittest.TestCase):
    def test_prefix_stripped_for_path_addressed_endpoints(self):
        """按路径寻址 ref 的端点（GET/PATCH/DELETE）要求相对形态——带 refs/ 前缀一律 404/422。"""
        from arbiter.backend import _quote_ref_path
        self.assertEqual(
            _quote_ref_path("refs/leases/o__r__1"),
            "leases%2Fo__r__1",
        )
        self.assertEqual(_quote_ref_path("heads/main"), "heads%2Fmain")  # 已相对形态原样
