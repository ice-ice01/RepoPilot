"use client";

import { createContext, FormEvent, useCallback, useContext, useEffect, useRef, useState } from "react";
import { ApiError, apiGet, apiPost } from "@/lib/api";

export type AuthUser = { id: string; username: string; name: string; role: string; is_admin: boolean };
type AuthStatus = { setup_required: boolean; registration_enabled: boolean; bootstrap_token_required: boolean };
type AuthContextValue = { user: AuthUser; logout: () => Promise<void>; refresh: () => Promise<void> };
const AuthContext = createContext<AuthContextValue | null>(null);

async function currentUser(silent = false): Promise<AuthUser> {
  try { return await apiGet<AuthUser>("/api/auth/me"); }
  catch (err) {
    if (!silent && err instanceof ApiError && err.status === 401) {
      window.dispatchEvent(new Event("devflow:session-expired"));
    }
    throw err;
  }
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("Authentication is required");
  return value;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [registering, setRegistering] = useState(false);
  const [pending, setPending] = useState(false);
  const authChannel = useRef<BroadcastChannel | null>(null);
  const authVersion = useRef(0);

  const loadSession = useCallback(async () => {
    const version = ++authVersion.current;
    setLoading(true);
    setError("");
    try {
      const authStatus = await apiGet<AuthStatus>("/api/auth/status");
      if (version !== authVersion.current) return;
      setStatus(authStatus);
      setRegistering(authStatus.setup_required);
      try {
        const current = await currentUser(true);
        if (version === authVersion.current) setUser(current);
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 401) throw err;
        if (version === authVersion.current) setUser(null);
      }
    } catch (err) {
      if (version === authVersion.current) setError(err instanceof Error ? err.message : "无法连接后端服务");
    } finally {
      if (version === authVersion.current) setLoading(false);
    }
  }, []);

  useEffect(() => { void loadSession(); }, [loadSession]);
  useEffect(() => {
    const expired = () => {
      ++authVersion.current;
      setUser(null); setLoading(false); setError("登录已失效，请重新登录。");
    };
    window.addEventListener("devflow:session-expired", expired);
    return () => window.removeEventListener("devflow:session-expired", expired);
  }, []);
  useEffect(() => {
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("devflow-auth");
    authChannel.current = channel;
    if (channel) channel.onmessage = () => { setUser(null); void loadSession(); };
    return () => { channel?.close(); authChannel.current = null; };
  }, [loadSession]);
  useEffect(() => {
    // Catch an account change or expiry even if another tab was suspended.
    const focused = () => {
      if (!user) return;
      const version = authVersion.current;
      void currentUser().then((current) => {
        if (version === authVersion.current) setUser(current);
      }).catch(() => {});
    };
    window.addEventListener("focus", focused);
    return () => window.removeEventListener("focus", focused);
  }, [user]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const version = ++authVersion.current;
    setPending(true);
    setError("");
    try {
      const body = { username: String(data.get("username") ?? ""), password: String(data.get("password") ?? ""),
        ...(registering ? { name: String(data.get("name") ?? "") } : {}) };
      const bootstrapToken = String(data.get("bootstrap_token") ?? "");
      const authenticated = await apiPost<AuthUser>(`/api/auth/${registering ? "register" : "login"}`, body,
        bootstrapToken ? { headers: { "X-Bootstrap-Token": bootstrapToken } } : {});
      if (version !== authVersion.current) return;
      setUser(authenticated);
      setStatus((old) => old ? { ...old, setup_required: false } : old);
      setRegistering(false);
      authChannel.current?.postMessage("changed");
    } catch (err) {
      if (version === authVersion.current) setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setPending(false);
    }
  }

  async function logout() {
    ++authVersion.current;
    try {
      await apiPost("/api/auth/logout", {});
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 401) throw err;
    }
    setUser(null);
    setError("");
    authChannel.current?.postMessage("changed");
  }

  if (loading) return <main className="grid min-h-screen place-items-center text-slate-500">正在验证登录状态…</main>;
  if (user) {
    return <AuthContext.Provider value={{ user, logout, refresh: async () => {
      const version = ++authVersion.current;
      const current = await currentUser();
      if (version !== authVersion.current) return;
      setUser(current);
      authChannel.current?.postMessage("changed");
    } }}>
      <div key={user.id}>{children}</div>
    </AuthContext.Provider>;
  }
  return <main className="grid min-h-screen place-items-center bg-slate-50 px-4 py-12">
    <section className="w-full max-w-md rounded-2xl border border-line bg-white p-8 shadow-sm">
      <p className="text-sm font-semibold text-teal-700">研伴 RepoPilot</p>
      <h1 className="mt-3 text-2xl font-semibold text-ink">{status?.setup_required ? "创建管理员账号" : registering ? "创建账号" : "登录工作区"}</h1>
      <p className="mt-2 text-sm text-slate-500">{status?.setup_required
        ? "首次初始化后，已有项目、会话和评测将归属这个管理员账号。"
        : "登录后仅能访问属于自己账号的项目与会话。"}</p>
      {error && <p role="alert" className="mt-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {!status ? <button onClick={() => void loadSession()} className="mt-5 rounded-lg bg-teal-700 px-4 py-2 text-white">重试连接</button>
        : <form onSubmit={submit} className="mt-6 space-y-4">
          <label className="block text-sm text-slate-700">用户名
            <input name="username" autoComplete="username" minLength={3} maxLength={80} pattern="[A-Za-z0-9][A-Za-z0-9_.@\-]*" required
              className="mt-1 w-full rounded-lg border border-line px-3 py-2" placeholder="3–80 位字母、数字或 _.@-" />
          </label>
          {registering && <label className="block text-sm text-slate-700">显示名称
            <input name="name" autoComplete="name" maxLength={200} className="mt-1 w-full rounded-lg border border-line px-3 py-2" />
          </label>}
          <label className="block text-sm text-slate-700">密码
            <input key={registering ? "new-password" : "password"} name="password" type="password" autoComplete={registering ? "new-password" : "current-password"}
              minLength={registering ? 12 : 1} maxLength={128} required className="mt-1 w-full rounded-lg border border-line px-3 py-2"
              placeholder={registering ? "至少 12 个字符" : "输入密码"} />
          </label>
          {status.setup_required && status.bootstrap_token_required && <label className="block text-sm text-slate-700">初始化密钥
            <input name="bootstrap_token" type="password" required autoComplete="off" className="mt-1 w-full rounded-lg border border-line px-3 py-2" />
          </label>}
          <button type="submit" disabled={pending} className="w-full rounded-lg bg-teal-700 px-4 py-2.5 font-medium text-white disabled:opacity-50">
            {pending ? "正在处理…" : registering ? "创建并登录" : "登录"}
          </button>
          {!status.setup_required && status.registration_enabled && <button type="button" onClick={() => { setRegistering(!registering); setError(""); }}
            className="w-full text-sm text-teal-700">{registering ? "已有账号，返回登录" : "创建新账号"}</button>}
        </form>}
    </section>
  </main>;
}
