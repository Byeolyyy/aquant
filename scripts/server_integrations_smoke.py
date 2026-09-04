"""服务器本机冒烟：验证 Tushare 与 Tavily 连接测试。"""

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

    post("/api/login", {"password": env["QUANT_AGENT_ACCESS_PASSWORD"]})

    for target in ("tushare", "tavily"):
        status, data = post(
            "/api/rpc",
            {
                "type": "request",
                "protocol_version": 1,
                "request_id": target,
                "method": "test_integration",
                "payload": {"target": target},
            },
            with_cookie=True,
        )
        if data.get("ok"):
            result = data["result"]
            print(f"{target}: 连接成功 | {str(result.get('message'))[:120]}")
        else:
            print(f"{target}: 失败 | {str(data.get('error'))[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
