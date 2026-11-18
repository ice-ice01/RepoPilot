const configuredBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function browserApiBase(): string {
  if (typeof window === "undefined") return configuredBase;
  const url = new URL(configuredBase, window.location.origin);
  const loopback = new Set(["localhost", "127.0.0.1", "[::1]"]);
  if (loopback.has(url.hostname) && loopback.has(window.location.hostname)) {
    url.hostname = window.location.hostname;
  }
  return url.toString().replace(/\/$/, "");
}

export const API_BASE_URL = browserApiBase();

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.cookie.split("; ").find((item) => item.startsWith("devflow_csrf="))?.slice("devflow_csrf=".length);
}

// 非流式请求的默认超时：后端挂起时页面不会无限 pending。流式接口（apiPostStream）不设整体超时。
const DEFAULT_TIMEOUT_MS = 120_000;

function mergeTimeoutSignal(options: RequestInit, timeoutMs: number): RequestInit {
  const external = options.signal;
  if (typeof AbortSignal === "undefined" || timeoutMs <= 0) return options;
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  if (!external) return { ...options, signal: timeoutSignal };
  // AbortSignal.any 在现代浏览器可用；不可用时退回只保留调用方自己的 signal。
  if (typeof (AbortSignal as unknown as { any?: (signals: AbortSignal[]) => AbortSignal }).any === "function") {
    return { ...options, signal: AbortSignal.any([external, timeoutSignal]) };
  }
  return options;
}

async function authenticatedFetch(
  path: string,
  options: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS
): Promise<Response> {
  const headers = new Headers(options.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes((options.method ?? "GET").toUpperCase())) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", decodeURIComponent(csrf));
  }
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...mergeTimeoutSignal(options, timeoutMs), headers, credentials: "include", cache: "no-store"
  });
  if (response.status === 401 && !["/api/auth/login", "/api/auth/register", "/api/auth/status", "/api/auth/me"].includes(path) && typeof window !== "undefined") {
    window.dispatchEvent(new Event("devflow:session-expired"));
  }
  return response;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await authenticatedFetch(path);
  if (!response.ok) await throwApiError(response);
  return response.json();
}

export async function apiPost<T>(path: string, body: unknown, options: { headers?: HeadersInit } = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  const response = await authenticatedFetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(body)
  });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await authenticatedFetch(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

export async function apiUpload<T>(path: string, body: FormData): Promise<T> {
  const response = await authenticatedFetch(path, {
    method: "POST",
    body
  });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

export async function apiPostStream(
  path: string,
  body: unknown,
  onEvent: (event: string, data: unknown) => void,
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  // 流式响应用调用方传入的 AbortSignal 控制取消，不设整体超时。
  const response = await authenticatedFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal
  }, 0);
  if (!response.ok) await throwApiError(response);
  if (!response.body) throw new Error("Streaming response body is not available.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const rawEvent of events) {
      const lines = rawEvent.split("\n");
      const event = lines.find((line) => line.startsWith("event: "))?.slice(7) ?? "message";
      const dataText = lines
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (!dataText) continue;
      // 单条畸形 SSE 数据只跳过该条，不让整条流式响应中断。
      try {
        onEvent(event, JSON.parse(dataText));
      } catch (error) {
        console.warn("忽略一条无法解析的 SSE 数据", error);
      }
    }
  }
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await authenticatedFetch(path, { method: "DELETE" });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

async function throwApiError(response: Response): Promise<never> {
  const fallback = `${response.status} ${response.statusText}`.trim();
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    let message: string | null = null;
    try {
      const payload = await response.json();
      message = readApiErrorMessage(payload);
    } catch {
      message = null;
    }
    throw new ApiError(response.status, message || fallback);
  }
  const text = (await response.text()).trim();
  throw new ApiError(response.status, text || fallback);
}

function readApiErrorMessage(payload: unknown): string | null {
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return null;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) return String((item as { msg: unknown }).msg);
        return typeof item === "string" ? item : null;
      })
      .filter(Boolean);
    return messages.length ? messages.join("; ") : null;
  }
  const message = (payload as { message?: unknown }).message;
  return typeof message === "string" ? message : null;
}
