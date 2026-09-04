"""从公网做端到端验证：登录、同步邮箱、启动分析、SSE 实时性。

口令从环境变量读取（公开仓库不落凭证）：
    QUANT_AGENT_ACCESS_PASSWORD=xxx python scripts/public_e2e.py
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

BASE = "http://1.14.160.122:8080"
PASSWORD = os.environ.get("QUANT_AGENT_ACCESS_PASSWORD", "")


def main() -> int:
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
                "request_id": "e2e",
                "method": method,
                "payload": payload,
            },
            with_cookie=True,
        )

    if not PASSWORD:
        print("请先设置环境变量 QUANT_AGENT_ACCESS_PASSWORD（访问口令在服务器 /etc/aquant.env）")
        return 2

    status, data = post("/api/login", {"password": PASSWORD})
    print(f"1) 登录: {status} {data}")

    status, data = rpc("sync_mailbox", {})
    if data.get("ok"):
        sync = data["result"]["sync"]
        print(
            f"2) 邮件同步: fetched={sync['fetched']} new={sync['new_reports']} "
            f"duplicates={sync['duplicate_reports']} pending_groups={len(sync['pending_groups'])}"
        )
    else:
        print("2) 邮件同步失败:", data.get("error"))

    _, listed = rpc("list_reports", {"limit": 50})
    reports = [r for r in listed["result"]["reports"] if r["parse_status"] != "invalid"]
    if not reports:
        print("没有可运行报告")
        return 1
    report = reports[0]
    print(f"3) 选用报告: {report['generated_at']} 轮次 {report['run_slot']}")

    received: list[dict] = []
    timestamps: list[float] = []

    def stream():
        opener.addheaders = [("Cookie", cookie)]
        request = urllib.request.Request(BASE + "/api/events")
        with opener.open(request, timeout=600) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))
                    timestamps.append(time.time())

    _, started = rpc("start_run", {"report_id": report["report_id"]})
    if not started.get("ok"):
        print("启动失败:", started)
        return 1
    run_id = started["result"]["run_id"]
    print(f"4) 运行已启动: {run_id[:8]}")

    streamer = threading.Thread(target=stream, daemon=True)
    streamer.start()

    deadline = time.time() + 300
    final = ""
    while time.time() < deadline:
        time.sleep(8)
        _, snapshot = rpc("get_run_snapshot", {"run_id": run_id})
        snap = (snapshot.get("result") or {}).get("snapshot") or {}
        final = snap.get("status", "")
        if final in {"completed", "error"}:
            break

    time.sleep(2)
    # SSE 实时性：每条事件的时间戳间隔应远小于轮询间隔（8s）。
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    max_gap = max(gaps) if gaps else 0
    print(f"5) 最终状态: {final}")
    print(f"6) SSE 事件: {len(received)} 条，最大到达间隔 {max_gap:.1f}s（<2s 即未被缓冲）")
    print(f"7) 事件种类: {sorted({e['kind'] for e in received})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
