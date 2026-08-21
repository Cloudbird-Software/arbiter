# arbiter — 仲裁内核（L2 最小仓）

Cloudbird-Software 组织的写入仲裁内核（宪法 §1 /
[ADR-0054](https://github.com/Cloudbird-Software/agent-registry/blob/main/decisions/ADR-0054-arbiter-kernel-v1.md) /
工作卡 [.github#165](https://github.com/Cloudbird-Software/.github/issues/165)）。

对写入类命令（`/claim` `/release` `/retry`）做**确定性裁决**：
策略表（[capabilities.yaml](capabilities.yaml)）+ 原子 CAS（git refs）+
TTL 租约 + 防重放台账。conductor 按 §11 唤醒矩阵把 issue_comment 事件
**转介**到这里，arbiter 不编排、不评论、只出裁决 JSON。

## 不变量（宪法 §1，机器可判定）

| 不变量 | 落地 | 验证 |
|---|---|---|
| 无 LLM | 纯 Python 3 标准库 + bash，零第三方依赖；网络访问仅 backend.py → api.github.com | `tests/test_no_llm.py` 静态断言 |
| 默认拒绝 | capabilities.yaml 无匹配规则 = deny（不是 abstain/error） | `tests/test_policy.py` |
| fail-closed | API 失败 → `infra`（exit 2），≠ 拒绝 ≠ 放行 | `tests/test_concurrency.py` |
| 自带测试 | 并发/重放/越权/过期四类用例 + 静态扫描 | `python3 -m unittest discover -s tests -v` |
| 误放行台账 | `tests/false_decision_ledger.jsonl` + [docs/FALSE-DECISIONS.md](docs/FALSE-DECISIONS.md) | 周审计独立复算 |

## 用法

CLI（conductor 转介形态；`--current-state` 由调用方传入——arbiter 不读 GitHub）：

```bash
python3 -m arbiter.cli /claim \
  --card .github#165 --sender octocat --sender-role agent \
  --delivery-id <guid> --event created --current-state ready
```

bash 包装（铸 App 令牌 → 调 CLI → 透传退出码与 JSON，见脚本头注释）：

```bash
scripts/adjudicate.sh /claim --card .github#165 --sender octocat \
  --sender-role agent --delivery-id <guid> --event created --current-state ready
```

裁决输出（单行 JSON）：`{"verdict": "allow|deny|noop|infra", "code": "...", "reason": "...", "lease": {...}}`

退出码：**0** = allow / noop（幂等重放）· **1** = deny · **2** = infra（重投，不是裁决）。

本地/离线（测试后端，git 裸仓即可）：加 `--backend local --local-repo <dir>`。

## 命令集 v1

| 命令 | 语义 | 败者/拒绝回复 |
|---|---|---|
| `/claim` | 卡 `ready` 态且角色 ∈ {agent, owner}：CAS `createRef refs/leases/<org>__<repo>__<n>` | 422 → `lost-race` / `lease-held`（明确败因） |
| `/release` | holder 本人或 owner，且租约未过期 → 删 ref | 非 holder / 过期 → deny（AC-4，非静默删） |
| `/retry` | 同 /release 语义（quarantine 回流预留） | 同上 |

租约：ref 指向的 commit message 内嵌 JSON `{card, holder, acquired_at, ttl_minutes}`；
TTL 唯一来源 = capabilities.yaml（默认 240 分钟）。过期租约由下一个 `/claim`
**原子接管**（新 commit 以旧为父 + 条件 update-ref），不允许静默删除。

## 安全模型

- **防重放（AC-3）**：只认 `--event created`（edited 直接 deny）；每个 delivery
  先落 `refs/seen/<sha1(delivery-id)>` 原子幂等标记（createRef CAS），422 =
  已处理 → `noop`。seen ref 指向的 commit message 即台账记录（delivery/命令/卡/发起者/时刻）。
- **每卡至多 1 个活跃租约（AC-2）**：租约 ref 名按卡唯一，createRef 原子性使
  第二个租约在结构上无法存在；并发认领恰一胜者，败者收到明确 code。
- **分层**：`kernel/policy/lease` 纯函数（不 import 网络模块、时钟注入、
  状态注入）；`backend.py` 唯一网络出口（仅 api.github.com）；
  `backend` 两种实现——GitHubRefBackend（生产，REST Git Data API）与
  LocalGitBackend（git CLI，测试/离线）。
- **越权（AC-1 默认拒绝）**：角色白名单在 capabilities.yaml；策略表未知键/
  未知值拒绝加载（fail-closed）；none 角色全部拒绝。
- **回滚**：整仓可拆（宪法 §6 新增式设施）——停转介即回到 W0 行为，无数据迁移。

## 仓库布局

```
capabilities.yaml          策略表（唯一授权真源）
arbiter/{policy,lease,kernel}.py   纯函数层（零网络 import）
arbiter/backend.py         CAS 后端（GitHubRefBackend / LocalGitBackend）
arbiter/cli.py             CLI 入口
scripts/adjudicate.sh      bash 包装（铸 App 令牌→CLI→透传）
tests/                     四类用例 + 静态扫描 + 误放行台账
docs/FALSE-DECISIONS.md    误放行/误拒记账约定
```
