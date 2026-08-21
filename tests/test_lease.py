"""租约模型测试：TTL / 过期边界 / holder 校验 / 序列化严格性。

对应 AC-4 基础（过期判定）与 ADR-0054 §8（租约格式）。
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter import lease as L  # noqa: E402
from arbiter.lease import Lease, LeaseError  # noqa: E402


def dt(text):
    return L.parse_iso(text)


T0 = dt("2026-08-21T12:00:00Z")


def make(ttl=240, acquired="2026-08-21T12:00:00Z", holder="octo"):
    return Lease("Cloudbird-Software/.github#165", holder, acquired, ttl)


class TestLeaseExpiry(unittest.TestCase):
    def test_not_expired_inside_ttl(self):
        lz = make(ttl=240)
        self.assertFalse(lz.is_expired(T0))
        self.assertFalse(lz.is_expired(dt("2026-08-21T15:59:59Z")))

    def test_expired_after_ttl(self):
        lz = make(ttl=240)
        self.assertTrue(lz.is_expired(dt("2026-08-21T16:00:01Z")))
        self.assertTrue(lz.is_expired(dt("2026-08-22T12:00:00Z")))

    def test_boundary_minute_counts_as_expired(self):
        # 边界归过期（fail-closed）：now == expires_at 即失效
        lz = make(ttl=240)
        self.assertEqual(L.to_iso(lz.expires_at), "2026-08-21T16:00:00Z")
        self.assertTrue(lz.is_expired(dt("2026-08-21T16:00:00Z")))

    def test_zero_ttl_immediately_expired(self):
        lz = make(ttl=1)
        self.assertTrue(lz.is_expired(dt("2026-08-21T12:01:00Z")))

    def test_expiry_uses_injected_clock_not_wall(self):
        # 纯函数：过期判定不取真实时钟（now 必须传入）
        lz = make(ttl=60)
        far_future = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        self.assertTrue(lz.is_expired(far_future))
        self.assertFalse(lz.is_expired(past))


class TestHolder(unittest.TestCase):
    def test_holder_match(self):
        self.assertTrue(make(holder="octo").held_by("octo"))

    def test_holder_mismatch(self):
        self.assertFalse(make(holder="octo").held_by("someone-else"))
        self.assertFalse(make(holder="octo").held_by(""))
        self.assertFalse(make(holder="octo").held_by("OCTO"))  # login 大小写敏感


class TestLeaseJson(unittest.TestCase):
    def test_roundtrip(self):
        lz = make()
        restored = Lease.from_json(lz.to_json())
        self.assertEqual(restored.holder, lz.holder)
        self.assertEqual(restored.card, lz.card)
        self.assertEqual(restored.ttl_minutes, 240)
        self.assertEqual(L.to_iso(restored.acquired_at), "2026-08-21T12:00:00Z")

    def test_unknown_key_rejected(self):
        raw = ('{"card":"o/r#1","holder":"a","acquired_at":"2026-08-21T12:00:00Z",'
               '"ttl_minutes":10,"extra":1}')
        with self.assertRaises(LeaseError):
            Lease.from_json(raw)

    def test_missing_key_rejected(self):
        raw = '{"card":"o/r#1","holder":"a","ttl_minutes":10}'
        with self.assertRaises(LeaseError):
            Lease.from_json(raw)

    def test_bad_timestamp_rejected(self):
        raw = ('{"card":"o/r#1","holder":"a","acquired_at":"not-a-time",'
               '"ttl_minutes":10}')
        with self.assertRaises(LeaseError):
            Lease.from_json(raw)

    def test_payload_keys_exact(self):
        self.assertEqual(set(make().to_payload()),
                         {"card", "holder", "acquired_at", "ttl_minutes"})


class TestRefNames(unittest.TestCase):
    def test_lease_ref_format(self):
        self.assertEqual(
            L.lease_ref("Cloudbird-Software", "arbiter", 165),
            "refs/leases/Cloudbird-Software__arbiter__165")

    def test_lease_ref_unique_per_card(self):
        # 每卡唯一 ref 名 = “每卡至多 1 个活跃租约”的结构性保证（ADR-0054 §2）
        a = L.lease_ref("Cloudbird-Software", ".github", 165)
        b = L.lease_ref("Cloudbird-Software", ".github", 166)
        c = L.lease_ref("Cloudbird-Software", "arbiter", 165)
        self.assertEqual(len({a, b, c}), 3)

    def test_seen_ref_is_sha1_of_delivery(self):
        import hashlib

        rid = "6d4b1e70-aaaa-bbbb-cccc-dddddddddddd"
        self.assertEqual(
            L.seen_ref(rid),
            "refs/seen/" + hashlib.sha1(rid.encode()).hexdigest())
        # 不同 delivery-id → 不同标记
        self.assertNotEqual(L.seen_ref(rid), L.seen_ref(rid + "x"))

    def test_seen_ref_rejects_empty(self):
        with self.assertRaises(LeaseError):
            L.seen_ref("  ")

    def test_parse_card_short_and_full(self):
        # '.github' 类以点开头的仓库名合法（组合段不以 '.' 开头即可）
        self.assertEqual(L.parse_card(".github#165"),
                         ("Cloudbird-Software", ".github", 165))
        self.assertEqual(L.parse_card("Cloudbird-Software/.github#165"),
                         ("Cloudbird-Software", ".github", 165))
        self.assertEqual(L.lease_ref("Cloudbird-Software", ".github", 165),
                         "refs/leases/Cloudbird-Software__.github__165")

    def test_ref_segment_rules(self):
        self.assertTrue(L.valid_ref_segment("org__repo__1"))
        self.assertFalse(L.valid_ref_segment(".lead"))
        self.assertFalse(L.valid_ref_segment("a..b"))
        self.assertFalse(L.valid_ref_segment("x.lock"))
        self.assertFalse(L.valid_ref_segment("a b"))
        self.assertFalse(L.valid_ref_segment("a~b"))
        # org 以 '.' 开头会让组合段非法 → parse 拒绝
        with self.assertRaises(LeaseError):
            L.parse_card("repo#1", org=".org")

    def test_parse_card_rejects_garbage(self):
        for bad in ("", "abc", "#1", "repo#x", "repo#0", "repo#-3",
                    "a/b/c#1", "re po#1", "repo#1;rm", "org/../repo#2"):
            with self.assertRaises(LeaseError, msg=bad):
                L.parse_card(bad)

    def test_iso_roundtrip_with_z(self):
        self.assertEqual(L.to_iso(L.parse_iso("2026-08-21T12:34:56Z")),
                         "2026-08-21T12:34:56Z")
        self.assertEqual(L.to_iso(L.parse_iso("2026-08-21T12:34:56+00:00")),
                         "2026-08-21T12:34:56Z")


if __name__ == "__main__":
    unittest.main()
