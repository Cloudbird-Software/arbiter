"""并发与 CAS 语义测试（AC-2 / AC-4）。

- 多线程并发 /claim 同卡 → 恰一胜者（LocalGitBackend + 裸仓 + git 子进程，
  git ref 锁即仲裁——与 GitHub createRef 422 同构）。
- 过期租约并发接管 → 恰一胜者（条件 update-ref CAS）。
- /release /retry 的 holder/过期双校验（AC-4：拒绝而非静默删除）。
"""

import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter import lease as L  # noqa: E402
from arbiter import policy as policy_mod  # noqa: E402
from arbiter.backend import LocalGitBackend  # noqa: E402
from arbiter.kernel import CasConflict, InfraError  # noqa: E402
from arbiter.kernel import adjudicate as kernel_adjudicate  # noqa: E402

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


def adjudicate(backend, command, sender, role="agent", delivery="d0",
               event="created", state="ready", now=NOW, card="arbiter#7"):
    return kernel_adjudicate(
        command=command, card=card, sender=sender, sender_role=role,
        delivery_id=delivery, event=event, current_state=state,
        now=now, policy=POLICY, backend=backend)


class BareRepoTest(unittest.TestCase):
    """裸仓 fixture：每用例临时初始化（git init --bare）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="arbiter-test-")
        self.repo = os.path.join(self._tmp.name, "bare.git")
        subprocess.run(["git", "init", "--bare", "-q", self.repo],
                       check=True, capture_output=True, text=True)

    def tearDown(self):
        self._tmp.cleanup()

    def backend(self):
        return LocalGitBackend(self.repo)

    def read_lease_holder(self, owner="Cloudbird-Software", repo="arbiter", n=7):
        be = self.backend()
        sha = be.read_ref(L.lease_ref(owner, repo, n))
        return L.Lease.from_json(be.commit_message(sha)).holder


class TestBackendCas(BareRepoTest):
    def test_create_ref_conflict_on_existing(self):
        be = self.backend()
        sha = be.create_commit("m1")
        be.create_ref("refs/leases/x", sha)
        with self.assertRaises(CasConflict):
            be.create_ref("refs/leases/x", be.create_commit("m2"))

    def test_update_ref_expected_mismatch(self):
        be = self.backend()
        s1 = be.create_commit("m1")
        s2 = be.create_commit("m2")
        be.create_ref("refs/leases/x", s1)
        with self.assertRaises(CasConflict):
            be.update_ref("refs/leases/x", s2, expected=s2)  # 期望值不符
        be.update_ref("refs/leases/x", s2, expected=s1)      # 期望值相符 → 成功
        self.assertEqual(be.read_ref("refs/leases/x"), s2)

    def test_read_missing_ref_not_found(self):
        from arbiter.kernel import NotFoundError

        with self.assertRaises(NotFoundError):
            self.backend().read_ref("refs/leases/nope")

    def test_commit_message_roundtrip(self):
        be = self.backend()
        msg = L.Lease("Cloudbird-Software/arbiter#7", "octo", NOW, 240).to_json()
        sha = be.create_commit(msg)
        self.assertEqual(be.commit_message(sha), msg)


class TestConcurrentClaim(BareRepoTest):
    def test_parallel_claim_single_winner(self):
        """AC-2：N 个并发 /claim 同卡 → 恰一 allow，其余明确 lost-race/lease-held。"""
        n = 8
        barrier = threading.Barrier(n)
        results = [None] * n
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=30)
                be = self.backend()
                results[i] = adjudicate(be, "/claim", sender=f"agent-{i}",
                                        delivery=f"delivery-{i}")
            except Exception as exc:  # pragma: no cover - 线程内异常收集
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertFalse(errors, f"线程异常: {errors}")

        allows = [r for r in results if r["verdict"] == "allow"]
        denies = [r for r in results if r["verdict"] == "deny"]
        self.assertEqual(len(allows), 1, f"恰一胜者，实际 {len(allows)}: {results}")
        self.assertEqual(allows[0]["code"], "claimed")
        self.assertEqual(len(denies), n - 1)
        for d in denies:
            self.assertIn(d["code"], ("lost-race", "lease-held"),
                          f"败者须收到明确回复: {d}")
            self.assertTrue(d["reason"], "败者回复必须带 reason")
        # 租约 ref 恰一个，指向胜者
        self.assertEqual(self.read_lease_holder(),
                         allows[0]["lease"]["holder"])

    def test_parallel_claim_after_expiry_single_taker(self):
        """过期租约的并发接管：条件 update-ref CAS → 恰一接管者。"""
        be = self.backend()
        first = adjudicate(be, "/claim", sender="old-agent", delivery="d-old")
        self.assertEqual(first["code"], "claimed")
        later = L.parse_iso("2026-08-22T00:00:00Z")  # 12h 后：租约已过期

        n = 6
        barrier = threading.Barrier(n)
        results = [None] * n
        errors = []

        def worker(i):
            try:
                barrier.wait(timeout=30)
                b = self.backend()
                results[i] = adjudicate(b, "/claim", sender=f"agent-{i}",
                                        delivery=f"d-take-{i}", now=later)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertFalse(errors, f"线程异常: {errors}")

        takers = [r for r in results if r["verdict"] == "allow"]
        losers = [r for r in results if r["verdict"] == "deny"]
        self.assertEqual(len(takers), 1, f"恰一接管者，实际 {len(takers)}: {results}")
        self.assertEqual(takers[0]["code"], "lease-taken-over")
        self.assertEqual(len(losers), n - 1)
        for d in losers:
            self.assertIn(d["code"], ("lost-race", "lease-held"), d)
        self.assertEqual(self.read_lease_holder(), takers[0]["lease"]["holder"])


class TestAc4ReleaseGuard(BareRepoTest):
    """AC-4：过期/holder 不符的 /release /retry → deny（不是静默删除）。"""

    def setUp(self):
        super().setUp()
        be = self.backend()
        first = adjudicate(be, "/claim", sender="holder-a", delivery="d-c")
        assert first["code"] == "claimed"

    def test_holder_can_release(self):
        d = adjudicate(self.backend(), "/release", sender="holder-a",
                       delivery="d-r1")
        self.assertEqual(d["verdict"], "allow")
        self.assertEqual(d["code"], "released")

    def test_non_holder_cannot_release(self):
        d = adjudicate(self.backend(), "/release", sender="intruder",
                       delivery="d-r2")
        self.assertEqual(d["verdict"], "deny")
        self.assertEqual(d["code"], "not-holder")
        self.assertIsNotNone(d["lease"], "拒绝回复应带当前租约证据")

    def test_owner_can_force_release(self):
        d = adjudicate(self.backend(), "/release", sender="randypanding",
                       role="owner", delivery="d-r3")
        self.assertEqual(d["verdict"], "allow")
        self.assertEqual(d["code"], "released")

    def test_expired_release_denied_not_silent(self):
        later = L.parse_iso("2026-08-22T00:00:00Z")
        d = adjudicate(self.backend(), "/release", sender="holder-a",
                       delivery="d-r4", now=later)
        self.assertEqual(d["verdict"], "deny")
        self.assertEqual(d["code"], "lease-expired")
        # 关键：不是静默删除——租约 ref 仍在，等待 /claim 原子接管
        self.assertIsNotNone(self.backend().read_ref(
            L.lease_ref("Cloudbird-Software", "arbiter", 7)))

    def test_expired_retry_denied(self):
        later = L.parse_iso("2026-08-22T00:00:00Z")
        d = adjudicate(self.backend(), "/retry", sender="holder-a",
                       delivery="d-r5", now=later)
        self.assertEqual(d["verdict"], "deny")
        self.assertEqual(d["code"], "lease-expired")

    def test_retry_same_semantics_as_release_for_holder(self):
        d = adjudicate(self.backend(), "/retry", sender="holder-a",
                       delivery="d-r6")
        self.assertEqual(d["verdict"], "allow")
        self.assertEqual(d["code"], "retried")

    def test_release_without_lease_denied(self):
        be = self.backend()
        adjudicate(be, "/release", sender="holder-a", delivery="d-x1")
        d = adjudicate(be, "/release", sender="holder-a", delivery="d-x2")
        self.assertEqual(d["verdict"], "deny")
        self.assertEqual(d["code"], "no-active-lease")

    def test_claim_after_release_allowed_again(self):
        be = self.backend()
        adjudicate(be, "/release", sender="holder-a", delivery="d-y1")
        d = adjudicate(be, "/claim", sender="agent-b", delivery="d-y2")
        self.assertEqual(d["verdict"], "allow")
        self.assertEqual(d["code"], "claimed")

    def test_same_sender_reclaim_idempotent(self):
        d = adjudicate(self.backend(), "/claim", sender="holder-a",
                       delivery="d-z2")
        self.assertEqual(d["verdict"], "allow")
        self.assertEqual(d["code"], "already-holder")


class TestInfraChannel(BareRepoTest):
    def test_infra_error_is_never_allow_or_deny(self):
        class ExplodingBackend(LocalGitBackend):
            def create_commit(self, message, parent=None):
                raise InfraError("模拟网络故障")

        d = adjudicate(ExplodingBackend(self.repo), "/claim", sender="a",
                       delivery="d-infra")
        self.assertEqual(d["verdict"], "infra")
        self.assertEqual(d["code"], "infra-error")
        from arbiter.kernel import EXIT_CODE

        self.assertEqual(EXIT_CODE["infra"], 2)

    def test_tampered_lease_json_fails_closed(self):
        # ref 指向的 commit 被篡改（非法 JSON/键集）→ deny（lease-data-invalid）
        be = self.backend()
        sha = be.create_commit('{"tampered": true}')
        be.create_ref(L.lease_ref("Cloudbird-Software", "arbiter", 7), sha)
        d = adjudicate(self.backend(), "/release", sender="x", delivery="d-t1")
        self.assertEqual(d["verdict"], "deny")
        self.assertEqual(d["code"], "lease-data-invalid")


if __name__ == "__main__":
    unittest.main()
