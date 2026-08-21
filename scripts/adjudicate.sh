#!/usr/bin/env bash
# adjudicate.sh —— arbiter 仲裁 bash 包装（ADR-0054）
#
# 职责：铸 App 令牌（可选）→ 调 python -m arbiter.cli → 原样透传退出码与 JSON。
# 退出码与 cli 一致：0=allow/noop 1=deny 2=infra（fail-closed 独立通道）。
#
# 令牌来源（按序，任一命中即用；--backend local 时跳过铸币）：
#   1) 环境变量 ARBITER_TOKEN / GH_TOKEN / GITHUB_TOKEN（已铸好则直接透传）
#   2) CB_APP_ID + AGENT_APP_SECRET(_FILE) → gh-app-token.sh（.github 仓
#      scripts/gh-app-token.sh——单一真源，本仓不复制其实现；路径经
#      GH_APP_TOKEN_SCRIPT 指定或从 PATH 查找），REPO=arbiter 单仓作用域
#      （AG-1 最小权限；后续 conductor 转介即用此通道——W1-C2 #165）
#
# 用法：
#   scripts/adjudicate.sh /claim --card .github#165 --sender octocat \
#     --sender-role agent --delivery-id <guid> --event created --current-state ready
#   本地测试（无网络）：scripts/adjudicate.sh /claim ... --backend local --local-repo /tmp/bare.git
set -uo pipefail

# ---------- python 探测（Windows 商店 python3 是无功能 stub，须验证可执行） ----------
PY_BIN=""
for _cand in python3 python; do
  if command -v "$_cand" >/dev/null 2>&1 && "$_cand" -c 'print(1)' >/dev/null 2>&1; then
    PY_BIN="$_cand"; break
  fi
done
[[ -n "$PY_BIN" ]] || { echo "错误：找不到可用的 python3/python" >&2; exit 2; }

# ---------- local 后端：跳过铸币（离线测试路径） ----------
IS_LOCAL=0
_prev=""
for _a in "$@"; do
  if [[ "$_prev" == "--backend" && "$_a" == "local" ]]; then IS_LOCAL=1; fi
  _prev="$_a"
done

if [[ $IS_LOCAL -eq 0 && -z "${ARBITER_TOKEN:-}" && -z "${GH_TOKEN:-}" && -z "${GITHUB_TOKEN:-}" ]]; then
  # 铸 App 令牌（REPO=arbiter 单仓作用域——租约 refs 宿主仓）
  TOKEN_SCRIPT="${GH_APP_TOKEN_SCRIPT:-}"
  if [[ -z "$TOKEN_SCRIPT" ]]; then
    _hit=$(command -v gh-app-token.sh 2>/dev/null || true)
    [[ -n "$_hit" ]] && TOKEN_SCRIPT="$_hit"
  fi
  if [[ -z "${CB_APP_ID:-}" ]]; then
    echo "错误：铸币需要 CB_APP_ID（或直接设 ARBITER_TOKEN/GH_TOKEN 透传令牌）" >&2
    exit 2
  fi
  if [[ -z "$TOKEN_SCRIPT" ]]; then
    echo "错误：未找到 gh-app-token.sh（设 GH_APP_TOKEN_SCRIPT 指向 .github 仓 scripts/gh-app-token.sh）" >&2
    exit 2
  fi
  if ! ARBITER_TOKEN=$(REPO=arbiter CB_APP_ID="$CB_APP_ID" bash "$TOKEN_SCRIPT"); then
    echo "错误：App 令牌铸造失败（fail-closed，不降级继续）" >&2
    exit 2
  fi
  export ARBITER_TOKEN
fi

# ---------- 调 cli，透传退出码与 JSON（stdout 不加任何包装） ----------
# MSYS 路径转换豁免（同 .github 仓 ghcb #159 修法）：Git Bash 会把以 / 开头的
# 参数改写成 Windows 路径（实测 "/claim"→"D:/development/Git/claim"，策略表
# 白名单不认）。只豁免命令 token（分号列表），不禁用整条转换——其余参数
# （如 --local-repo /tmp/...）仍需要正常的 MSYS→Win 路径转换；
# 非 MSYS 环境该 env 为无害空设。
MSYS2_ARG_CONV_EXCL='/claim;/release;/retry' \
  exec "$PY_BIN" -m arbiter.cli "$@"
