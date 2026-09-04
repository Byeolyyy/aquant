"""服务器本机冒烟：挑一份邮件报告，用真实模型跑完整分析并等待完成。"""

import json
import threading
import time
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
            response = opener.open(request, timeout=600)
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
                "request_id": "r",
                "method": method,
                "payload": payload,
            },
            with_cookie=True,
        )

    post("/api/login", {"password": env["QUANT_AGENT_ACCESS_PASSWORD"]})

    _, listed = rpc("list_reports", {"limit": 50})
    reports = listed.get("result", {}).get("reports", [])
    candidates = [r for r in reports if r["parse_status"] != "invalid"]
    if not candidates:
        print("没有可运行的报告")
        return 1
    report = candidates[0]
    print(f"选用报告: {report['generated_at']} 轮次 {report['run_slot']} ({report['parse_status']})")

    _, started = rpc("start_run", {"report_id": report["report_id"]})
    if not started.get("ok"):
        print("start_run 失败:", started)
        return 1
    run_id = started["result"]["run_id"]
    print("run_id:", run_id[:8], "| 开始等待事件…")

    received: list[dict] = []

    def stream():
        opener.addheaders = [("Cookie", cookie)]
        request = urllib.request.Request(BASE + "/api/events")
        with opener.open(request, timeout=900) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))

    thread = threading.Thread(target=stream, daemon=True)
    thread.start()

    deadline = time.time() + 600
    final_status = ""
    while time.time() < deadline:
        time.sleep(10)
        _, snapshot = rpc("get_run_snapshot", {"run_id": run_id})
        snap = (snapshot.get("result") or {}).get("snapshot") or {}
        final_status = snap.get("status", "")
        if final_status in {"completed", "error", "cancelled"}:
            break
        kinds = sorted({e["kind"] for e in received})
        print(f"…{final_status or 'planning/running'}，SSE 已收 {len(received)} 条事件 [{','.join(kinds)}]")

    print("最终状态:", final_status or "超时")
    print("SSE 事件总数:", len(received))
    counts: dict[str, int] = {}
    for event in received:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    print("事件分布:", counts)
    agents = sorted({event.get("agent_id") or "-" for event in received if event["kind"] == "agent.message"})
    print("产出消息的 Agent:", agents)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
