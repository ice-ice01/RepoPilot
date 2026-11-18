"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/auth-provider";
import { apiDelete, apiGet, apiPost } from "@/lib/api";

type AuthSession = {
  id: string;
  created_at: string | null;
  expires_at: string | null;
  user_agent: string | null;
  current: boolean;
};

type AdminUser = {
  id: string;
  username: string;
  name: string;
  role: string;
  is_active: boolean;
  active_sessions: number;
};

function describeAgent(userAgent: string | null): string {
  if (!userAgent) return "未知设备";
  if (/iphone|ipad/i.test(userAgent)) return "iOS 设备";
  if (/android/i.test(userAgent)) return "Android 设备";
  if (/macintosh/i.test(userAgent)) return "macOS 浏览器";
  if (/windows/i.test(userAgent)) return "Windows 浏览器";
  if (/linux/i.test(userAgent)) return "Linux 浏览器";
  return userAgent.slice(0, 60);
}

function formatTime(value: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function AccountPage() {
  const { user, refresh } = useAuth();
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [sessions, setSessions] = useState<AuthSession[]>([]);
  const [adminUsers, setAdminUsers] = useState<AdminUser[]>([]);
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [resetPassword, setResetPassword] = useState("");

  const loadSessions = useCallback(async () => {
    try {
      setSessions(await apiGet<AuthSession[]>("/api/auth/sessions"));
    } catch {
      setSessions([]);
    }
  }, []);

  const loadAdminUsers = useCallback(async () => {
    if (!user.is_admin) return;
    try {
      setAdminUsers(await apiGet<AdminUser[]>("/api/auth/users"));
    } catch {
      setAdminUsers([]);
    }
  }, [user.is_admin]);

  useEffect(() => {
    loadSessions();
    loadAdminUsers();
  }, [loadSessions, loadAdminUsers]);

  async function submit(event: FormEvent<HTMLFormElement>, createUser: boolean) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setPending(true); setError(""); setMessage("");
    try {
      if (createUser) {
        await apiPost("/api/auth/users", Object.fromEntries(data));
        setMessage("账号已创建。新用户可使用自己的用户名和密码登录，项目数据独立保存。");
        await loadAdminUsers();
      } else {
        await apiPost("/api/auth/password", Object.fromEntries(data));
        await refresh();
        await loadSessions();
        setMessage("密码已更新，其他已登录会话全部失效。");
      }
      form.reset();
    } catch (err) { setError(err instanceof Error ? err.message : "操作失败"); }
    finally { setPending(false); }
  }

  async function revokeSession(id: string) {
    setError(""); setMessage("");
    try {
      await apiDelete(`/api/auth/sessions/${id}`);
      setMessage("该会话已退出。");
      await loadSessions();
    } catch (err) { setError(err instanceof Error ? err.message : "操作失败"); }
  }

  async function submitReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!resetTarget) return;
    if (resetPassword.length < 12) {
      setError("新密码至少 12 个字符。");
      return;
    }
    setPending(true); setError(""); setMessage("");
    try {
      const result = await apiPost<{ revoked_sessions: number }>(`/api/auth/users/${resetTarget.id}/password`, {
        new_password: resetPassword
      });
      setMessage(`已重置 ${resetTarget.username} 的密码，其 ${result.revoked_sessions} 个会话已全部失效。`);
      setResetTarget(null);
      setResetPassword("");
      await loadAdminUsers();
      await loadSessions();
    } catch (err) { setError(err instanceof Error ? err.message : "操作失败"); }
    finally { setPending(false); }
  }

  const inputClass = "mt-1 w-full rounded-lg border border-line px-3 py-2";
  return <div className="mx-auto max-w-2xl space-y-6 py-8">
    <div><h1 className="text-2xl font-semibold text-ink">账号设置</h1><p className="mt-2 text-sm text-slate-500">{user.username} · {user.role}</p></div>
    {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
    {message && <p role="status" className="rounded-lg bg-teal-50 p-3 text-sm text-teal-800">{message}</p>}
    <form onSubmit={(event) => void submit(event, false)} className="space-y-4 rounded-xl border border-line bg-white p-6">
      <h2 className="font-semibold text-ink">修改密码</h2>
      <input type="hidden" name="username" value={user.username} autoComplete="username" />
      <label className="block text-sm">当前密码<input name="current_password" type="password" autoComplete="current-password" required maxLength={128} className={inputClass} /></label>
      <label className="block text-sm">新密码<input name="new_password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} className={inputClass} placeholder="至少 12 个字符" /></label>
      <button disabled={pending} className="rounded-lg bg-teal-700 px-4 py-2 text-white disabled:opacity-50">更新密码</button>
    </form>

    <section className="space-y-4 rounded-xl border border-line bg-white p-6">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold text-ink">登录会话</h2>
        <button onClick={() => void loadSessions()} className="text-xs font-medium text-teal-700 hover:underline">刷新</button>
      </div>
      <p className="text-sm text-slate-500">当前账号的全部有效会话；可疑或不再使用的设备可立即踢出。</p>
      <div className="space-y-2">
        {sessions.map((session) => (
          <div key={session.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2 text-sm">
            <div>
              <span className="font-medium text-ink">{describeAgent(session.user_agent)}</span>
              {session.current && <span className="ml-2 rounded-full bg-teal-50 px-2 py-0.5 text-xs text-teal-800">当前会话</span>}
              <div className="mt-1 text-xs text-slate-500">登录：{formatTime(session.created_at)} · 过期：{formatTime(session.expires_at)}</div>
            </div>
            {!session.current && (
              <button onClick={() => void revokeSession(session.id)} className="rounded-md border border-red-200 px-2.5 py-1 text-xs font-medium text-red-700 transition hover:bg-red-50">踢出</button>
            )}
          </div>
        ))}
        {sessions.length === 0 && <p className="text-sm text-slate-500">暂无其他有效会话。</p>}
      </div>
    </section>

    {user.is_admin && <section className="space-y-3 rounded-xl border border-line bg-white p-6">
      <h2 className="font-semibold text-ink">用户管理</h2>
      <div className="space-y-2">
        {adminUsers.map((item) => (
          <div key={item.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2 text-sm">
            <div>
              <span className="font-medium text-ink">{item.username}</span>
              <span className="ml-2 rounded-full bg-panel px-2 py-0.5 text-xs text-slate-500">{item.role}</span>
              <span className="ml-1 text-xs text-slate-400">{item.active_sessions} 个活跃会话</span>
            </div>
            <button onClick={() => { setResetTarget(item); setResetPassword(""); }} className="rounded-md border border-line px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:bg-panel">重置密码</button>
          </div>
        ))}
      </div>
      {resetTarget && (
        <form onSubmit={submitReset} className="space-y-3 rounded-lg border border-teal-100 bg-teal-50/60 p-4">
          <div className="text-sm font-medium text-teal-900">重置 {resetTarget.username} 的密码</div>
          <input
            type="password"
            value={resetPassword}
            onChange={(event) => setResetPassword(event.target.value)}
            placeholder="新密码，至少 12 个字符"
            minLength={12}
            maxLength={128}
            required
            className="w-full rounded-lg border border-line px-3 py-2"
            autoComplete="new-password"
          />
          <div className="flex gap-2">
            <button type="submit" disabled={pending} className="rounded-lg bg-teal-700 px-4 py-2 text-sm text-white disabled:opacity-50">确认重置</button>
            <button type="button" onClick={() => setResetTarget(null)} className="rounded-lg border border-line px-4 py-2 text-sm text-slate-600">取消</button>
          </div>
          <p className="text-xs text-slate-500">重置后该账号全部会话立即失效，需要用新密码重新登录。</p>
        </form>
      )}
    </section>}

    {user.is_admin && <form onSubmit={(event) => void submit(event, true)} className="space-y-4 rounded-xl border border-line bg-white p-6">
      <h2 className="font-semibold text-ink">创建用户</h2>
      <p className="text-sm text-slate-500">角色控制可用操作；各账号的项目、会话、知识库和评测相互隔离。</p>
      <label className="block text-sm">用户名<input name="username" autoComplete="off" required minLength={3} maxLength={80} className={inputClass} /></label>
      <label className="block text-sm">显示名称<input name="name" maxLength={200} className={inputClass} /></label>
      <label className="block text-sm">初始密码<input name="password" type="password" autoComplete="new-password" required minLength={12} maxLength={128} className={inputClass} /></label>
      <label className="block text-sm">角色<select name="role" defaultValue="developer" className={inputClass}>
        <option value="developer">developer · 对话、分析与创建草稿</option><option value="maintainer">maintainer · 同步仓库与确认草稿</option>
        <option value="viewer">viewer · 只读</option><option value="owner">owner · 自有项目的全部操作</option>
      </select></label>
      <button disabled={pending} className="rounded-lg bg-teal-700 px-4 py-2 text-white disabled:opacity-50">创建用户</button>
    </form>}
  </div>;
}
