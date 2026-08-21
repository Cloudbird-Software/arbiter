"""CLI 入口：python3 -m arbiter.cli <cmd> --card <repo>#<n> ...

用法（conductor 转介形态，宪法 §11）：
  python3 -m arbiter.cli /claim \\
      --card .github#165 --sender octocat --sender-role agent \\
      --delivery-id 6d4b1e70-... --event created --current-state ready \\
      [--now 2026-08-21T12:00:00Z]

输出：单行 JSON {verdict, code, reason, lease?, ...}
退出码：0=allow/noop  1=deny  2=infra（fail-closed 独立通道）

后端选择：
  --backend github（默认）：令牌取 --token-env 指定的环境变量
      （默认依次 ARBITER_TOKEN → GH_TOKEN → GITHUB_TOKEN）；
      宿主仓 --leases-repo（默认 Cloudbird-Software/arbiter）
  --backend local --local-repo <dir>：测试后端（git CLI，可用裸仓）
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from . import lease as lease_mod
from . import policy as policy_mod
from .kernel import EXIT_CODE, adjudicate

DEFAULT_LEASES_REPO = "Cloudbird-Software/arbiter"
TOKEN_ENV_CHAIN = ("ARBITER_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arbiter.cli",
        description="Cloudbird-Software 仲裁内核 v1（ADR-0054；无 LLM/默认拒绝/fail-closed）")
    p.add_argument("command", help="/claim | /release | /retry（未知命令默认拒绝）")
    p.add_argument("--card", required=True, help="<repo>#<n> 或 <org>/<repo>#<n>")
    p.add_argument("--sender", required=True, help="发起者 login")
    p.add_argument("--sender-role", required=True, choices=["owner", "agent", "none"],
                   help="角色（conductor 判定后传入，ADR-0049）")
    p.add_argument("--delivery-id", required=True, help="GitHub delivery GUID（防重放幂等键）")
    p.add_argument("--event", required=True, choices=["created", "edited"],
                   help="只认 created（edited 直接 deny——纵深防御）")
    p.add_argument("--current-state", default="",
                   help="卡当前状态（调用方传入；缺省=空=不满足任何 required_state → deny）")
    p.add_argument("--now", default=None,
                   help="裁决时钟（ISO8601；缺省=当前 UTC——测试可注入）")
    p.add_argument("--org", default=lease_mod.DEFAULT_ORG, help="card 缺省 org 段时使用")
    p.add_argument("--capabilities", default=None,
                   help="策略表路径（缺省=仓根 capabilities.yaml）")
    p.add_argument("--backend", default="github", choices=["github", "local"],
                   help="CAS 后端（github=生产 / local=测试）")
    p.add_argument("--leases-repo", default=DEFAULT_LEASES_REPO,
                   help="github 后端宿主仓（租约 refs 所在仓）")
    p.add_argument("--token-env", default=None,
                   help="github 后端令牌环境变量名（缺省依次 %s）" % "/".join(TOKEN_ENV_CHAIN))
    p.add_argument("--local-repo", default=None,
                   help="local 后端的 git 仓库路径（裸仓即可）")
    return p


def _resolve_now(text: str | None) -> datetime.datetime:
    if text:
        return lease_mod.parse_iso(text)
    return lease_mod.utc_now()


def _make_backend(args):
    if args.backend == "local":
        if not args.local_repo:
            print("错误：--backend local 需要 --local-repo <dir>", file=sys.stderr)
            raise SystemExit(2)
        from .backend import LocalGitBackend

        return LocalGitBackend(args.local_repo)
    token = ""
    if args.token_env:
        token = os.environ.get(args.token_env, "")
    else:
        for name in TOKEN_ENV_CHAIN:
            token = os.environ.get(name, "")
            if token:
                break
    if not token:
        print("错误：github 后端缺少令牌（设 %s 或用 --token-env 指定）"
              % "/".join(TOKEN_ENV_CHAIN), file=sys.stderr)
        raise SystemExit(2)
    from .backend import GitHubRefBackend

    return GitHubRefBackend(token, args.leases_repo)


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    cap_path = args.capabilities or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capabilities.yaml")
    try:
        policy = policy_mod.Policy.load(cap_path)
    except (OSError, policy_mod.PolicyError) as exc:
        # 策略表加载失败 = fail-closed infra（不是 deny：拒绝会误导调用方归因）
        print(json.dumps({
            "verdict": "infra", "code": "policy-load-failed", "reason": str(exc),
            "lease": None, "command": args.command,
        }, ensure_ascii=False))
        return EXIT_CODE["infra"]

    decision = adjudicate(
        command=args.command,
        card=args.card,
        sender=args.sender,
        sender_role=args.sender_role,
        delivery_id=args.delivery_id,
        event=args.event,
        current_state=args.current_state,
        now=_resolve_now(args.now),
        policy=policy,
        backend=_make_backend(args),
        org=args.org,
    )
    print(json.dumps(decision, ensure_ascii=False, sort_keys=True))
    return EXIT_CODE[decision["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
