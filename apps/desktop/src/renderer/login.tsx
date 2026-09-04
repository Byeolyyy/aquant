import { useState } from "react";

interface LoginScreenProps {
  onAuthed: () => void;
}

/**
 * 网页版的口令门。桌面版不会渲染到这里——它的 quantAgent.authState
 * 是 undefined，App 直接进正文。
 */
export function LoginScreen({ onAuthed }: LoginScreenProps) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!password.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      await window.quantAgent.login?.(password);
      onAuthed();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="eyebrow">aquant · 研房</div>
        <h1>凭口令入内</h1>
        <p>
          把 PTrade 盘中筛选结果，做成有证据、可追溯的研究简报。
          <br />
          这是一处演示环境，请输入访问口令。
        </p>
        <label>
          <span>访问口令</span>
          <input
            type="password"
            autoComplete="current-password"
            autoFocus
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="请输入访问口令"
          />
        </label>
        {error && <div className="login-error">{error}</div>}
        <button className="primary" type="submit" disabled={busy || !password.trim()}>
          {busy ? "正在核对…" : "进入研房"}
        </button>
        <div className="login-foot">输出为研究解读与风险提示，不构成买卖或仓位建议。</div>
      </form>
    </div>
  );
}
