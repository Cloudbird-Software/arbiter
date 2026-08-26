"""命令裁决入口 adjudicate() —— 纯函数核心。

分层铁律（ADR-0054 §3/§5）：
- 本模块不 import 任何网络模块（连 backend 也不 import——后端经鸭子类型注入，
  协议异常在本模块定义；tests/test_no_llm.py 静态断言）。
- 不读 GitHub issue（卡当前状态由调用方传入）、不取真实时钟（now 注入）。

裁决输出 JSON：{verdict, code, reason, lease?}；退出码 0=allow/noop、1=deny、
2=infra（fail-closed：API 失败≠拒绝≠放行，单独通道——宪法 §6 精神）。
"""

from __future__ import annotations

import json

from . import lease as lease_mod
from .policy import Policy

VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"
VERDICT_NOOP = "noop"   # 幂等重放：该 delivery 已处理过，不再产生任何效果（AC-3）
VERDICT_INFRA = "infra" # 基础设施失败：既非 allow 也非 deny（fail-closed 独立通道）

EXIT_ALLOW = 0
EXIT_DENY = 1
EXIT_INFRA = 2

# verdict → exit code 映射（allow/noop 同为放行侧退出码，见模块 docstring）
EXIT_CODE = {VERDICT_ALLOW: EXIT_ALLOW, VERDICT_NOOP: EXIT_ALLOW,
             VERDICT_DENY: EXIT_DENY, VERDICT_INFRA: EXIT_INFRA}

CLAIM = "/claim"
RELEASE = "/release"
RETRY = "/retry"


# ---------------------------------------------------------------------------
# 后端协议（backend.py 实现两个：GitHubRefBackend / LocalGitBackend）
# 异常定义在此处使 kernel 零网络 import；backend 导入使用。
# ---------------------------------------------------------------------------

class InfraError(Exception):
    """后端基础设施失败（网络/API/5xx）——裁决走 infra 通道，不是 deny。"""


class CasConflict(Exception):
    """CAS 冲突：createRef 422（ref 已存在）或条件 update 失败。"""


class NotFoundError(Exception):
    """目标 ref/commit 不存在（404）。"""


def _decision(verdict, code, reason, lease=None, **extra):
    out = {"verdict": verdict, "code": code, "reason": reason}
    out["lease"] = lease.summary() if isinstance(lease, lease_mod.Lease) else lease
    out.update(extra)
    return out


def adjudicate(command, card, sender, sender_role, delivery_id, event,
               current_state, now, policy, backend, org=lease_mod.DEFAULT_ORG) -> dict:
    """裁决一条写入命令。

    参数：
      command        /claim | /release | /retry（未知命令走默认拒绝）
      card           '<repo>#<n>' 或 '<org>/<repo>#<n>'
      sender         发起者 login
      sender_role    owner | agent | none（conductor 判定后传入，ADR-0049）
      delivery_id    GitHub 事件 delivery GUID（防重放幂等键）
      event          created | edited（只认 created——纵深防御）
      current_state  卡当前状态（调用方传入，arbiter 不读 GitHub）
      now            datetime（注入时钟）
      policy         policy.Policy（capabilities.yaml 加载产物）
      backend        后端实例（create_ref/read_ref/update_ref/delete_ref/
                     create_commit/commit_message，异常协议见本模块）
    """
    base = {
        "command": command,
        "card": card,
        "sender": sender,
        "sender_role": sender_role,
        "delivery_id": delivery_id,
    }
    try:
        owner, repo, number = lease_mod.parse_card(card, org)
        canonical_card = f"{owner}/{repo}#{number}"
        base["card"] = canonical_card

        # ---- 1. 事件白名单（纵深防御：编辑后重触发不进任何写路径，AC-3）----
        if event != "created":
            return _decision(VERDICT_DENY, "event-not-created",
                             f"只认 created 事件（实际 {event!r}）——编辑重触发拒绝",
                             **base)

        # ---- 2. 防重放：原子幂等标记（refs/seen/<sha1>，422=已处理=no-op）----
        # （InfraError 不在此捕获——交由函数级 infra 通道统一上报，可重投）
        seen = lease_mod.seen_ref(delivery_id)
        ledger_msg = json.dumps(
            {"delivery_id": delivery_id, "command": command, "card": canonical_card,
             "sender": sender, "at": lease_mod.to_iso(now)},
            ensure_ascii=False, sort_keys=True)
        try:
            marker = backend.create_commit(ledger_msg)
            backend.create_ref(seen, marker)
        except CasConflict:
            # 台账可查：refs/seen/<sha1> 指向的 commit message 即首次处理记录
            return _decision(VERDICT_NOOP, "replay-detected",
                             f"delivery {delivery_id} 已处理过（seen ref 存在）——幂等 no-op",
                             **base)

        # ---- 3. 策略表静态判定（默认拒绝在 policy.check 内）----
        allowed, code, reason = policy.check(command, sender_role, current_state)
        if not allowed:
            return _decision(VERDICT_DENY, code, reason, **base)

        # ---- 4. 命令语义 ----
        if command == CLAIM:
            return _claim(policy, backend, owner, repo, number, sender, now, base)
        if command in (RELEASE, RETRY):
            return _release(command, backend, owner, repo, number, sender,
                            sender_role, now, base)
        # policy.check 已保证命令在表内；表内命令必须被上面分支覆盖——
        # 到这里是策略表与 kernel 命令集脱节，按 infra 报告（不是静默）
        return _decision(VERDICT_INFRA, "kernel-command-gap",
                         f"命令 {command!r} 在策略表内但 kernel 未实现（表/内核脱节）",
                         **base)
    except InfraError as exc:
        return _decision(VERDICT_INFRA, "infra-error",
                         f"后端失败（不裁夺，可重投）: {exc}", **base)
    except lease_mod.LeaseError as exc:
        # 租约数据非法（如 ref 指向的 commit JSON 被篡改/损坏）→ fail-closed 拒绝
        return _decision(VERDICT_DENY, "lease-data-invalid",
                         f"租约数据非法: {exc}", **base)


# ---------------------------------------------------------------------------
# /claim —— CAS 抢注；422=败者明确回复（AC-2）
# ---------------------------------------------------------------------------

def _claim(policy, backend, owner, repo, number, sender, now, base):
    ref = lease_mod.lease_ref(owner, repo, number)
    payload = lease_mod.Lease(
        f"{owner}/{repo}#{number}", sender, now, policy.ttl_minutes)
    new_sha = backend.create_commit(payload.to_json())
    try:
        backend.create_ref(ref, new_sha)
        return _decision(VERDICT_ALLOW, "claimed",
                         f"租约创建成功（TTL {policy.ttl_minutes} 分钟）",
                         lease=payload, **base)
    except CasConflict:
        pass  # 落入下述既有租约处置

    try:
        existing_sha = backend.read_ref(ref)
    except NotFoundError:
        # 既有 ref 在 422 与读取之间被释放——重试 CAS 一次，仍冲突则 lost-race
        try:
            backend.create_ref(ref, new_sha)
            return _decision(VERDICT_ALLOW, "claimed",
                             f"租约创建成功（竞态窗口后重试，TTL {policy.ttl_minutes} 分钟）",
                             lease=payload, **base)
        except CasConflict:
            return _decision(VERDICT_DENY, "lost-race",
                             "CAS 两次冲突——并发认领竞速失败", **base)

    existing = lease_mod.Lease.from_json(backend.commit_message(existing_sha))

    if not existing.is_expired(now):
        if existing.held_by(sender):
            # 同一持有者重复 /claim：幂等返回既有租约（不新建、不改时戳）
            return _decision(VERDICT_ALLOW, "already-holder",
                             "你已持有该卡租约（幂等返回既有租约）",
                             lease=existing, **base)
        return _decision(VERDICT_DENY, "lease-held",
                         f"卡租约被 {existing.holder} 持有至 "
                         f"{lease_mod.to_iso(existing.expires_at)}（UTC）",
                         lease=existing, **base)

    # ---- 过期接管：ff-CAS（新 commit 以旧为父，条件更新=原子接管）----
    takeover = lease_mod.Lease(
        f"{owner}/{repo}#{number}", sender, now, policy.ttl_minutes)
    takeover_sha = backend.create_commit(takeover.to_json(), parent=existing_sha)
    try:
        backend.update_ref(ref, takeover_sha, expected=existing_sha)
    except CasConflict:
        return _decision(VERDICT_DENY, "lost-race",
                         "过期租约接管竞速失败（他人先完成接管）", **base)
    return _decision(VERDICT_ALLOW, "lease-taken-over",
                     f"前租约（{existing.holder}）已过期，原子接管成功",
                     lease=takeover, **base)


# ---------------------------------------------------------------------------
# /release、/retry —— holder+未过期双校验后删 ref（AC-4：拒绝，非静默删）
# ---------------------------------------------------------------------------

def _release(command, backend, owner, repo, number, sender, sender_role, now, base):
    ref = lease_mod.lease_ref(owner, repo, number)
    try:
        sha = backend.read_ref(ref)
    except NotFoundError:
        return _decision(VERDICT_DENY, "no-active-lease",
                         f"卡 {owner}/{repo}#{number} 无活跃租约，无可释放", **base)

    existing = lease_mod.Lease.from_json(backend.commit_message(sha))

    if not existing.held_by(sender) and sender_role != "owner":
        return _decision(VERDICT_DENY, "not-holder",
                         f"sender {sender!r} 非持有者（{existing.holder}）且非 owner",
                         lease=existing, **base)
    if existing.is_expired(now):
        # AC-4：过期租约的 release/retry = 拒绝（不是静默删除——留待接管/回收路径）
        return _decision(VERDICT_DENY, "lease-expired",
                         f"租约已于 {lease_mod.to_iso(existing.expires_at)}（UTC）过期——"
                         "过期租约不接受 release/retry，等待 /claim 原子接管",
                         lease=existing, **base)

    backend.delete_ref(ref)
    code = "released" if command == RELEASE else "retried"
    verb = "释放" if command == RELEASE else "重试回流释放"
    return _decision(VERDICT_ALLOW, code,
                     f"租约{verb}成功（holder={existing.holder}）",
                     lease=existing, **base)
