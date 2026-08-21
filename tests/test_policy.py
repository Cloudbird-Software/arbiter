"""策略表测试：越权 / 默认拒绝 / 未知命令 / 严格校验。

对应 AC-1（默认拒绝——无匹配规则=拒）与 ADR-0054 §4。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter.policy import Policy, PolicyError  # noqa: E402

GOOD = """
schema_version: 1
defaults:
  ttl_minutes: 240
commands:
  /claim:
    allowed_roles: [agent, owner]
    required_state: ready
  /release:
    allowed_roles: [agent, owner]
    require_holder: true
  /retry:
    allowed_roles: [agent, owner]
    require_holder: true
"""


class TestPolicyBasics(unittest.TestCase):
    def setUp(self):
        self.policy = Policy.from_text(GOOD)

    def test_load_real_capabilities(self):
        # 真源文件必须可加载（CI policy-validate 的单测面）
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pol = Policy.load(os.path.join(root, "capabilities.yaml"))
        self.assertEqual(pol.ttl_minutes, 240)
        self.assertIn("/claim", pol.command_names)

    def test_claim_allowed_for_agent_ready(self):
        ok, code, _ = self.policy.check("/claim", "agent", "ready")
        self.assertTrue(ok)
        self.assertEqual(code, "policy-ok")

    def test_claim_allowed_for_owner(self):
        ok, _, _ = self.policy.check("/claim", "owner", "ready")
        self.assertTrue(ok)

    def test_privilege_escalation_denied(self):
        # 越权：none 角色不允许任何命令（AC-1）
        for cmd in ("/claim", "/release", "/retry"):
            ok, code, _ = self.policy.check(cmd, "none", "ready")
            self.assertFalse(ok, cmd)
            self.assertEqual(code, "role-not-allowed")

    def test_unknown_role_denied(self):
        ok, code, _ = self.policy.check("/claim", "superuser", "ready")
        self.assertFalse(ok)
        self.assertEqual(code, "unknown-role")

    def test_default_deny_unknown_command(self):
        # 默认拒绝：未知命令 = deny（不是异常、不是 abstain）
        ok, code, _ = self.policy.check("/nuke", "owner", "ready")
        self.assertFalse(ok)
        self.assertEqual(code, "unknown-command")

    def test_state_mismatch_denied(self):
        ok, code, _ = self.policy.check("/claim", "agent", "in-progress")
        self.assertFalse(ok)
        self.assertEqual(code, "state-not-allowed")

    def test_missing_state_denied(self):
        # conductor 未传状态 → 空 → 不满足 required_state → 拒（fail-closed）
        ok, code, _ = self.policy.check("/claim", "agent", "")
        self.assertFalse(ok)
        self.assertEqual(code, "state-not-allowed")


class TestPolicyStrictLoading(unittest.TestCase):
    def test_unknown_top_key_rejected(self):
        with self.assertRaises(PolicyError):
            Policy.from_text(GOOD + "extra_key: 1\n")

    def test_unknown_rule_key_rejected(self):
        bad = GOOD.replace("require_holder: true", "require_holder: true\n    surprise: 1")
        with self.assertRaises(PolicyError):
            Policy.from_text(bad)

    def test_unknown_default_key_rejected(self):
        bad = GOOD.replace("ttl_minutes: 240", "ttl_minutes: 240\n  extra: 1")
        with self.assertRaises(PolicyError):
            Policy.from_text(bad)

    def test_bad_schema_version_rejected(self):
        bad = GOOD.replace("schema_version: 1", "schema_version: 2")
        with self.assertRaises(PolicyError):
            Policy.from_text(bad)

    def test_bad_ttl_rejected(self):
        for ttl in ("0", "-5", '"x"'):
            bad = GOOD.replace("ttl_minutes: 240", f"ttl_minutes: {ttl}")
            with self.assertRaises(PolicyError, msg=ttl):
                Policy.from_text(bad)

    def test_unknown_role_value_rejected(self):
        bad = GOOD.replace("allowed_roles: [agent, owner]",
                           "allowed_roles: [agent, deity]", 1)
        with self.assertRaises(PolicyError):
            Policy.from_text(bad)

    def test_command_without_slash_rejected(self):
        bad = GOOD + "  claim2:\n    allowed_roles: [owner]\n"
        with self.assertRaises(PolicyError):
            Policy.from_text(bad)

    def test_empty_commands_rejected(self):
        bad = "schema_version: 1\ndefaults:\n  ttl_minutes: 240\ncommands:\n"
        # commands: 为空值（None）→ 拒绝
        with self.assertRaises(PolicyError):
            Policy.from_text(bad)

    def test_duplicate_key_rejected(self):
        dup = GOOD + "  /retry:\n    allowed_roles: [owner]\n/claim:\n    allowed_roles: [owner]\n"
        with self.assertRaises(PolicyError):
            Policy.from_text(dup)

    def test_tab_indent_rejected(self):
        bad = "schema_version: 1\ndefaults:\n\tttl_minutes: 240\ncommands:\n  /claim:\n    allowed_roles: [owner]\n"
        with self.assertRaises(PolicyError):
            Policy.from_text(bad)

    def test_trailing_comment_and_inline_list(self):
        text = """
schema_version: 1
defaults:
  ttl_minutes: 30
commands:
  /claim:   # 行内注释
    allowed_roles: ["agent", 'owner']
    required_state: ready
"""
        pol = Policy.from_text(text)
        ok, _, _ = pol.check("/claim", "agent", "ready")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
