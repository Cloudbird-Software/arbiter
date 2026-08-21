# arbiter — 仲裁内核（L2 最小仓）

Cloudbird-Software 组织的写入仲裁内核（宪法 §1 / [ADR-0054](https://github.com/Cloudbird-Software/agent-registry/blob/main/decisions/ADR-0054-arbiter-kernel-v1.md) / 工作卡 .github#165）。

职责：对写入类命令（/claim /release /retry）做**确定性裁决** —— 策略表
（capabilities.yaml）+ 原子 CAS（git refs）+ TTL 租约 + 防重放台账。

不变量（宪法 §1）：

- **无 LLM** —— 授权决策零 LLM；纯 Python 3 标准库 + bash，零第三方依赖
- **默认拒绝** —— 无匹配规则 = deny
- **fail-closed** —— API 失败 ≠ 拒绝 ≠ 放行（infra 独立通道，exit 2）
- **自带测试** —— 并发/重放/越权/过期四类用例 + 静态扫描断言
- **误放行台账** —— tests/false_decision_ledger.jsonl + docs/FALSE-DECISIONS.md

> bootstrap 仓（W1-C2）：内核 v1 内容经 PR 进入；本 README 为建仓初始 commit。
