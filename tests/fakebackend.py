"""测试替身后端：内存版 refs/commits，语义与 GitHub/Local 后端一致。

供 test_replay 等离线用例复用（unittest discover 的 -s tests 模式下作为
顶层模块导入）。
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter.kernel import CasConflict, NotFoundError  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.refs = {}      # ref -> sha
        self.commits = {}   # sha -> message
        self._seq = 0

    # -- 协议方法（与 arbiter.backend 两个实现一致） -------------------------

    def create_commit(self, message, parent=None):
        self._seq += 1
        raw = f"{self._seq}|{message}|{parent or ''}"
        sha = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        self.commits[sha] = message
        return sha

    def create_ref(self, ref, sha):
        if ref in self.refs:
            raise CasConflict(f"ref 已存在: {ref}")
        self.refs[ref] = sha

    def read_ref(self, ref):
        if ref not in self.refs:
            raise NotFoundError(f"ref 不存在: {ref}")
        return self.refs[ref]

    def update_ref(self, ref, new_sha, expected=None, force=False):
        if ref not in self.refs:
            raise NotFoundError(f"ref 不存在: {ref}")
        if expected is not None and self.refs[ref] != expected:
            raise CasConflict(f"CAS 不符: {ref}")
        self.refs[ref] = new_sha

    def delete_ref(self, ref):
        if ref not in self.refs:
            raise NotFoundError(f"ref 不存在: {ref}")
        del self.refs[ref]

    def commit_message(self, sha):
        if sha not in self.commits:
            raise NotFoundError(f"commit 不存在: {sha}")
        return self.commits[sha]

    # -- 断言辅助 -----------------------------------------------------------

    def seen_count(self):
        return sum(1 for r in self.refs if r.startswith("refs/seen/"))

    def lease_shas(self):
        return [sha for ref, sha in self.refs.items()
                if ref.startswith("refs/leases/")]
