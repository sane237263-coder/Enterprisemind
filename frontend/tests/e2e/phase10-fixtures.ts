import { expect, type Browser, type Page, type TestInfo } from '@playwright/test';
import crypto from 'node:crypto';
import { readFileSync } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';

export type BrowserCaseResult = {
  case_id: string;
  status: 'passed' | 'failed';
  actual_layer: 'real' | 'deterministic';
  browser: string;
  browser_version: string;
  started_at: string;
  finished_at: string;
  url: string;
  screenshot?: string;
  downloads: DownloadEvidence[];
  observations?: string[];
  error?: string;
};

export type DownloadEvidence = {
  filename: string;
  path: string;
  bytes: number;
  sha256: string;
  content_type?: string;
  mime_hint: 'png' | 'pdf' | 'zip' | 'unknown';
  structure_checks: string[];
};

export type DownloadProfile = 'content' | 'chart' | 'original';

export type Credentials = { username: string; password: string };

const evidenceRoot = path.resolve(
  process.env.P10_BROWSER_EVIDENCE_DIR ?? path.resolve('..', '.local', 'phase10', 'p10-07-playwright-local', 'browser'),
);

function safeName(value: string): string {
  return value.replace(/[^A-Za-z0-9_.-]+/g, '_').slice(0, 120) || 'case';
}

function redact(value: string): string {
  return value
    .replace(/Bearer\s+[A-Za-z0-9._-]+/gi, 'Bearer <redacted>')
    .replace(/password[=:]\s*[^\s,;]+/gi, 'password=<redacted>');
}

export function realBrowserEnabled(): boolean {
  return process.env.P10_RUN_REAL_BROWSER === '1';
}

export function credentials(role: 'admin' | 'user' = 'admin'): Credentials | undefined {
  const prefix = role === 'admin' ? 'P10_ADMIN' : 'P10_USER';
  const username = process.env[`${prefix}_USERNAME`];
  const passwordFile = process.env[`${prefix}_PASSWORD_FILE`];
  let password = process.env[`${prefix}_PASSWORD`];
  if (!password && passwordFile) {
    // Secret paths are accepted for local acceptance; the password value is
    // read only in this process and is never emitted into case evidence.
    try {
      password = readFileSync(passwordFile, 'utf8').trim();
    } catch {
      password = undefined;
    }
  }
  return username && password ? { username, password } : undefined;
}

export function evidenceDirectory(caseId: string, browserName: string): string {
  return path.join(evidenceRoot, safeName(caseId), safeName(browserName));
}

export async function withBrowserCase(
  page: Page,
  browser: Browser,
  testInfo: TestInfo,
  caseId: string,
  fn: (directory: string, result: BrowserCaseResult) => Promise<void>,
): Promise<void> {
  const directory = evidenceDirectory(caseId, testInfo.project.name);
  await fs.mkdir(path.join(directory, 'downloads'), { recursive: true });
  const result: BrowserCaseResult = {
    case_id: caseId,
    status: 'failed',
    actual_layer: 'real',
    browser: testInfo.project.name,
    browser_version: browser.version(),
    started_at: new Date().toISOString(),
    finished_at: '',
    url: '',
    downloads: [],
  };
  try {
    await fn(directory, result);
    result.status = 'passed';
  } catch (error) {
    result.error = redact(error instanceof Error ? error.message : String(error));
    throw error;
  } finally {
    result.finished_at = new Date().toISOString();
    result.url = page.url();
    const screenshotPath = path.join(directory, 'final.png');
    try {
      await page.screenshot({ path: screenshotPath, fullPage: true });
      result.screenshot = path.relative(evidenceRoot, screenshotPath).replaceAll('\\', '/');
    } catch {
      // Preserve the result even if the browser has already closed.
    }
    await fs.writeFile(path.join(directory, 'case.json'), `${JSON.stringify(result, null, 2)}\n`, 'utf8');
  }
}

export async function login(page: Page, user: Credentials): Promise<void> {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
  await page.getByLabel('用户名').fill(user.username);
  await page.getByLabel('密码').fill(user.password);
  await page.getByRole('button', { name: /登录/ }).click();
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible();
}

function fileKind(bytes: Buffer): DownloadEvidence['mime_hint'] {
  if (bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'png';
  if (bytes.subarray(0, 5).toString('ascii') === '%PDF-') return 'pdf';
  if (bytes.subarray(0, 4).toString('ascii') === 'PK\x03\x04') return 'zip';
  return 'unknown';
}

function zipEntries(bytes: Buffer): string[] {
  const names: string[] = [];
  const signature = Buffer.from([0x50, 0x4b, 0x01, 0x02]);
  for (let offset = 0; offset + 46 <= bytes.length; offset += 1) {
    if (!bytes.subarray(offset, offset + 4).equals(signature)) continue;
    const nameLength = bytes.readUInt16LE(offset + 28);
    const extraLength = bytes.readUInt16LE(offset + 30);
    const commentLength = bytes.readUInt16LE(offset + 32);
    const end = offset + 46 + nameLength + extraLength + commentLength;
    if (end > bytes.length) continue;
    names.push(bytes.subarray(offset + 46, offset + 46 + nameLength).toString('utf8'));
    offset = end - 1;
  }
  return names;
}

function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted && char === '"' && text[index + 1] === '"') {
      cell += '"';
      index += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if (!quoted && char === ',') {
      row.push(cell);
      cell = '';
    } else if (!quoted && (char === '\n' || char === '\r')) {
      if (char === '\r' && text[index + 1] === '\n') index += 1;
      row.push(cell);
      if (row.some((value) => value.length > 0)) rows.push(row);
      row = [];
      cell = '';
    } else {
      cell += char;
    }
  }
  if (cell.length || row.length) {
    row.push(cell);
    if (row.some((value) => value.length > 0)) rows.push(row);
  }
  return rows;
}

function structureChecks(
  filename: string,
  bytes: Buffer,
  kind: DownloadEvidence['mime_hint'],
  profile?: DownloadProfile,
): string[] {
  const checks: string[] = [];
  const lower = filename.toLowerCase();
  if (kind === 'png') checks.push('png-signature');
  if (kind === 'pdf') checks.push('pdf-header');
  if (kind === 'zip') {
    checks.push('zip-header');
    const entries = zipEntries(bytes);
    if (lower.endsWith('.xlsx')) {
      if (!entries.includes('[Content_Types].xml')) throw new Error(`XLSX 缺少 [Content_Types].xml: ${filename}`);
      if (!entries.some((entry) => entry.startsWith('xl/worksheets/sheet'))) throw new Error(`XLSX 缺少 worksheet: ${filename}`);
      checks.push('xlsx-content-types-present', 'xlsx-worksheet-present');
      // Content exports are valid workbooks without a chart part. Chart
      // exports must opt into the stronger native-chart assertion.
      if (profile === 'chart') {
        if (!entries.some((entry) => entry.startsWith('xl/charts/chart'))) throw new Error(`XLSX 缺少原生 chart: ${filename}`);
        checks.push('xlsx-native-chart-present');
      }
    }
    if (lower.endsWith('.docx')) {
      if (!entries.includes('[Content_Types].xml')) throw new Error(`DOCX 缺少 [Content_Types].xml: ${filename}`);
      if (!entries.includes('word/document.xml')) throw new Error(`DOCX 缺少 word/document.xml: ${filename}`);
      checks.push('docx-content-types-present', 'docx-document-present');
    }
  }
  if (lower.endsWith('.csv') && profile !== 'original') {
    if (!(bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf)) throw new Error(`CSV 缺少 UTF-8 BOM: ${filename}`);
    const text = bytes.toString('utf8');
    if (text.includes('\ufffd')) throw new Error(`CSV 不是有效 UTF-8: ${filename}`);
    const rows = parseCsv(text.slice(1));
    if (rows.length < 2 || rows.some((row) => row.length !== rows[0].length)) throw new Error(`CSV 不是规整矩形数据: ${filename}`);
    const header = rows[0].map((value) => value.toLowerCase());
    if (!header.some((value) => /source|queried|scope|chart|meta/.test(value))) throw new Error(`CSV 缺少来源/口径元数据列: ${filename}`);
    checks.push('csv-utf8-bom', 'csv-utf8-valid', 'csv-rectangular-columns', 'csv-metadata-columns-present');
  }
  if ((lower.endsWith('.md') || lower.endsWith('.markdown')) && profile !== 'original') {
    const text = bytes.toString('utf8');
    if (!text.trim()) throw new Error(`Markdown 内容为空: ${filename}`);
    if (!/^\s*#\s+/m.test(text)) throw new Error(`Markdown 缺少标题: ${filename}`);
    if (!/(来源|source|查询时间|queried_at)/i.test(text)) throw new Error(`Markdown 缺少来源/查询时间: ${filename}`);
    checks.push('markdown-nonempty', 'markdown-heading', 'markdown-source-or-time');
  }
  return checks;
}

export async function saveDownload(
  directory: string,
  download: { suggestedFilename(): string; saveAs(path: string): Promise<void> },
  contentType?: string,
  profile?: DownloadProfile,
): Promise<DownloadEvidence> {
  const filename = safeName(download.suggestedFilename());
  const target = path.join(directory, 'downloads', filename);
  await download.saveAs(target);
  const bytes = await fs.readFile(target);
  if (!bytes.length) throw new Error(`下载文件为空: ${filename}`);
  const kind = fileKind(bytes);
  const evidence: DownloadEvidence = {
    filename,
    path: path.relative(evidenceRoot, target).replaceAll('\\', '/'),
    bytes: bytes.length,
    sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    content_type: contentType,
    mime_hint: kind,
    structure_checks: structureChecks(filename, bytes, kind, profile),
  };
  return evidence;
}

/**
 * Trigger one real browser download while retaining the response MIME when
 * Chromium/Edge exposes it.  Download objects do not expose response headers,
 * so the response listener is installed before the trigger and matched by URL.
 */
export async function triggerAndSaveDownload(
  page: Page,
  directory: string,
  trigger: () => Promise<void>,
  profile?: DownloadProfile,
): Promise<DownloadEvidence> {
  const responseTypes = new Map<string, string>();
  let fallbackContentType: string | undefined;
  const onResponse = (response: { url(): string; headers(): Record<string, string> }) => {
    const contentType = response.headers()['content-type'];
    if (contentType) {
      responseTypes.set(response.url(), contentType);
      if (response.headers()['content-disposition'] || /\/download(?:\?|$)/.test(response.url())) {
        fallbackContentType = contentType;
      }
    }
  };
  page.on('response', onResponse);
  try {
    const downloadPromise = page.waitForEvent('download');
    await trigger();
    const download = await downloadPromise;
    return await saveDownload(directory, download, responseTypes.get(download.url()) ?? fallbackContentType, profile);
  } finally {
    page.off('response', onResponse);
  }
}
