"""给 relay 打补丁：市值分档门槛（2026-08 新规则）。

1. report_parser.py：注入资金门槛时按反推市值分档（1000 亿以上 0.4%×市值）
2. prompts/system_prompt.md：策略背景改为大市值通道口径

幂等：重复执行不会重复插入。
"""

from __future__ import annotations

import pathlib

PARSER_PATH = pathlib.Path("/opt/ptrade-agent-relay/ptrade_agent_relay/report_parser.py")
PROMPT_PATH = pathlib.Path("/opt/ptrade-agent-relay/prompts/system_prompt.md")

PARSER_FUNCTION = '''

def tiered_flow_threshold_wanyuan(
    realtime_formula_wanyuan: Decimal | None,
    realtime_formula_ratio_pct: Decimal | None,
) -> Decimal | None:
    """按反推市值分档估算真实资金门槛（单位万元），与上游 PTrade 2026-08 口径一致。

    邮件不携带市值，用「资金公式 / 占比 / 100」反推流通市值（亿元）：
    - 市值 > 1020 亿：门槛 = 市值 x 40（即 0.4% x 市值，无封顶）
    - 其余：返回 None，由调用方回退 4000 万参考常量。

    占比列只保留两位小数，1000 亿边界附近反推误差约 2%；落在模糊窗口内
    时回退参考常量，宁可"超额"偏乐观，也不把达标票显示成"低于门槛"。
    """
    if not realtime_formula_wanyuan or not realtime_formula_ratio_pct:
        return None
    if realtime_formula_wanyuan <= 0 or realtime_formula_ratio_pct <= 0:
        return None
    cap_yi = realtime_formula_wanyuan / (realtime_formula_ratio_pct * Decimal("100"))
    if cap_yi <= Decimal("1020"):
        return None
    return (cap_yi * Decimal("40")).quantize(Decimal("1"))
'''

OLD_INJECT = '''    if "small_net_wanyuan" in raw and "flow_threshold_wanyuan" not in raw:
        values["flow_threshold_wanyuan"] = _strategy_flow_threshold()'''

NEW_INJECT = '''    if "small_net_wanyuan" in raw and "flow_threshold_wanyuan" not in raw:
        values["flow_threshold_wanyuan"] = (
            tiered_flow_threshold_wanyuan(
                values.get("realtime_formula_wanyuan"),
                values.get("realtime_formula_ratio_pct"),
            )
            or _strategy_flow_threshold()
        )'''

PROMPT_RULE_1_OLD = "1. L1 过滤：流通市值约 20亿~1000亿；营收同比、资产负债率为空时默认不拦截。"
PROMPT_RULE_1_NEW = "1. L1 过滤：流通市值 20 亿以上（无上限）；营收同比、资产负债率为空时默认不拦截。市值 1000 亿以上的标的走“大市值通道”，资金公式必须达到流通市值的 0.4% 才能通过资金条件。"

PROMPT_RULE_6_OLD = """6. 实时资金门槛：
   - 资金门槛 = min(4000万, max(最低金额门槛, 市值比例门槛))；
   - 50亿以下最低门槛约2000万；50亿到200亿约4000万；200亿以上最高也只卡到4000万。"""
PROMPT_RULE_6_NEW = """6. 实时资金门槛（按流通市值分档）：
   - 1000亿以上：资金门槛 = 流通市值 x 0.4%，不套用固定金额门槛或 4000 万封顶（1000 亿处即 4 亿元，逐票不同；下游按 资金公式/占比 反推市值计算注入）；
   - 1000亿及以下：资金门槛 = min(4000万, max(最低金额门槛, 市值比例门槛))；50亿以下最低门槛约2000万；50亿到200亿约4000万；200亿以上最高也只卡到4000万。"""


def patch_parser() -> None:
    text = PARSER_PATH.read_text(encoding="utf-8")
    changed = False
    if "tiered_flow_threshold_wanyuan" not in text:
        anchor = "SECTION_RE = re.compile("
        index = text.index(anchor)
        text = text[:index] + PARSER_FUNCTION.lstrip("\n") + "\n\n" + text[index:]
        changed = True
    if OLD_INJECT in text:
        text = text.replace(OLD_INJECT, NEW_INJECT)
        changed = True
    PARSER_PATH.write_text(text, encoding="utf-8")
    print("report_parser.py:", "已更新" if changed else "无需改动")


def patch_prompt() -> None:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    changed = False
    if PROMPT_RULE_1_OLD in text:
        text = text.replace(PROMPT_RULE_1_OLD, PROMPT_RULE_1_NEW)
        changed = True
    if PROMPT_RULE_6_OLD in text:
        text = text.replace(PROMPT_RULE_6_OLD, PROMPT_RULE_6_NEW)
        changed = True
    PROMPT_PATH.write_text(text, encoding="utf-8")
    print("system_prompt.md:", "已更新" if changed else "无需改动")


if __name__ == "__main__":
    patch_parser()
    patch_prompt()
