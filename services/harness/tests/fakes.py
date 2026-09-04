from __future__ import annotations


class FakeGlobalMarket:
    """外围行情桩。

    Harness 缺省会自建真实的 GlobalMarketClient，单元测试若不显式传入
    就会在跑测试时打真实网络（Yahoo/腾讯/东财/新浪），既慢又不确定：
    多级 fallback 的重试间隔会把单次 run 拖到 4 秒以上，接近测试的
    wait 超时，全套测试并发时就会越线，表现为事件列表被截断的诡异断言失败。
    """

    def __init__(
        self,
        sector_change_percent: float = 0.6,
        sector_status: str = "live_delayed",
        index_change_percent: float = 0.48,
    ):
        self.sector_change_percent = sector_change_percent
        self.sector_status = sector_status
        self.index_change_percent = index_change_percent

    def snapshot(self, as_of=None):
        self.as_of = as_of
        return {
            "status": "live_delayed",
            "provider": "fake delayed feed",
            "retrieved_at": "2026-07-31T18:00:00+08:00",
            "notice": "测试延迟行情",
            "errors": [],
            "quality_flags": [],
            "market_indices": [
                {
                    "ticker": "^GSPC", "name": "标普 500", "region": "美国", "currency": "USD",
                    "trade_date": "2026-07-30", "timezone": "America/New_York", "close": 6300.0,
                    "previous_close": 6270.0, "change": 30.0, "change_percent": self.index_change_percent,
                    "history": [{"date": "2026-07-29", "close": 6270.0}, {"date": "2026-07-30", "close": 6300.0}],
                    "source_url": "https://example.com/gspc",
                },
                {
                    "ticker": "^KS11", "name": "KOSPI", "region": "韩国", "currency": "KRW",
                    "trade_date": "2026-07-31", "timezone": "Asia/Seoul", "close": 3250.0,
                    "previous_close": 3260.0, "change": -10.0, "change_percent": -0.31,
                    "history": [{"date": "2026-07-30", "close": 3260.0}, {"date": "2026-07-31", "close": 3250.0}],
                    "source_url": "https://example.com/kospi",
                },
            ],
        }

    def sector_snapshot(self, as_of=None):
        """行业 ETF 快照桩：默认 XLK 涨 0.6%（不足 2% 阈值不触发事件规则）。"""
        change = self.sector_change_percent
        sectors = [
            {
                "ticker": "XLK", "name": "科技", "region": "美国", "currency": "USD",
                "trade_date": "2026-07-30", "timezone": "America/New_York", "close": 120.0,
                "previous_close": 120.0 / (1 + change / 100), "change": 120.0 - 120.0 / (1 + change / 100),
                "change_percent": change, "source_url": "https://example.com/xlk",
            },
            {
                "ticker": "XLF", "name": "金融", "region": "美国", "currency": "USD",
                "trade_date": "2026-07-30", "timezone": "America/New_York", "close": 100.0,
                "previous_close": 99.5, "change": 0.5, "change_percent": 0.5,
                "source_url": "https://example.com/xlf",
            },
        ]
        return {
            "sectors": sectors,
            "status": self.sector_status,
            "provider": "fake delayed feed",
            "retrieved_at": "2026-07-31T18:00:00+08:00",
            "notice": "测试延迟行情",
            "errors": [],
            "a_share_report_date": "2026-07-31",
        }


class FakePublicAStock:
    """公共 A 股行情桩：板块列表固定返回半导体与银行两个板块。

    flow_rows 可注入资金流历史（默认 7 日，末日 2600 为单日脉冲形态）。
    """

    def __init__(self, board_error: bool = False, flow_rows: list[dict] | None = None):
        self.board_error = board_error
        self.flow_rows = flow_rows or [
            {"date": "2026-07-23", "main_net": 150, "small_net": 10, "medium_net": 20, "large_net": 30, "super_net": 90},
            {"date": "2026-07-24", "main_net": -120, "small_net": 10, "medium_net": 20, "large_net": 30, "super_net": -180},
            {"date": "2026-07-25", "main_net": 180, "small_net": 10, "medium_net": 20, "large_net": 30, "super_net": 120},
            {"date": "2026-07-28", "main_net": -160, "small_net": 10, "medium_net": 20, "large_net": 30, "super_net": -220},
            {"date": "2026-07-29", "main_net": 140, "small_net": 10, "medium_net": 20, "large_net": 30, "super_net": 80},
            {"date": "2026-07-30", "main_net": 130, "small_net": 10, "medium_net": 20, "large_net": 30, "super_net": 70},
            {"date": "2026-07-31", "main_net": 2600, "small_net": 5, "medium_net": 10, "large_net": 40, "super_net": 2545},
        ]

    def research(self, symbols, **_kwargs):
        return []

    def industry_board_quotes(self, **_kwargs):
        if self.board_error:
            raise RuntimeError("板块行情接口不可用")
        return [
            {"code": "BK1036", "name": "半导体", "close": 2000.0, "change_percent": 1.1},
            {"code": "BK0475", "name": "银行", "close": 1500.0, "change_percent": -0.2},
        ]

    def stock_fund_flow_history(self, code, **_kwargs):
        return [dict(row) for row in self.flow_rows]
