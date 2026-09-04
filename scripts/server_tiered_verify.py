"""服务器验证：解析大市值/小市值行，检查分档门槛注入；再跑一次完整分析。"""

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8788"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    with open("/etc/aquant.env", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key] = value
    return values


def main() -> int:
    env = read_env()
    opener = urllib.request.build_opener()
    cookie = ""

    def post(path: str, body: dict, with_cookie: bool = False):
        nonlocal cookie
        opener.addheaders = [("Content-Type", "application/json")] + (
            [("Cookie", cookie)] if with_cookie else []
        )
        request = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode("utf-8"), method="POST"
        )
        try:
            response = opener.open(request, timeout=120)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
        payload = json.loads(response.read())
        set_cookie = response.headers.get("Set-Cookie")
        if set_cookie:
            cookie = set_cookie.split(";", 1)[0]
        return response.status, payload

    def rpc(method: str, payload: dict):
        return post(
            "/api/rpc",
            {
                "type": "request",
                "protocol_version": 1,
                "request_id": "tiered",
                "method": method,
                "payload": payload,
            },
            with_cookie=True,
        )

    post("/api/login", {"password": env["QUANT_AGENT_ACCESS_PASSWORD"]})

    header = (
        "symbol super_net_wanyuan large_net_wanyuan medium_net_wanyuan small_net_wanyuan "
        "realtime_formula_wanyuan realtime_formula_ratio_pct l4_buy_sell vol_ratio turnover_now_pct"
    )
    raw = (
        "selected_head:\n" + header + "\n"
        "600519.SS 8564.53 3000 -1200 -1500 60000 0.4 True 1.42 1.93\n"
        "000001.SZ 3000 1200 -500 -600 5000 0.5 True 1.30 2.10\n"
        "near_head: empty"
    )
    _, data = rpc("parse_report", {"raw_text": raw})
    if not data.get("ok"):
        print("解析失败:", data)
        return 1
    rows = data["result"]["report"]["selected_rows"]
    for row in rows:
        print(
            f"{row['symbol']}: formula={row['realtime_formula_wanyuan']} "
            f"ratio={row['realtime_formula_ratio_pct']} "
            f"注入门槛={row['flow_threshold_wanyuan']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
