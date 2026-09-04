"""服务器本机冒烟测试：自读 /etc/aquant.env 的口令登录，触发一次真实邮箱同步。

口令与授权码都不打印。用 systemd 里的同一份 env 跑。
"""

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
            response = opener.open(request, timeout=180)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
        payload = json.loads(response.read())
        set_cookie = response.headers.get("Set-Cookie")
        if set_cookie:
            cookie = set_cookie.split(";", 1)[0]
        return response.status, payload

    status, data = post("/api/login", {"password": env["QUANT_AGENT_ACCESS_PASSWORD"]})
    print(f"login: {status} {data}")

    status, data = post(
        "/api/rpc",
        {
            "type": "request",
            "protocol_version": 1,
            "request_id": "s1",
            "method": "sync_mailbox",
            "payload": {},
        },
        with_cookie=True,
    )
    if not data.get("ok"):
        print(f"sync failed (http {status}):", data)
        return 1
    sync = data["result"]["sync"]
    print("fetched:", sync["fetched"])
    print("new_reports:", sync["new_reports"], "| duplicates:", sync["duplicate_reports"])
    print("stored_segments:", sync["stored_segments"], "| assembled:", sync["assembled_groups"])
    print("expired_groups:", sync["expired_groups"])
    print("pending_groups:", len(sync["pending_groups"]))
    for err in sync["errors"][:5]:
        print("  error:", str(err)[:160])
    print("--- 最近报告（最多 8 条）---")
    for report in data["result"]["reports"][:8]:
        when = report["generated_at"] or report["mail_received_at"] or "无时间"
        slot = report["run_slot"] or "?"
        print(
            f"  {when} | 轮次 {slot} | {report['parse_status']} | "
            f"精选 {report['selected_count']} | {report['source']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
