from __future__ import annotations

import hashlib
import os
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from .models import ParsedReport, ReportStock, SourcePool


NUMERIC_FIELDS = {
    "pct20",
    "market_cap_yi",
    "turnover_now_pct",
    "vol_ratio",
    "super_net_wanyuan",
    "large_net_wanyuan",
    "medium_net_wanyuan",
    "small_net_wanyuan",
    "main_net_wanyuan",
    "realtime_formula_wanyuan",
    "realtime_formula_ratio_pct",
    "flow_threshold_wanyuan",
    "buy_volume",
    "sell_volume",
    "close_pos_in_range",
}
BOOLEAN_FIELDS = {"l4_buy_sell", "super_large_anomaly", "intraday_strong_ok"}
INTEGER_FIELDS = {"pass_count"}
KNOWN_FIELDS = {
    "symbol",
    "code",
    "name",
    "reason",
    "unmet_items",
    *NUMERIC_FIELDS,
    *BOOLEAN_FIELDS,
    *INTEGER_FIELDS,
}
REQUIRED_FIELDS = {
    "symbol",
    "reason",
    "realtime_formula_wanyuan",
    "flow_threshold_wanyuan",
    "vol_ratio",
    "turnover_now_pct",
    "l4_buy_sell",
}
EMPTY_VALUES = {"", "-", "--", "none", "null", "nan", "n/a"}
VALID_CODE_PREFIXES = ("0", "3", "4", "6", "8", "9")
REASON_VALUES = {"all_conditions_met", "near_miss", "abnormal", "selected", "candidate"}
# 资金门槛是策略固定参数：精简邮件不再随行下发，由解析器按常量注入（单位万元）。
# 环境变量 PTRADE_FLOW_THRESHOLD_WANYUAN 可覆盖，例如上调门槛时无需改上游与消息格式。
def _strategy_flow_threshold() -> Decimal:
    raw = os.environ.get("PTRADE_FLOW_THRESHOLD_WANYUAN", "4000")
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation:
        return Decimal("4000")


# 市值分档门槛（与上游 PTrade 2026-08 版口径一致）：
# - 1000 亿以上：资金公式 ≥ 流通市值 × 0.4%（无固定金额封顶，1000 亿处即 4 亿元）
# - 1000 亿及以下：沿用原门槛（50 亿以上档实际生效值恒为 4000 万封顶）
LARGE_CAP_BOUNDARY_YI = Decimal("1000")
LARGE_CAP_FLOW_RATIO_PCT = Decimal("0.4")
# 邮件占比只保留两位小数，反推市值在 1000 亿边界附近有约 2% 误差。
# 落在这个窗口内时按左档（4000 万参考口径）处理：宁可"高出"偏乐观，
# 也不把一只真实达标的票显示成"低于门槛"。
CAP_ESTIMATE_AMBIGUITY_YI = Decimal("20")


def tiered_flow_threshold_wanyuan(
    realtime_formula_wanyuan: Decimal | None,
    realtime_formula_ratio_pct: Decimal | None,
) -> Decimal | None:
    """按反推市值分档估算该标的的真实资金门槛（单位万元）。

    邮件不携带市值，用「资金公式 ÷ 占比 × 100」反推流通市值（亿元）。
    反推在 1000 亿边界 ±20 亿的模糊窗口内返回 None，由调用方回退到
    4000 万参考口径；窗口之外：
    - 市值 > 1000 亿 → 0.4% × 市值（每只票门槛不同）
    - 市值 ≤ 1000 亿 → None（沿用注入常量，即原门槛口径）
    """
    if not realtime_formula_wanyuan or not realtime_formula_ratio_pct:
        return None
    if realtime_formula_wanyuan <= 0 or realtime_formula_ratio_pct <= 0:
        return None
    cap_yi = realtime_formula_wanyuan / (realtime_formula_ratio_pct * Decimal("100"))
    if cap_yi <= LARGE_CAP_BOUNDARY_YI + CAP_ESTIMATE_AMBIGUITY_YI:
        return None
    # 门槛 = 市值(亿元) × 10000 × 0.4% = 市值 × 40（万元），取整到万元。
    # 不取整的话 Decimal 除法会带长尾数，str() 会以科学计数法呈现。
    return (cap_yi * Decimal("40")).quantize(Decimal("1"))
# 邮件页脚（以及被截断后与页脚粘连的尾部残片）不能进入区段内容。
# 页脚本身不是数据行；粘连行既可能缺列，也可能把 "14:16:46}" 这类
# 页脚残片误对齐进 reason 等列，直接丢弃整行、保留前面的完整行。
MAIL_FOOTER_RE = re.compile(r"邮件发送时间|sent_at", re.I)
# PTrade 终端日志行的前缀，如 "2026-08-20 13:06:09 - INFO - "；
# 用户直接复制运行日志粘贴时，区段标记和数据行会带上这类前缀，需先剥离。
LOG_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s*-\s*(?:INFO|WARNING|ERROR|DEBUG|CRITICAL)\s*-\s*",
    re.I,
)


def parse_ptrade_report(raw_text: str) -> ParsedReport:
    raw_text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    if not raw_text:
        return ParsedReport(
            content_hash=content_hash,
            raw_text=raw_text,
            parse_status="invalid",
            parse_errors=["报告原文为空"],
        )

    metadata = _extract_metadata(raw_text)
    sections, section_errors = _extract_sections(raw_text)
    diagnostics: list[str] = []
    parse_errors = list(section_errors)
    selected_rows, selected_nonempty = _parse_section(
        sections.get("selected", []), "selected", diagnostics, parse_errors
    )
    near_rows, near_nonempty = _parse_section(
        sections.get("near", []), "near", diagnostics, parse_errors
    )

    # 邮件可能在两个区段之间被截断（例如 selected_head 之后直接跟页脚）。
    # 缺失的区段按空池处理并记为诊断，保留已解析完整的行；
    # 只有完全没有任何区段标记时才 fail closed。
    for pool, marker in (("selected", "selected_head"), ("near", "near_head")):
        if pool not in sections:
            diagnostics.append(f"缺少 {marker} 区段（可能被邮件截断）")
    if "selected" not in sections and "near" not in sections:
        parse_errors.append("报告必须包含 selected_head 或 near_head 区段")

    selected_incomplete = [row for row in selected_rows if row.missing_fields]
    near_incomplete = [row for row in near_rows if row.missing_fields]
    if selected_incomplete:
        missing = sorted({item for row in selected_incomplete for item in row.missing_fields})
        parse_errors.append("selected 行缺少核心字段: " + ", ".join(missing))
    if near_incomplete:
        missing = sorted({item for row in near_incomplete for item in row.missing_fields})
        diagnostics.append("near 行缺少核心字段: " + ", ".join(missing))

    if (selected_nonempty and not selected_rows) or (near_nonempty and not near_rows):
        parse_errors.append("非空表格未解析出任何有效股票行")

    if parse_errors:
        status = "invalid"
    elif diagnostics:
        status = "partial"
    else:
        status = "valid"

    return ParsedReport(
        content_hash=content_hash,
        raw_text=raw_text,
        report_date=metadata["report_date"],
        generated_at=metadata["generated_at"],
        run_slot=metadata["run_slot"],
        parse_status=status,
        selected_rows=selected_rows,
        near_rows=near_rows,
        diagnostics=_dedupe(diagnostics),
        parse_errors=_dedupe(parse_errors),
    )


def _extract_metadata(raw_text: str) -> dict[str, str]:
    generated_match = re.search(
        r"(?:生成时间|邮件发送时间|generated_at|sent_at)\s*[:：]\s*([^\n]+)",
        raw_text,
        re.I,
    )
    run_match = re.search(r"(?:运行轮次|run_slot)\s*[:：]\s*([^\s\n]+)", raw_text, re.I)
    generated_at = ""
    report_date = ""
    if generated_match:
        raw_generated_at = generated_match.group(1).strip()
        timestamp_match = re.search(
            r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?",
            raw_generated_at,
        )
        generated_at = timestamp_match.group(0) if timestamp_match else raw_generated_at.strip("{} ")
        report_date = generated_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", generated_at) else ""
    return {
        "generated_at": generated_at,
        "report_date": report_date,
        "run_slot": run_match.group(1).strip() if run_match else "",
    }


def _extract_sections(raw_text: str) -> tuple[dict[str, list[str]], list[str]]:
    lines = raw_text.splitlines()
    markers: list[tuple[int, str, str]] = []
    # 回避池/abnormal 等区段已从精简消息中移除；若旧邮件仍有残留，
    # 识别为区段边界并丢弃其内容，避免被误读为 near 数据行。
    # 兼容两种来源：邮件正文（行首即标记）与 PTrade 终端日志（带时间戳前缀，
    # 分隔符可能是 "="，如 "2026-08-20 13:06:09 - INFO - selected_head="）。
    pattern = re.compile(
        r"^\s*\{?\s*(selected_head|near_head|abnormal_head|avoid_head|回避池)\s*[:：=]\s*(.*?)\s*\}?\s*$",
        re.I,
    )
    for index, line in enumerate(lines):
        stripped = LOG_PREFIX_RE.sub("", line)
        match = pattern.match(stripped)
        if not match:
            continue
        marker = match.group(1).lower()
        if marker.startswith("selected"):
            section = "selected"
        elif marker.startswith("near"):
            section = "near"
        else:
            section = "ignore"
        markers.append((index, section, match.group(2)))

    sections: dict[str, list[str]] = {}
    errors: list[str] = []
    for marker_index, (line_index, section, inline) in enumerate(markers):
        if section == "ignore":
            continue
        if section in sections:
            errors.append(f"重复的 {section}_head 区段")
            continue
        next_index = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(lines)
        content: list[str] = []
        if inline and inline.lower() not in {"empty", "[]", "none"}:
            content.append(inline)
        elif inline.lower() in {"empty", "[]", "none"}:
            sections[section] = []
            continue
        content.extend(
            line.strip()
            for line in lines[line_index + 1 : next_index]
            if line.strip() and not MAIL_FOOTER_RE.search(line) and not LOG_PREFIX_RE.match(line)
        )
        # 上游空池可能打印成区段名单独一行、下一行 "empty"；同样视为空池。
        if len(content) == 1 and content[0].lower() in {"empty", "[]", "none"}:
            sections[section] = []
            continue
        sections[section] = content
    return sections, errors


def _parse_section(
    lines: list[str],
    source_pool: SourcePool,
    diagnostics: list[str],
    errors: list[str],
) -> tuple[list[ReportStock], bool]:
    if not lines:
        return [], False
    if len(lines) < 2:
        errors.append(f"{source_pool}_head 非空但缺少数据行")
        return [], True

    headers = re.split(r"\s+", lines[0].strip())
    if "symbol" not in headers:
        errors.append(f"{source_pool}_head 表头缺少 symbol")
        return [], True

    rows: list[ReportStock] = []
    for row_number, line in enumerate(lines[1:], start=1):
        tokens = re.split(r"\s+", line.strip())
        aligned = _align_columns(headers, tokens)
        symbol = _normalize_symbol(str(aligned.get("symbol") or ""))
        if not symbol:
            diagnostics.append(f"{source_pool} 第 {row_number} 行股票代码无效，已跳过")
            continue
        raw_values = {key: value for key, value in aligned.items()}
        parsed: dict[str, Any] = {
            "symbol": symbol,
            "code": symbol[:6],
            "source_pool": source_pool,
            "reason": str(aligned.get("reason") or ""),
            "raw_row": raw_values,
        }
        for field in NUMERIC_FIELDS:
            parsed[field] = _decimal(aligned.get(field))
        for field in BOOLEAN_FIELDS:
            parsed[field] = _boolean(aligned.get(field))
        for field in INTEGER_FIELDS:
            parsed[field] = _integer(aligned.get(field))
        parsed["name"] = str(aligned.get("name") or "")
        unmet = str(aligned.get("unmet_items") or "")
        parsed["unmet_items"] = [item for item in unmet.split(",") if item and item.lower() not in EMPTY_VALUES]
        parsed["unknown_fields"] = {
            key: value for key, value in aligned.items() if key not in KNOWN_FIELDS and value not in EMPTY_VALUES
        }
        # 精简格式不再下发 reason 与 flow_threshold_wanyuan 两列：
        # 以 small_net_wanyuan 列作为精简格式标识，此时才按区段默认 reason、
        # 注入资金门槛；旧格式缺列仍按缺失处理（fail closed）。
        # 门槛按市值分档（2026-08 起）：1000 亿以上为流通市值×0.4%（逐票
        # 不同，用 资金公式÷占比 反推市值计算）；其余沿用策略常量。
        if "small_net_wanyuan" in headers:
            if "reason" not in headers:
                parsed["reason"] = "all_conditions_met" if source_pool == "selected" else "near_miss"
            if "flow_threshold_wanyuan" not in headers:
                parsed["flow_threshold_wanyuan"] = (
                    tiered_flow_threshold_wanyuan(
                        parsed.get("realtime_formula_wanyuan"),
                        parsed.get("realtime_formula_ratio_pct"),
                    )
                    or _strategy_flow_threshold()
                )
        parsed["missing_fields"] = sorted(
            field for field in REQUIRED_FIELDS if _missing_required(field, parsed.get(field))
        )
        rows.append(ReportStock.model_validate(parsed))
    return rows, True


def _align_columns(headers: list[str], tokens: list[str]) -> dict[str, str]:
    """Align whitespace tables while tolerating blank Pandas cells.

    A small dynamic program prefers values whose types match the destination
    column and may leave headers blank. This prevents a missing middle cell from
    shifting every later value into the wrong field.
    """

    @lru_cache(maxsize=None)
    def solve(i: int, j: int) -> tuple[float, tuple[tuple[str, str], ...]]:
        if i == len(headers):
            return (-1000.0 * (len(tokens) - j), ())
        if j == len(tokens):
            return (-0.35 * (len(headers) - i), tuple((header, "") for header in headers[i:]))

        header = headers[i]
        assign_score, assign_tail = solve(i + 1, j + 1)
        assign_score += _compatibility(header, tokens[j])
        skip_score, skip_tail = solve(i + 1, j)
        skip_score -= 0.35
        if assign_score >= skip_score:
            return assign_score, ((header, tokens[j]), *assign_tail)
        return skip_score, ((header, ""), *skip_tail)

    _score, pairs = solve(0, 0)
    result: dict[str, str] = {}
    duplicate_count: dict[str, int] = {}
    for header, value in pairs:
        if header in result:
            duplicate_count[header] = duplicate_count.get(header, 1) + 1
            result[f"{header}__duplicate_{duplicate_count[header]}"] = value
        else:
            result[header] = value
    return result


def _compatibility(header: str, token: str) -> float:
    if header == "symbol":
        return 9.0 if _normalize_symbol(token) else -20.0
    if header in NUMERIC_FIELDS:
        return 5.0 if _decimal(token) is not None or token.lower() in EMPTY_VALUES else -6.0
    if header in BOOLEAN_FIELDS:
        return 5.0 if _boolean(token) is not None or token.lower() in EMPTY_VALUES else -6.0
    if header in INTEGER_FIELDS:
        return 4.0 if _integer(token) is not None or token.lower() in EMPTY_VALUES else -4.0
    if header == "reason":
        return 8.0 if token.lower() in REASON_VALUES else 0.5
    if header == "unmet_items":
        return 3.0 if "," in token or token.startswith("l4_") else 0.5
    return 1.0


def _normalize_symbol(value: str) -> str:
    match = re.fullmatch(r"(\d{6})(?:\.(SH|SS|SZ|BJ))?", value.strip().upper())
    if not match or not match.group(1).startswith(VALID_CODE_PREFIXES):
        return ""
    suffix = match.group(2)
    return match.group(1) + (f".{suffix}" if suffix else "")


def _decimal(value: Any) -> Decimal | None:
    text = str(value or "").strip().lower()
    if text in EMPTY_VALUES:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _boolean(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _integer(value: Any) -> int | None:
    number = _decimal(value)
    return int(number) if number is not None and number == number.to_integral_value() else None


def _missing_required(field: str, value: Any) -> bool:
    if field in {"symbol", "reason"}:
        return not str(value or "").strip()
    return value is None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
