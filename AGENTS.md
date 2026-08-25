# AGENTS.md（索引型——只放不可推断的约束，细节按需读索引）

## 命令

- 测试：`python3 -m unittest discover -s tests -v`（纯标准库零依赖，无需安装步骤）
- 裁决 CLI：`python3 -m arbiter.cli --help`（conductor 转介形态与参数示例见 [README.md](README.md)）
- bash 包装自检：`bash -n scripts/adjudicate.sh`

## 硬规则（违反 = PR 打回）

1. 认证：一切 push/PR 用 cloudbrid-agent App 令牌，禁个人 PAT。获取（脚本 pin 到已审阅提交，禁 `curl|bash` 浮动 main 指针，ADR-0021）：
   `GH_TOKEN=$(REPO=arbiter bash <(curl -sS https://raw.githubusercontent.com/Cloudbird-Software/.github/f72d9520706c8fca974d92456f65cae5c1412bb7/scripts/gh-app-token.sh))`
2. 无 LLM 不变量（宪法 §1）：纯 Python 3 标准库 + bash、零第三方依赖；网络出口仅 backend.py → api.github.com——`tests/test_no_llm.py` 静态断言，违反即红
3. 默认拒绝 + fail-closed：策略表无匹配规则 = deny；API 失败 = `infra`（exit 2），禁止把 infra 当裁决或放行
4. 误放行必须记账：`tests/false_decision_ledger.jsonl` + [docs/FALSE-DECISIONS.md](docs/FALSE-DECISIONS.md)，周审计独立复算
5. 一个 PR 一件事，diff < 400 行；bug 修复先在 tests/ 写复现失败测试（离线注入用 `tests/fakebackend.py`）；提交信息用 Conventional Commits

## 索引（用到再读，不要全读）

| 场景 | 读这个 |
| --- | --- |
| 谁能 /claim /release /retry、TTL 等裁决规则 | [capabilities.yaml](capabilities.yaml)（策略表唯一授权真源，ADR-0054 §4） |
| 内核结构与分层 | [arbiter/](arbiter/) 包注释（cli / kernel / policy / lease / backend） |
| 设计依据与不变量表 | [README.md](README.md) + ADR-0054（正本在 [archive 仓 adr/](https://github.com/Cloudbird-Software/archive/tree/main/adr)） |
| 并发 / 重放 / 越权 / 过期用例 | `tests/` |
