# arbiter —— Cloudbird-Software 仲裁内核（宪法 §1 / ADR-0054）
#
# 不变量：无 LLM（授权决策零 LLM）、默认拒绝、fail-closed、自带测试。
# kernel/policy/lease 三个模块不得 import 任何网络模块（tests/test_no_llm.py 静态断言）。
__version__ = "1.0.0"
