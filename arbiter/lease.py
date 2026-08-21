"""租约模型（纯逻辑：TTL 计算、过期判定、holder 校验、ref 命名）。

无 I/O、无时钟、无网络——时间一律由调用方注入（kernel 纯函数化，ADR-0054 §5）。
本模块不得 import 任何网络模块（tests/test_no_llm.py 断言）。
"""

from __future__ import annotations

import datetime
import json
import re

LEASE_REF_PREFIX = "refs/leases/"
SEEN_REF_PREFIX = "refs/seen/"

DEFAULT_ORG = "Cloudbird-Software"

# GitHub 仓/组织名形态（'.github' 类仓以点开头是合法存在）
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_CARD_RE = re.compile(r"^([A-Za-z0-9_.\-]+)#(\d+)$")

LEASE_JSON_KEYS = {"card", "holder", "acquired_at", "ttl_minutes"}


def valid_ref_segment(seg: str) -> bool:
    """git check-ref-format 单段规则：非空、不以 '.' 开头、无 '..'、
    不以 '.lock' 结尾、不含 ' ~^:?*[\\' 与控制字符。"""
    if not isinstance(seg, str) or not seg:
        return False
    if seg.startswith(".") or ".." in seg or seg.endswith(".lock"):
        return False
    if any(c in seg for c in " ~^:?*[]\\") or any(ord(c) < 32 for c in seg):
        return False
    return True


class LeaseError(Exception):
    """租约数据非法（解析/构造失败）——fail-closed。"""


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def to_iso(dt: datetime.datetime) -> str:
    """UTC ISO8601（Z 后缀，秒精度）——租约 JSON 的时间形态。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime.datetime:
    """严格解析 ISO8601；接受 Z 或 +00:00 后缀；无时区视为 UTC。"""
    if not isinstance(text, str) or not text:
        raise LeaseError(f"时间戳为空/非字符串: {text!r}")
    s = text.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError as exc:
        raise LeaseError(f"非法 ISO8601 时间戳 {text!r}: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def parse_card(card: str, org: str = DEFAULT_ORG) -> tuple:
    """解析 '<repo>#<n>' 或 '<org>/<repo>#<n>' → (org, repo, number)。

    '.github' 类以点开头的仓库名合法（ref 段合法性在 card_key 组合后校验）。
    """
    if not isinstance(card, str):
        raise LeaseError(f"card 必须为字符串: {card!r}")
    m = _CARD_RE.fullmatch(card.strip())
    if m:
        repo, n = m.group(1), int(m.group(2))
        owner = org
    else:
        # 形态 '<org>/<repo>#<n>'
        if "/" not in card or "#" not in card:
            raise LeaseError(f"card 形态必须为 <repo>#<n> 或 <org>/<repo>#<n>: {card!r}")
        prefix, _, num = card.rpartition("#")
        owner_repo = prefix.strip()
        if "/" not in owner_repo:
            raise LeaseError(f"card 形态非法: {card!r}")
        owner, _, repo = owner_repo.rpartition("/")
        m2 = re.fullmatch(r"\d+", num.strip())
        if not m2 or not _NAME_RE.fullmatch(owner) or not _NAME_RE.fullmatch(repo):
            raise LeaseError(f"card 形态非法: {card!r}")
        n = int(num)
    if n <= 0:
        raise LeaseError(f"卡号必须为正整数: {card!r}")
    if not _NAME_RE.fullmatch(repo) or not _NAME_RE.fullmatch(owner):
        raise LeaseError(f"card 含非法字符: {card!r}")
    # 组合段必须能落进 ref 名（org 前缀保证不以 '.' 开头）
    if not valid_ref_segment(card_key(owner, repo, n)):
        raise LeaseError(f"card 无法生成合法 ref 段: {card!r}")
    return owner, repo, n


def card_key(org: str, repo: str, number: int) -> str:
    """租约 ref 的卡段：'<org>__<repo>__<n>'（'/' 不能进 ref 名，双下划线替代）。"""
    if not isinstance(number, int) or number <= 0:
        raise LeaseError(f"卡号必须为正整数: {number!r}")
    seg = f"{org}__{repo}__{number}"
    if not valid_ref_segment(seg):
        raise LeaseError(f"非法 ref 段: {seg!r}")
    return seg


def lease_ref(org: str, repo: str, number: int) -> str:
    """每卡唯一租约 ref——'每卡至多 1 个活跃租约'的结构性保证（ADR-0054 §2）。"""
    return LEASE_REF_PREFIX + card_key(org, repo, number)


def seen_ref(delivery_id: str) -> str:
    """防重放幂等标记 ref：refs/seen/<sha1(delivery-id)>。"""
    import hashlib

    if not isinstance(delivery_id, str) or not delivery_id.strip():
        raise LeaseError(f"delivery-id 非法: {delivery_id!r}")
    digest = hashlib.sha1(delivery_id.strip().encode("utf-8")).hexdigest()
    return SEEN_REF_PREFIX + digest


class Lease:
    """一份租约：卡、持有者、取得时间、TTL。"""

    __slots__ = ("card", "holder", "acquired_at", "ttl_minutes")

    def __init__(self, card, holder, acquired_at, ttl_minutes):
        if not isinstance(holder, str) or not holder.strip():
            raise LeaseError(f"holder 必须为非空字符串: {holder!r}")
        parse_card(card)  # 校验 card 形态（card 保留原始规范串 '<org>/<repo>#<n>'）
        self.card = card
        self.holder = holder
        self.acquired_at = acquired_at if isinstance(acquired_at, datetime.datetime) else parse_iso(acquired_at)
        if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool) or ttl_minutes <= 0:
            raise LeaseError(f"ttl_minutes 必须为正整数: {ttl_minutes!r}")
        self.ttl_minutes = ttl_minutes

    # -- 过期判定（AC-4 的基础） -------------------------------------------

    @property
    def expires_at(self) -> datetime.datetime:
        return self.acquired_at + datetime.timedelta(minutes=self.ttl_minutes)

    def is_expired(self, now: datetime.datetime) -> bool:
        """到期即失效：now >= expires_at 视为过期（边界归过期——fail-closed）。"""
        if not isinstance(now, datetime.datetime):
            raise LeaseError(f"now 必须为 datetime: {now!r}")
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        return now >= self.expires_at

    def held_by(self, sender: str) -> bool:
        return isinstance(sender, str) and sender == self.holder

    # -- 序列化（commit 内嵌 JSON，严格键集） -------------------------------

    def to_payload(self) -> dict:
        return {
            "card": self.card,
            "holder": self.holder,
            "acquired_at": to_iso(self.acquired_at),
            "ttl_minutes": self.ttl_minutes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_payload(cls, data) -> "Lease":
        if not isinstance(data, dict):
            raise LeaseError(f"租约 payload 必须为 JSON 对象: {data!r}")
        keys = set(data)
        if keys != LEASE_JSON_KEYS:
            raise LeaseError(f"租约 payload 键集不符: {sorted(keys)}（期望 {sorted(LEASE_JSON_KEYS)}）")
        acquired = parse_iso(data["acquired_at"])
        return cls(data["card"], data["holder"], acquired, data["ttl_minutes"])

    @classmethod
    def from_json(cls, text: str) -> "Lease":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LeaseError(f"租约 JSON 解析失败: {exc}") from exc
        return cls.from_payload(data)

    def summary(self) -> dict:
        """裁决输出里的 lease 字段（人/机两读）。"""
        return {
            "card": self.card,
            "holder": self.holder,
            "ref": ref_of_card(self.card),
            "acquired_at": to_iso(self.acquired_at),
            "expires_at": to_iso(self.expires_at),
            "ttl_minutes": self.ttl_minutes,
        }


def ref_of_card(card: str, org: str = DEFAULT_ORG) -> str:
    owner, repo, n = parse_card(card, org)
    return lease_ref(owner, repo, n)
