from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantAgent/0.2"
EASTMONEY_REPORT_API = "https://reportapi.eastmoney.com/report/list"
EASTMONEY_REPORT_PDF = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"


class PublicAStockClient:
    """Best-effort, read-only public A-share sources that require no API key."""

    def __init__(self, *, timeout_seconds: int = 8, total_timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds

    def research(self, symbols: list[str], *, max_stocks: int = 3) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.total_timeout_seconds
        bundles = []
        for symbol in symbols[:max_stocks]:
            if time.monotonic() >= deadline:
                break
            code = _code(symbol)
            bundle: dict[str, Any] = {
                "symbol": symbol,
                "code": code,
                "announcements": [],
                "news": [],
                "reports": [],
                "errors": [],
            }
            for label, key, fetcher in (
                ("巨潮公告", "announcements", lambda: self.cninfo_announcements(code)),
                ("东方财富新闻", "news", lambda: self.eastmoney_news(code)),
                ("东方财富研报", "reports", lambda: self.eastmoney_reports(code)),
            ):
                if time.monotonic() >= deadline:
                    bundle["errors"].append(f"{label}：超过本轮公共查询时限")
                    continue
                try:
                    bundle[key] = fetcher()
                except Exception as exc:
                    bundle["errors"].append(f"{label}：{_safe_error(exc)}")
            bundles.append(bundle)
        return bundles

    def cninfo_announcements(self, code: str, *, days: int = 90, limit: int = 5) -> list[dict[str, str]]:
        org_id = _cninfo_org_id(code)
        payload = {
            "stock": f"{code},{org_id}",
            "tabName": "fulltext",
            "pageSize": str(max(10, limit * 3)),
            "pageNum": "1",
            "column": "",
            "category": "",
            "plate": "",
            "seDate": "",
            "searchkey": "",
            "secid": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        data = self._post_form_json(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            payload,
            {"Referer": "https://www.cninfo.com.cn/new/disclosure", "Origin": "https://www.cninfo.com.cn"},
        )
        rows = []
        for item in data.get("announcements") or []:
            date = _date_text(item.get("announcementTime"))
            if not _recent(date, days):
                continue
            announcement_id = str(item.get("announcementId") or "")
            rows.append(
                {
                    "date": date,
                    "source": "巨潮资讯",
                    "type": _clean(item.get("announcementTypeName"), 60),
                    "title": _clean(item.get("announcementTitle"), 180),
                    "summary": "",
                    "url": "https://www.cninfo.com.cn/new/disclosure/detail?annoId=" + announcement_id
                    if announcement_id
                    else "",
                }
            )
        return _sort_recent(rows)[:limit]

    def eastmoney_news(self, code: str, *, days: int = 30, limit: int = 4) -> list[dict[str, str]]:
        callback = "jQuery_quant_agent"
        inner = json.dumps(
            {
                "uid": "",
                "keyword": code,
                "type": ["cmsArticleWebOld"],
                "client": "web",
                "clientType": "web",
                "clientVersion": "curr",
                "param": {
                    "cmsArticleWebOld": {
                        "searchScope": "default",
                        "sort": "default",
                        "pageIndex": 1,
                        "pageSize": max(10, limit * 3),
                        "preTag": "",
                        "postTag": "",
                    }
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = self._get_text(
            "https://search-api-web.eastmoney.com/search/jsonp",
            {"cb": callback, "param": inner},
            {"Referer": "https://so.eastmoney.com/"},
        )
        start, end = raw.find("("), raw.rfind(")")
        if start < 0 or end <= start:
            return []
        data = json.loads(raw[start + 1 : end])
        rows = []
        for item in data.get("result", {}).get("cmsArticleWebOld", []) or []:
            date = _date_text(item.get("date"))
            if _recent(date, days):
                rows.append(
                    {
                        "date": date,
                        "source": _clean(item.get("mediaName"), 60) or "东方财富",
                        "title": _clean(item.get("title"), 180),
                        "summary": _clean(item.get("content"), 500),
                        "url": str(item.get("url") or "").strip(),
                    }
                )
        return _sort_recent(rows)[:limit]

    def eastmoney_reports(self, code: str, *, days: int = 120, limit: int = 3) -> list[dict[str, str]]:
        begin = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        data = self._get_json(
            EASTMONEY_REPORT_API,
            {
                "industryCode": "*",
                "pageSize": str(max(10, limit * 3)),
                "industry": "*",
                "rating": "*",
                "ratingChange": "*",
                "beginTime": begin,
                "endTime": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
                "pageNo": "1",
                "qType": "0",
                "code": code,
            },
            {"Referer": "https://data.eastmoney.com/"},
        )
        rows = []
        for item in data.get("data") or []:
            date = _date_text(item.get("publishDate"))
            if not _recent(date, days):
                continue
            info_code = str(item.get("infoCode") or "")
            rating = _clean(item.get("emRatingName"), 40)
            rows.append(
                {
                    "date": date,
                    "source": _clean(item.get("orgSName"), 60) or "东方财富研报",
                    "title": _clean(item.get("title"), 180),
                    "summary": f"机构评级：{rating}" if rating else "",
                    "url": EASTMONEY_REPORT_PDF.format(info_code=info_code) if info_code else "",
                }
            )
        return _sort_recent(rows)[:limit]

    def _get_json(self, endpoint: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return json.loads(self._get_text(endpoint, params, headers))

    def _get_text(self, endpoint: str, params: dict[str, Any], headers: dict[str, str]) -> str:
        query = urllib.parse.urlencode(params)
        target = endpoint + (("&" if "?" in endpoint else "?") + query if query else "")
        request = urllib.request.Request(
            target,
            headers={"User-Agent": USER_AGENT, **headers},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")

    def _post_form_json(self, endpoint: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                **headers,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))


def _code(symbol: str) -> str:
    return symbol.split(".", 1)[0].strip()


def _cninfo_org_id(code: str) -> str:
    if code.startswith("6"):
        return "gssh0" + code
    if code.startswith(("4", "8")):
        return "gsbj0" + code
    return "gssz0" + code


def _clean(value: object, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _date_text(value: object) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
    return str(value or "")[:10]


def _recent(date_text: str, days: int) -> bool:
    try:
        return datetime.strptime(date_text, "%Y-%m-%d") >= datetime.now() - timedelta(days=days)
    except ValueError:
        return True


def _sort_recent(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(rows, key=lambda item: item.get("date", ""), reverse=True)


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    lowered = text.lower()
    if any(token in lowered for token in ("ssl", "eof", "timed out", "timeout", "connection reset")):
        return "网络连接中断或超时"
    if any(token in lowered for token in ("403", "forbidden", "拒绝访问")):
        return "来源暂时拒绝访问"
    if "404" in lowered:
        return "来源页面不存在"
    return (text or type(exc).__name__)[:120]
