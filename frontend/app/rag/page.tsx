"use client";

import {
  BookOpen,
  Calendar,
  CircleAlert,
  Database,
  FileText,
  GitPullRequest,
  LoaderCircle,
  Play,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  StickyNote,
  Trash2,
  Upload,
  Users,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { apiDelete, apiGet, apiPost, apiUpload } from "@/lib/api";
import type { Repository } from "@/lib/types";

type KnowledgeItem = {
  id: string;
  source_type: string;
  title: string;
  content_preview: string;
  metadata: Record<string, unknown>;
  chunk_count: number;
  created_at?: string | null;
};

type KnowledgeChunk = {
  id: string;
  position: number;
  title: string;
  content: string;
  character_count: number;
};

type RAGSource = {
  citation: number;
  document_id: string;
  source_id: string;
  source_type: string;
  title: string;
  snippet: string;
  score: number;
  metadata: Record<string, unknown>;
  retrieval: Record<string, unknown>;
};

type RAGAnswer = {
  question: string;
  answer: string;
  sources: RAGSource[];
  retrieval_count: number;
  generation_mode: "llm" | "extractive" | "no_evidence";
  model?: string | null;
  took_ms: number;
};

type RAGSearch = {
  query: string;
  results: RAGSource[];
  result_count: number;
  took_ms: number;
};

type RAGStatus = {
  repo_id: string;
  document_count: number;
  source_count: number;
  source_types: Record<string, number>;
  embedding: { provider?: string; model?: string; dimensions?: number };
  vector_store: { backend?: string; status?: string; available?: boolean; error?: string | null; fallback?: string };
  generation: { llm_configured?: boolean; model?: string | null; fallback?: string };
  supported_files: string[];
};

type ChatTurn = {
  id: string;
  role: "user" | "assistant";
  content: string;
  mode?: RAGAnswer["generation_mode"];
  tookMs?: number;
  sourceCount?: number;
};

const sourceTypeLabels: Record<string, string> = {
  knowledge_file: "文档",
  memory_note: "笔记",
  project_doc: "项目文档",
  project_overview: "项目概览",
  issue: "Issue",
  pull_request: "PR",
  workflow_run: "CI",
  team_member: "成员",
  weekly_report: "周报",
};

const sourceTypeMeta: Record<string, { icon: typeof FileText; badge: string }> = {
  knowledge_file: { icon: FileText, badge: "bg-sky-50 text-sky-600" },
  memory_note: { icon: StickyNote, badge: "bg-violet-50 text-violet-600" },
  project_doc: { icon: BookOpen, badge: "bg-teal-50 text-teal-700" },
  project_overview: { icon: FileText, badge: "bg-cyan-50 text-cyan-700" },
  issue: { icon: CircleAlert, badge: "bg-amber-50 text-amber-700" },
  pull_request: { icon: GitPullRequest, badge: "bg-indigo-50 text-indigo-700" },
  workflow_run: { icon: Play, badge: "bg-rose-50 text-rose-600" },
  team_member: { icon: Users, badge: "bg-pink-50 text-pink-600" },
  weekly_report: { icon: Calendar, badge: "bg-lime-50 text-lime-700" },
};

const sourceGroupOrder = [
  "knowledge_file",
  "memory_note",
  "project_doc",
  "project_overview",
  "weekly_report",
  "issue",
  "pull_request",
  "workflow_run",
  "team_member",
];

function sourceLabel(value: string) {
  return sourceTypeLabels[value] ?? value.replaceAll("_", " ");
}

function sourceMeta(value: string) {
  return sourceTypeMeta[value] ?? { icon: FileText, badge: "bg-slate-100 text-slate-500" };
}

function formatDate(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export default function RAGPage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState("");
  const [knowledge, setKnowledge] = useState<KnowledgeItem[]>([]);
  const [materialQuery, setMaterialQuery] = useState("");
  const [status, setStatus] = useState<RAGStatus | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sources, setSources] = useState<RAGSource[]>([]);
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState(5);
  const [sourceType, setSourceType] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [previewItem, setPreviewItem] = useState<KnowledgeItem | null>(null);
  const [previewChunks, setPreviewChunks] = useState<KnowledgeChunk[] | null>(null);
  const [previewState, setPreviewState] = useState<"loading" | "ready" | "error">("loading");
  const fileInput = useRef<HTMLInputElement>(null);
  const qaScrollRef = useRef<HTMLDivElement>(null);

  const currentRepo = useMemo(() => repos.find((repo) => repo.id === repoId) ?? null, [repos, repoId]);
  const availableSourceTypes = useMemo(() => Object.keys(status?.source_types ?? {}).sort(), [status]);
  const visibleKnowledge = useMemo(() => {
    const keyword = materialQuery.trim().toLowerCase();
    if (!keyword) return knowledge;
    return knowledge.filter((item) => item.title.toLowerCase().includes(keyword));
  }, [knowledge, materialQuery]);
  const groupedKnowledge = useMemo(() => {
    const groups = new Map<string, KnowledgeItem[]>();
    for (const item of visibleKnowledge) {
      const list = groups.get(item.source_type) ?? [];
      list.push(item);
      groups.set(item.source_type, list);
    }
    return Array.from(groups.entries())
      .map(([type, items]) => ({
        type,
        items,
        order: sourceGroupOrder.indexOf(type),
        label: sourceLabel(type),
      }))
      .sort((a, b) => (a.order === -1 && b.order === -1 ? a.label.localeCompare(b.label) : a.order - b.order));
  }, [visibleKnowledge]);

  async function loadRepos(preferredId?: string) {
    const items = await apiGet<Repository[]>("/api/rag/repositories");
    setRepos(items);
    const nextId = preferredId && items.some((item) => item.id === preferredId) ? preferredId : repoId && items.some((item) => item.id === repoId) ? repoId : items[0]?.id ?? "";
    setRepoId(nextId);
  }

  async function loadKnowledge(id: string) {
    const [nextKnowledge, nextStatus] = await Promise.all([
      apiGet<KnowledgeItem[]>(`/api/repos/${id}/knowledge`),
      apiGet<RAGStatus>(`/api/rag/${id}/status`),
    ]);
    setKnowledge(nextKnowledge);
    setStatus(nextStatus);
  }

  useEffect(() => {
    loadRepos().catch((loadError) => setError(errorMessage(loadError)));
  }, []);

  useEffect(() => {
    if (!repoId) {
      setKnowledge([]);
      setStatus(null);
      return;
    }
    setError(null);
    loadKnowledge(repoId).catch((loadError) => setError(errorMessage(loadError)));
  }, [repoId]);

  useEffect(() => {
    const node = qaScrollRef.current;
    if (!node) return;
    node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
  }, [turns, busy]);

  async function uploadFile(file?: File) {
    if (!file || !repoId) return;
    setBusy("upload");
    setError(null);
    setNotice(null);
    try {
      const body = new FormData();
      body.append("file", file);
      await apiUpload(`/api/rag/repositories/${repoId}/documents`, body);
      setNotice("文件已提交，正在解析、分段并建立索引");
      await new Promise((resolve) => window.setTimeout(resolve, 900));
      await loadKnowledge(repoId);
    } catch (uploadError) {
      setError(errorMessage(uploadError));
    } finally {
      setBusy(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function removeKnowledge(item: KnowledgeItem) {
    if (!repoId || !window.confirm(`删除“${item.title}”及其全部切片？`)) return;
    setBusy(`delete:${item.id}`);
    setError(null);
    try {
      await apiDelete(`/api/rag/repositories/${repoId}/documents/${item.id}`);
      if (previewItem?.id === item.id) setPreviewItem(null);
      await loadKnowledge(repoId);
      setNotice("资料已从索引中删除");
    } catch (deleteError) {
      setError(errorMessage(deleteError));
    } finally {
      setBusy(null);
    }
  }

  async function openMaterial(item: KnowledgeItem) {
    if (!repoId) return;
    setPreviewItem(item);
    setPreviewChunks(null);
    setPreviewState("loading");
    try {
      const chunks = await apiGet<KnowledgeChunk[]>(`/api/rag/repositories/${repoId}/documents/${item.id}/chunks`);
      setPreviewChunks(chunks);
      setPreviewState("ready");
    } catch (previewError) {
      setPreviewState("error");
      setError(errorMessage(previewError));
    }
  }

  async function reindex() {
    if (!repoId) return;
    setBusy("reindex");
    setError(null);
    try {
      const result = await apiPost<{ indexed_documents: number }>(`/api/rag/${repoId}/reindex`, {});
      await loadKnowledge(repoId);
      setNotice(`已重建 ${result.indexed_documents} 个文档向量`);
    } catch (reindexError) {
      setError(errorMessage(reindexError));
    } finally {
      setBusy(null);
    }
  }

  function queryBody() {
    return { question: question.trim(), top_k: topK, source_types: sourceType ? [sourceType] : [] };
  }

  async function ask() {
    const prompt = question.trim();
    if (!repoId || !prompt || busy) return;
    setTurns((current) => [...current, { id: crypto.randomUUID(), role: "user", content: prompt }]);
    setQuestion("");
    setBusy("ask");
    setError(null);
    try {
      const result = await apiPost<RAGAnswer>(`/api/rag/${repoId}/ask`, { ...queryBody(), question: prompt });
      setSources(result.sources);
      setTurns((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: result.answer,
          mode: result.generation_mode,
          tookMs: result.took_ms,
          sourceCount: result.retrieval_count,
        },
      ]);
    } catch (askError) {
      setError(errorMessage(askError));
      setTurns((current) => [...current, { id: crypto.randomUUID(), role: "assistant", content: "请求失败，请检查后端配置后重试。", mode: "no_evidence" }]);
    } finally {
      setBusy(null);
    }
  }

  async function searchOnly() {
    if (!repoId || !question.trim() || busy) return;
    setBusy("search");
    setError(null);
    try {
      const result = await apiPost<RAGSearch>(`/api/rag/${repoId}/search`, queryBody());
      setSources(result.results);
      setNotice(`召回 ${result.result_count} 条证据，耗时 ${result.took_ms} ms`);
    } catch (searchError) {
      setError(errorMessage(searchError));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="-m-3 flex min-h-[calc(100vh-53px)] flex-col bg-[#f6f8f8] lg:h-[calc(100vh-53px)] lg:overflow-hidden">
      <header className="flex flex-wrap items-center justify-between gap-3 bg-white px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-teal-50 text-teal-700">
            <BookOpen className="h-4 w-4" />
          </span>
          <div>
            <h1 className="text-base font-semibold text-ink">RAG 工作室</h1>
            <p className="text-xs text-slate-500">{currentRepo?.full_name ?? "未选择项目"}</p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2">
          <select
            value={repoId}
            onChange={(event) => {
              setRepoId(event.target.value);
              setTurns([]);
              setSources([]);
              setPreviewItem(null);
            }}
            className="h-9 min-w-0 max-w-64 rounded-md border border-line bg-white px-3 text-sm outline-none focus:border-teal-600"
            aria-label="选择项目"
          >
            {repos.length === 0 && <option value="">暂无项目</option>}
            {repos.map((repo) => (
              <option key={repo.id} value={repo.id}>{repo.full_name}</option>
            ))}
          </select>
          {repoId ? (
            <Link href={`/knowledge/${repoId}`} className="hidden h-9 items-center gap-1.5 rounded-md px-2.5 text-sm text-slate-500 hover:bg-slate-100 hover:text-ink sm:inline-flex">管理项目知识</Link>
          ) : null}
          <button type="button" onClick={() => repoId && loadKnowledge(repoId)} className="flex h-9 w-9 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 hover:text-ink" title="刷新" aria-label="刷新">
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 bg-white px-4 py-2 text-xs text-slate-500">
        <StatusChip label="资料" value={status ? `${status.source_count} 份` : "-"} />
        <StatusChip label="切片" value={status ? `${status.document_count} 条` : "-"} />
        <StatusChip
          label="Embedding"
          value={status ? `${status.embedding.provider} · ${status.embedding.dimensions}d` : "-"}
          ok={Boolean(status && status.vector_store.available !== false)}
        />
        <StatusChip
          label="向量索引"
          value={status?.vector_store ? `${status.vector_store.backend ?? "vector"} · ${status.vector_store.available === false ? "degraded" : status.vector_store.status ?? "ready"}` : "-"}
          ok={status?.vector_store?.available !== false}
        />
        <StatusChip
          label="生成"
          value={status?.generation.llm_configured ? status.generation.model ?? "LLM" : "本地证据模式"}
          ok={Boolean(status?.generation.llm_configured)}
        />
      </div>

      {(error || notice) && (
        <div className={`px-4 py-2 text-sm ${error ? "bg-red-50 text-red-700" : "bg-emerald-50 text-emerald-800"}`}>
          {error ?? notice}
        </div>
      )}

      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(280px,330px)_minmax(400px,1fr)_minmax(320px,360px)] lg:overflow-hidden">
        <aside className="flex min-h-[360px] flex-col border-b border-line lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between px-4 pb-2 pt-4">
            <div className="flex min-w-0 items-center gap-2 text-sm font-semibold text-ink">
              知识库
              {knowledge.length > 0 && (
                <span className="rounded-full bg-slate-200/70 px-2 py-0.5 text-[11px] font-medium text-slate-600">{knowledge.length}</span>
              )}
            </div>
            <button type="button" onClick={reindex} disabled={!repoId || busy === "reindex"} title="重建向量索引" aria-label="重建向量索引" className="flex h-8 w-8 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-600 disabled:opacity-50">
              <RefreshCw className={`h-4 w-4 ${busy === "reindex" ? "animate-spin" : ""}`} />
            </button>
          </div>
          <div className="space-y-2 px-3 pb-3">
            <input ref={fileInput} type="file" accept={(status?.supported_files ?? []).join(",")} className="hidden" onChange={(event) => uploadFile(event.target.files?.[0])} />
            <button type="button" disabled={!repoId || busy === "upload"} onClick={() => fileInput.current?.click()} className="flex h-10 w-full items-center justify-center gap-2 rounded-lg bg-teal-50 text-sm font-medium text-teal-800 hover:bg-teal-100 disabled:opacity-50">
              {busy === "upload" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
              {busy === "upload" ? "解析与入库中" : "上传资料"}
            </button>
            {knowledge.length > 0 && (
              <div className="flex h-9 items-center gap-2 rounded-lg bg-white px-3">
                <Search className="h-3.5 w-3.5 shrink-0 text-slate-400" />
                <input
                  value={materialQuery}
                  onChange={(event) => setMaterialQuery(event.target.value)}
                  placeholder="筛选资料名称"
                  className="min-w-0 flex-1 bg-transparent text-xs text-ink outline-none placeholder:text-slate-400"
                  aria-label="筛选资料名称"
                />
                {materialQuery && (
                  <button type="button" onClick={() => setMaterialQuery("")} title="清空筛选" aria-label="清空筛选" className="grid h-5 w-5 shrink-0 place-items-center rounded text-slate-400 hover:text-ink">
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
            {knowledge.length === 0 && <EmptyPanel icon={FileText} label="知识库为空" />}
            {knowledge.length > 0 && visibleKnowledge.length === 0 && <EmptyPanel icon={Search} label="没有匹配的资料" />}
            {groupedKnowledge.map((group) => {
              const GroupIcon = sourceMeta(group.type).icon;
              return (
                <section key={group.type} className="mb-3">
                  <h2 className="flex items-center gap-1.5 px-2 pb-1.5 text-[11px] font-medium text-slate-400">
                    <GroupIcon className="h-3 w-3" />
                    {group.label}
                    <span className="font-normal">{group.items.length}</span>
                  </h2>
                  <div className="space-y-0.5">
                    {group.items.map((item) => {
                      const meta = sourceMeta(item.source_type);
                      const createdAt = formatDate(item.created_at);
                      return (
                        <button
                          key={`${item.source_type}:${item.id}`}
                          type="button"
                          onClick={() => openMaterial(item)}
                          className={`group block w-full rounded-lg px-2 py-2.5 text-left transition hover:bg-white ${previewItem?.id === item.id ? "bg-white" : ""}`}
                        >
                          <div className="flex items-start gap-2.5">
                            <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md ${meta.badge}`}>
                              <meta.icon className="h-3.5 w-3.5" />
                            </span>
                            <div className="min-w-0 flex-1">
                              <div className="line-clamp-2 break-words text-sm font-medium leading-5 text-ink">{item.title}</div>
                              <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11px] text-slate-400">
                                <span>{item.chunk_count} 切片</span>
                                {createdAt && (
                                  <>
                                    <span className="text-slate-300">·</span>
                                    <span>{createdAt}</span>
                                  </>
                                )}
                              </div>
                              {item.content_preview?.trim() && <p className="mt-1.5 line-clamp-2 break-words text-xs leading-5 text-slate-500">{item.content_preview}</p>}
                            </div>
                            <span
                              role="button"
                              tabIndex={0}
                              onClick={(event) => {
                                event.stopPropagation();
                                removeKnowledge(item);
                              }}
                              onKeyDown={(event) => {
                                if (event.key === "Enter" || event.key === " ") {
                                  event.stopPropagation();
                                  event.preventDefault();
                                  removeKnowledge(item);
                                }
                              }}
                              title="删除资料"
                              aria-label="删除资料"
                              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-300 hover:bg-red-50 hover:text-red-600 lg:opacity-0 lg:group-hover:opacity-100 lg:focus-visible:opacity-100"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </section>
              );
            })}
          </div>
        </aside>

        <main className="flex min-h-[520px] min-w-0 flex-col">
          <div className="flex items-center justify-between px-4 pb-1 pt-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-ink"><Sparkles className="h-4 w-4 text-[#9a5b13]" />问答</div>
            {turns.length > 0 && <button type="button" onClick={() => { setTurns([]); setSources([]); }} className="text-xs text-slate-400 hover:text-ink">清空会话</button>}
          </div>
          <div ref={qaScrollRef} className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4">
            {turns.length === 0 && <EmptyPanel icon={BookOpen} label={status?.document_count ? "输入问题开始检索" : "上传资料后开始提问"} />}
            {turns.map((turn) =>
              turn.role === "user" ? (
                <div key={turn.id} className="flex justify-end">
                  <div className="max-w-[82%] rounded-2xl rounded-br-md bg-[#143642] px-4 py-2.5 text-sm leading-7 text-white">{turn.content}</div>
                </div>
              ) : (
                <div key={turn.id} className="max-w-[46rem]">
                  <div className="markdown-body text-sm leading-7 text-ink">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.content}</ReactMarkdown>
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-400">
                    <span>{turn.mode === "llm" ? "LLM 生成" : turn.mode === "extractive" ? "证据提取" : "无证据"}</span>
                    <span>{turn.sourceCount ?? 0} 个引用</span>
                    {typeof turn.tookMs === "number" && <span>{turn.tookMs} ms</span>}
                  </div>
                </div>
              )
            )}
            {busy === "ask" && <div className="flex items-center gap-2 text-sm text-slate-400"><LoaderCircle className="h-4 w-4 animate-spin" />正在检索并生成回答</div>}
          </div>
          <div className="p-3 sm:p-4">
            <div className="rounded-2xl border border-line bg-white p-3 shadow-sm">
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    ask();
                  }
                }}
                placeholder="向当前知识库提问"
                rows={3}
                className="w-full resize-none bg-transparent px-1.5 py-1 text-sm leading-6 text-ink outline-none placeholder:text-slate-400"
              />
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <select value={sourceType} onChange={(event) => setSourceType(event.target.value)} className="h-8 rounded-md border border-line bg-white px-2 text-xs text-slate-600" aria-label="资料类型">
                  <option value="">全部资料</option>
                  {availableSourceTypes.map((type) => <option key={type} value={type}>{sourceLabel(type)}</option>)}
                </select>
                <label className="flex h-8 items-center gap-2 rounded-md border border-line bg-white px-2 text-xs text-slate-600">Top K
                  <select value={topK} onChange={(event) => setTopK(Number(event.target.value))} className="bg-transparent font-medium text-ink outline-none">
                    {[3, 5, 8, 10].map((value) => <option key={value} value={value}>{value}</option>)}
                  </select>
                </label>
                <div className="flex-1" />
                <button type="button" title="只检索证据" onClick={searchOnly} disabled={!repoId || !question.trim() || Boolean(busy)} className="flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 disabled:opacity-40"><Search className="h-4 w-4" /></button>
                <button type="button" onClick={ask} disabled={!repoId || !question.trim() || Boolean(busy)} className="inline-flex h-8 items-center gap-2 rounded-lg bg-teal-700 px-4 text-sm font-medium text-white hover:bg-teal-800 disabled:opacity-40"><Send className="h-4 w-4" />提问</button>
              </div>
            </div>
          </div>
        </main>

        <aside className="flex min-h-[360px] flex-col border-t border-line bg-white/60 lg:border-t-0 lg:border-l">
          <div className="flex items-center justify-between px-4 pb-2 pt-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-ink">检索证据</div>
            <span className="text-xs text-slate-400">{sources.length} 条</span>
          </div>
          <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 pb-4">
            {sources.length === 0 && <EmptyPanel icon={Search} label="暂无检索结果" />}
            {sources.map((source) => (
              <article key={`${source.document_id}:${source.citation}`} className="group cursor-pointer" onClick={() => openMaterial({ id: source.source_id || source.document_id, source_type: source.source_type, title: source.title, content_preview: source.snippet, metadata: source.metadata, chunk_count: 0, created_at: null })}>
                <div className="flex items-start gap-2.5">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-[#143642] text-xs font-semibold text-white">{source.citation}</span>
                  <div className="min-w-0 flex-1">
                    <h2 className="break-words text-sm font-medium leading-5 text-ink group-hover:text-teal-800">{source.title}</h2>
                    <div className="mt-1 flex items-center gap-2 text-[11px] text-slate-400">
                      <span>{sourceLabel(source.source_type)}</span>
                      <span className="tabular-nums">{(source.score * 100).toFixed(1)}%</span>
                    </div>
                  </div>
                </div>
                <div className="mt-2 h-1 overflow-hidden rounded bg-slate-200/70"><div className="h-full rounded bg-teal-500" style={{ width: `${Math.max(3, Math.min(100, source.score * 100))}%` }} /></div>
                <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-slate-600">{source.snippet}</p>
                {Boolean(source.metadata.filename || source.metadata.path) && <div className="mt-1.5 break-all text-[11px] text-slate-400">{String(source.metadata.filename ?? source.metadata.path)}</div>}
              </article>
            ))}
          </div>
        </aside>
      </div>

      {previewItem ? (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/25" onMouseDown={() => setPreviewItem(null)}>
          <div className="h-full w-full max-w-2xl overflow-y-auto bg-white shadow-2xl" onMouseDown={(event) => event.stopPropagation()}>
            <header className="sticky top-0 z-10 flex items-start gap-3 bg-white/95 px-5 pb-3 pt-4 backdrop-blur">
              <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${sourceMeta(previewItem.source_type).badge}`}>
                {(() => {
                  const Icon = sourceMeta(previewItem.source_type).icon;
                  return <Icon className="h-4 w-4" />;
                })()}
              </span>
              <div className="min-w-0 flex-1">
                <h2 className="break-words text-sm font-semibold leading-6 text-ink">{previewItem.title}</h2>
                <p className="mt-0.5 text-xs text-slate-400">
                  {sourceLabel(previewItem.source_type)}
                  {previewItem.chunk_count > 0 && ` · ${previewItem.chunk_count} 个切片`}
                  {previewState === "ready" && previewChunks && ` · ${previewChunks.reduce((sum, chunk) => sum + chunk.character_count, 0)} 字符`}
                </p>
              </div>
              <button title="关闭" onClick={() => setPreviewItem(null)} aria-label="关闭资料详情" className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-ink">
                <X className="h-4 w-4" />
              </button>
            </header>
            <div className="space-y-3 px-5 pb-6">
              {previewState === "loading" && (
                <div className="flex items-center gap-2 py-16 text-sm text-slate-400"><LoaderCircle className="h-4 w-4 animate-spin" />正在读取切片内容...</div>
              )}
              {previewState === "error" && (
                <div className="py-16 text-center text-sm text-slate-400">切片读取失败，请稍后重试。</div>
              )}
              {previewState === "ready" && previewChunks?.length === 0 && (
                <div className="py-16 text-center text-sm text-slate-400">该资料暂无切片内容。</div>
              )}
              {previewState === "ready" &&
                previewChunks?.map((chunk) => (
                  <article key={chunk.id} className="rounded-xl bg-[#f7f9f8] p-4">
                    <div className="mb-2 flex items-center justify-between text-[11px]">
                      <span className="font-semibold text-teal-700">切片 #{chunk.position}</span>
                      <span className="text-slate-400">{chunk.character_count} 字符</span>
                    </div>
                    <p className="whitespace-pre-wrap text-sm leading-6 text-slate-700">{chunk.content}</p>
                  </article>
                ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function StatusChip({ label, value, ok }: { label: string; value: string; ok?: boolean }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-1.5 w-1.5 rounded-full ${ok === undefined ? "bg-slate-300" : ok ? "bg-emerald-500" : "bg-amber-500"}`} aria-hidden="true" />
      <span>{label}</span>
      <span className="font-medium text-slate-700" title={value}>{value}</span>
    </span>
  );
}

function EmptyPanel({ icon: Icon, label }: { icon: typeof Search; label: string }) {
  return (
    <div className="flex min-h-36 flex-col items-center justify-center gap-2 p-4 text-center text-sm text-slate-400">
      <Icon className="h-5 w-5 text-slate-300" />
      <span>{label}</span>
    </div>
  );
}
