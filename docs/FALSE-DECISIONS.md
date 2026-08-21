# 误放行/误拒台账记账约定（docs/FALSE-DECISIONS.md，ADR-0054 §7）

本文件定义什么算误放行（false-allow）、什么算误拒（false-deny）、
如何记录进 `tests/false_decision_ledger.jsonl`、以及回流路径。
台账是宪法 §8 安全正确性指标「仲裁误放行/误拒数」的落盘形态。

## 1. 什么算误放行（false-allow）

裁决输出 `verdict=allow`（或产生等效状态写入），但按 capabilities.yaml 与
命令语义本应 deny。典型：

- **越权放行**：sender_role 不在 allowed_roles 却 allow；
- **状态放行**：卡非 required_state（如 in-progress）仍 /claim 成功；
- **双主放行**：并发 /claim 后存在两个自认持有者（结构性不可能——每卡唯一
  ref 名；若出现即最高级事故，标 `scenario=structural-break`）;
- **过期释放放行**：租约已过期仍 /release /retry 成功（AC-4 明确禁止）;
- **冒名放行**：非 holder 且非 owner 的 /release 成功。

## 2. 什么算误拒（false-deny）

裁决输出 `verdict=deny`，但按策略表与命令语义本应 allow。典型：

- **合法认领误拒**：role/state 均满足却 deny（如 lost-race 误判、
  already-holder 误报 lease-held）;
- **合法释放误拒**：holder 本人、租约未过期，却因 holder 大小写/空白
  处理等缺陷被拒；
- **重放误伤**：不同 delivery-id 被误判 replay-detected（no-op 吞掉合法请求）。

注意：**infra 不是误拒**。API 失败走 exit 2 独立通道（fail-closed），
调用方重投即可，不计入台账（宪法 §6）。

## 3. 如何记录

1. 每例一行 JSON 追加到 `tests/false_decision_ledger.jsonl`
   （`#` 注释行除外；字段见该文件头注释）；
2. `expected` 必须引用可执行依据：AC 编号（卡 .github#165）或
   capabilities.yaml 条款，不接受"感觉应该"；
3. 记录时**不得修改裁决代码**——先固化观察，再走回流。

## 4. 回流路径

1. 记台账 → 2. 开 failing 回归测试（先红）→ 3. 修复 kernel/policy →
   4. 全套测试绿 → 5. PR（body 引用台账行日期与 kind）→ 6. 台账行补
   `fix_ref`。误放行类修复必须附"为何旧测试没抓住"的一句话分析。

## 5. 与周审计的关系

owner 周审计（宪法 §7）从原始事件独立复算仲裁输出；复算差异即本台账
输入源之一。台账非空行数 = 当期误放行/误拒数，进 dashboard 账本。
