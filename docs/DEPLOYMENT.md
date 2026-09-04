# aquant 部署说明

> 本文档描述 aquant 的服务器部署（Web 版）。桌面版（Electron）不受影响，见 README。

## 现状（2026-08-21）

- **服务器**：腾讯云 Ubuntu 22.04，2 核 / 1.9 GB 内存（+2 GB swap）
- **入口**：`http://1.14.160.122:8080`（裸 IP + 端口，临时方案；域名 + HTTPS 待备案域名就绪后启用）
- **访问口令**：存于服务器 `/etc/aquant.env` 的 `QUANT_AGENT_ACCESS_PASSWORD`
- **服务**：systemd `aquant.service`（开机自启、故障自动重启）
- **模型**：DeepSeek（OpenAI 兼容）—— Base URL / 模型名 / API key 均存于 `/etc/aquant.env`

## 架构

```
浏览器 ──:8080──> nginx ──┬── /             /opt/aquant/web/dist（本地构建后上传的静态文件）
                          └── /api/*        反代 127.0.0.1:8788
                                              │
                              systemd: aquant.service（User=ubuntu）
                              python -m quant_agent_harness.web
                                              │
                              ProtocolServer / Harness / SQLite
                              /var/lib/aquant/quant-agent.sqlite
                              ──按需──> imap.qq.com（PTrade 邮件）
```

## 目录布局（服务器）

| 路径 | 内容 |
| --- | --- |
| `/opt/aquant/code` | harness 代码（`services/harness/`） |
| `/opt/aquant/web/dist` | 前端静态产物 |
| `/opt/aquant/.venv` | Python 3.10 venv（仅 pydantic） |
| `/var/lib/aquant/` | SQLite 数据 |
| `/etc/aquant.env` | 全部配置与密钥（chmod 600，root） |
| `/etc/systemd/system/aquant.service` | systemd unit |
| `/etc/nginx/sites-available/aquant` | nginx server 块（:8080） |

## 配置（`/etc/aquant.env` 全部键）

| 键 | 说明 |
| --- | --- |
| `QUANT_AGENT_ACCESS_PASSWORD` | 网页访问口令（必填，缺省服务拒绝启动） |
| `QUANT_AGENT_MODEL_BASE_URL` / `QUANT_AGENT_MODEL_NAME` / `QUANT_AGENT_MODEL_API_KEY` | 协调者模型（DeepSeek 等 OpenAI 兼容服务） |
| `QUANT_AGENT_MAIL_ADDRESS` / `QUANT_AGENT_MAIL_AUTH_CODE` | QQ 邮箱收信（授权码，不是登录密码） |
| `QUANT_AGENT_MAIL_IMAP_HOST` / `QUANT_AGENT_MAIL_MAILBOX` | IMAP 服务器/文件夹，默认 `imap.qq.com` / `INBOX` |
| `QUANT_AGENT_TUSHARE_TOKEN` / `QUANT_AGENT_TAVILY_API_KEY` | 可选外部数据源 |
| `QUANT_AGENT_DAILY_RUN_LIMIT` | 每会话每日 start_run 上限，默认 20 |
| `QUANT_AGENT_MAX_CONCURRENT_RUNS` | 并发 run 上限，默认 2（机器只有 2 核 2G） |
| `QUANT_AGENT_COOKIE_SECURE` | 1 时 cookie 带 Secure（HTTPS 启用后置 1） |

## 部署 / 更新

**后端**（改 Python 后）：

```powershell
# 本地
tar czf aq-code.tgz --exclude='__pycache__' services/harness
scp aq-code.tgz ubuntu@<服务器>:/tmp/
ssh ubuntu@<服务器> "tar xzf /tmp/aq-code.tgz -C /opt/aquant/code && sudo chown -R ubuntu:ubuntu /opt/aquant/code && sudo systemctl restart aquant"
```

**前端**（改 React 后）：

```powershell
npm run build   # 产出 apps/desktop/dist
# 上传并覆盖 /opt/aquant/web/dist，然后 sudo systemctl reload nginx（静态文件无需重启 python）
```

**验证**：

```bash
systemctl status aquant
journalctl -u aquant -f
curl http://127.0.0.1:8788/api/healthz
python scripts/public_e2e.py          # 公网端到端（登录/同步/跑分析/SSE）
python scripts/server_mail_smoke.py   # 服务器本机邮箱同步验证
```

## 域名 + HTTPS（待办）

1. 在云控制台把已备案域名解析（A 记录）到服务器公网 IP
2. `sudo certbot --nginx -d <域名>` 签发证书（80 端口已有 nginx；certbot 会临时接管验证，不影响 pcba）
3. 把 `/etc/nginx/sites-available/aquant` 改为 `listen 443 ssl` + `server_name <域名>`
4. `/etc/aquant.env` 置 `QUANT_AGENT_COOKIE_SECURE=1`，重启 `aquant`
5. 轮换访问口令（裸 IP 时期的临时口令作废）

## 已知限制

- **HTTP 明文**：裸 IP 入口没有 TLS，访问口令走明文网络。仅演示期可接受；上 HTTPS 后解决。
- **配置全局共享**：Agent 配置与 Prompt 工作台在 Web 模式只读；模型/邮箱由服务器统一配置。
- **Tavily 在大陆机房大概率不可达**：会被记为 unknown，不中断分析。外围行情自动走腾讯/东财/新浪源。
- **上游 PTrade 脚本只发第一片**：长报告被静默截断（发信侧 `first_part_only=True`），下游按 partial 降级处理。
- **会话在内存**：`aquant` 重启后所有访客需重新登录；未完成 run 会被标记为 interrupted。
- **单进程**：并发上限 2，再高需要上多 worker + 解决 SQLite 与内存态控制的跨进程问题。
