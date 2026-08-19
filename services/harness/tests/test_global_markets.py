from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from quant_agent_harness.global_markets import GlobalMarketClient


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


def _tencent_quote(ticker: str, close: str, previous: str, change: str, pct: str, trade_time: str) -> bytes:
    fields = [""] * 36
    fields[0] = "200"
    fields[1] = "纳斯达克综合" if ticker == "usIXIC" else "道琼斯工业"
    fields[2] = "." + ticker[2:]
    fields[3] = close
    fields[4] = previous
    fields[30] = trade_time
    fields[31] = change
    fields[32] = pct
    fields[35] = "USD"
    return ('v_%s="%s";' % (ticker, "~".join(fields))).encode("gbk")


def _tencent_kline(ticker: str, rows: list[list]) -> bytes:
    payload = {"code": 0, "msg": "", "data": {ticker: {"day": rows}}}
    return json.dumps(payload).encode("utf-8")


def _eastmoney_kline(klines: list[str]) -> bytes:
    payload = {"rc": 0, "data": {"code": "KS11", "market": 100, "klines": klines}}
    return json.dumps(payload).encode("utf-8")


def _fake_urlopen(request, timeout=None):
    url = str(request.full_url)
    if "query1.finance.yahoo.com" in url:
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
    if "qt.gtimg.cn" in url:
        ticker = url.split("=")[-1]
        quote = {
            "usINX": ("7753.11", "7767.51", "-14.40", "-0.19"),
            "usIXIC": ("26729.16", "26803.03", "-73.87", "-0.28"),
            "usDJI": ("53732.41", "53839.99", "-107.58", "-0.20"),
        }[ticker]
        return _FakeResponse(_tencent_quote(ticker, *quote, "2026-08-14 16:41:05"))
    if "ifzq.gtimg.cn" in url:
        ticker = url.split("param=")[-1].split(",")[0]
        closes = {
            "usINX": ["7728.20", "7740.10", "7750.30", "7767.51", "7753.11"],
            "usIXIC": ["26661.95", "26710.20", "26750.40", "26803.03", "26729.16"],
            "usDJI": ["53850.00", "53810.20", "53850.60", "53839.99", "53732.41"],
        }[ticker]
        rows = [
            [date, close, close, close, close, "0", {}, ""]
            for date, close in zip(
                ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"),
                closes,
            )
        ]
        return _FakeResponse(_tencent_kline(ticker, rows))
    if "push2his.eastmoney.com" in url:
        secid = url.split("secid=")[-1].split("&")[0]
        klines = {
            "100.KS11": ["2026-08-11,6258.77", "2026-08-12,6345.53", "2026-08-13,6579.04", "2026-08-14,6813.34"],
            "100.N225": ["2026-08-11,66970.22", "2026-08-12,67524.06", "2026-08-13,68308.59", "2026-08-14,68713.80"],
        }[secid]
        return _FakeResponse(_eastmoney_kline(klines))
    raise urllib.error.URLError("unexpected url: " + url)


class GlobalMarketsFallbackTests(unittest.TestCase):
    def test_yahoo_403_switches_to_tencent_and_eastmoney(self):
        client = GlobalMarketClient(demo_fallback=False)
        with mock.patch("quant_agent_harness.global_markets.urllib.request.urlopen", side_effect=_fake_urlopen):
            snapshot = client.snapshot("2026-08-17 14:30:00")
        self.assertEqual(snapshot["status"], "live_delayed")
        self.assertEqual(snapshot["errors"], [])
        by_ticker = {item["ticker"]: item for item in snapshot["market_indices"]}
        self.assertEqual(set(by_ticker), {"^GSPC", "^IXIC", "^DJI", "^KS11", "^N225"})
        self.assertEqual(by_ticker["^DJI"]["provider"], "tencent")
        # 以 K 线过滤后的最近两个交易日为准，与 Yahoo 主源的锚点规则一致
        self.assertEqual(by_ticker["^DJI"]["trade_date"], "2026-08-14")
        self.assertEqual(by_ticker["^DJI"]["close"], 53732.41)
        self.assertEqual(by_ticker["^DJI"]["previous_close"], 53839.99)
        self.assertAlmostEqual(by_ticker["^DJI"]["change_percent"], -0.2, places=1)
        self.assertEqual(by_ticker["^DJI"]["currency"], "USD")
        self.assertEqual(by_ticker["^KS11"]["provider"], "eastmoney")
        self.assertEqual(by_ticker["^KS11"]["close"], 6813.34)
        self.assertEqual(by_ticker["^N225"]["provider"], "eastmoney")
        self.assertEqual(by_ticker["^N225"]["region"], "日本")
        self.assertEqual(by_ticker["^N225"]["currency"], "JPY")
        self.assertEqual(by_ticker["^N225"]["timezone"], "Asia/Tokyo")
        self.assertIn("备用行情源", snapshot["notice"])
        self.assertIn("日本", snapshot["session_rule"])

    def test_fallback_quote_respects_cutoff_anchor(self):
        # 腾讯报价日期晚于美股锚点（报告日-1）时，必须取 K 线中
        # 早于锚点的最近交易日，避免使用报告日之后的数据。
        late_quote = _tencent_quote("usDJI", "54000.00", "53839.99", "160.01", "0.30", "2026-08-17 10:00:00")
        rows = [
            ["2026-08-13", "53839.99", "53839.99", "53839.99", "53839.99", "0", {}, ""],
            ["2026-08-14", "53732.41", "53732.41", "53732.41", "53732.41", "0", {}, ""],
        ]

        def urlopen(request, timeout=None):
            url = str(request.full_url)
            if "query1.finance.yahoo.com" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
            if "qt.gtimg.cn" in url:
                return _FakeResponse(late_quote)
            if "ifzq.gtimg.cn" in url:
                return _FakeResponse(_tencent_kline("usDJI", rows))
            raise urllib.error.URLError("unexpected url: " + url)

        client = GlobalMarketClient(demo_fallback=False)
        with mock.patch("quant_agent_harness.global_markets.urllib.request.urlopen", side_effect=urlopen):
            snapshot = client.snapshot("2026-08-17 14:30:00")
        dji = next(item for item in snapshot["market_indices"] if item["ticker"] == "^DJI")
        self.assertEqual(dji["trade_date"], "2026-08-14")
        self.assertEqual(dji["close"], 53732.41)

    def test_sina_fallback_for_nikkei_when_eastmoney_unavailable(self):
        sina_rows = (
            '/*<script>location.href=\'//sina.com\';</script>*/\n'
            'var t=([{"date":"2026-08-13","open":"68300.000","high":"68410.000",'
            '"low":"68190.000","close":"68308.590","volume":"1000","position":"0","s":"0.000"},'
            '{"date":"2026-08-14","open":"68600.000","high":"68800.000",'
            '"low":"68550.000","close":"68713.800","volume":"1000","position":"0","s":"0.000"},'
            '{"date":"2026-08-18","open":"69000.000","high":"69200.000",'
            '"low":"68900.000","close":"69015.000","volume":"1000","position":"0","s":"0.000"}]);'
        )

        def urlopen(request, timeout=None):
            url = str(request.full_url)
            if "query1.finance.yahoo.com" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
            if "push2his.eastmoney.com" in url:
                raise urllib.error.URLError("Remote end closed connection without response")
            if "qt.gtimg.cn" in url or "ifzq.gtimg.cn" in url:
                ticker = url.split("=")[-1].split(",")[0]
                quote = {
                    "usINX": ("7753.11", "7767.51", "-14.40", "-0.19"),
                    "usIXIC": ("26729.16", "26803.03", "-73.87", "-0.28"),
                    "usDJI": ("53732.41", "53839.99", "-107.58", "-0.20"),
                }[ticker]
                if "qt.gtimg.cn" in url:
                    return _FakeResponse(_tencent_quote(ticker, *quote, "2026-08-14 16:41:05"))
                closes = {
                    "usINX": ["7728.20", "7740.10", "7750.30", "7767.51", "7753.11"],
                    "usIXIC": ["26661.95", "26710.20", "26750.40", "26803.03", "26729.16"],
                    "usDJI": ["53850.00", "53810.20", "53850.60", "53839.99", "53732.41"],
                }[ticker]
                rows = [
                    [date, close, close, close, close, "0", {}, ""]
                    for date, close in zip(
                        ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"),
                        closes,
                    )
                ]
                return _FakeResponse(_tencent_kline(ticker, rows))
            if "stock2.finance.sina.com.cn" in url:
                return _FakeResponse(sina_rows.encode("utf-8"))
            raise urllib.error.URLError("unexpected url: " + url)

        client = GlobalMarketClient(demo_fallback=False)
        with mock.patch("quant_agent_harness.global_markets.urllib.request.urlopen", side_effect=urlopen):
            snapshot = client.snapshot("2026-08-17 14:30:00")
        self.assertEqual(snapshot["status"], "live_delayed")
        by_ticker = {item["ticker"]: item for item in snapshot["market_indices"]}
        self.assertEqual(by_ticker["^N225"]["provider"], "sina")
        # 08-18 的期货夜盘行必须被锚点规则过滤，最近有效交易日是 08-14
        self.assertEqual(by_ticker["^N225"]["trade_date"], "2026-08-14")
        self.assertEqual(by_ticker["^N225"]["close"], 68713.8)
        self.assertEqual(by_ticker["^N225"]["currency"], "JPY")
        # 东财全部失败时 KOSPI 进入 errors，其余指数保持真实行情
        self.assertEqual(len(snapshot["errors"]), 1)
        self.assertIn("KOSPI", snapshot["errors"][0])
        self.assertIn("新浪财经", snapshot["notice"])

    def test_all_sources_fail_falls_back_to_demo_without_kosdaq(self):
        def always_fail(_request, timeout=None):
            raise urllib.error.HTTPError("https://x", 403, "Forbidden", None, None)

        client = GlobalMarketClient(demo_fallback=True)
        with mock.patch("quant_agent_harness.global_markets.urllib.request.urlopen", side_effect=always_fail):
            snapshot = client.snapshot("2026-07-31 14:30:00")
        self.assertEqual(snapshot["status"], "demo_fallback")
        self.assertEqual(len(snapshot["market_indices"]), 5)
        names = [item["name"] for item in snapshot["market_indices"]]
        self.assertNotIn("KOSDAQ", names)
        nikkei = next(item for item in snapshot["market_indices"] if item["name"] == "日经 225")
        self.assertEqual(nikkei["region"], "日本")
        self.assertEqual(nikkei["provider"], "demo")
        self.assertEqual(nikkei["currency"], "JPY")


if __name__ == "__main__":
    unittest.main()
