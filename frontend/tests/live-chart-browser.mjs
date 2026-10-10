import { chromium } from '@playwright/test';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

// Credentials arrive through stdin and stay in memory, outside the evidence.
let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
const report = { status: 'incomplete', browsers: [], cases: [] };
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const check = (value, code) => { if (!value) throw new Error(code); };
await fs.mkdir(config.output, { recursive: true });
const saveReport = () => fs.writeFile(path.join(config.output, 'browser-result.json'), JSON.stringify(report, null, 2));
let stage = 'startup';
try {
  for (const channel of ['chrome', 'msedge']) {
    stage = `${channel}:login`;
    const browser = await chromium.launch({ channel, headless: true });
    try {
      const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', () => errors.push('pageerror'));
      await page.goto(config.baseUrl);
      await page.getByLabel('用户名', { exact: true }).fill(config.username);
      await page.getByLabel('密码', { exact: true }).fill(config.password);
      const loginResponse = page.waitForResponse(r => r.url().endsWith('/api/v1/auth/login') && r.request().method() === 'POST');
      await page.getByRole('button', { name: /登录/ }).click();
      const login = await (await loginResponse).json();
      const headers = { Authorization: `Bearer ${login.data.access_token}` };
      async function get(url) {
        const response = await context.request.get(config.baseUrl + url, { headers });
        check(response.status() === 200, 'api_read_failed');
        return (await response.json()).data;
      }
      await page.getByLabel('输入消息').waitFor();
      for (const [index, specification] of config.charts.entries()) {
        stage = `${channel}:${specification.type}`;
        let item;
        if (channel === 'chrome') {
          await page.getByRole('button', { name: '新建会话', exact: true }).first().click();
          const messageResponse = page.waitForResponse(r => /\/conversations\/\d+\/messages$/.test(r.url()) && r.request().method() === 'POST', { timeout: 20000 });
          await page.getByLabel('输入消息').fill(specification.question);
          await page.getByRole('button', { name: '发送消息', exact: true }).click();
          const response = await messageResponse;
          check(response.status() === 202, 'message_submit_failed');
          const initialRun = (await response.json()).data;
          let run;
          const deadline = Date.now() + 700000;
          do {
            run = await get(`/api/v1/agent-runs/${initialRun.id}`);
            if (['completed', 'failed', 'cancelled', 'waiting_confirmation'].includes(run.status)) break;
            await new Promise(resolve => setTimeout(resolve, 1000));
          } while (Date.now() < deadline);
          check(run.status === 'completed', `agent_${run.status}_${run.failure_code || 'none'}`);
          const history = await get(`/api/v1/conversations/${run.conversation_id}/messages`);
          const assistant = history.find(m => m.id === run.assistant_message_id);
          const part = assistant?.content_parts?.find(p => p.type === 'generated_file_task' && p.kind === 'chart_render');
          if (!part) {
            const previews = [];
            for (const source of assistant?.sources || []) {
              if (source.source_type === 'database') {
                const preview = await get(`/api/v1/answer-sources/${source.id}/preview`);
                previews.push({ columns: preview.columns, returnedRows: preview.returned_rows, kind: preview.query_kind });
              }
            }
            report.diagnostic = {
              runId: run.id, previews,
              scopeClarification: /请明确图表范围/.test(assistant?.content_text || ''),
              columnClarification: /多个可用维度或指标列/.test(assistant?.content_text || ''),
              partTypes: (assistant?.content_parts || []).map(p => p.type),
            };
          }
          check(part, 'chart_task_missing');
          let task;
          const taskDeadline = Date.now() + 180000;
          do {
            task = await get(`/api/v1/generation-tasks/${part.task_id}`);
            if (['succeeded', 'failed', 'cancelled'].includes(task.status)) break;
            await new Promise(resolve => setTimeout(resolve, 1000));
          } while (Date.now() < taskDeadline);
          check(task.status === 'succeeded', `task_${task.status}_${task.error?.code || 'none'}`);
          check(task.chart.chart_type === specification.type, 'chart_type_wrong');
          check(task.chart.scope === 'all_aggregated', 'scope_wrong');
          check(task.exported_rows === specification.points, 'point_count_wrong');
          check(task.source_refs.length === 1 && assistant.sources.length === 1, 'source_count_wrong');
          check(task.source_refs[0].executed_at || task.source_refs[0].queried_at, 'query_time_missing');
          item = { type: specification.type, taskId: part.task_id, conversationId: run.conversation_id, points: task.exported_rows, statistic: task.chart.statistic, downloads: [] };
          report.cases.push(item);
          await saveReport();
        } else {
          item = report.cases[index];
          // Normal UI opens the existing conversation; no fixture-created messages.
          const conversations = await get('/api/v1/conversations');
          const wantedIndex = conversations.findIndex(c => c.id === item.conversationId);
          check(wantedIndex >= 0, 'conversation_missing');
          await page.locator('.conversation-name').nth(wantedIndex).click();
        }
        const task = await get(`/api/v1/generation-tasks/${item.taskId}`);
        check(new Set(task.artifacts.map(a => a.format)).size === 4 && task.artifacts.length === 4, 'four_formats_required');
        const card = page.locator('.chart-preview-card');
        await card.waitFor({ timeout: 20000 });
        const image = card.locator('img');
        await image.waitFor();
        await page.waitForFunction(() => {
          const img = document.querySelector('.chart-preview-card img');
          return img?.complete && img.naturalWidth === 1600 && img.naturalHeight === 900;
        }, undefined, { timeout: 20000 });
        check((await card.innerText()).includes(task.chart.statistic), 'statistic_not_visible');
        for (const artifact of task.artifacts) {
          const response = await context.request.get(config.baseUrl + artifact.download_url, { headers });
          check(response.status() === 200, 'download_http_failed');
          const expected = await response.body();
          const label = { png: 'PNG 图片', pdf: 'PDF 文件', xlsx: 'XLSX 原生图表', csv: '导出图表数据 CSV' }[artifact.format];
          // File size is part of the button's accessible name.
          const [download] = await Promise.all([
            page.waitForEvent('download', { timeout: 20000 }),
            card.getByRole('button', { name: new RegExp(`^${label}`) }).click(),
          ]);
          const saved = path.join(config.output, `${channel}-${item.type}.${artifact.format}`);
          await download.saveAs(saved);
          check(await download.failure() === null, 'browser_save_failed');
          const bytes = await fs.readFile(saved);
          check(bytes.length === expected.length && digest(bytes) === digest(expected), 'saved_hash_mismatch');
          item.downloads.push({ channel, format: artifact.format, fileId: artifact.file_id, bytes: bytes.length, sha256: digest(bytes), saved });
          await saveReport();
        }
        await page.screenshot({ path: path.join(config.output, `${channel}-${item.type}-page.png`), fullPage: true });
        check(errors.length === 0, 'page_error');
      }
      report.browsers.push({ channel, version: browser.version(), status: 'passed' });
      await saveReport();
    } finally { await browser.close(); }
  }
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.stage = stage;
  // Only fixed diagnostics; no responses, credentials or model text.
  report.failure = /^[A-Za-z_]+$/.test(error.message || '') ? error.message : error.name;
  process.exitCode = 1;
} finally {
  await saveReport();
}
