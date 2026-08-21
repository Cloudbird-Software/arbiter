"""CAS 后端实现。两类：

- GitHubRefBackend：生产后端——仅 api.github.com（urllib，零第三方依赖）。
  租约/seen refs 存放在 arbiter 仓自身的 git refs（ADR-0054 §2）。
- LocalGitBackend：测试后端——git CLI（subprocess）操作裸仓 fixture，
  与 GitHub refs 语义一致（update-ref 的 old-value 即真 CAS）。

协议异常（InfraError/CasConflict/NotFoundError）定义在 arbiter.kernel——
本模块 import 它们，保证 kernel 零网络 import（分层铁律）。

注意：本文件是全仓唯一允许出现网络的模块；URL 常量必须 ⊆ {api.github.com}
（tests/test_no_llm.py 静态断言 (b)）。
"""

from __future__ import annotations

import json
import os
import subprocess

from .kernel import CasConflict, InfraError, NotFoundError

API_BASE = "https://api.github.com"   # 唯一 API 主机（静态扫描断言）
ZERO_SHA = "0" * 40


def _quote_ref_path(ref: str) -> str:
    """ref 路径段编码（'/'→'%' 之外的保留字符按 RFC 3986 编码）。"""
    import urllib.parse

    return urllib.parse.quote(ref, safe="")


class GitHubRefBackend:
    """api.github.com REST 后端（Git Data API：refs/commits）。

    - create_ref：POST /git/refs → 201 成功 / 422 CasConflict（已存在=CAS 败者）
    - update_ref：PATCH /git/refs/{ref} force=false —— ff 校验即原子接管 CAS
      （接管 commit 以旧租约 commit 为 parent，非后代更新必 422，ADR-0054 §2）
    - 5xx/网络/超时 → InfraError（fail-closed 独立通道，不是 deny）
    """

    def __init__(self, token: str, repo: str, timeout: int = 20):
        if not token:
            raise InfraError("缺少 GitHub 令牌（GitHubRefBackend）")
        self._token = token
        self._repo = repo  # 'owner/repo'（租约宿主仓，生产=Cloudbird-Software/arbiter）
        self._timeout = timeout

    def _request(self, method: str, path: str, body=None):
        import urllib.request
        import urllib.error

        url = f"{API_BASE}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "cloudbird-arbiter",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, payload
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                payload = {}
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise InfraError(f"网络失败 {method} {path}: {exc}") from exc

    # -- Git Data API ------------------------------------------------------

    def create_ref(self, ref: str, sha: str) -> None:
        status, payload = self._request(
            "POST", f"/repos/{self._repo}/git/refs", {"ref": ref, "sha": sha})
        if status == 201:
            return
        if status == 422:
            raise CasConflict(f"createRef 422: {payload.get('message', 'ref 已存在')}")
        raise InfraError(f"createRef HTTP {status}: {payload.get('message', '')}")

    def read_ref(self, ref: str) -> str:
        status, payload = self._request(
            "GET", f"/repos/{self._repo}/git/ref/{_quote_ref_path(ref)}")
        if status == 200:
            return payload["object"]["sha"]
        if status == 404:
            raise NotFoundError(f"ref 不存在: {ref}")
        raise InfraError(f"readRef HTTP {status}: {payload.get('message', '')}")

    def update_ref(self, ref: str, new_sha: str, expected: str | None = None,
                   force: bool = False) -> None:
        """条件更新。force=false + parent 链 = ff 校验 CAS（expected 语义近似）。"""
        status, payload = self._request(
            "PATCH", f"/repos/{self._repo}/git/refs/{_quote_ref_path(ref)}",
            {"sha": new_sha, "force": force})
        if status == 200:
            return
        if status == 422:
            raise CasConflict(f"updateRef 422: {payload.get('message', '非快进/冲突')}")
        raise InfraError(f"updateRef HTTP {status}: {payload.get('message', '')}")

    def delete_ref(self, ref: str) -> None:
        status, payload = self._request(
            "DELETE", f"/repos/{self._repo}/git/refs/{_quote_ref_path(ref)}")
        if status in (204, 200):
            return
        if status == 404:
            raise NotFoundError(f"ref 不存在: {ref}")
        raise InfraError(f"deleteRef HTTP {status}: {payload.get('message', '')}")

    def create_commit(self, message: str, parent: str | None = None) -> str:
        """空树 commit，message 内嵌 JSON（租约/台账载体，ADR-0054 §2）。"""
        status, payload = self._request(
            "POST", f"/repos/{self._repo}/git/trees", {})
        if status != 201:
            raise InfraError(f"createTree HTTP {status}: {payload.get('message', '')}")
        tree = payload.get("sha")
        if not tree:
            raise InfraError(f"createTree 未返回 sha: {payload}")
        body = {"message": message, "tree": tree}
        if parent:
            body["parents"] = [parent]
        status, payload = self._request(
            "POST", f"/repos/{self._repo}/git/commits", body)
        if status == 201:
            return payload["sha"]
        raise InfraError(f"createCommit HTTP {status}: {payload.get('message', '')}")

    def commit_message(self, sha: str) -> str:
        status, payload = self._request(
            "GET", f"/repos/{self._repo}/git/commits/{sha}")
        if status == 200:
            return (payload.get("message") or "").strip()
        if status == 404:
            raise NotFoundError(f"commit 不存在: {sha}")
        raise InfraError(f"getCommit HTTP {status}: {payload.get('message', '')}")


class LocalGitBackend:
    """git CLI 后端（subprocess）——测试/离线用，语义与 GitHub refs 对齐。

    - create_ref = `git update-ref <ref> <sha> <zero-sha>`（old=zero ⇒ 必须不存在，
      与 GitHub createRef 422 语义一致）
    - update_ref = `git update-ref <ref> <new> <expected>`（真 CAS）
    """

    # commit-tree 在无 user.name/email 的环境（CI runner）会拒绝提交——
    # 注入兜底 ident（调用方已配置的环境变量优先）
    _IDENT_ENV = {
        "GIT_AUTHOR_NAME": "arbiter-test",
        "GIT_AUTHOR_EMAIL": "arbiter@cloudbird.invalid",
        "GIT_COMMITTER_NAME": "arbiter-test",
        "GIT_COMMITTER_EMAIL": "arbiter@cloudbird.invalid",
    }

    def __init__(self, repo_dir: str):
        self._dir = repo_dir

    def _git(self, *args: str, stdin: str | None = None) -> str:
        env = dict(os.environ)
        for key, val in self._IDENT_ENV.items():
            env.setdefault(key, val)
        try:
            proc = subprocess.run(
                ["git", "-C", self._dir] + list(args),
                input=stdin, capture_output=True, text=True, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfraError(f"git 子进程失败: {exc}") from exc
        if proc.returncode != 0:
            return self._fail(args, proc)
        return proc.stdout.strip()

    def _fail(self, args, proc):
        stderr = proc.stderr.strip()
        # CAS 冲突的两种确定形态：ref 已存在 / old-value 期望不符
        if "already exists" in stderr or "but expected" in stderr:
            raise CasConflict(f"git {' '.join(args)}: {stderr}")
        # 其余（含 .lock 竞争、仓库损坏、网络盘抖动）→ Infra（fail-closed）
        raise InfraError(f"git {' '.join(args)}: rc={proc.returncode} {stderr}")

    def _is_transient_lock(self, stderr: str) -> bool:
        # '.lock' 文件竞争（并发 update-ref 在途）——可重试，不是裁决结果
        return "Unable to create" in stderr and ".lock" in stderr

    def _exists(self, ref: str) -> bool:
        try:
            proc = subprocess.run(
                ["git", "-C", self._dir, "rev-parse", "--verify", "--quiet", ref],
                capture_output=True, text=True, timeout=30)
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfraError(f"git rev-parse 失败: {exc}") from exc

    def _retry_on_lock(self, op):
        """锁竞争（transient）重试至多 3 次；确定冲突/其他错误按类型抛出。"""
        import time

        last = None
        for _ in range(3):
            try:
                return op()
            except InfraError as exc:
                if not self._is_transient_lock(str(exc)):
                    raise
                last = exc
                time.sleep(0.15)
        raise last

    def create_ref(self, ref: str, sha: str) -> None:
        def op():
            try:
                self._git("update-ref", ref, sha, ZERO_SHA)
            except InfraError as exc:
                if self._is_transient_lock(str(exc)):
                    raise
                if self._exists(ref):  # 失败原因=ref 已存在 → 确定冲突
                    raise CasConflict(f"create_ref: ref 已存在 {ref}") from exc
                raise
        self._retry_on_lock(op)

    def read_ref(self, ref: str) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", self._dir, "rev-parse", "--verify", ref],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfraError(f"git rev-parse 失败: {exc}") from exc
        if proc.returncode == 0:
            return proc.stdout.strip()
        raise NotFoundError(f"ref 不存在: {ref}")

    def update_ref(self, ref: str, new_sha: str, expected: str | None = None,
                   force: bool = False) -> None:
        def op():
            try:
                if expected:
                    self._git("update-ref", ref, new_sha, expected)
                else:
                    self._git("update-ref", ref, new_sha)
            except InfraError as exc:
                if self._is_transient_lock(str(exc)):
                    raise
                # 冲突之外还需区分：目标 ref 已被删除（释放竞态）→ NotFound
                if not self._exists(ref):
                    raise NotFoundError(f"update_ref 目标已不存在: {ref}") from exc
                raise
        self._retry_on_lock(op)

    def delete_ref(self, ref: str) -> None:
        try:
            self._git("update-ref", "-d", ref)
        except CasConflict:
            raise InfraError(f"delete_ref 异常冲突: {ref}")

    def create_commit(self, message: str, parent: str | None = None) -> str:
        """空树 + message commit（git 底层命令，无需工作树——裸仓可用）。"""
        tree = self._git("mktree", stdin="")   # 物化空树对象
        args = ["commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        return self._git(*args)

    def commit_message(self, sha: str) -> str:
        try:
            return self._git("log", "-1", "--format=%B", sha).strip()
        except InfraError:
            # 短路检查：对象不存在 → NotFound（其余仍是 Infra）
            proc = subprocess.run(
                ["git", "-C", self._dir, "cat-file", "-t", sha],
                capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                raise NotFoundError(f"commit 不存在: {sha}") from None
            raise
