"use client";

import { useEffect, useState } from "react";
import { Folder, Trash2 } from "lucide-react";

import { Card, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiDelete, apiGet, apiPost } from "@/lib/api";
import { API_BASE_URL } from "@/lib/api";
import type { Repository } from "@/lib/types";

type RepoConnectResult = {
  repo_id: string;
  full_name: string;
  provider: string;
  api_base_url?: string | null;
  local_path?: string | null;
  checkout_mode?: string;
  default_branch?: string | null;
  connected: boolean;
  demo_mode: boolean;
  message: string;
};

type RepoSource = "remote" | "local";

type RepoSyncResult = {
  repo_id: string;
  status: string;
  synced: {
    issues: number;
    pull_requests: number;
    workflow_runs: number;
  };
  skipped_unchanged?: {
    pull_requests?: number;
    workflow_runs?: number;
  };
};

type SyncRunInfo = {
  id: string;
  trigger: string;
  status: string;
  error: string | null;
  synced: Record<string, number>;
  skipped_unchanged: Record<string, number>;
  rate_limit: { remaining?: string; limit?: string; reset?: string } | null;
  started_at: string | null;
  finished_at: string | null;
};

type RateLimitInfo = {
  repo_id: string;
  core: { limit: number | null; remaining: number | null; reset: number | null };
  actions: { limit: number | null; remaining: number | null; reset: number | null };
};

type DeletedRepo = {
  repo_id: string;
  full_name: string;
  deleted_at: string;
  purges_at: string;
};

const providers = [
  { value: "github", label: "GitHub", hint: "默认平台，支持 public repo 和 Personal Access Token。" },
  { value: "github_compatible", label: "自定义 GitHub-compatible API", hint: "适合 GitHub Enterprise、Gitea 或兼容 GitHub REST 的网关。" },
  { value: "gitlab", label: "GitLab", hint: "界面已预留，真实同步需要单独适配 GitLab API。" },
  { value: "gitee", label: "Gitee", hint: "界面已预留，真实同步需要单独适配 Gitee API。" }
];

export default function ReposPage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState<string>("");
  const [source, setSource] = useState<RepoSource>("remote");
  const [provider, setProvider] = useState("github");
  const [owner, setOwner] = useState("");
  const [repo, setRepo] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [cloneParentDir, setCloneParentDir] = useState("");
  const [apiBaseUrl, setApiBaseUrl] = useState("https://api.github.com");
  const [token, setToken] = useState("");
  const [result, setResult] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [deletedRepos, setDeletedRepos] = useState<DeletedRepo[]>([]);
  const [healthRepoId, setHealthRepoId] = useState<string>("");
  const [syncRuns, setSyncRuns] = useState<SyncRunInfo[]>([]);
  const [rateLimit, setRateLimit] = useState<RateLimitInfo | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);

  const selectedProvider = providers.find((item) => item.value === provider) ?? providers[0];
  const supportsRealConnect = provider === "github" || provider === "github_compatible";

  async function loadDeletedRepos() {
    try {
      setDeletedRepos(await apiGet<DeletedRepo[]>("/api/repos/deleted"));
    } catch {
      setDeletedRepos([]);
    }
  }

  async function refreshRepos(nextSelectedId?: string) {
    const items = await apiGet<Repository[]>("/api/repos");
    setRepos(items);
    setSelectedRepoId(nextSelectedId || selectedRepoId || items[0]?.id || "");
    await loadDeletedRepos();
  }

  useEffect(() => {
    refreshRepos().catch(() => setRepos([]));
  }, []);

  async function syncRepo(repoId: string) {
    const data = await apiPost<RepoSyncResult>(`/api/repos/${repoId}/sync`, {
      sync_issues: true,
      sync_pull_requests: true,
      sync_workflow_runs: true,
      limit: 50
    });
    setSelectedRepoId(repoId);
    await refreshRepos(repoId);
    return data;
  }

  async function connect() {
    if (source === "remote" && (!owner.trim() || !repo.trim())) {
      setResult(JSON.stringify({ error: "请填写 Owner / Namespace 和 Repository。" }, null, 2));
      return;
    }
    if (source === "local" && !localPath.trim()) {
      setResult(JSON.stringify({ error: "请填写本地 Git 仓库路径。" }, null, 2));
      return;
    }
    setLoading(true);
    try {
      const connected = await apiPost<RepoConnectResult>("/api/repos/connect", {
        owner: source === "remote" ? owner.trim() : undefined,
        repo: source === "remote" ? repo.trim() : undefined,
        local_path: source === "local" ? localPath.trim() : undefined,
        clone_parent_dir: source === "remote" && cloneParentDir.trim() ? cloneParentDir.trim() : undefined,
        provider,
        api_base_url: provider === "github" ? undefined : apiBaseUrl,
        token: token || undefined,
        demo_mode: false
      });
      setResult(JSON.stringify({ connected, background_sync: "scheduled" }, null, 2));
      await refreshRepos(connected.repo_id);
    } catch (error) {
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  async function refreshRepo(repoId: string) {
    setLoading(true);
    try {
      const synced = await syncRepo(repoId);
      setResult(JSON.stringify({ refreshed: synced }, null, 2));
    } catch (error) {
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  async function selectRepo(repoId: string) {
    setSelectedRepoId(repoId);
    const data = await apiPost(`/api/repos/${repoId}/select`, {});
    setResult(JSON.stringify(data, null, 2));
    await refreshRepos(repoId);
  }

  async function deleteRepo(item: Repository) {
    const confirmed = window.confirm(`确认删除仓库 ${item.full_name} 吗？\n删除后仓库立即从各页面隐藏，保留期内可在“已删除仓库”中恢复；到期后系统将彻底清理本地数据。\n此操作只影响本系统本地数据，不会删除远程 Git 仓库。`);
    if (!confirmed) return;
    setLoading(true);
    try {
      const data = await apiDelete(`/api/repos/${item.id}`);
      setResult(JSON.stringify(data, null, 2));
      await refreshRepos();
    } catch (error) {
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  async function restoreRepo(item: DeletedRepo) {
    setLoading(true);
    try {
      const data = await apiPost(`/api/repos/${item.repo_id}/restore`, {});
      setResult(JSON.stringify(data, null, 2));
      await refreshRepos(item.repo_id);
    } catch (error) {
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  async function purgeRepo(item: DeletedRepo) {
    const confirmed = window.confirm(`彻底删除 ${item.full_name} 的全部本地数据（Issues、PR、CI、会话、知识库）？\n该操作不可恢复，且不会影响远程 Git 仓库。`);
    if (!confirmed) return;
    setLoading(true);
    try {
      const data = await apiPost(`/api/repos/${item.repo_id}/purge`, {});
      setResult(JSON.stringify(data, null, 2));
      await refreshRepos();
    } catch (error) {
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setLoading(false);
    }
  }

  async function toggleHealth(repoId: string) {
    if (healthRepoId === repoId) {
      setHealthRepoId("");
      return;
    }
    setHealthRepoId(repoId);
    setRateLimit(null);
    setHealthLoading(true);
    try {
      setSyncRuns(await apiGet<SyncRunInfo[]>(`/api/repos/${repoId}/sync-runs?limit=8`));
    } catch {
      setSyncRuns([]);
    } finally {
      setHealthLoading(false);
    }
  }

  async function checkRateLimit(repoId: string) {
    setHealthLoading(true);
    try {
      setRateLimit(await apiPost<RateLimitInfo>(`/api/repos/${repoId}/rate-limit`, {}));
    } catch (error) {
      setRateLimit(null);
      setResult(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }, null, 2));
    } finally {
      setHealthLoading(false);
    }
  }

  return (
    <>
      <PageTitle title="仓库接入" description="连接真实 Git 仓库后会立即触发后台同步；生产环境可配置 Webhook 实时触发，并通过定时同步兜底。" />

      <div className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Card>
          <div className="grid gap-4">
            <div className="grid grid-cols-2 overflow-hidden rounded-md border border-line bg-slate-50 p-1">
              <button
                type="button"
                onClick={() => setSource("remote")}
                className={`h-9 rounded px-3 text-sm font-medium transition ${source === "remote" ? "bg-white text-ink shadow-sm" : "text-slate-600 hover:text-ink"}`}
              >
                GitHub 地址
              </button>
              <button
                type="button"
                onClick={() => setSource("local")}
                className={`h-9 rounded px-3 text-sm font-medium transition ${source === "local" ? "bg-white text-ink shadow-sm" : "text-slate-600 hover:text-ink"}`}
              >
                本地仓库
              </button>
            </div>

            <div className="grid gap-4 lg:grid-cols-[260px_1fr_1fr]">
              <label className="text-sm font-medium text-slate-700">
                托管平台
                <select
                  className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                  value={provider}
                  onChange={(event) => {
                    const next = event.target.value;
                    setProvider(next);
                    if (next === "github") setApiBaseUrl("https://api.github.com");
                    if (next === "github_compatible") setApiBaseUrl("https://api.example.com");
                    if (next === "gitlab") setApiBaseUrl("https://gitlab.com/api/v4");
                    if (next === "gitee") setApiBaseUrl("https://gitee.com/api/v5");
                  }}
                >
                  {providers.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>

              {source === "remote" ? (
                <>
                  <label className="text-sm font-medium text-slate-700">
                    Owner / Namespace
                    <input
                      className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                      value={owner}
                      onChange={(event) => setOwner(event.target.value)}
                      placeholder="296569015"
                    />
                  </label>

                  <label className="text-sm font-medium text-slate-700">
                    Repository
                    <input
                      className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                      value={repo}
                      onChange={(event) => setRepo(event.target.value)}
                      placeholder="clowder-ai"
                    />
                  </label>
                </>
              ) : (
                <label className="text-sm font-medium text-slate-700 lg:col-span-2">
                  本地仓库路径
                  <div className="mt-2 flex gap-2">
                    <input
                      className="min-w-0 flex-1 rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                      value={localPath}
                      onChange={(event) => setLocalPath(event.target.value)}
                      placeholder="例如 D:\\code\\my-repo"
                    />
                    <span className="inline-flex w-10 shrink-0 items-center justify-center rounded-md border border-line bg-white text-slate-500">
                      <Folder className="h-4 w-4" />
                    </span>
                  </div>
                </label>
              )}
            </div>
          </div>

          <div className="mt-4 rounded-md border border-teal-100 bg-teal-50/70 p-3 text-sm leading-6 text-slate-700">
            <div className="font-medium text-teal-900">{selectedProvider.label}</div>
            <div>{selectedProvider.hint}</div>
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <label className="text-sm font-medium text-slate-700">
              API Base URL
              <input
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100 disabled:bg-slate-100"
                value={apiBaseUrl}
                disabled={provider === "github"}
                onChange={(event) => setApiBaseUrl(event.target.value)}
              />
            </label>

            <label className="text-sm font-medium text-slate-700">
              Access Token
              <input
                type="password"
                className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="public repo 可留空，私有仓库需要 token"
              />
            </label>

            {source === "remote" && (
              <label className="text-sm font-medium text-slate-700 lg:col-span-2">
                下载父目录
                <input
                  className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-teal-500 focus:ring-2 focus:ring-teal-100"
                  value={cloneParentDir}
                  onChange={(event) => setCloneParentDir(event.target.value)}
                  placeholder="例如 D:\\code\\repos，留空则使用默认缓存目录"
                />
              </label>
            )}
          </div>

          <div className="mt-5 flex flex-wrap gap-3">
            <PrimaryButton
              onClick={connect}
              disabled={loading || !supportsRealConnect || (source === "remote" ? !owner.trim() || !repo.trim() : !localPath.trim())}
            >
              {loading ? "连接并同步中..." : "连接并自动同步"}
            </PrimaryButton>
          </div>

          {!supportsRealConnect && (
            <p className="mt-3 text-sm text-amber-700">当前平台已在界面预留，但真实 API 同步尚未实现。请选择 GitHub 或 GitHub-compatible API。</p>
          )}

          {result && <pre className="mt-4 overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-xs text-slate-100">{result}</pre>}
        </Card>

        <Card>
          <div className="flex items-center justify-between gap-3">
            <h2 className="font-semibold text-ink">已连接仓库</h2>
            <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{repos.length} 个</span>
          </div>
          <div className="mt-4 space-y-3">
            {repos.length === 0 && <p className="text-sm text-slate-600">还没有仓库。请连接一个真实 Git 仓库。</p>}
            {repos.map((item) => (
              <div
                key={item.id}
                className={`rounded-lg border p-4 transition ${selectedRepoId === item.id ? "border-teal-300 bg-teal-50/70" : "border-line bg-white"}`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="font-medium text-ink">{item.full_name}</div>
                    <div className="mt-1 text-xs text-slate-500">
                      {item.provider || "github"} · {item.default_branch || "main"}
                    </div>
                    {item.local_path && <div className="mt-1 break-all text-xs text-slate-500">{item.checkout_mode === "local" ? "本地仓库" : "代码路径"}：{item.local_path}</div>}
                  </div>
                  <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{selectedRepoId === item.id ? "当前查看" : "已连接"}</span>
                </div>
                <p className="mt-3 line-clamp-2 text-sm leading-6 text-slate-600">{item.description || "暂无仓库描述"}</p>
                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    onClick={() => selectRepo(item.id)}
                    className="rounded-md border border-line bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:border-teal-200 hover:bg-teal-50"
                  >
                    设为当前查看
                  </button>
                  <button
                    onClick={() => refreshRepo(item.id)}
                    disabled={loading}
                    className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white transition hover:bg-teal-700 disabled:opacity-60"
                  >
                    立即刷新
                  </button>
                  <button
                    onClick={() => void toggleHealth(item.id)}
                    className="rounded-md border border-line bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:border-teal-200 hover:bg-teal-50"
                  >
                    {healthRepoId === item.id ? "收起同步健康" : "同步健康"}
                  </button>
                  <button
                    onClick={() => deleteRepo(item)}
                    disabled={loading}
                    className="inline-flex items-center gap-1 rounded-md border border-red-200 bg-white px-3 py-1.5 text-xs font-medium text-red-700 transition hover:bg-red-50 disabled:opacity-60"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                    删除
                  </button>
                </div>
                {healthRepoId === item.id && (
                  <div className="mt-3 rounded-lg border border-line bg-panel/60 p-3 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium text-ink">最近同步记录</span>
                      <button onClick={() => void checkRateLimit(item.id)} disabled={healthLoading} className="rounded-md border border-line bg-white px-2 py-1 font-medium text-slate-700 transition hover:bg-white/70 disabled:opacity-60">
                        查询 GitHub 限流余量
                      </button>
                    </div>
                    {rateLimit && (
                      <div className="mt-2 rounded-md border border-teal-100 bg-teal-50/70 p-2 text-slate-700">
                        Core API 余量：{rateLimit.core.remaining ?? "-"} / {rateLimit.core.limit ?? "-"}
                        <span className="mx-2 text-slate-400">|</span>
                        Actions API 余量：{rateLimit.actions.remaining ?? "-"} / {rateLimit.actions.limit ?? "-"}
                      </div>
                    )}
                    {healthLoading && syncRuns.length === 0 ? (
                      <p className="mt-2 text-slate-500">加载中...</p>
                    ) : syncRuns.length === 0 ? (
                      <p className="mt-2 text-slate-500">还没有同步记录。</p>
                    ) : (
                      <div className="mt-2 space-y-1.5">
                        {syncRuns.map((run) => (
                          <div key={run.id} className="rounded-md border border-line bg-white px-2 py-1.5">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className={run.status === "completed" ? "font-medium text-emerald-700" : run.status === "error" ? "font-medium text-red-600" : "font-medium text-amber-600"}>
                                {run.status === "completed" ? "成功" : run.status === "error" ? "失败" : run.status}
                              </span>
                              <span className="text-slate-500">{run.trigger === "scheduler" ? "定时" : run.trigger === "webhook" ? "Webhook" : run.trigger === "connect" ? "连接" : "手动"}</span>
                              <span className="text-slate-400">{new Date(run.started_at || "").toLocaleString()}</span>
                              {run.rate_limit?.remaining != null && <span className="text-slate-500">余量 {run.rate_limit.remaining}</span>}
                            </div>
                            <div className="mt-0.5 text-slate-500">
                              处理：Issue {run.synced.issues ?? 0} · PR {run.synced.pull_requests ?? 0} · CI {run.synced.workflow_runs ?? 0}
                              {(run.skipped_unchanged?.pull_requests || run.skipped_unchanged?.workflow_runs) ? (
                                <span className="text-slate-400">（未变化跳过：PR {run.skipped_unchanged.pull_requests ?? 0} · CI {run.skipped_unchanged.workflow_runs ?? 0}）</span>
                              ) : null}
                            </div>
                            {run.error && <div className="mt-0.5 break-all text-red-600">{run.error}</div>}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Card>
      </div>

      {deletedRepos.length > 0 && (
        <Card className="mt-4 border-amber-200">
          <h2 className="font-semibold text-ink">已删除仓库（保留期内可恢复）</h2>
          <p className="mt-1 text-xs text-slate-500">超过保留期后系统会自动彻底清理；重新连接同名仓库也会自动恢复原数据。</p>
          <div className="mt-3 space-y-2">
            {deletedRepos.map((item) => (
              <div key={item.repo_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-100 bg-amber-50/50 px-3 py-2 text-sm">
                <div>
                  <span className="font-medium text-ink">{item.full_name}</span>
                  <div className="text-xs text-slate-500">删除于 {new Date(item.deleted_at).toLocaleString()} · 将于 {new Date(item.purges_at).toLocaleString()} 彻底清理</div>
                </div>
                <div className="flex gap-2">
                  <button onClick={() => void restoreRepo(item)} disabled={loading} className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white transition hover:bg-teal-700 disabled:opacity-60">恢复</button>
                  <button onClick={() => void purgeRepo(item)} disabled={loading} className="rounded-md border border-red-200 bg-white px-3 py-1.5 text-xs font-medium text-red-700 transition hover:bg-red-50 disabled:opacity-60">彻底删除</button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card className="mt-4">
        <h2 className="font-semibold text-ink">生产同步策略</h2>
        <div className="mt-3 grid gap-3 text-sm leading-6 text-slate-600 md:grid-cols-3">
          <div className="rounded-md bg-panel p-3">
            <div className="font-medium text-ink">连接后自动同步</div>
            <p className="mt-1">仓库校验成功后，后端会立即调度一次后台同步。</p>
          </div>
          <div className="rounded-md bg-panel p-3">
            <div className="font-medium text-ink">Webhook 实时触发</div>
            <p className="mt-1 break-all">GitHub Webhook URL：{API_BASE_URL}/api/webhooks/github</p>
          </div>
          <div className="rounded-md bg-panel p-3">
            <div className="font-medium text-ink">定时同步兜底</div>
            <p className="mt-1">后端启动后会定时刷新已连接仓库，默认 5 分钟一次。</p>
          </div>
        </div>
      </Card>
    </>
  );
}
