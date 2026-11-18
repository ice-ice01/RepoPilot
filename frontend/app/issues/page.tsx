"use client";

import { useEffect, useState } from "react";

import { Card, GhostButton, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";

type Issue = { id: string; number: number; title: string; state: string; labels: string[]; body?: string };

const PAGE_SIZE = 50;

export default function IssuesPage() {
  const [issues, setIssues] = useState<Issue[]>([]);
  const [selected, setSelected] = useState<Issue | null>(null);
  const [analysis, setAnalysis] = useState("");
  // 大仓库全量渲染会卡：先渲染一页，滚动到底部按钮再展开。
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

  useEffect(() => {
    apiGet<Repository[]>("/api/repos").then((repos) => {
      if (repos[0]) {
        apiGet<Issue[]>(`/api/repos/${repos[0].id}/issues`).then((items) => {
          setIssues(items);
          setSelected(items[0] ?? null);
        });
      }
    });
  }, []);

  const visibleIssues = issues.slice(0, visibleCount);

  async function analyze() {
    if (!selected) return;
    const data = await apiPost(`/api/issues/${selected.id}/analyze`, {});
    setAnalysis(JSON.stringify(data, null, 2));
  }

  return (
    <>
      <PageTitle title="Issue 智能分析" description="自动生成 Issue 分类、优先级、复杂度、负责人建议、相似问题和下一步行动项。" />
      <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-ink">Issue 列表</h2>
            <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{issues.length} 条</span>
          </div>
          <div className="mt-4 space-y-2">
            {visibleIssues.map((issue) => (
              <GhostButton key={issue.id} onClick={() => setSelected(issue)} className={selected?.id === issue.id ? "border-teal-300 bg-teal-50 text-teal-900" : ""}>
                <div className="flex items-center justify-between gap-3">
                  <span>#{issue.number} {issue.title}</span>
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs">{issue.state}</span>
                </div>
              </GhostButton>
            ))}
            {visibleCount < issues.length && (
              <button
                onClick={() => setVisibleCount((count) => count + PAGE_SIZE)}
                className="w-full rounded-lg border border-dashed border-line px-3 py-2 text-sm text-slate-600 transition hover:bg-panel"
              >
                加载更多（已显示 {visibleIssues.length} / {issues.length}）
              </button>
            )}
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-ink">分析结果</h2>
          {selected && (
            <div className="mt-3 rounded-md bg-panel p-3 text-sm leading-6 text-slate-600">
              <div className="font-medium text-ink">#{selected.number} {selected.title}</div>
              <div className="mt-1">{selected.body || "暂无正文"}</div>
            </div>
          )}
          <PrimaryButton onClick={analyze} disabled={!selected} className="mt-4">分析当前 Issue</PrimaryButton>
          {analysis && <pre className="mt-4 max-h-[520px] overflow-auto rounded-md border border-slate-800 bg-slate-950 p-4 text-xs text-slate-100">{analysis}</pre>}
        </Card>
      </div>
    </>
  );
}
