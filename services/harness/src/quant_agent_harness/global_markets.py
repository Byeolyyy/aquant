from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


INDEX_SPECS = (
    {"ticker": "^GSPC", "name": "标普 500", "region": "美国"},
    {"ticker": "^IXIC", "name": "纳斯达克综合", "region": "美国"},
    {"ticker": "^DJI", "name": "道琼斯工业", "region": "美国"},
    {"ticker": "^KS11", "name": "KOSPI", "region": "韩国"},
    {"ticker": "^KQ11", "name": "KOSDAQ", "region": "韩国"},
)

A_SHARE_ZONE = ZoneInfo("Asia/Shanghai")


class GlobalMarketClient:
    """Small no-key demo adapter for delayed public index quotes.

    The provider is deliberately isolated behind this interface so a licensed
    enterprise feed can replace it without changing the Agent workflow or UI.
    """

    endpoint = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

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
            "美股取严格早于报告日的最近交易日，韩股取不晚于报告日的最近交易日。"
        )
        if not indices and self.demo_fallback:
            indices = _demo_indices(a_share_report_date)
            status = "demo_fallback"
            notice += " 行情接口暂时不可用，当前为界面演示数据，不代表真实市场行情。"
        quality_flags = [
            f"{item['name']} 单日涨跌 {float(item['change_percent']):+.2f}%，超过 12% 异常阈值，需要第二行情源复核"
            for item in indices
            if abs(float(item.get("change_percent") or 0)) > 12
        ]
        return {
            "market_indices": indices,
            "status": status,
            "provider": "Yahoo Finance delayed quote" if status == "live_delayed" else "demo sample",
            "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "notice": notice,
            "errors": errors,
            "quality_flags": quality_flags,
            "a_share_report_date": a_share_report_date.isoformat(),
            "session_rule": {
                "美国": "取当地交易日 < A股报告日的最近收盘",
                "韩国": "取当地交易日 <= A股报告日的最近收盘",
            },
        }

    def _fetch_one(self, spec: dict[str, str], a_share_report_date: date) -> dict[str, Any]:
        ticker = urllib.parse.quote(spec["ticker"], safe="")
        period1 = int(
            datetime.combine(a_share_report_date - timedelta(days=16), time.min, timezone.utc).timestamp()
        )
        period2 = int(
            datetime.combine(a_share_report_date + timedelta(days=2), time.min, timezone.utc).timestamp()
        )
        url = (
            self.endpoint.format(ticker=ticker)
            + f"?period1={period1}&period2={period2}&interval=1d&events=history"
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 QuantAgent/0.1", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not isinstance(result, dict):
            raise RuntimeError(str(((payload.get("chart") or {}).get("error") or {}).get("description") or "无行情结果"))
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quotes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
        timezone_name = str(meta.get("exchangeTimezoneName") or "UTC")
        try:
            market_zone = ZoneInfo(timezone_name)
        except Exception:
            market_zone = ZoneInfo("UTC")
            timezone_name = "UTC"
        history_by_date: dict[str, dict[str, Any]] = {}
        for timestamp, close in zip(timestamps, quotes, strict=False):
            if close is None:
                continue
            market_date = datetime.fromtimestamp(int(timestamp), market_zone).date().isoformat()
            history_by_date[market_date] = {"date": market_date, "close": round(float(close), 2)}
        cutoff = (
            a_share_report_date - timedelta(days=1)
            if spec["region"] == "美国"
            else a_share_report_date
        )
        history = [
            item
            for item in sorted(history_by_date.values(), key=lambda value: str(value["date"]))
            if str(item["date"]) <= cutoff.isoformat()
        ]
        if len(history) < 2:
            raise RuntimeError("有效收盘价不足两日")
        latest, previous = history[-1], history[-2]
        change = float(latest["close"]) - float(previous["close"])
        change_percent = change / float(previous["close"]) * 100 if previous["close"] else 0.0
        return {
            "ticker": spec["ticker"],
            "name": spec["name"],
            "region": spec["region"],
            "currency": str(meta.get("currency") or ("KRW" if spec["region"] == "韩国" else "USD")),
            "trade_date": latest["date"],
            "timezone": timezone_name,
            "close": latest["close"],
            "previous_close": previous["close"],
            "change": round(change, 2),
            "change_percent": round(change_percent, 2),
            "history": history[-5:],
            "source_url": "https://finance.yahoo.com/quote/" + ticker + "/",
        }


def _demo_indices(a_share_report_date: date) -> list[dict[str, Any]]:
    samples = (
        ("^GSPC", "标普 500", "美国", "USD", 6328.0, 0.52),
        ("^IXIC", "纳斯达克综合", "美国", "USD", 21125.0, 0.78),
        ("^DJI", "道琼斯工业", "美国", "USD", 44110.0, 0.18),
        ("^KS11", "KOSPI", "韩国", "KRW", 3245.0, -0.44),
        ("^KQ11", "KOSDAQ", "韩国", "KRW", 812.0, -0.71),
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
                "timezone": "America/New_York" if region == "美国" else "Asia/Seoul",
                "close": close,
                "previous_close": round(previous, 2),
                "change": round(close - previous, 2),
                "change_percent": change_percent,
                "history": [
                    {"date": history_dates[index].isoformat(), "close": round(close * factor, 2)}
                    for index, factor in enumerate((0.982, 0.991, 0.987, 0.996, 1.0))
                ],
                "source_url": "",
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
