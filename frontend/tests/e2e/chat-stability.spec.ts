import { expect, test, type Page, type Route, type TestInfo } from '@playwright/test';

type Run = {
  id: string;
  status: 'running' | 'failed' | 'cancelled';
  current_step: string;
  started_at: string | null;
  failure_message?: string;
};

type ControlledApi = {
  waitForOperation: () => Promise<void>;
  releaseOperation: () => void;
  eventRequests: () => number;
  failBMessages: () => void;
  messageKeys: () => string[];
  pageKeys: () => string[];
  uploadRequests: () => number;
  waitForInitialLoad: () => Promise<void>;
  releaseInitialLoad: () => void;
};

const user = {
  id: 'user-1', username: 'controlled-user', role: 'admin' as const,
  department_id: 'department-1',
  department: { id: 'department-1', name: 'Test', status: 'active' as const, row_version: 1 },
  status: 'active' as const, row_version: 1,
};
const startedAt = '2026-10-07T00:00:00Z';
const runA: Run = { id: 'run-a', status: 'running', current_step: 'A_RUN_ACTIVE', started_at: startedAt };

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  return { promise: new Promise<void>((done) => { resolve = done; }), resolve: () => resolve() };
}

function response(data: unknown, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify({ data, meta: {} }) };
}

function failure(message: string, status: number) {
  return {
    status,
    contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'CONTROLLED_FAILURE', message } }),
  };
}

function conversation(id: string, title: string, latestRunId: string | null) {
  return { id, title, row_version: 1, last_message_at: null, latest_run_id: latestRunId };
}

async function installControlledApi(
  page: Page,
  testInfo: TestInfo,
  options: {
    initialRun?: Run;
    initialAssistantMessage?: unknown;
    rejectOperation?: boolean;
    terminalSendResponse?: boolean;
    terminalPageResponse?: boolean;
    delayInitialConversationLoad?: boolean;
    emptyEventStream?: boolean;
    completeAfterPolls?: number;
  } = {},
): Promise<ControlledApi> {
  testInfo.annotations.push({
    type: 'evidence-layer',
    description: 'controlled browser regression: all /api/v1 requests are fulfilled locally; no real API or model call',
  });
  const operationStarted = deferred();
  const operationGate = deferred();
  const initialLoadStarted = deferred();
  const initialLoadGate = deferred();
  const runById = new Map<string, Run>();
  const latestRunByConversation = new Map<string, string | null>([
    ['conv-a', options.initialRun?.id ?? null], ['conv-b', null],
  ]);
  const messages = new Map<string, unknown[]>([
    ['conv-a', options.initialAssistantMessage ? [options.initialAssistantMessage] : []],
    ['conv-b', []],
  ]);
  let eventRequests = 0;
  let runPolls = 0;
  let messageNumber = 0;
  const messageKeys: string[] = [];
  const pageKeys: string[] = [];
  let uploadRequestCount = 0;
  let failBMessages = false;
  if (options.initialRun) runById.set(options.initialRun.id, options.initialRun);

  async function json(route: Route, data: unknown, status = 200) {
    await route.fulfill(response(data, status));
  }

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path === '/api/v1/auth/login' && method === 'POST') {
      await json(route, { user, access_token: 'controlled-token' });
      return;
    }
    if (path === '/api/v1/auth/me' && method === 'GET') {
      await json(route, user);
      return;
    }
    if (path === '/api/v1/conversations' && method === 'GET') {
      await json(route, [
        conversation('conv-a', 'Conversation A', latestRunByConversation.get('conv-a') ?? null),
        conversation('conv-b', 'Conversation B', latestRunByConversation.get('conv-b') ?? null),
      ]);
      return;
    }

    const conversationMatch = path.match(/^\/api\/v1\/conversations\/(conv-[ab])(?:\/(.*))?$/);
    if (conversationMatch) {
      const [, conversationId, endpoint = ''] = conversationMatch;
      if (!endpoint && method === 'GET') {
        if (conversationId === 'conv-a' && options.delayInitialConversationLoad) {
          initialLoadStarted.resolve();
          await initialLoadGate.promise;
        }
        await json(route, conversation(
          conversationId,
          conversationId === 'conv-a' ? 'Conversation A' : 'Conversation B',
          latestRunByConversation.get(conversationId) ?? null,
        ));
        return;
      }
      if (endpoint === 'attachments' && method === 'POST') {
        uploadRequestCount += 1;
        await json(route, { items: [] });
        return;
      }
      if (endpoint === 'messages' && method === 'GET') {
        if (conversationId === 'conv-b' && failBMessages) {
          await route.fulfill(failure('B_MESSAGES_ERROR', 503));
          return;
        }
        await json(route, messages.get(conversationId) ?? []);
        return;
      }
      const isNewMessage = endpoint === 'messages' && method === 'POST';
      const isNextPage = /^database-queries\/[^/]+\/pages$/.test(endpoint) && method === 'POST';
      if ((isNewMessage && conversationId === 'conv-a') || (isNextPage && conversationId === 'conv-a')) {
        const body = request.postDataJSON() as { content_text?: string };
        const nextRun = isNewMessage
          ? (options.rejectOperation ? undefined : options.terminalSendResponse
            ? { ...runA, status: 'failed' as const, current_step: '', failure_message: 'A_FIRST_SEND_COMPLETE' }
            : runA)
          : options.terminalPageResponse
            ? { ...runA, status: 'failed' as const, current_step: '', failure_message: 'A_PAGE_COMPLETE' }
            : runA;
        if (isNewMessage) messageKeys.push(request.headers()['idempotency-key'] ?? '');
        if (isNextPage) pageKeys.push(request.headers()['idempotency-key'] ?? '');
        operationStarted.resolve();
        await operationGate.promise;
        if (options.rejectOperation) {
          await route.fulfill(failure('A operation failed', 409));
          return;
        }
        if (nextRun) {
          runById.set(nextRun.id, nextRun);
          latestRunByConversation.set(conversationId, nextRun.id);
        }
        if (isNewMessage) {
          messageNumber += 1;
          messages.set(conversationId, [
            ...(messages.get(conversationId) ?? []),
            { id: `message-a-user-${messageNumber}`, role: 'user', content_text: body.content_text ?? '', content_parts: null, status: 'completed', sources: [] },
          ]);
        }
        await json(route, nextRun);
        return;
      }
    }

    if (path === '/api/v1/agent-runs/run-a' && method === 'GET') {
      runPolls += 1;
      if (options.completeAfterPolls && runPolls >= options.completeAfterPolls) {
        runById.set('run-a', {
          id: 'run-a', status: 'failed', current_step: '', started_at: startedAt,
          failure_message: 'A_TERMINAL_FAILURE',
        });
      }
      await json(route, runById.get('run-a') ?? runA);
      return;
    }
    if (path === '/api/v1/agent-runs/run-a/events' && method === 'GET') {
      eventRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'cache-control': 'no-cache' },
        body: options.emptyEventStream ? '' : 'event: answer.delta\ndata: {"text":"A_ONLY_REPLY"}\n\n',
      });
      return;
    }
    if (/^\/api\/v1\/agent-runs\/run-a\/(resume|stop)$/.test(path) && method === 'POST') {
      if (path.endsWith('/resume')) {
        operationStarted.resolve();
        await operationGate.promise;
        runById.set('run-a', runA);
        await json(route, runA);
      } else {
        const stopped: Run = { id: 'run-a', status: 'cancelled', current_step: '', started_at: startedAt };
        runById.set('run-a', stopped);
        await json(route, stopped);
      }
      return;
    }

    await route.fulfill(response({ error: { code: 'UNEXPECTED_CONTROLLED_REQUEST', message: 'Unexpected controlled test request' } }, 500));
  });

  return {
    waitForOperation: () => operationStarted.promise,
    releaseOperation: () => operationGate.resolve(),
    eventRequests: () => eventRequests,
    failBMessages: () => { failBMessages = true; },
    messageKeys: () => [...messageKeys],
    pageKeys: () => [...pageKeys],
    uploadRequests: () => uploadRequestCount,
    waitForInitialLoad: () => initialLoadStarted.promise,
    releaseInitialLoad: () => initialLoadGate.resolve(),
  };
}

async function enterWorkspace(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByLabel('用户名').fill(user.username);
  await page.getByLabel('密码').fill('controlled-only');
  await page.getByRole('button', { name: /登录/ }).click();
  await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible();
}

async function chooseConversation(page: Page, title: string): Promise<void> {
  await page.getByRole('button', { name: title, exact: true }).click();
  await expect(page.getByRole('heading', { name: title })).toBeVisible();
}

test.describe('CONTROLLED chat async ownership regressions', () => {
  test('a delayed send response stays with its conversation and can be reloaded later', async ({ page }, testInfo) => {
    const controlled = await installControlledApi(page, testInfo);
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await page.getByLabel('输入消息').fill('Question from A');
    await page.getByRole('button', { name: '发送消息' }).click();
    await controlled.waitForOperation();

    await chooseConversation(page, 'Conversation B');
    const composer = page.getByLabel('输入消息');
    // Send currently disables the one shared composer while the POST is in flight.
    // Seed its controlled React state anyway so a stale response clearing B's draft
    // is observable independently of the active-run ownership regression.
    await composer.evaluate((element) => {
      const input = element as HTMLTextAreaElement;
      const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      setValue?.call(input, 'B draft staged while A send is pending');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expect(composer).toHaveValue('B draft staged while A send is pending');
    controlled.releaseOperation();
    await expect(composer).toBeEnabled();

    await expect(page.getByRole('button', { name: '停止' })).toHaveCount(0);
    await expect(page.getByText('A_ONLY_REPLY')).toHaveCount(0);
    await expect(page.locator('.run-status')).toHaveCount(0);
    await expect(composer).toHaveValue('B draft staged while A send is pending');
    expect(controlled.eventRequests()).toBe(0);

    await chooseConversation(page, 'Conversation A');
    await expect(page.locator('.run-status')).toContainText('A_RUN_ACTIVE');
    await expect(page.getByText('A_ONLY_REPLY')).toBeVisible();
    expect(controlled.eventRequests()).toBeGreaterThan(0);
  });

  test('selecting the already open conversation keeps the composer usable', async ({ page }, testInfo) => {
    await installControlledApi(page, testInfo);
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    const composer = page.getByLabel('输入消息');
    await expect(composer).toBeEnabled();
    await composer.fill('Draft stays here');
    await chooseConversation(page, 'Conversation A');
    await expect(composer).toBeEnabled();
    await expect(composer).toHaveValue('Draft stays here');
    await expect(page.getByText('正在读取会话…')).toHaveCount(0);
  });

  test('a successful stale send consumes its idempotency key before a later send', async ({ page }, testInfo) => {
    const controlled = await installControlledApi(page, testInfo, { terminalSendResponse: true });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await page.getByLabel('输入消息').fill('First A question');
    await page.getByRole('button', { name: '发送消息' }).click();
    await controlled.waitForOperation();

    await chooseConversation(page, 'Conversation B');
    const composer = page.getByLabel('输入消息');
    await expect(composer).toBeDisabled();
    controlled.releaseOperation();
    await expect(composer).toBeEnabled();

    await chooseConversation(page, 'Conversation A');
    await expect(page.locator('.run-status')).toContainText('A_FIRST_SEND_COMPLETE');
    await expect(composer).toBeEnabled();
    await composer.fill('Second A question');
    await page.getByRole('button', { name: '发送消息' }).click();

    await expect(page.getByText('Second A question')).toBeVisible();
    const keys = controlled.messageKeys();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });

  test('a successful stale database page consumes its key before the next page request', async ({ page }, testInfo) => {
    const firstPage = {
      id: 'source-a', ordinal: 1, available: true, source_type: 'database', source_title: 'Controlled database',
      referenced_relations: ['orders'], returned_rows: 1, can_continue: true, query_id: 'query-a', result_id: 'result-a',
      has_more: true, locator: { database_name: 'controlled', page_index: 0, page_size: 1, continuation_available: true },
    };
    const controlled = await installControlledApi(page, testInfo, {
      initialAssistantMessage: {
        id: 'message-a-assistant', role: 'assistant', content_text: 'First page', content_parts: null,
        status: 'completed', sources: [firstPage],
      },
      terminalPageResponse: true,
    });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await controlled.waitForOperation();

    await chooseConversation(page, 'Conversation B');
    controlled.releaseOperation();
    await expect(page.getByLabel('输入消息')).toBeEnabled();
    await chooseConversation(page, 'Conversation A');
    await expect(page.locator('.run-status')).toContainText('A_PAGE_COMPLETE');
    await page.getByRole('button', { name: '下一页', exact: true }).click();

    await expect.poll(() => controlled.pageKeys().length).toBe(2);
    const keys = controlled.pageKeys();
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });

  test('initial conversation loading blocks message, attachment and page mutations', async ({ page }, testInfo) => {
    const firstPage = {
      id: 'source-a', ordinal: 1, available: true, source_type: 'database', source_title: 'Controlled database',
      referenced_relations: ['orders'], returned_rows: 1, can_continue: true, query_id: 'query-a', result_id: 'result-a',
      has_more: true, locator: { database_name: 'controlled', page_index: 0, page_size: 1, continuation_available: true },
    };
    const controlled = await installControlledApi(page, testInfo, {
      initialRun: runA,
      initialAssistantMessage: {
        id: 'message-a-assistant', role: 'assistant', content_text: 'First page', content_parts: null,
        status: 'completed', sources: [firstPage],
      },
      delayInitialConversationLoad: true,
    });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await controlled.waitForInitialLoad();

    const composer = page.getByLabel('输入消息');
    const sendButton = page.getByRole('button', { name: '发送消息' });
    const attachButton = page.getByRole('button', { name: '添加附件' });
    const nextPageButton = page.getByRole('button', { name: '下一页', exact: true });
    await expect(nextPageButton).toBeVisible();
    await expect(composer).toBeDisabled();
    await expect(sendButton).toBeDisabled();
    await expect(attachButton).toBeDisabled();
    await expect(nextPageButton).toBeDisabled();

    await page.locator('input[type="file"]').setInputFiles({
      name: 'blocked.txt', mimeType: 'text/plain', buffer: Buffer.from('controlled'),
    });
    await composer.evaluate((element) => {
      const input = element as HTMLTextAreaElement;
      const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      setValue?.call(input, 'Synthetic draft solely to exercise the guarded submit handler');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.locator('form.composer').evaluate((form) => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    await nextPageButton.evaluate((button) => { (button as HTMLButtonElement).disabled = false; });
    await nextPageButton.click();
    expect(controlled.messageKeys()).toHaveLength(0);
    expect(controlled.pageKeys()).toHaveLength(0);
    expect(controlled.uploadRequests()).toBe(0);

    controlled.releaseInitialLoad();
    await expect(page.locator('.run-status')).toContainText('A_RUN_ACTIVE');
    expect(controlled.messageKeys()).toHaveLength(0);
    expect(controlled.pageKeys()).toHaveLength(0);
    expect(controlled.uploadRequests()).toBe(0);

    await page.getByRole('button', { name: '新建会话' }).click();
    await expect(page.getByRole('heading', { name: '新的对话' })).toBeVisible();
    await expect(composer).toBeEnabled();
    await composer.fill('Draft in the new conversation');
  });

  test('a delayed resume response cannot attach the old run to the selected conversation', async ({ page }, testInfo) => {
    const cancelledRun: Run = { id: 'run-a', status: 'cancelled', current_step: '', started_at: startedAt };
    const controlled = await installControlledApi(page, testInfo, { initialRun: cancelledRun });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await expect(page.getByRole('button', { name: /继续任务/ })).toBeVisible();
    await page.getByRole('button', { name: /继续任务/ }).click();
    await controlled.waitForOperation();

    await chooseConversation(page, 'Conversation B');
    const composer = page.getByLabel('输入消息');
    await composer.fill('B draft before resume returns');
    controlled.releaseOperation();

    await expect(page.getByRole('button', { name: '停止' })).toHaveCount(0);
    await expect(page.getByText('A_ONLY_REPLY')).toHaveCount(0);
    await expect(composer).toHaveValue('B draft before resume returns');
    expect(controlled.eventRequests()).toBe(0);

    await chooseConversation(page, 'Conversation A');
    await expect(page.locator('.run-status')).toContainText('A_RUN_ACTIVE');
    await expect(page.getByText('A_ONLY_REPLY')).toBeVisible();
  });

  test('a delayed database next-page response cannot attach its run to another conversation', async ({ page }, testInfo) => {
    const firstPage = {
      id: 'source-a', ordinal: 1, available: true, source_type: 'database', source_title: 'Controlled database',
      referenced_relations: ['orders'], returned_rows: 1, can_continue: true, query_id: 'query-a', result_id: 'result-a',
      has_more: true, locator: { database_name: 'controlled', page_index: 0, page_size: 1, continuation_available: true },
    };
    const assistantMessage = {
      id: 'message-a-assistant', role: 'assistant', content_text: 'First page', content_parts: null,
      status: 'completed', sources: [firstPage],
    };
    const controlled = await installControlledApi(page, testInfo, { initialAssistantMessage: assistantMessage });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await controlled.waitForOperation();

    await chooseConversation(page, 'Conversation B');
    controlled.releaseOperation();
    const composer = page.getByLabel('输入消息');
    await expect(composer).toBeEnabled();
    await composer.fill('B draft before page response');

    await expect(page.getByRole('button', { name: '停止' })).toHaveCount(0);
    await expect(page.getByText('A_ONLY_REPLY')).toHaveCount(0);
    await expect(composer).toHaveValue('B draft before page response');
    expect(controlled.eventRequests()).toBe(0);

    await chooseConversation(page, 'Conversation A');
    await expect(page.locator('.run-status')).toContainText('A_RUN_ACTIVE');
    await expect(page.getByText('A_ONLY_REPLY')).toBeVisible();
  });

  test('a stale send failure does not replace the newly selected conversation error', async ({ page }, testInfo) => {
    const controlled = await installControlledApi(page, testInfo, { rejectOperation: true });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await page.getByLabel('输入消息').fill('Question from A');
    await page.getByRole('button', { name: '发送消息' }).click();
    await controlled.waitForOperation();

    controlled.failBMessages();
    await chooseConversation(page, 'Conversation B');
    await expect(page.getByText('B_MESSAGES_ERROR')).toBeVisible();
    controlled.releaseOperation();

    await expect(page.getByText('B_MESSAGES_ERROR')).toBeVisible();
    await expect(page.getByText('A operation failed')).toHaveCount(0);
    expect(controlled.eventRequests()).toBe(0);
  });

  test('an empty event stream falls back to polling and terminal failure unlocks submit', async ({ page }, testInfo) => {
    const controlled = await installControlledApi(page, testInfo, { emptyEventStream: true, completeAfterPolls: 2 });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await page.getByLabel('输入消息').fill('Question with empty SSE');
    await page.getByRole('button', { name: '发送消息' }).click();
    await controlled.waitForOperation();
    controlled.releaseOperation();

    await expect(page.locator('.run-status')).toContainText('A_TERMINAL_FAILURE', { timeout: 6_000 });
    const composer = page.getByLabel('输入消息');
    await expect(composer).toBeEnabled();
    await composer.fill('Follow-up after failure');
    await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled();
    expect(controlled.eventRequests()).toBeGreaterThan(0);
  });

  test('stop reaches cancelled state and unlocks the composer', async ({ page }, testInfo) => {
    const controlled = await installControlledApi(page, testInfo, { initialRun: runA, emptyEventStream: true });
    await enterWorkspace(page);
    await chooseConversation(page, 'Conversation A');
    await expect(page.locator('.run-status')).toContainText('A_RUN_ACTIVE');
    await page.getByRole('button', { name: '停止' }).click();

    await expect(page.getByRole('button', { name: /继续任务/ })).toBeVisible();
    const composer = page.getByLabel('输入消息');
    await expect(composer).toBeEnabled();
    await composer.fill('Follow-up after stop');
    await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled();
    expect(controlled.eventRequests()).toBeGreaterThan(0);
  });
});
