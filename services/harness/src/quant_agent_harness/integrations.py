from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class TushareClient:
    endpoint = "https://api.tushare.pro"

    def __init__(self, token: str, *, timeout_seconds: int = 20):
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds
        if not self.token:
            raise ValueError("Tushare token 不能为空")

    def test_connection(self) -> dict[str, Any]:
        items = self.query(
            "stock_basic",
            params={"list_status": "L"},
            fields="ts_code,name,market,list_date",
        )
        return {"message": "Tushare 连接成功", "sample_count": len(items)}

    def query(self, api_name: str, *, params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
        response = _post_json(
            self.endpoint,
            {"api_name": api_name, "token": self.token, "params": params, "fields": fields},
            timeout_seconds=self.timeout_seconds,
        )
        if int(response.get("code") or 0) != 0:
            raise RuntimeError(str(response.get("msg") or "Tushare 返回错误"))
        data = response.get("data") or {}
        columns = data.get("fields") or []
        items = data.get("items") or []
        return [dict(zip(columns, item, strict=False)) for item in items]

    def company_snapshot(self, symbol: str) -> dict[str, Any]:
        ts_code = normalize_ts_code(symbol)
        snapshot: dict[str, Any] = {"symbol": symbol, "ts_code": ts_code, "errors": []}
        requests = [
            (
                "stock_basic",
                {"ts_code": ts_code},
                "ts_code,symbol,name,area,industry,market,list_date",
                "basic",
            ),
            (
                "stock_company",
                {"ts_code": ts_code},
                "ts_code,com_name,exchange,province,city,setup_date,introduction",
                "company",
            ),
            (
                "daily_basic",
                {"ts_code": ts_code},
                "ts_code,trade_date,close,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv",
                "daily_basic",
            ),
            (
                "fina_indicator",
                {"ts_code": ts_code},
                (
                    "ts_code,ann_date,end_date,eps,roe,roe_waa,grossprofit_margin,netprofit_margin,"
                    "debt_to_assets,current_ratio,quick_ratio,or_yoy,netprofit_yoy,assets_yoy"
                ),
                "financial_indicator",
            ),
            (
                "forecast",
                {"ts_code": ts_code},
                (
                    "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,"
                    "net_profit_max,summary,change_reason"
                ),
                "forecast",
            ),
        ]
        for api_name, params, fields, key in requests:
            try:
                rows = self.query(api_name, params=params, fields=fields)
                snapshot[key] = rows[0] if rows else None
            except Exception as exc:
                snapshot[key] = None
                snapshot["errors"].append(f"{api_name}: {_safe_error(exc)}")
        return snapshot


class TavilyClient:
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str, *, timeout_seconds: int = 20):
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise ValueError("Tavily API Key 不能为空")

    def test_connection(self) -> dict[str, Any]:
        response = self.search("A股 市场 最新信息", max_results=1, time_range="week")
        results = response.get("results") or []
        return {"message": "Tavily 连接成功", "sample_count": len(results)}

    def search(
        self,
        query: str,
        *,
        max_results: int = 6,
        time_range: str = "month",
    ) -> dict[str, Any]:
        return _post_json(
            self.endpoint,
            {
                "query": query,
                "topic": "finance",
                "search_depth": "basic",
                "max_results": max(1, min(max_results, 20)),
                "time_range": time_range,
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout_seconds=self.timeout_seconds,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )


def _post_json(
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: int,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "QuantAgent/0.1",
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("外部服务响应不是 JSON 对象")
    return parsed


def normalize_ts_code(symbol: str) -> str:
    value = symbol.strip().upper()
    if value.endswith(".SS"):
        return value[:-3] + ".SH"
    if "." not in value and value.isdigit():
        return value + (".SH" if value.startswith(("5", "6", "9")) else ".SZ")
    return value


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return str(exc)[:240]
