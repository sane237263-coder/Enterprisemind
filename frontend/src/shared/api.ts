import { EventStreamDecoder } from './sse';
type Envelope<T> = { data: T; meta: { next_cursor?: string | null } };
export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) { super(message); }
}
let credential = '';
export function setCredential(value: string) { credential = value; }
export function websocketCredential() { return credential; }
export function authorizedPath(path: string) {
  if (!path.startsWith('/api/v1/') || path.includes('\\') || /[\r\n]/.test(path)) {
    throw new Error('无法打开此内容地址。');
  }
  const url = new URL(path, location.origin);
  if (url.origin !== location.origin) throw new Error('无法打开此内容地址。');
  url.hash = '';
  return url.pathname + url.search;
}
async function response(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  if (credential) headers.set('Authorization', `Bearer ${credential}`);
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  let result: Response;
  try { result = await fetch(authorizedPath(path), { ...init, headers, cache: 'no-store', redirect: 'error' }); }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('连接失败，请检查网络后重试。');
  }
  if (!result.ok) {
    const data = await result.json().catch(() => null);
    const code = data?.error?.code ?? 'REQUEST_FAILED';
    if (result.status === 401 || (result.status === 403 && (code === 'ACCOUNT_DISABLED' || code === 'DEPARTMENT_DISABLED'))) {
      window.dispatchEvent(new Event('session-expired'));
    }
    throw new ApiError(code, data?.error?.message ?? '操作未完成，请重试。', result.status);
  }
  return result;
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<Envelope<T>> {
  const result = await response(path, init);
  if (result.status === 204) return { data: undefined as T, meta: {} };
  return result.json();
}
export async function all<T>(path: string, signal?: AbortSignal): Promise<T[]> {
  const rows: T[] = []; let cursor: string | null | undefined;
  do {
    const page = await api<T[]>(`${path}${path.includes('?') ? '&' : '?'}limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { signal });
    rows.push(...page.data); cursor = page.meta.next_cursor;
  } while (cursor);
  return rows;
}
export const mutation = (method: string, body?: unknown, key: string = crypto.randomUUID()): RequestInit => ({
  method, headers: { 'Idempotency-Key': key }, body: body === undefined ? undefined : JSON.stringify(body),
});
export const upload = (body: FormData, key: string = crypto.randomUUID()): RequestInit => ({ method: 'POST', headers: { 'Idempotency-Key': key }, body });
export async function blob(path: string, signal?: AbortSignal) { return (await response(path, { signal })).blob(); }
export async function download(path: string, filename: string) {
  const url = URL.createObjectURL(await blob(path));
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
export async function stream(path: string, signal: AbortSignal, receive: (event: string, data: Record<string, unknown>) => void) {
  const result = await response(path, { signal, headers: { Accept: 'text/event-stream' } });
  if (!result.body) throw new Error('无法读取回答，请刷新会话。');
  const reader = result.body.getReader(); const decoder = new EventStreamDecoder();
  try {
    while (true) {
      const { value, done } = await reader.read();
      decoder.push(value, done, receive);
      if (done) break;
    }
  } finally { reader.releaseLock(); }
}
