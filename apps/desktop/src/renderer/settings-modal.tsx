import { useEffect, useState } from "react";

export interface SettingsData {
  model: {
    base_url: string;
    model: string;
    api_key_configured: boolean;
    ready: boolean;
  };
  tushare: { token_configured: boolean };
  tavily: { api_key_configured: boolean };
  storage: { database: string; secret_backend: string };
}

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
  onSaved: (settings: SettingsData) => void;
}

type IntegrationTarget = "model" | "tushare" | "tavily";

const SECRET_KEYS: Record<IntegrationTarget, string> = {
  model: "model.api_key",
  tushare: "tushare.token",
  tavily: "tavily.api_key",
};

export function SettingsModal({ open, onClose, onSaved }: SettingsModalProps) {
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [baseUrl, setBaseUrl] = useState("https://api.openai.com/v1");
  const [modelName, setModelName] = useState("");
  const [modelKey, setModelKey] = useState("");
  const [tushareToken, setTushareToken] = useState("");
  const [tavilyKey, setTavilyKey] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<{ type: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    setBusy("loading");
    setNotice(null);
    window.quantAgent
      .request("get_settings")
      .then((result) => {
        const value = result.settings as unknown as SettingsData;
        setSettings(value);
        setBaseUrl(value.model.base_url || "https://api.openai.com/v1");
        setModelName(value.model.model || "");
      })
      .catch((reason: Error) => setNotice({ type: "error", text: reason.message }))
      .finally(() => setBusy(""));
  }, [open]);

  if (!open) return null;

  async function persist(showNotice = true): Promise<SettingsData> {
    const result = await window.quantAgent.request("save_settings", {
      model: { base_url: baseUrl, model: modelName, api_key: modelKey },
      tushare: { token: tushareToken },
      tavily: { api_key: tavilyKey },
    });
    const value = result.settings as unknown as SettingsData;
    setSettings(value);
    setModelKey("");
    setTushareToken("");
    setTavilyKey("");
    onSaved(value);
    if (showNotice) setNotice({ type: "ok", text: "配置已安全保存并即时生效。" });
    return value;
  }

  async function save() {
    setBusy("save");
    setNotice(null);
    try {
      await persist();
    } catch (reason) {
      setNotice({ type: "error", text: reason instanceof Error ? reason.message : String(reason) });
    } finally {
      setBusy("");
    }
  }

  async function test(target: IntegrationTarget) {
    setBusy(`test-${target}`);
    setNotice(null);
    try {
      await persist(false);
      const result = await window.quantAgent.request("test_integration", { target });
      setNotice({ type: "ok", text: String(result.message || `${target} 连接成功`) });
    } catch (reason) {
      setNotice({ type: "error", text: reason instanceof Error ? reason.message : String(reason) });
    } finally {
      setBusy("");
    }
  }

  async function clearSecret(target: IntegrationTarget) {
    setBusy(`clear-${target}`);
    setNotice(null);
    try {
      const result = await window.quantAgent.request("save_settings", {
        model: { base_url: baseUrl, model: modelName },
        clear_secrets: [SECRET_KEYS[target]],
      });
      const value = result.settings as unknown as SettingsData;
      setSettings(value);
      onSaved(value);
      setNotice({ type: "ok", text: "密钥已清除。" });
    } catch (reason) {
      setNotice({ type: "error", text: reason instanceof Error ? reason.message : String(reason) });
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="settings-modal" role="dialog" aria-modal="true" aria-label="连接与密钥设置">
        <header className="settings-header">
          <div>
            <div className="eyebrow">Application Settings</div>
            <h2>连接与密钥</h2>
            <p>所有必需配置都在这里完成。密钥使用 Windows DPAPI 加密，应用不会显示已保存的明文。</p>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="关闭">×</button>
        </header>

        <div className="settings-scroll">
          <section className="settings-section">
            <div className="settings-section-head">
              <div className="settings-icon">M</div>
              <div><h3>统筹模型</h3><p>任意兼容 OpenAI Chat Completions 的服务。</p></div>
              <Status configured={Boolean(settings?.model.ready)} readyText="可以运行" configuredText="尚未完整配置" />
            </div>
            <div className="form-grid">
              <label className="wide"><span>Base URL</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" /></label>
              <label><span>模型名</span><input value={modelName} onChange={(event) => setModelName(event.target.value)} placeholder="例如 gpt-5-mini" /></label>
              <label>
                <span>API Key <SecretState configured={Boolean(settings?.model.api_key_configured)} /></span>
                <input type="password" autoComplete="new-password" value={modelKey} onChange={(event) => setModelKey(event.target.value)} placeholder={settings?.model.api_key_configured ? "已保存；留空表示不修改" : "输入 API Key"} />
              </label>
            </div>
            <div className="integration-actions">
              {settings?.model.api_key_configured && <button className="text-danger" disabled={Boolean(busy)} onClick={() => clearSecret("model")}>清除密钥</button>}
              <button className="ghost" disabled={Boolean(busy)} onClick={() => test("model")}>{busy === "test-model" ? "测试中…" : "保存并测试模型"}</button>
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section-head">
              <div className="settings-icon tushare-icon">T</div>
              <div><h3>Tushare</h3><p>供公司与行业 Agent 核验股票身份、行业、公司信息和每日指标。</p></div>
              <Status configured={Boolean(settings?.tushare.token_configured)} />
            </div>
            <div className="form-grid one-column">
              <label><span>Token <SecretState configured={Boolean(settings?.tushare.token_configured)} /></span><input type="password" autoComplete="new-password" value={tushareToken} onChange={(event) => setTushareToken(event.target.value)} placeholder={settings?.tushare.token_configured ? "已保存；留空表示不修改" : "输入 Tushare token"} /></label>
            </div>
            <div className="integration-actions">
              {settings?.tushare.token_configured && <button className="text-danger" disabled={Boolean(busy)} onClick={() => clearSecret("tushare")}>清除 token</button>}
              <button className="ghost" disabled={Boolean(busy)} onClick={() => test("tushare")}>{busy === "test-tushare" ? "测试中…" : "保存并测试 Tushare"}</button>
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section-head">
              <div className="settings-icon tavily-icon">W</div>
              <div><h3>Tavily Web Search</h3><p>供公司与行业 Agent 补充近期行业资料，结果带来源进入证据库。</p></div>
              <Status configured={Boolean(settings?.tavily.api_key_configured)} />
            </div>
            <div className="form-grid one-column">
              <label><span>API Key <SecretState configured={Boolean(settings?.tavily.api_key_configured)} /></span><input type="password" autoComplete="new-password" value={tavilyKey} onChange={(event) => setTavilyKey(event.target.value)} placeholder={settings?.tavily.api_key_configured ? "已保存；留空表示不修改" : "输入 Tavily API Key"} /></label>
            </div>
            <div className="integration-actions">
              {settings?.tavily.api_key_configured && <button className="text-danger" disabled={Boolean(busy)} onClick={() => clearSecret("tavily")}>清除密钥</button>}
              <button className="ghost" disabled={Boolean(busy)} onClick={() => test("tavily")}>{busy === "test-tavily" ? "测试中…" : "保存并测试 Tavily"}</button>
            </div>
          </section>

          <section className="storage-note">
            <div>🔒</div>
            <div><b>{settings?.storage.secret_backend || "Windows DPAPI"}</b><p>普通设置：{settings?.storage.database || "本地应用数据库"}</p></div>
          </section>
        </div>

        <footer className="settings-footer">
          <div>{notice && <span className={`settings-notice ${notice.type}`}>{notice.text}</span>}</div>
          <button className="ghost" onClick={onClose}>关闭</button>
          <button className="primary" disabled={Boolean(busy)} onClick={save}>{busy === "save" ? "正在保存…" : "保存全部配置"}</button>
        </footer>
      </div>
    </div>
  );
}

function Status({ configured, readyText = "已配置", configuredText = "未配置" }: { configured: boolean; readyText?: string; configuredText?: string }) {
  return <span className={`integration-status ${configured ? "configured" : "unconfigured"}`}><i />{configured ? readyText : configuredText}</span>;
}

function SecretState({ configured }: { configured: boolean }) {
  return <small className={configured ? "secret-saved" : "secret-missing"}>{configured ? "已加密保存" : "未保存"}</small>;
}
