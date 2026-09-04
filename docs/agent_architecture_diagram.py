# -*- coding: utf-8 -*-
"""aquant 量化研究多 Agent 架构图生成脚本。

运行:  python docs/agent_architecture_diagram.py
输出:  docs/agent-architecture.png
"""

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager, pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# ---------- 中文字体 ----------
for _font in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
    try:
        font_manager.fontManager.addfont(_font)
    except Exception:
        pass
plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False

# ---------- 画布 ----------
W, H = 100.0, 70.0
fig = plt.figure(figsize=(19.2, 13.4), dpi=170)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W)
ax.set_ylim(0, H)
ax.axis("off")

# ---------- 配色 ----------
C = {
    "ui_face": "#E3F2FD", "ui_edge": "#1565C0",
    "proto_face": "#ECEFF1", "proto_edge": "#546E7A",
    "sidecar_face": "#FFFDE7", "sidecar_edge": "#F9A825",
    "sub_face": "#F3E5F5", "sub_edge": "#7B1FA2",
    "harness_face": "#FFF3E0", "harness_edge": "#EF6C00",
    "coord_face": "#FFE0B2", "coord_edge": "#E65100",
    "spec_face": "#C8E6C9", "spec_edge": "#2E7D32",
    "risk_face": "#FFCDD2", "risk_edge": "#C62828",
    "wf_face": "#E1BEE7", "wf_edge": "#6A1B9A",
    "strip_face": "#FFF8E1", "strip_edge": "#BCAAA4",
    "tool_face": "#E8EAF6", "tool_edge": "#3949AB",
    "persist_face": "#E0F7FA", "persist_edge": "#00838F",
    "ext_face": "#FBE9E7", "ext_edge": "#D84315",
    "ink": "#263238", "gray": "#546E7A",
    "arrow": "#37474F",
}

# ---------- 工具函数 ----------

_BOXES = []  # (x, y, w, h, label, texts)


def box(x, y, w, h, title, body="", face="#FFFFFF", edge="#90A4AE",
        title_color="#FFFFFF", title_fs=9.4, body_fs=7.2, lw=1.3, zorder=3,
        body_dy=0.0):
    p = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.22,rounding_size=0.7",
        linewidth=lw, edgecolor=edge, facecolor=face, zorder=zorder,
    )
    ax.add_patch(p)
    texts = []
    if title:
        t = ax.text(x + 0.5, y + h - 0.6, title, fontsize=title_fs, fontweight="bold",
                    color=title_color, va="top", ha="left", zorder=zorder + 1, linespacing=1.25)
        texts.append(t)
    if body:
        by = (y + h - 1.35 + body_dy) if title else (y + h - 0.75 + body_dy)
        t = ax.text(x + 0.5, by, body, fontsize=body_fs, color=C["ink"], va="top",
                    ha="left", zorder=zorder + 1, linespacing=1.42)
        texts.append(t)
    _BOXES.append((x, y, w, h, title or body, texts))


def arrow(p1, p2, color=C["arrow"], lw=1.5, style="-|>", dashed=False,
          rad=0.0, zorder=6, ms=10, shrink=1.0):
    a = FancyArrowPatch(
        p1, p2, arrowstyle=style, mutation_scale=ms, linewidth=lw, color=color,
        linestyle=(0, (4, 2)) if dashed else "solid",
        connectionstyle=f"arc3,rad={rad}", zorder=zorder,
        shrinkA=shrink, shrinkB=shrink,
    )
    ax.add_patch(a)


def label(x, y, text, fs=6.6, color=C["gray"], ha="center", va="center", rot=0, weight="normal"):
    ax.text(x, y, text, fontsize=fs, color=color, ha=ha, va=va, rotation=rot,
            fontweight=weight, zorder=8, linespacing=1.3)


def lane_chip(x, y, h, text, face, edge):
    w = max(2.7, 0.36 * len(text))
    p = FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.15,rounding_size=0.5",
        linewidth=1.1, edgecolor=edge, facecolor=face, zorder=3,
    )
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, fontsize=8.6, fontweight="bold", color=edge,
            ha="center", va="center", zorder=4)

# =====================================================================
# 0) 标题
# =====================================================================
ax.text(50, 68.6, "aquant —— 量化研究多 Agent 架构图", fontsize=20,
        fontweight="bold", color="#263238", ha="center", va="center", zorder=9)
ax.text(50, 66.9, "确定性外层 Workflow（解析 / 权限 / 预算 / 审计 / 终止）+ 动态内层专家 Agent · 本地优先 Windows 桌面应用",
        fontsize=9.5, color=C["gray"], ha="center", va="center", zorder=9)
ax.text(97.5, 66.9, "v1 架构 · 运行态快照", fontsize=7.5, color=C["gray"],
        ha="right", va="center", style="italic")

# =====================================================================
# ① 桌面端 (Electron + React)
# =====================================================================
lane_chip(0.6, 58.6, 6.6, "① 桌面端", C["ui_face"], C["ui_edge"])
box(5, 58.6, 38, 6.6,
    "React Renderer（沙箱渲染进程）",
    "三栏“研房”工作台：\n· 报告输入 + 解析确认预览\n· 运行工作台（消息 / 证据 / 子流程 / 图表）\n· 历史回放 / Agent 治理 / 提示词工作台 / 连接设置\n无 Node 权限 · CSP · 外链仅凭证无关 HTTPS",
    face=C["ui_face"], edge=C["ui_edge"], title_color="#0D47A1", body_fs=7.1)
box(45, 58.6, 14, 6.6,
    "Preload (contextIsolation)",
    "仅暴露白名单桥：\nrequest() · onEvent()\nonCrash() · platform",
    face="#E8EAF6", edge=C["ui_edge"], title_color="#0D47A1", body_fs=7.0, title_fs=9.0)
box(61, 58.6, 34, 6.6,
    "Electron Main 进程",
    "窗口生命周期 · sidecar 进程生命周期\nIPC 路由（校验 sender 可信来源）\nMarkdown 导出 · 权限默认拒绝",
    face=C["ui_face"], edge=C["ui_edge"], title_color="#0D47A1", body_fs=7.1)

# =====================================================================
# ② 进程协议
# =====================================================================
lane_chip(0.6, 55.9, 2.0, "② 进程协议", C["proto_face"], C["proto_edge"])
box(5, 55.9, 90, 2.0,
    "",
    "② 双向 JSONL 进程协议 —— 一行一个 JSON：request / response / event（protocol_version=1 · request_id · run_id+seq 稳定游标）· Python 日志只写 stderr",
    face=C["proto_face"], edge=C["proto_edge"], body_fs=7.6)

# =====================================================================
# ③ Python Sidecar 容器
# =====================================================================
lane_chip(0.6, 20.0, 34.3, "③ Python Sidecar", C["sidecar_face"], C["sidecar_edge"])
box(4.5, 20.0, 91, 34.3,
    "",
    "③ Python Sidecar · services/harness/src/quant_agent_harness —— 领域数据、编排、模型与工具的唯一执行面",
    face=C["sidecar_face"], edge=C["sidecar_edge"], body_fs=8.0, body_dy=0.55)

# --- 子行 A：协议 / 解析 / 模型 / 提示词 ---
box(6, 46.0, 21.5, 6.2,
    "ProtocolServer (server.py)",
    "RPC 方法分发：\nparse_report · start_run · pause/\nresume/cancel/steer · retry\nsettings · agents · prompts · workflows",
    face=C["sub_face"], edge=C["sub_edge"], title_color="#4A148C", body_fs=6.9)
box(29.5, 46.0, 18.5, 6.2,
    "Parser (parser.py)",
    "确定性解析 PTrade 文本\n→ valid（可运行）\n→ partial（需人工确认）\n→ invalid（禁止运行）",
    face=C["sub_face"], edge=C["sub_edge"], title_color="#4A148C", body_fs=6.9)
box(50, 46.0, 20, 6.2,
    "LLM 客户端 (llm.py)",
    "OpenAI 兼容 Chat Completions\n强制 JSON 输出 · 低温度\n失败自动回退确定性结果\n（DeepSeek 关闭 thinking）",
    face=C["sub_face"], edge=C["sub_edge"], title_color="#4A148C", body_fs=6.9)
box(72, 46.0, 21, 6.2,
    "Agent Prompts (agent_prompts.py)",
    "不可变平台安全策略 + 角色提示词\n（统筹规划/审阅/综合、量化、\n公司行业、外围市场、风险）\n草稿 → 发布 → 历史 → 回滚（版本化）",
    face=C["sub_face"], edge=C["sub_edge"], title_color="#4A148C", body_fs=6.9)

# --- Harness 编排核心 ---
box(6, 20.5, 60.5, 24.1,
    "",
    "Harness 编排核心 (harness.py) —— 外层确定性 workflow：CapabilityRegistry 能力注册 · RunControl 暂停/取消/插话 · RunPolicy 预算(max_rounds) · 重复任务过滤 · Agent 白名单",
    face=C["harness_face"], edge=C["harness_edge"], body_fs=7.3, body_dy=0.65)

box(8, 36.0, 22, 6.6,
    "① 统筹 Agent（coordinator）",
    "planning：读报告、按能力注册表组队\nreview：检查矛盾 / 重要信息 / 证据缺口\nsynthesis：吸收复核意见形成研究解读\n（不输出交易建议）",
    face=C["coord_face"], edge=C["coord_edge"], title_color="#BF360C", body_fs=6.7)
box(32, 36.0, 14, 6.6,
    "② 量化信号 Agent",
    "确定性规则子流程：\n身份库 → 正式池 + P1/P2/P3\n候选 → 稳定性记忆 → 解释",
    face=C["spec_face"], edge=C["spec_edge"], title_color="#1B5E20", body_fs=6.7)
box(48, 36.0, 16, 6.6,
    "② 公司与行业 Agent",
    "Tushare 公司/财务/估值\n巨潮公告 · 东财新闻与研报\nTavily 行业背景补充",
    face=C["spec_face"], edge=C["spec_edge"], title_color="#1B5E20", body_fs=6.7)

box(8, 27.0, 14, 6.6,
    "② 外围市场 Agent",
    "交易日口径（美早于/韩日不晚于报告日）\n美/韩/日核心指数 → 标准化涨跌 → 图表",
    face=C["spec_face"], edge=C["spec_edge"], title_color="#1B5E20", body_fs=6.6)
box(24, 27.0, 16, 6.6,
    "③ 风险 Agent",
    "逐票负面公告/新闻检索\n报告日过滤 → 利空分类总结\n（关键词 + 来源证据）",
    face=C["risk_face"], edge=C["risk_edge"], title_color="#B71C1C", body_fs=6.7)
box(42, 27.0, 16, 6.6,
    "子工作流 (workflows.py)",
    "quant-signal-subgraph\nglobal-market-subgraph\nnegative-news-risk-subgraph\n（workflow.plan/node 事件）",
    face=C["wf_face"], edge=C["wf_edge"], title_color="#4A148C", body_fs=6.6)

box(8, 21.0, 56, 4.6,
    "④ 运行状态机与事件审计",
    "draft → parsing → review_required → planning → specialists_running → risk_review → synthesizing → completed | failed | cancelled\n统筹审阅发现矛盾/重要信息/证据缺口 → 追问白名单内现有 Agent（去重 · max_rounds 封顶）；每个节点发 HarnessEvent(run_id+seq) → 落库 + 实时推 UI",
    face=C["strip_face"], edge=C["strip_edge"], title_color="#795548", body_fs=6.7)

# --- 只读数据源客户端 ---
box(68.5, 20.5, 25.5, 24.1,
    "",
    "只读数据源客户端（工具层）",
    face=C["tool_face"], edge=C["tool_edge"], body_fs=8.0, body_dy=0.65)
box(70.5, 38.5, 21.5, 5.0,
    "GlobalMarketClient",
    "Yahoo(主) → 腾讯/东财/新浪(镜像)\n延迟行情 · 超 12% 双源复核 · demo 兜底\n← 外围市场 Agent",
    face=C["tool_face"], edge=C["tool_edge"], title_color="#283593", body_fs=6.5)
box(70.5, 32.5, 21.5, 5.0,
    "PublicAStockClient",
    "巨潮公告 · 东方财富新闻/研报（免密钥）\n只读 · 限时 · 部分失败继续\n← 公司与行业 / 风险 Agent",
    face=C["tool_face"], edge=C["tool_edge"], title_color="#283593", body_fs=6.5)
box(70.5, 26.5, 21.5, 5.0,
    "TushareClient",
    "公司信息 · 财务指标 · 估值 · 业绩预告\n部分成功允许 · 失败项入 unknowns\n← 公司与行业 Agent",
    face=C["tool_face"], edge=C["tool_edge"], title_color="#283593", body_fs=6.5)
box(70.5, 21.0, 21.5, 4.5,
    "TavilyClient",
    "行业 / 负面联网检索（受控额度）\n保留原始来源链接\n← 公司与行业 / 风险 Agent",
    face=C["tool_face"], edge=C["tool_edge"], title_color="#283593", body_fs=6.5)

# =====================================================================
# ④ 持久化
# =====================================================================
lane_chip(0.6, 8.0, 9.8, "④ 持久化", C["persist_face"], C["persist_edge"])
box(4.5, 8.0, 91, 9.8,
    "",
    "④ 持久化 —— SQLite · Windows DPAPI · 本地向量",
    face=C["persist_face"], edge=C["persist_edge"], body_fs=8.0, body_dy=0.55)
box(6, 9.8, 46, 6.2,
    "Repository (repository.py) —— quant-agent.sqlite",
    "reports（原文/哈希/解析状态）· runs（状态/综合）· events（run_id+seq 全轨迹）\nsettings · agent_configs · prompt_templates / prompt_versions\nsecurity_master / security_name_history · signal_observations\nknowledge_documents / knowledge_chunks",
    face="#E0F2F1", edge=C["persist_edge"], title_color="#00695C", body_fs=6.5)
box(54, 9.8, 12, 6.2,
    "secret_store.py",
    "Windows DPAPI 当前用户加密\n只存密文 · 不落日志/导出\n界面仅返回“是否已配置”",
    face="#E0F2F1", edge=C["persist_edge"], title_color="#00695C", body_fs=6.5)
box(68, 9.8, 12, 6.2,
    "local_knowledge.py",
    "本地向量知识库：\n检索 → 内容哈希去重 → 切块\n→ 向量化 → 混合召回\n（可换 Ollama/向量库）",
    face="#E0F2F1", edge=C["persist_edge"], title_color="#00695C", body_fs=6.4)

# =====================================================================
# ⑤ 外部只读依赖
# =====================================================================
lane_chip(0.6, 1.8, 5.0, "⑤ 外部依赖", C["ext_face"], C["ext_edge"])
box(4.5, 1.8, 91, 5.0,
    "",
    "⑤ 外部只读依赖：OpenAI 兼容模型 API · Tushare · Tavily · 巨潮资讯 / 东方财富（公开网页）· Yahoo Finance（主）→ 腾讯 / 东财 / 新浪（镜像）\n无下单 / 写盘 / Shell 工具 · 密钥 DPAPI 加密 · 输出仅研究解读与风险提示",
    face=C["ext_face"], edge=C["ext_edge"], body_fs=7.3, body_dy=0.45)

# =====================================================================
# 箭头
# =====================================================================
# 桌面端内部
arrow((43, 61.9), (45, 61.9), style="<|-|>", ms=8)
arrow((59, 61.9), (61, 61.9), style="<|-|>", ms=8)

# Main ↔ ProtocolServer（跨协议层）
arrow((78, 58.6), (16.75, 52.2), style="<|-|>", rad=0.12, lw=1.8)
label(58, 55.1, "JSONL stdio 双向 · 事件实时上行", fs=7.0)

# 子行 A 之间
arrow((27.5, 49.1), (29.5, 49.1))
arrow((16.75, 46.0), (16.75, 44.8), lw=1.6)
label(12.6, 45.4, "start_run / pause\nresume / cancel / steer", fs=6.2, ha="right")
arrow((38.75, 46.0), (38.75, 44.8), lw=1.6)
label(40.1, 45.4, "valid/partial 报告", fs=6.2, ha="left")
arrow((60, 46.0), (58, 44.8), lw=1.4)
label(61.6, 45.4, "complete_json（严格 JSON · 失败回退）", fs=6.2, ha="left")
arrow((82.5, 46.0), (64, 44.8), dashed=True, rad=-0.1, lw=1.3)
label(74, 45.4, "角色提示词（平台策略+角色，版本化）", fs=6.2)

# 事件流（Harness → 协议层 → UI）
arrow((66, 44.7), (71, 44.7), dashed=True, lw=1.3)
arrow((71, 44.7), (71, 56.1), dashed=True, lw=1.3)
label(71.6, 51.0, "事件流 event（run_id+seq）实时推送 UI", fs=6.2, rot=90)

# Harness 内部：统筹 → 派单
arrow((30, 39.3), (31.8, 39.3), lw=1.5)
label(31, 38.9, "规划组队", fs=6.2, va="top")
# 并行分发总线
arrow((19, 36.0), (19, 35.4), lw=1.4)
arrow((19, 35.4), (39, 35.4), lw=1.4)
arrow((39, 35.4), (39, 35.85), lw=1.4)
arrow((19, 35.4), (56, 35.4), lw=1.4)
arrow((56, 35.4), (56, 35.85), lw=1.4)
arrow((19, 35.4), (15, 35.4), lw=1.4)
arrow((15, 35.4), (15, 33.8), lw=1.4)
label(39, 35.65, "并行派单（ThreadPoolExecutor）", fs=6.2, va="bottom")
# 统筹审阅后 → 风险
arrow((12, 36.0), (12, 34.6), lw=1.4)
arrow((12, 34.6), (32, 34.6), lw=1.4)
arrow((32, 34.6), (32, 33.8), lw=1.4)
label(22, 34.85, "统筹审阅后：风险 Agent 逐票检索", fs=6.2, va="bottom")
# 风险 → 状态条（二次审阅）
arrow((32, 27.0), (32, 25.8), lw=1.4)
label(33.4, 26.4, "结果回统筹二次审阅（可追问）", fs=6.2, ha="left")

# Harness → 工具列
arrow((66.6, 32.5), (68.4, 32.5), lw=2.2)
label(67.5, 33.3, "只读工具调用", fs=6.4)

# 工具列 → 外部依赖
arrow((81.25, 20.5), (81.25, 7.0), dashed=True, lw=1.5)
label(82.0, 13.0, "外部网络（只读）", fs=6.2, rot=90)

# Harness → Repository
arrow((36.25, 20.5), (36.25, 16.2), lw=1.6)
label(37.5, 18.5, "运行轨迹 / 事件 / 报告 落库", fs=6.2, ha="left")

# Repository ↔ secret_store / local_knowledge
arrow((52, 13.5), (53.8, 13.5), style="<|-|>", lw=1.2, ms=8)
label(53, 14.3, "secrets 加解密", fs=5.8)
arrow((30, 9.8), (30, 7.7), dashed=True, lw=1.2)
arrow((30, 7.7), (73, 7.7), dashed=True, lw=1.2)
arrow((73, 7.7), (73, 9.6), dashed=True, lw=1.2)
label(51.5, 7.2, "本地向量知识库读写", fs=5.8)

# =====================================================================
# 排版自检：文本是否超出盒子、盒子是否互相重叠
# =====================================================================
fig.canvas.draw()
renderer = fig.canvas.get_renderer()
inv = ax.transData.inverted()
problems = []

for bx, by, bw, bh, blabel, texts in _BOXES:
    for t in texts:
        ext = t.get_window_extent(renderer=renderer)
        (x0, y0), (x1, y1) = inv.transform([(ext.x0, ext.y0), (ext.x1, ext.y1)])
        tol = 0.35
        if x0 < bx - tol or x1 > bx + bw + tol or y0 < by - tol or y1 > by + bh + tol:
            problems.append(f"[超出盒子] {blabel!r}: 文本范围 x[{x0:.1f},{x1:.1f}] y[{y0:.1f},{y1:.1f}] vs 盒子 x[{bx},{bx+bw}] y[{by},{by+bh}]")

# 盒子重叠（排除容器与其子盒子的合法包含关系）
def _rect_overlap(a, b, tol=0.3):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    if ax1 - tol <= bx0 or bx1 - tol <= ax0 or ay1 - tol <= by0 or by1 - tol <= ay0:
        return False
    # 完全包含不算冲突（容器包含子盒子是设计意图）
    if ax0 <= bx0 and ay0 <= by0 and ax1 >= bx1 and ay1 >= by1:
        return False
    if bx0 <= ax0 and by0 <= ay0 and bx1 >= ax1 and by1 >= ay1:
        return False
    return True

rects = [(x, y, x + w, y + h, blabel) for x, y, w, h, blabel, _ in _BOXES]
for i in range(len(rects)):
    for j in range(i + 1, len(rects)):
        if _rect_overlap(rects[i][:4], rects[j][:4]):
            problems.append(f"[盒子重叠] {rects[i][4]!r} ↔ {rects[j][4]!r}")

if problems:
    print("发现排版问题:")
    for p in problems:
        print(" -", p)
else:
    print("排版自检通过：所有文本均在盒子内，盒子之间无意外重叠。")

fig.savefig(r"E:\quant-agent\docs\agent-architecture.png", facecolor="white")
plt.close(fig)
print("saved docs/agent-architecture.png")
