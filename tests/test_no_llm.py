"""AC-1 静态扫描：无 LLM 调用边界的机器可判定断言。

(a) kernel/policy/lease 纯函数模块不得 import 任何网络（或进程逃逸）模块；
(b) backend.py 中全部 URL 常量的主机 ⊆ {api.github.com}；
(c) 全部源码（.py/.sh/.yaml/.yml/.json/.toml）不含 LLM 端点/SDK 特征串。

特征串以碎片拼装（本文件自身不得出现字面量——否则自噬）。
"""

import ast
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# (a) 纯函数模块禁止 import 的模块根名（网络 + 进程逃逸通道）
FORBIDDEN_IMPORTS = {
    "urllib", "requests", "socket", "ssl", "http", "httpx", "aiohttp",
    "ftplib", "poplib", "imaplib", "smtplib", "telnetlib", "xmlrpc",
    "websocket", "subprocess", "asyncio", "importlib",
}
PURE_MODULES = ["kernel.py", "policy.py", "lease.py", "__init__.py", "cli.py"]

# (b) 允许的唯一 API 主机
ALLOWED_HOSTS = {"api.github.com"}

# (c) LLM 端点/SDK 特征串（碎片拼装，防自噬）
_NEEDLE_PARTS = [
    ("api.", "openai.", "com"),
    ("api.", "anthropic.", "com"),
    ("generativelanguage.", "googleapis"),
    ("dash", "scope"),
    ("big", "model"),
    ("chat/", "completions"),
    ("LLM", "_API"),
]

SOURCE_SUFFIXES = (".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".cfg")


def iter_source_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", ".pytest_cache")]
        for name in filenames:
            if name.endswith(SOURCE_SUFFIXES):
                yield os.path.join(dirpath, name)


class TestNoLlmBoundary(unittest.TestCase):
    def test_pure_modules_import_no_network(self):
        # (a) kernel/policy/lease/__init__（cli 同样不直接触网——后端唯一入口）
        for mod in PURE_MODULES:
            path = os.path.join(REPO_ROOT, "arbiter", mod)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=mod)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.level == 0:
                        imported.add(node.module.split(".")[0])
            bad = imported & FORBIDDEN_IMPORTS
            self.assertFalse(
                bad, f"arbiter/{mod} import 了禁用模块 {sorted(bad)}（纯函数层不得触网/起进程）")

    def test_backend_urls_limited_to_github_api(self):
        # (b) backend.py 是唯一网络模块；其 URL 主机必须 ⊆ {api.github.com}
        path = os.path.join(REPO_ROOT, "arbiter", "backend.py")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        hosts = set(re.findall(r"https?://([^/\"'\s)]+)", text))
        self.assertTrue(hosts, "backend.py 应至少声明一个 API 主机")
        bad = hosts - ALLOWED_HOSTS
        self.assertFalse(bad, f"backend.py 出现白名单外主机: {sorted(bad)}")

    def test_backend_only_network_module(self):
        # 网络模块白名单：只有 backend.py 允许 import urllib
        for path in iter_source_files():
            if not path.endswith(".py"):
                continue
            with open(path, encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read(), filename=path)
                except SyntaxError:
                    continue
            rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
            if rel == "arbiter/backend.py":
                continue
            for node in ast.walk(tree):
                roots = set()
                if isinstance(node, ast.Import):
                    roots.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.level == 0:
                        roots.add(node.module.split(".")[0])
                bad = roots & {"urllib", "requests", "socket", "http", "httpx", "aiohttp"}
                self.assertFalse(bad, f"{rel} 不得 import 网络模块 {sorted(bad)}（唯一网络模块=backend.py）")

    def test_no_llm_endpoint_strings_anywhere(self):
        # (c) 全部源码不含 LLM 端点/SDK 特征串（INV：授权决策零 LLM）
        needles = ["".join(parts) for parts in _NEEDLE_PARTS]
        offenders = []
        for path in iter_source_files():
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for needle in needles:
                if needle in text:
                    offenders.append((os.path.relpath(path, REPO_ROOT), needle))
        self.assertFalse(
            offenders,
            f"源码出现 LLM 特征串 {offenders}——arbiter 授权决策零 LLM（宪法 §1/ADR-0054 §3）")

    def test_default_deny_is_tested(self):
        # 默认拒绝的可执行性：真源策略表 + 未知命令 → deny（与 test_policy 呼应，
        # 这里断言"默认拒绝"存在专门单测用例，防测试套件退化后 AC-1 失守）
        import arbiter.policy as policy_mod

        pol = policy_mod.Policy.from_text(
            "schema_version: 1\ndefaults:\n  ttl_minutes: 5\n"
            "commands:\n  /claim:\n    allowed_roles: [owner]\n")
        ok, code, _ = pol.check("/anything-else", "owner", "ready")
        self.assertFalse(ok)
        self.assertEqual(code, "unknown-command")


if __name__ == "__main__":
    unittest.main()
