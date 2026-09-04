// 复现并验证用户场景：登录 → 服务状态/邮件 tab/设置弹窗 → 服务端重启
// （会话作废）→ 应自动回登录门而不是卡"服务异常" → 重新登录 → 恢复。
// 用法：npx electron scripts/diag-browser.mjs
import { writeFile } from "node:fs/promises";
import { app, BrowserWindow, session } from "electron";

const TARGET = "http://1.14.160.122:8080";
// 口令从环境变量读取（公开仓库不落凭证）。
const PASSWORD = process.env.QUANT_AGENT_ACCESS_PASSWORD || "";

// 兜底：任何一步卡住 90 秒就退出。
setTimeout(() => {
  console.log("TIMEOUT: 诊断卡住，强制退出");
  app.exit(2);
}, 90000);

app.whenReady().then(async () => {
  const ses = session.defaultSession;
  const consoleErrors = [];
  const apiRequests = [];
  ses.webRequest.onBeforeSendHeaders((details, callback) => {
    if (details.url.includes("/api/")) {
      const cookieHeader = details.requestHeaders?.Cookie || details.requestHeaders?.["cookie"] || "";
      apiRequests.push({
        url: details.url.replace(TARGET, ""),
        hasCookie: Boolean(cookieHeader),
      });
    }
    callback({});
  });
  ses.webRequest.onErrorOccurred((details) => {
    if (details.error && !details.url.startsWith("data:")) {
      consoleErrors.push({ url: details.url.slice(0, 110), error: details.error });
    }
  });

  const win = new BrowserWindow({
    width: 1480,
    height: 940,
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });

  win.webContents.on("console-message", (event) => {
    consoleErrors.push(`[${event.level}] ${event.message}`);
  });

  const js = (code) => win.webContents.executeJavaScript(code);

  await win.loadURL(TARGET);
  await sleep(2200);

  const initial = await js(`({
    hasLogin: Boolean(document.querySelector(".login-card")),
    hasShell: Boolean(document.querySelector(".app-shell")),
  })`);
  console.log("0) 初载:", JSON.stringify(initial));

  // 登录：填值 + 点提交按钮（真实点击路径，与用户操作一致）
  const submitResult = await js(`
    (() => {
      const input = document.querySelector("form.login-card input[type=password]");
      const button = document.querySelector("form.login-card button[type=submit]");
      if (!input || !button) return "NO_INPUT_OR_BUTTON";
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
      setter.call(input, ${JSON.stringify(PASSWORD)});
      input.dispatchEvent(new Event("input", { bubbles: true }));
      button.click();
      return "CLICKED";
    })();
  `).catch((error) => "SCRIPT_ERROR: " + String(error));
  console.log("登录提交:", submitResult);
  await sleep(6000);

  const afterLogin = await js(`({
    hasLogin: Boolean(document.querySelector(".login-card")),
    hasShell: Boolean(document.querySelector(".app-shell")),
    serviceChip: document.querySelector(".service-chip")?.textContent?.trim() || null,
    serviceClass: document.querySelector(".service-chip")?.className || null,
    mailTab: [...document.querySelectorAll(".composer-tabs button")].map(b => b.textContent.trim()),
    activeTab: document.querySelector(".composer-tabs button.active")?.textContent?.trim() || null,
    pasteArea: Boolean(document.querySelector("textarea")),
    aqSession: window.__aqSession || null,
    aqGen: window.__aqGen ?? null,
    aqOnAuthed: window.__aqOnAuthed ?? null,
  })`);
  console.log("1) 登录后:", JSON.stringify(afterLogin));

  // 打开设置弹窗
  await js(`[...document.querySelectorAll(".topbar-actions button")].find(b => b.textContent.includes("连接与密钥"))?.click()`);
  await sleep(1200);
  const settings = await js(`({
    modalOpen: Boolean(document.querySelector(".settings-modal")),
    modalHeader: document.querySelector(".settings-modal header p")?.textContent?.trim() || null,
    disabledInputs: document.querySelectorAll(".settings-modal input:disabled").length,
    totalInputs: document.querySelectorAll(".settings-modal input").length,
    storageNote: document.querySelector(".storage-note b")?.textContent?.trim() || null,
  })`);
  console.log("2) 设置弹窗:", JSON.stringify(settings));

  // 关闭弹窗，等待"服务端重启 → 会话作废"的外部模拟（由外层 shell 控制），
  // 这里只轮询界面是否自动回到登录门，最多等 40 秒。
  await js(`document.querySelector(".modal-close")?.click()`);
  console.log("3) 等待服务端重启信号（最多 40 秒）…");
  let bounced = false;
  for (let i = 0; i < 40; i++) {
    await sleep(1000);
    const now = await js(`Boolean(document.querySelector(".login-card"))`);
    if (now) {
      bounced = true;
      break;
    }
  }
  console.log("4) 会话作废后自动回登录门:", bounced);

  await sleep(600);
  await win.webContents.capturePage().then((image) => writeFile("E:/quant-agent/.diag-recovery.png", image.toPNG()));
  console.log("5) 截图: .diag-recovery.png");
  console.log("6) 控制台错误:", consoleErrors.length, consoleErrors.slice(0, 5));
  console.log("7) API 请求明细:");
  for (const item of apiRequests) {
    console.log(`   ${item.hasCookie ? "[cookie] " : "[无cookie]"} ${item.url}`);
  }
  app.exit(bounced ? 0 : 3);
});

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
