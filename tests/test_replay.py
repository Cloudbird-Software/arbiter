"""防重放测试（AC-3）：同 delivery-id 重复投递 → 幂等 no-op；edited 拒绝。

台账可查性：refs/seen/<sha1(delivery-id)> 指向的 commit message 即首次处理
记录（delivery_id/command/card/sender/at）。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter import lease as L  # noqa: E402
from arbiter import policy as policy_mod  # noqa: E402
from arbiter.kernel import EXIT_CODE, adjudicate  # noqa: E402
from fakebackend import FakeBackend  # noqa: E402

POLICY = policy_mod.Policy.from_text("""
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
""")

NOW = L.parse_iso("2026-08-21T12:00:00Z")
DELIVERY = "6d4b1e70-1111-2222-3333-444444444444"


def run(backend, command="/claim", delivery=DELIVERY, event="created",
        sender="octo", role="agent", state="ready"):
    return adjudicate(
        command=command, card="arbiter#7", sender=sender, sender_role=role,
        delivery_id=delivery, event=event, current_state=state,
        now=NOW, policy=POLICY, backend=backend)


class TestReplay(unittest.TestCase):
    def test_first_delivery_claims(self):
        be = FakeBackend()
        d = run(be)
        self.assertEqual(d["verdict"], "allow")
        self.assertEqual(d["code"], "claimed")
        self.assertEqual(be.seen_count(), 1)
        self.assertEqual(len(be.lease_shas()), 1)

    def test_duplicate_delivery_is_noop(self):
        be = FakeBackend()
        first = run(be)
        self.assertEqual(first["verdict"], "allow")
        before = dict(be.refs)
        second = run(be)  # 同 delivery-id 重复投递
        self.assertEqual(second["verdict"], "noop")
        self.assertEqual(second["code"], "replay-detected")
        self.assertEqual(EXIT_CODE[second["verdict"]], 0)
        # 状态零变化：无新 ref、无新 commit 效果、租约未被二次创建/释放
        self.assertEqual(be.refs, before)

    def test_replay_of_denied_delivery_is_noop(self):
        # 被拒的投递同样占用 delivery 幂等键——重复投递不重复判定
        be = FakeBackend()
        denied = run(be, role="none")
        self.assertEqual(denied["verdict"], "deny")
        again = run(be, role="none")
        self.assertEqual(again["verdict"], "noop")
        self.assertEqual(again["code"], "replay-detected")

    def test_different_delivery_not_treated_as_replay(self):
        be = FakeBackend()
        run(be, sender="a")
        other = run(be, delivery="00000000-9999-8888-7777-666666666666",
                    sender="b")
        # 第二个 delivery：正常裁决路径 → lease-held 拒绝（不是 replay）
        self.assertEqual(other["verdict"], "deny")
        self.assertEqual(other["code"], "lease-held")

    def test_edited_event_denied_without_consuming_delivery(self):
        be = FakeBackend()
        d = run(be, event="edited")
        self.assertEqual(d["verdict"], "deny")
        self.assertEqual(d["code"], "event-not-created")
        # 纵深防御：edited 不进任何写路径——无 seen ref、无租约
        self.assertEqual(be.seen_count(), 0)
        self.assertEqual(len(be.lease_shas()), 0)

    def test_seen_ref_commit_is_auditable_ledger(self):
        be = FakeBackend()
        run(be)
        seen_ref = L.seen_ref(DELIVERY)
        marker_sha = be.refs[seen_ref]
        record = json.loads(be.commit_message(marker_sha))
        self.assertEqual(record["delivery_id"], DELIVERY)
        self.assertEqual(record["command"], "/claim")
        self.assertEqual(record["sender"], "octo")
        self.assertEqual(record["at"], "2026-08-21T12:00:00Z")

    def test_replay_after_release_still_noop(self):
        # 已处理的 delivery 即便租约已被释放，重复投递仍 no-op（不重新抢注）
        be = FakeBackend()
        run(be, delivery=DELIVERY)
        rel = run(be, command="/release", delivery="d-release-1")
        self.assertEqual(rel["code"], "released")
        again = run(be, delivery=DELIVERY)
        self.assertEqual(again["verdict"], "noop")


class TestExitCodeMapping(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(EXIT_CODE["allow"], 0)
        self.assertEqual(EXIT_CODE["noop"], 0)
        self.assertEqual(EXIT_CODE["deny"], 1)
        self.assertEqual(EXIT_CODE["infra"], 2)


if __name__ == "__main__":
    unittest.main()
