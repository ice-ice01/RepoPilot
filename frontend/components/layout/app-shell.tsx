"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/components/auth-provider";

const navItems = [
  { href: "/", label: "Workspace" },
  { href: "/knowledge", label: "项目知识" },
  { href: "/rag", label: "RAG 工作室" },
  { href: "/repos", label: "Repos" },
  { href: "/action-drafts", label: "Drafts" },
  { href: "/workflow-runs", label: "Runs" },
  { href: "/workspaces", label: "Reports", activePaths: ["/workspaces", "/reports"] },
  { href: "/code-graph", label: "Code Graph" },
  { href: "/evals", label: "Evals" }
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isWorkspace = pathname === "/";
  const { user, logout } = useAuth();
  const [logoutError, setLogoutError] = useState("");

  return (
    <main className={isWorkspace ? "flex h-dvh min-h-0 flex-col overflow-hidden" : "min-h-screen"}>
      <div className="shrink-0 border-b border-line bg-white/85 px-4 py-3">
        <nav aria-label="主导航" className="flex flex-wrap items-center gap-2">
          <div className="mr-2 whitespace-nowrap text-sm font-semibold text-ink">研伴 RepoPilot</div>
          {navItems.map((item) => {
            const isActive = (item.activePaths ?? [item.href]).some(
              (path) => pathname === path || (path !== "/" && pathname.startsWith(`${path}/`))
            );

            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isActive ? "page" : undefined}
                className={`whitespace-nowrap rounded-md px-3 py-1.5 text-sm transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-600 focus-visible:ring-offset-2 ${
                  isActive
                    ? "bg-teal-100 font-semibold text-teal-900 ring-1 ring-inset ring-teal-200"
                    : "text-slate-600 hover:bg-teal-50 hover:text-teal-800"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
          <div className="ml-auto flex items-center gap-3 text-sm">
            <Link href="/account" className="text-slate-600 hover:text-teal-800">{user.name} · {user.role}</Link>
            <button type="button" className="text-slate-500 hover:text-red-700" onClick={() => {
              setLogoutError("");
              void logout().catch((err: unknown) => setLogoutError(err instanceof Error ? err.message : "退出失败，请重试"));
            }}>退出</button>
          </div>
        </nav>
        {logoutError && <p role="alert" className="mt-2 text-sm text-red-700">{logoutError}</p>}
      </div>
      <div className={`w-full p-3 ${isWorkspace ? "min-h-0 flex-1" : ""}`}>{children}</div>
    </main>
  );
}
