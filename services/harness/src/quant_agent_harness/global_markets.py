from __future__ import annotations

import ast
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from time import sleep
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


INDEX_SPECS = (
    {"ticker": "^GSPC", "name": "标普 500", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "^IXIC", "name": "纳斯达克综合", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "^DJI", "name": "道琼斯工业", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "^KS11", "name": "KOSPI", "region": "韩国", "currency": "KRW", "timezone": "Asia/Seoul"},
    {"ticker": "^N225", "name": "日经 225", "region": "日本", "currency": "JPY", "timezone": "Asia/Tokyo"},
)

A_SHARE_ZONE = ZoneInfo("Asia/Shanghai")
USER_AGENT = "Mozilla/5.0 QuantAgent/0.1"

# Yahoo Finance 对国内网络常返回 HTTP 403（地域/同意拦截），因此每个指数
# 配置备用行情源：美股走腾讯证券，韩国与日本走东方财富，日经再备新浪期货
# 日线。任一主源可用时仍优先使用 Yahoo，备用源只在主源失败时接管。
TENCENT_TICKERS = {"^GSPC": "usINX", "^IXIC": "usIXIC", "^DJI": "usDJI"}
EASTMONEY_SECIDS = {"^KS11": "100.KS11", "^N225": "100.N225"}
SINA_SYMBOLS = {"^N225": "NK"}

# 美股 SPDR 行业 ETF：事件规则（外围极端波动）触发外围板块资金 Agent 时，
# 用它们定位异常板块；腾讯美股行情作为 Yahoo 的备用源。
SECTOR_ETF_SPECS = (
    {"ticker": "XLF", "name": "金融", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLK", "name": "科技", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLE", "name": "能源", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLV", "name": "医疗保健", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLY", "name": "可选消费", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLP", "name": "必选消费", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLI", "name": "工业", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLB", "name": "材料", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLRE", "name": "房地产", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
    {"ticker": "XLU", "name": "公用事业", "region": "美国", "currency": "USD", "timezone": "America/New_York"},
)
SECTOR_TENCENT_TICKERS = {spec["ticker"]: "us" + spec["ticker"] for spec in SECTOR_ETF_SPECS}
TENCENT_TICKERS = {**TENCENT_TICKERS, **SECTOR_TENCENT_TICKERS}
# 东财 WAF 对 100.KS11 的 K 线接口会稳定重置连接，因此 KOSPI 需要第三
# 备用源：Naver 金融日线 JSON，免 key，返回完整日线历史。
NAVER_SYMBOLS = {"^KS11": "KOSPI"}

_PROVIDER_LABELS = {
    "yahoo": "Yahoo Finance",
    "tencent": "腾讯证券",
    "eastmoney": "东方财富",
    "sina": "新浪财经",
    "naver": "Naver 金融",
}

# 东财对突发/连续请求会直接重置连接，第三次尝试前留出间隔可显著提高成功率。
_EASTMONEY_RETRY_DELAY = 1.5


def _open(url: str, timeout: int, *, decode: str = "utf-8", headers: dict[str, str] | None = None) -> str:
    request = urllib.request.Request(
        url,
        headers={**{"User-Agent": USER_AGENT, "Accept": "application/json"}, **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode(decode, errors="replace")


def _cutoff_date(spec: dict[str, str], a_share_report_date: date) -> date:
    return (
        a_share_report_date - timedelta(days=1)
        if spec["region"] == "美国"
        else a_share_report_date
    )


def _fetch_yahoo(spec: dict[str, str], a_share_report_date: date, timeout: int) -> dict[str, Any]:
    ticker = urllib.parse.quote(spec["ticker"], safe="")
    period1 = int(
        datetime.combine(a_share_report_date - timedelta(days=16), time.min, timezone.utc).timestamp()
    )
    period2 = int(
        datetime.combine(a_share_report_date + timedelta(days=2), time.min, timezone.utc).timestamp()
    )
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker
        + f"?period1={period1}&period2={period2}&interval=1d&events=history"
    )
    payload = json.loads(_open(url, timeout))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise RuntimeError(
            str(((payload.get("chart") or {}).get("error") or {}).get("description") or "无行情结果")
        )
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quotes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    timezone_name = str(meta.get("exchangeTimezoneName") or "UTC")
    try:
        market_zone = ZoneInfo(timezone_name)
    except Exception:
        market_zone = ZoneInfo("UTC")
        timezone_name = "UTC"
    history: list[dict[str, Any]] = []
    for timestamp, close in zip(timestamps, quotes, strict=False):
        if close is None:
            continue
        history.append(
            {
                "date": datetime.fromtimestamp(int(timestamp), market_zone).date().isoformat(),
                "close": round(float(close), 2),
            }
        )
    history.sort(key=lambda item: str(item["date"]))
    if len(history) < 2:
        raise RuntimeError("有效收盘价不足两日")
    latest, previous = history[-1], history[-2]
    return {
        "trade_date": str(latest["date"]),
        "close": float(latest["close"]),
        "previous_close": float(previous["close"]),
        "history": history,
        "currency": str(meta.get("currency") or spec["currency"]),
        "source_url": "https://finance.yahoo.com/quote/" + ticker + "/",
    }


def _fetch_tencent(spec: dict[str, str], a_share_report_date: date, timeout: int) -> dict[str, Any]:
    ticker = TENCENT_TICKERS[spec["ticker"]]
    quote_text = _open(f"https://qt.gtimg.cn/q={ticker}", timeout, decode="gbk")
    match = re.search(r'="(.*)"', quote_text)
    if not match or not match.group(1):
        raise RuntimeError("腾讯行情返回为空")
    fields = match.group(1).split("~")
    if len(fields) < 36 or not fields[3] or not fields[4]:
        raise RuntimeError("腾讯行情字段不完整")
    raw: dict[str, Any] = {
        "trade_date": str(fields[30])[:10] or None,
        "close": float(fields[3]),
        "previous_close": float(fields[4]),
        "change": float(fields[31]),
        "change_percent": float(fields[32]),
        "currency": str(fields[35] or spec["currency"]),
        "history": [],
        "source_url": f"https://gu.qq.com/{ticker}/gp",
    }
    kline_text = _open(
        f"https://ifzq.gtimg.cn/appstock/app/usfqkline/get?param={ticker},day,,,8,qfq",
        timeout,
    )
    payload = json.loads(kline_text)
    rows = ((payload.get("data") or {}).get(ticker) or {}).get("day") or []
    history = [
        {"date": str(row[0]), "close": round(float(row[2]), 2)}
        for row in rows
        if len(row) > 2 and row[2]
    ]
    if history:
        raw["history"] = history
        if not raw["trade_date"]:
            raw["trade_date"] = history[-1]["date"]
    return raw


def _fetch_eastmoney(spec: dict[str, str], a_share_report_date: date, timeout: int) -> dict[str, Any]:
    secid = EASTMONEY_SECIDS[spec["ticker"]]
    query = f"?secid={secid}&klt=101&fqt=0&end=20500101&lmt=8&fields1=f1,f2&fields2=f51,f53"
    hosts = ("push2his.eastmoney.com", "1.push2his.eastmoney.com")
    payload: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt, host in enumerate((*hosts, hosts[0])):
        if attempt == 2:
            sleep(_EASTMONEY_RETRY_DELAY)
        url = f"https://{host}/api/qt/stock/kline/get{query}"
        try:
            payload = json.loads(_open(url, timeout))
            break
        except Exception as exc:
            last_error = exc
    if payload is None:
        raise RuntimeError(f"东方财富K线不可用: {_safe_error(last_error) if last_error else '未知错误'}")
    klines = ((payload.get("data") or {}).get("klines")) or []
    history: list[dict[str, Any]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) >= 2 and parts[1]:
            history.append({"date": str(parts[0]), "close": round(float(parts[1]), 2)})
    history.sort(key=lambda item: str(item["date"]))
    if len(history) < 2:
        raise RuntimeError("有效收盘价不足两日")
    latest, previous = history[-1], history[-2]
    return {
        "trade_date": str(latest["date"]),
        "close": float(latest["close"]),
        "previous_close": float(previous["close"]),
        "history": history,
        "currency": str(spec["currency"]),
        "source_url": url,
    }


def _fetch_naver(spec: dict[str, str], a_share_report_date: date, timeout: int) -> dict[str, Any]:
    symbol = NAVER_SYMBOLS[spec["ticker"]]
    start = (a_share_report_date - timedelta(days=16)).strftime("%Y%m%d")
    end = (a_share_report_date + timedelta(days=1)).strftime("%Y%m%d")
    url = (
        "https://api.finance.naver.com/siseJson.naver?symbol=" + symbol
        + f"&requestType=1&startTime={start}&endTime={end}&timeframe=day"
    )
    text = _open(url, timeout, headers={"Referer": "https://finance.naver.com"})
    # Naver 返回 JS 数组语法（单引号字符串），不是严格 JSON，用字面量解析。
    payload = ast.literal_eval(text.replace("null", "None").lstrip())
    if not isinstance(payload, list):
        raise RuntimeError("Naver 行情返回为空")
    history: list[dict[str, Any]] = []
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) < 5:
            continue
        raw_date = str(row[0] or "").strip()
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        try:
            close = float(row[4])
        except (TypeError, ValueError):
            continue
        history.append(
            {
                "date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}",
                "close": round(close, 2),
            }
        )
    history.sort(key=lambda item: str(item["date"]))
    if len(history) < 2:
        raise RuntimeError("有效收盘价不足两日")
    latest, previous = history[-1], history[-2]
    return {
        "trade_date": str(latest["date"]),
        "close": float(latest["close"]),
        "previous_close": float(previous["close"]),
        "history": history,
        "currency": str(spec["currency"]),
        "source_url": "https://finance.naver.com/sise/sise_index.naver?code=" + symbol,
    }


def _fetch_sina(spec: dict[str, str], a_share_report_date: date, timeout: int) -> dict[str, Any]:
    symbol = SINA_SYMBOLS[spec["ticker"]]
    url = (
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        "var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=" + symbol
    )
    text = _open(url, timeout, headers={"Referer": "https://finance.sina.com.cn"})
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        raise RuntimeError("新浪行情返回格式异常")
    rows = json.loads(text[start + 1 : end])
    if not isinstance(rows, list):
        raise RuntimeError("新浪行情返回为空")
    history: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        trade_date = str(row.get("date") or "")
        close = row.get("close")
        if trade_date and close not in (None, "", "0.000"):
            history.append({"date": trade_date, "close": round(float(close), 2)})
    history.sort(key=lambda item: str(item["date"]))
    if len(history) < 2:
        raise RuntimeError("有效收盘价不足两日")
    latest, previous = history[-1], history[-2]
    return {
        "trade_date": str(latest["date"]),
        "close": float(latest["close"]),
        "previous_close": float(previous["close"]),
        "history": history,
        "currency": str(spec["currency"]),
        "source_url": url,
    }


_PROVIDER_FETCHERS = {
    "yahoo": _fetch_yahoo,
    "tencent": _fetch_tencent,
    "eastmoney": _fetch_eastmoney,
    "sina": _fetch_sina,
    "naver": _fetch_naver,
}


def _provider_chain(spec: dict[str, str]) -> tuple[str, ...]:
    chain = ["yahoo"]
    if spec["ticker"] in TENCENT_TICKERS:
        chain.append("tencent")
    if spec["ticker"] in EASTMONEY_SECIDS:
        chain.append("eastmoney")
    if spec["ticker"] in SINA_SYMBOLS:
        chain.append("sina")
    if spec["ticker"] in NAVER_SYMBOLS:
        chain.append("naver")
    return tuple(chain)


def _finalize(
    spec: dict[str, str],
    a_share_report_date: date,
    raw: dict[str, Any],
    provider: str,
) -> dict[str, Any]:
    cutoff = _cutoff_date(spec, a_share_report_date)
    history = sorted(
        (item for item in raw["history"] if str(item["date"]) <= cutoff.isoformat()),
        key=lambda item: str(item["date"]),
    )
    if len(history) >= 2:
        latest, previous = history[-1], history[-2]
        close, previous_close = float(latest["close"]), float(previous["close"])
        trade_date = str(latest["date"])
        change = close - previous_close
        change_percent = change / previous_close * 100 if previous_close else 0.0
    else:
        close = float(raw["close"])
        previous_close = float(raw["previous_close"])
        trade_date = str(raw["trade_date"] or "")
        change = float(raw["change"]) if raw.get("change") is not None else close - previous_close
        change_percent = (
            float(raw["change_percent"])
            if raw.get("change_percent") is not None
            else (change / previous_close * 100 if previous_close else 0.0)
        )
        if not history:
            history = [{"date": trade_date, "close": round(close, 2)}]
    return {
        "ticker": spec["ticker"],
        "name": spec["name"],
        "region": spec["region"],
        "currency": str(raw.get("currency") or spec["currency"]),
        "trade_date": trade_date,
        "timezone": spec["timezone"],
        "close": round(close, 2),
        "previous_close": round(previous_close, 2),
        "change": round(change, 2),
        "change_percent": round(change_percent, 2),
        "history": history[-5:],
        "source_url": str(raw.get("source_url") or ""),
        "provider": provider,
    }


class GlobalMarketClient:
    """Small no-key adapter for delayed public index quotes.

    Primary source is Yahoo Finance; on networks where Yahoo returns 403 the
    client switches to domestic mirror sources (Tencent for US indices,
    Eastmoney for Korea and Japan) before giving up.  The provider is
    deliberately isolated behind this interface so a licensed enterprise feed
    can replace it without changing the Agent workflow or UI.
    """

    def __init__(self, *, timeout_seconds: int = 8, demo_fallback: bool = True):
        self.timeout_seconds = timeout_seconds
        self.demo_fallback = demo_fallback

    def snapshot(self, as_of: str | date | datetime | None = None) -> dict[str, Any]:
        a_share_report_date = _parse_report_date(as_of)
        indices: list[dict[str, Any]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(INDEX_SPECS)) as executor:
            futures = {
                executor.submit(self._fetch_one, spec, a_share_report_date): spec
                for spec in INDEX_SPECS
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    indices.append(future.result())
                except Exception as exc:
                    errors.append(f"{spec['name']}: {_safe_error(exc)}")
        order = {spec["ticker"]: index for index, spec in enumerate(INDEX_SPECS)}
        indices.sort(key=lambda item: order.get(item["ticker"], 999))
        status = "live_delayed"
        notice = (
            f"以 A 股报告日 {a_share_report_date.isoformat()} 为锚点："
            "美股取严格早于报告日的最近交易日，韩国与日本取不晚于报告日的最近交易日。"
        )
        if not indices and self.demo_fallback:
            indices = _demo_indices(a_share_report_date)
            status = "demo_fallback"
            notice += " 行情接口暂时不可用，当前为界面演示数据，不代表真实市场行情。"
        providers = {item["provider"] for item in indices}
        if status == "live_delayed" and providers - {"yahoo"}:
            fallback_labels = "、".join(
                _PROVIDER_LABELS.get(p, p) for p in sorted(providers - {"yahoo"})
            )
            notice += f" 已自动切换备用行情源（{fallback_labels}），数据为延迟行情。"
        quality_flags = [
            f"{item['name']} 单日涨跌 {float(item['change_percent']):+.2f}%，超过 12% 异常阈值，需要第二行情源复核"
            for item in indices
            if abs(float(item.get("change_percent") or 0)) > 12
        ]
        return {
            "market_indices": indices,
            "status": status,
            "provider": _provider_summary(providers) if status == "live_delayed" else "demo sample",
            "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "notice": notice,
            "errors": errors,
            "quality_flags": quality_flags,
            "a_share_report_date": a_share_report_date.isoformat(),
            "session_rule": {
                "美国": "取当地交易日 < A股报告日的最近收盘",
                "韩国": "取当地交易日 <= A股报告日的最近收盘",
                "日本": "取当地交易日 <= A股报告日的最近收盘",
            },
        }

    def _fetch_one(self, spec: dict[str, str], a_share_report_date: date) -> dict[str, Any]:
        failures: list[str] = []
        for provider in _provider_chain(spec):
            try:
                raw = _PROVIDER_FETCHERS[provider](spec, a_share_report_date, self.timeout_seconds)
                return _finalize(spec, a_share_report_date, raw, provider)
            except Exception as exc:
                failures.append(f"{provider}:{_safe_error(exc)}")
        raise RuntimeError("；".join(failures))

    def sector_snapshot(self, as_of: str | date | datetime | None = None) -> dict[str, Any]:
        """行业 ETF 快照：与 snapshot 同口径（美股取严格早于 A 股报告日的最近交易日）。

        全部源失败时返回 demo 数据（status=demo_fallback）；事件规则对演示
        数据不派单（require_live），只用于界面展示。
        """
        a_share_report_date = _parse_report_date(as_of)
        sectors: list[dict[str, Any]] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(SECTOR_ETF_SPECS)) as executor:
            futures = {
                executor.submit(self._fetch_one, spec, a_share_report_date): spec
                for spec in SECTOR_ETF_SPECS
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    sectors.append(future.result())
                except Exception as exc:
                    errors.append(f"{spec['name']}: {_safe_error(exc)}")
        order = {spec["ticker"]: index for index, spec in enumerate(SECTOR_ETF_SPECS)}
        sectors.sort(key=lambda item: order.get(item["ticker"], 999))
        status = "live_delayed"
        notice = (
            f"以 A 股报告日 {a_share_report_date.isoformat()} 为锚点，"
            "各行业 ETF 取当地交易日严格早于报告日的最近收盘。"
        )
        if not sectors and self.demo_fallback:
            sectors = _demo_sector_indices(a_share_report_date)
            status = "demo_fallback"
            notice += " 行业 ETF 行情接口暂时不可用，当前为演示数据，不代表真实市场行情。"
        providers = {item["provider"] for item in sectors}
        if status == "live_delayed" and providers - {"yahoo"}:
            fallback_labels = "、".join(
                _PROVIDER_LABELS.get(p, p) for p in sorted(providers - {"yahoo"})
            )
            notice += f" 已自动切换备用行情源（{fallback_labels}），数据为延迟行情。"
        return {
            "sectors": sectors,
            "status": status,
            "provider": _provider_summary(providers) if status == "live_delayed" else "demo sample",
            "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "notice": notice,
            "errors": errors,
            "a_share_report_date": a_share_report_date.isoformat(),
        }


def _provider_summary(providers: set[str]) -> str:
    return " / ".join(_PROVIDER_LABELS.get(p, p) for p in sorted(providers)) + " 延迟行情"


def _demo_sector_indices(a_share_report_date: date) -> list[dict[str, Any]]:
    samples = (
        ("XLK", "科技", 3.5),
        ("XLE", "能源", -2.8),
        ("XLF", "金融", 0.6),
        ("XLV", "医疗保健", -0.4),
        ("XLY", "可选消费", 1.1),
    )
    trade_date = _previous_weekday(a_share_report_date - timedelta(days=1))
    history_dates = _recent_weekdays(trade_date, 5)
    values = []
    for ticker, name, change_percent in samples:
        close = round(100 * (1 + change_percent / 100), 2)
        previous = 100.0
        values.append(
            {
                "ticker": ticker,
                "name": name,
                "region": "美国",
                "currency": "USD",
                "trade_date": trade_date.isoformat(),
                "timezone": "America/New_York",
                "close": close,
                "previous_close": previous,
                "change": round(close - previous, 2),
                "change_percent": change_percent,
                "history": [
                    {"date": history_dates[index].isoformat(), "close": round(close * factor, 2)}
                    for index, factor in enumerate((0.988, 0.995, 0.992, 0.998, 1.0))
                ],
                "source_url": "",
                "provider": "demo",
            }
        )
    return values


def _demo_indices(a_share_report_date: date) -> list[dict[str, Any]]:
    samples = (
        ("^GSPC", "标普 500", "美国", "USD", 6328.0, 0.52),
        ("^IXIC", "纳斯达克综合", "美国", "USD", 21125.0, 0.78),
        ("^DJI", "道琼斯工业", "美国", "USD", 44110.0, 0.18),
        ("^KS11", "KOSPI", "韩国", "KRW", 3245.0, -0.44),
        ("^N225", "日经 225", "日本", "JPY", 41500.0, 0.35),
    )
    values = []
    for ticker, name, region, currency, close, change_percent in samples:
        target = (
            a_share_report_date - timedelta(days=1)
            if region == "美国"
            else a_share_report_date
        )
        trade_date = _previous_weekday(target)
        history_dates = _recent_weekdays(trade_date, 5)
        previous = close / (1 + change_percent / 100)
        values.append(
            {
                "ticker": ticker,
                "name": name,
                "region": region,
                "currency": currency,
                "trade_date": trade_date.isoformat(),
                "timezone": "America/New_York" if region == "美国" else ("Asia/Seoul" if region == "韩国" else "Asia/Tokyo"),
                "close": close,
                "previous_close": round(previous, 2),
                "change": round(close - previous, 2),
                "change_percent": change_percent,
                "history": [
                    {"date": history_dates[index].isoformat(), "close": round(close * factor, 2)}
                    for index, factor in enumerate((0.982, 0.991, 0.987, 0.996, 1.0))
                ],
                "source_url": "",
                "provider": "demo",
            }
        )
    return values


def _parse_report_date(value: str | date | datetime | None) -> date:
    if isinstance(value, datetime):
        return value.astimezone(A_SHARE_ZONE).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if text:
        for candidate in (text[:10], text):
            try:
                return datetime.fromisoformat(candidate).date()
            except ValueError:
                continue
    return datetime.now(A_SHARE_ZONE).date()


def _previous_weekday(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _recent_weekdays(end: date, count: int) -> list[date]:
    values = []
    current = end
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current -= timedelta(days=1)
    return list(reversed(values))


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "网络连接失败"
    return str(exc)[:160]
