import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/layout/app-shell";
import { AuthProvider } from "@/components/auth-provider";

export const metadata: Metadata = {
  title: "研伴 RepoPilot",
  description: "PR / Issue intelligent collaboration agent for engineering teams"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <AuthProvider><AppShell>{children}</AppShell></AuthProvider>
      </body>
    </html>
  );
}
