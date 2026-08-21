"""策略表加载与判定（arbiter 唯一授权真源 = capabilities.yaml）。

设计约束（ADR-0054 §3/§4）：
- 零第三方依赖：本文件用受限子集解析器加载 capabilities.yaml（不支持
  anchors/多行字符串/标签等——任何超出子集的构造都报错，fail-closed）。
- 严格校验：未知键、未知值、类型不符 → PolicyError，拒绝启动。
- 默认拒绝：无匹配规则 = deny（check 返回 not allowed）。

本模块是纯函数模块：不得 import 任何网络模块（tests/test_no_llm.py 断言）。
"""

from __future__ import annotations

import re

SCHEMA_VERSION = 1

# 顶层合法键（未知键拒绝——防策略表悄悄漂移）
_TOP_KEYS = {"schema_version", "defaults", "commands"}
# defaults 子键
_DEFAULTS_KEYS = {"ttl_minutes"}
# 每条命令规则的合法子键
_RULE_KEYS = {"allowed_roles", "required_state", "require_holder"}
# sender_role 取值域（与 conductor 的角色判定对齐，ADR-0049）
KNOWN_ROLES = {"owner", "agent", "none"}

_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-\.]*$|^/[A-Za-z0-9_\-]+$")


class PolicyError(Exception):
    """策略表非法（加载失败）——fail-closed：调用方必须按 infra 处理，不得降级为 deny。"""


# ---------------------------------------------------------------------------
# 受限 YAML 子集解析（无 PyYAML——零依赖约束，ADR-0054 §3）
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    """去掉行内注释：仅识别' # '形态（引号外）。保守起见引号字符串中含 # 时不去除。"""
    out = []
    in_quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
            out.append(ch)
        elif ch in "\"'":
            in_quote = ch
            out.append(ch)
        elif ch == "#" and (not out or out[-1] in (" ", "\t")):
            break  # 行内注释起点，其后全部丢弃
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _parse_scalar(token: str, where: str):
    token = token.strip()
    if token == "":
        raise PolicyError(f"{where}: 值为空")
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if inner == "":
            return []
        return [_parse_scalar(t, where) for t in _split_list(inner, where)]
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        if len(token) < 2:
            raise PolicyError(f"{where}: 引号未闭合")
        return token[1:-1]
    if token == "true":
        return True
    if token == "false":
        return False
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_\-\./]*", token):
        return token
    raise PolicyError(f"{where}: 不支持的值形态 {token!r}（受限子集：标量/布尔/整数/内联列表）")


def _split_list(inner: str, where: str) -> list:
    parts, buf, in_quote = [], [], None
    for ch in inner:
        if in_quote:
            buf.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in "\"'":
            in_quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if in_quote:
        raise PolicyError(f"{where}: 列表内引号未闭合")
    if "".join(buf).strip() or parts:
        parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def parse_mini_yaml(text: str) -> dict:
    """解析受限 YAML 子集 → 嵌套 dict。任何超集构造都抛 PolicyError。"""
    root: dict = {}
    # 栈元素：(缩进, 容器)；根缩进 -1
    stack = [(-1, root)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise PolicyError(f"line {lineno}: 禁用 Tab 缩进（用空格）")
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if line.strip().startswith("#"):  # 整行注释
            continue
        stripped = line.strip()
        if ":" not in stripped:
            raise PolicyError(f"line {lineno}: 缺少 key: 形态 → {stripped!r}")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2 != 0:
            raise PolicyError(f"line {lineno}: 缩进必须是 2 的倍数（当前 {indent}）")
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise PolicyError(f"line {lineno}: 非法键名 {key!r}")
        # 弹栈到父容器（缩进严格小于当前的最近一层）
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent_indent, parent = stack[-1]
        if indent == 0:
            if len(stack) != 1:
                raise PolicyError(f"line {lineno}: 顶层缩进必须为 0")
        elif parent_indent != indent - 2:
            raise PolicyError(
                f"line {lineno}: 缩进跳级（父层 {parent_indent} → 当前 {indent}，应逐级 2 空格）"
            )
        if key in parent:
            raise PolicyError(f"line {lineno}: 重复键 {key!r}")
        if value.strip() == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value, f"line {lineno}")
    return root


# ---------------------------------------------------------------------------
# 策略模型
# ---------------------------------------------------------------------------

class Policy:
    """裁决用策略表。只读快照——运行期不可变。"""

    def __init__(self, ttl_minutes: int, commands: dict):
        self.ttl_minutes = ttl_minutes
        self._commands = commands  # {"/claim": {"allowed_roles": [...], ...}}

    @property
    def command_names(self) -> set:
        return set(self._commands)

    def rule(self, command: str) -> dict | None:
        return self._commands.get(command)

    def check(self, command: str, sender_role: str, current_state: str) -> tuple:
        """静态授权判定（纯函数）。

        返回 (allowed, code, reason)。默认拒绝：命令不在表内 = deny。
        """
        if sender_role not in KNOWN_ROLES:
            return False, "unknown-role", f"未知角色 {sender_role!r}（取值域 {sorted(KNOWN_ROLES)}）"
        rule = self._commands.get(command)
        if rule is None:
            # 默认拒绝（ADR-0054 §4）：无匹配规则不是错误，就是拒绝
            return False, "unknown-command", f"命令 {command!r} 不在策略表（默认拒绝）"
        if sender_role not in rule["allowed_roles"]:
            return False, "role-not-allowed", (
                f"角色 {sender_role!r} 不在 {command} 的 allowed_roles"
                f" {rule['allowed_roles']}（越权拒绝）"
            )
        required_state = rule.get("required_state")
        if required_state is not None and current_state != required_state:
            return False, "state-not-allowed", (
                f"卡当前状态 {current_state!r} != required_state {required_state!r}"
            )
        return True, "policy-ok", "策略表静态校验通过"

    # -- 加载（严格校验，未知键拒绝） --------------------------------------

    @classmethod
    def from_text(cls, text: str) -> "Policy":
        data = parse_mini_yaml(text)
        unknown_top = set(data) - _TOP_KEYS
        if unknown_top:
            raise PolicyError(f"未知顶层键 {sorted(unknown_top)}（合法：{sorted(_TOP_KEYS)}）")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise PolicyError(f"schema_version 必须为 {SCHEMA_VERSION}，实际 {data.get('schema_version')!r}")
        defaults = data.get("defaults")
        if not isinstance(defaults, dict) or not defaults:
            raise PolicyError("缺少 defaults 段")
        unknown_def = set(defaults) - _DEFAULTS_KEYS
        if unknown_def:
            raise PolicyError(f"defaults 段未知键 {sorted(unknown_def)}")
        ttl = defaults.get("ttl_minutes")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise PolicyError(f"defaults.ttl_minutes 必须为正整数，实际 {ttl!r}")
        commands = data.get("commands")
        if not isinstance(commands, dict) or not commands:
            raise PolicyError("缺少 commands 段（至少一条命令规则）")
        for name, rule in commands.items():
            if not isinstance(name, str) or not name.startswith("/"):
                raise PolicyError(f"命令名必须以 / 开头，实际 {name!r}")
            if not isinstance(rule, dict) or not rule:
                raise PolicyError(f"{name} 规则必须为非空映射")
            unknown = set(rule) - _RULE_KEYS
            if unknown:
                raise PolicyError(f"{name} 规则未知键 {sorted(unknown)}（合法：{sorted(_RULE_KEYS)}）")
            roles = rule.get("allowed_roles")
            if not isinstance(roles, list) or not roles:
                raise PolicyError(f"{name}.allowed_roles 必须为非空列表")
            for r in roles:
                if not isinstance(r, str) or r not in KNOWN_ROLES:
                    raise PolicyError(f"{name}.allowed_roles 含未知角色 {r!r}（取值域 {sorted(KNOWN_ROLES)}）")
            if "required_state" in rule:
                st = rule["required_state"]
                if not isinstance(st, str) or not st:
                    raise PolicyError(f"{name}.required_state 必须为非空字符串")
            if "require_holder" in rule:
                rh = rule["require_holder"]
                if not isinstance(rh, bool):
                    raise PolicyError(f"{name}.require_holder 必须为布尔值，实际 {rh!r}")
        return cls(ttl, commands)

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path, encoding="utf-8") as f:
            return cls.from_text(f.read())


if __name__ == "__main__":  # CI policy-validate job 入口
    import sys

    try:
        pol = Policy.load(sys.argv[1] if len(sys.argv) > 1 else "capabilities.yaml")
    except (OSError, PolicyError) as exc:
        print(f"POLICY-INVALID: {exc}")
        raise SystemExit(1)
    ok, code, _ = pol.check("/__nonexistent__", "owner", "ready")
    assert not ok and code == "unknown-command", "默认拒绝路径失效"
    print(
        f"POLICY-OK commands={sorted(pol.command_names)} "
        f"ttl_minutes={pol.ttl_minutes} default-deny=verified"
    )
