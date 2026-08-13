import { writeFile } from "node:fs/promises";

const port = Number(process.argv[2] || 9333);
const outputPath = process.argv[3];
if (!outputPath) throw new Error("缺少截图输出路径");

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function findPage() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json`).then((response) => response.json());
      const page = targets.find((target) => target.type === "page" && target.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      // Electron may still be opening the debugger socket.
    }
    await sleep(250);
  }
  throw new Error("未找到 Electron 页面调试目标");
}

const page = await findPage();
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let nextId = 1;
const pending = new Map();
socket.addEventListener("message", (event) => {
  const message = JSON.parse(String(event.data));
  if (!message.id) return;
  const request = pending.get(message.id);
  if (!request) return;
  pending.delete(message.id);
  if (message.error) request.reject(new Error(message.error.message));
  else request.resolve(message.result);
});

function command(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await command("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "页面脚本执行失败");
  return result.result.value;
}

async function waitForText(text, timeout = 10_000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (await evaluate(`document.body.innerText.includes(${JSON.stringify(text)})`)) return;
    await sleep(200);
  }
  throw new Error(`页面未在时限内出现文本: ${text}`);
}

await command("Page.enable");
await command("Runtime.enable");
if (!(await evaluate(`document.body.innerText.includes("粘贴一份 PTrade 原始报告")`))) {
  await evaluate(`Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("新建研究"))?.click()`);
}
await waitForText("粘贴一份 PTrade 原始报告");

const chineseReport = `生成时间: 2026-07-31 14:30:00
运行轮次: 中文输入测试
selected_head:
symbol name reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS 浦发银行 all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty`;

await evaluate(`(() => {
  const textarea = document.querySelector("textarea");
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  setter.call(textarea, ${JSON.stringify(chineseReport)});
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  return textarea.value;
})()`);
await evaluate(`Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("解析并预览"))?.click()`);
await waitForText("结构化预览");
await evaluate(`Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("启动多 Agent"))?.click()`);
await waitForText("完整流程");
await waitForText("风险 Agent");
await waitForText("外围指数温度", 45_000);
await waitForText("本轮已完成", 45_000);
await sleep(800);
await evaluate(`(() => {
  const scroller = document.querySelector(".conversation-scroll");
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
})()`);
await sleep(250);

const audit = await evaluate(`({
  replacementCharacters: (document.body.innerText.match(/�/g) || []).length,
  harnessChatMessages: Array.from(document.querySelectorAll(".message-meta b")).filter((node) => node.textContent === "Harness").length,
  coordinatorPlans: document.querySelectorAll(".plan-message").length,
  agentMessages: document.querySelectorAll(".message").length,
  globalMarketCards: document.querySelectorAll(".global-market-card").length,
  activeMarketEventAgents: document.body.innerText.includes("市场事件 Agent") ? 1 : 0,
  chineseInputRoundtrip: document.body.innerText.includes("浦发银行")
})`);
const screenshot = await command("Page.captureScreenshot", { format: "png", fromSurface: true });
await writeFile(outputPath, Buffer.from(screenshot.data, "base64"));

const extensionIndex = outputPath.toLowerCase().lastIndexOf(".png");
const basePath = extensionIndex >= 0 ? outputPath.slice(0, extensionIndex) : outputPath;
const runsPath = `${basePath}-runs.png`;
const agentsPath = `${basePath}-agents.png`;
const promptsPath = `${basePath}-prompts.png`;
const marketPath = `${basePath}-global-market.png`;
await evaluate(`document.querySelector(".global-market-card")?.scrollIntoView({ block: "center" })`);
await sleep(250);
const marketScreenshot = await command("Page.captureScreenshot", { format: "png", fromSurface: true });
await writeFile(marketPath, Buffer.from(marketScreenshot.data, "base64"));
await evaluate(`Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("运行记录"))?.click()`);
await waitForText("累计运行");
await sleep(300);
const runsScreenshot = await command("Page.captureScreenshot", { format: "png", fromSurface: true });
await writeFile(runsPath, Buffer.from(runsScreenshot.data, "base64"));
await evaluate(`Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("Agent 管理"))?.click()`);
await waitForText("项目级附加要求");
await sleep(300);
const agentsScreenshot = await command("Page.captureScreenshot", { format: "png", fromSurface: true });
await writeFile(agentsPath, Buffer.from(agentsScreenshot.data, "base64"));
await evaluate(`Array.from(document.querySelectorAll("button")).find((button) => button.textContent.includes("Prompt 工作台"))?.click()`);
await waitForText("Prompt 模板");
await waitForText("独立子工作流");
await sleep(300);
const promptsScreenshot = await command("Page.captureScreenshot", { format: "png", fromSurface: true });
await writeFile(promptsPath, Buffer.from(promptsScreenshot.data, "base64"));
socket.close();
process.stdout.write(JSON.stringify({ ...audit, outputPath, marketPath, runsPath, agentsPath, promptsPath }, null, 2));
