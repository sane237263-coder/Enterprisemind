import path from 'node:path';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test, expect } from '@playwright/test';
import {
  credentials,
  login,
  realBrowserEnabled,
  triggerAndSaveDownload,
  withBrowserCase,
} from './phase10-fixtures';

test('P10-07-HARNESS-BROWSER-START static frontend shell', async ({ page, browser }, testInfo) => {
  await withBrowserCase(page, browser, testInfo, 'P10-07-HARNESS-BROWSER-START', async (_directory, result) => {
    const response = await page.goto('/');
    expect(response?.status()).toBe(200);
    await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
    result.actual_layer = 'deterministic';
  });
});

test.describe('EnterpriseMind P10-07 browser acceptance', () => {
  test.beforeEach(({ }, testInfo) => {
    test.skip(!realBrowserEnabled(), 'Set P10_RUN_REAL_BROWSER=1 to execute against the live isolated application.');
    // Invalid credentials are intentionally self-contained and must remain
    // runnable even when the valid admin Secret is not injected.
    test.skip(
      !testInfo.title.includes('AUTH-INVALID') && !credentials('admin'),
      'P10_ADMIN_USERNAME and P10_ADMIN_PASSWORD are required for authenticated paths; values are never recorded.',
    );
  });

  test('P10-07-AUTH-LOGIN valid identity and logout', async ({ page, browser }, testInfo) => {
    const user = credentials('admin');
    test.skip(!user, 'P10_ADMIN_USERNAME and P10_ADMIN_PASSWORD are required; values are never recorded.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-AUTH-LOGIN', async (_directory, result) => {
      await login(page, user!);
      await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '知识助手' })).toBeVisible();
      await page.getByRole('button', { name: '退出登录' }).click();
      await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
      result.actual_layer = 'real';
    });
  });

  test('P10-07-AUTH-INVALID invalid credentials stay unauthenticated', async ({ page, browser }, testInfo) => {
    await withBrowserCase(page, browser, testInfo, 'P10-07-AUTH-INVALID', async (_directory, result) => {
      await page.goto('/');
      await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
      await page.getByLabel('用户名').fill(process.env.P10_INVALID_USERNAME ?? 'p10-invalid-user');
      await page.getByLabel('密码').fill(process.env.P10_INVALID_PASSWORD ?? 'invalid-password');
      await page.getByRole('button', { name: /登录/ }).click();
      await expect(page.getByRole('alert')).toBeVisible();
      await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
      result.actual_layer = 'real';
    });
  });

  test('P10-07-NAV-ALL-PAGES permitted navigation is visible', async ({ page, browser }, testInfo) => {
    const user = credentials('admin');
    test.skip(!user, 'P10_ADMIN_USERNAME and P10_ADMIN_PASSWORD are required; values are never recorded.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-NAV-ALL-PAGES', async () => {
      await login(page, user!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      for (const label of ['知识助手', '知识库', '我的 Memory', '回收站', '成员管理']) {
        const item = navigation.getByRole('button', { name: label });
        await expect(item).toBeVisible();
        await item.click();
        await expect(item).toHaveAttribute('aria-current', 'page');
      }
    });
  });

  test('P10-07-EDGE-SMOKE login, source-visible agent shell, logout', async ({ page, browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'edge', 'Representative Windows Edge smoke only.');
    const user = credentials('admin');
    test.skip(!user, 'P10_ADMIN_USERNAME and P10_ADMIN_PASSWORD are required; values are never recorded.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-EDGE-SMOKE', async () => {
      await login(page, user!);
      await expect(page.getByText('仅使用你有权访问的资料')).toBeVisible();
      await page.getByRole('button', { name: '退出登录' }).click();
      await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible();
    });
  });

  test('P10-07-ADMIN-MANAGEMENT create isolated department and ordinary user', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const ordinary = credentials('user');
    const department = process.env.P10_TEST_DEPARTMENT;
    test.skip(!admin || !ordinary || !department, 'Admin and ordinary-user Secret paths plus P10_TEST_DEPARTMENT are required; values are never recorded.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-ADMIN-MANAGEMENT', async (_directory, result) => {
      await login(page, admin!);
      await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '成员管理' }).click();
      await expect(page.getByRole('heading', { name: '成员管理' })).toBeVisible();

      await page.getByRole('button', { name: '部门', exact: true }).click();
      await page.getByRole('button', { name: '创建部门' }).click();
      const departmentDialog = page.getByRole('dialog');
      await departmentDialog.getByLabel('部门名称').fill(department!);
      await departmentDialog.getByRole('button', { name: '保存' }).click();
      await expect(page.getByText(department!, { exact: true })).toBeVisible();

      await page.getByRole('button', { name: '用户', exact: true }).click();
      await page.getByRole('button', { name: '创建用户' }).click();
      const userDialog = page.getByRole('dialog');
      await userDialog.getByLabel('用户名').fill(ordinary!.username);
      await userDialog.getByLabel('所属部门').selectOption({ label: department! });
      await userDialog.getByLabel('初始密码').fill(ordinary!.password);
      await userDialog.getByRole('button', { name: '保存' }).click();
      await expect(page.getByText(ordinary!.username, { exact: true })).toBeVisible();
      result.actual_layer = 'real';
    });
  });

  test('P10-07-ORDINARY-AUTHZ ordinary user cannot see administrator navigation', async ({ page, browser }, testInfo) => {
    const user = credentials('user');
    test.skip(!user, 'P10_USER_USERNAME and P10_USER_PASSWORD_FILE are required; values are never recorded.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-ORDINARY-AUTHZ', async (_directory, result) => {
      await login(page, user!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      await expect(navigation.getByRole('button', { name: '成员管理' })).toHaveCount(0);
      for (const label of ['知识库', '我的 Memory', '回收站']) {
        const item = navigation.getByRole('button', { name: label });
        await expect(item).toBeVisible();
        await item.click();
        await expect(item).toHaveAttribute('aria-current', 'page');
      }
      result.actual_layer = 'real';
    });
  });

  test('P10-07-KNOWLEDGE-LIFECYCLE create folder, upload, parse, same-name reject and download', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const sourceFixture = process.env.P10_SOURCE_FIXTURE;
    const knowledgeBase = process.env.P10_TEST_KNOWLEDGE_BASE;
    const folder = process.env.P10_TEST_FOLDER;
    test.skip(!admin || !sourceFixture || !knowledgeBase || !folder, 'Admin Secret path, source fixture and unique KB/folder names are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-KNOWLEDGE-LIFECYCLE', async (directory, result) => {
      await login(page, admin!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      await navigation.getByRole('button', { name: '知识库' }).click();
      await page.getByRole('button', { name: '创建知识库' }).click();
      let dialog = page.getByRole('dialog');
      await dialog.getByLabel('名称').fill(knowledgeBase!);
      await dialog.getByLabel('说明').fill('P10 browser lifecycle fixture with real API, Worker and MinIO.');
      await dialog.getByLabel('知识库类型').selectOption('public');
      await dialog.getByRole('button', { name: '保存' }).click();
      await expect(page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') })).toBeVisible();
      await page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') }).click();

      await page.getByRole('button', { name: '新建文件夹' }).click();
      dialog = page.getByRole('dialog');
      await dialog.getByLabel('名称').fill(folder!);
      await dialog.getByRole('button', { name: '保存' }).click();
      await expect(page.getByRole('button', { name: folder!, exact: true })).toBeVisible();
      await page.getByRole('button', { name: folder!, exact: true }).click();

      const fixtureName = sourceFixture!.split(/[\\/]/).pop()!;
      await page.getByRole('button', { name: '上传文件' }).click();
      dialog = page.getByRole('dialog');
      await dialog.locator('input[type="file"]').setInputFiles(sourceFixture!);
      await dialog.getByRole('button', { name: '上传并处理' }).click();
      await expect(page.getByRole('status')).toContainText('1 个文件已提交处理');
      // The upload response acknowledges the task; revisit the base/folder so
      // the browser reads the durable document row before asserting Worker
      // completion and downloading the object.
      await page.getByRole('navigation', { name: '文件路径' }).getByRole('button', { name: '知识库', exact: true }).click();
      await page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') }).click();
      await page.getByRole('button', { name: folder!, exact: true }).click();
      const row = page.getByRole('row').filter({ hasText: fixtureName });
      await expect(row).toBeVisible({ timeout: 15_000 });

      // The live Worker state must reach a terminal success before the file
      // download is accepted as a real ingestion result.
      await expect(row).toContainText('已完成', { timeout: 45_000 });
      const downloadEvidence = await triggerAndSaveDownload(
        page,
        directory,
        async () => row.getByRole('button', { name: `下载 ${fixtureName}` }).click(),
        'original',
      );
      const uploadedBytes = await readFile(sourceFixture!);
      expect(downloadEvidence.sha256).toBe(createHash('sha256').update(uploadedBytes).digest('hex'));
      expect(downloadEvidence.bytes).toBe(uploadedBytes.length);
      downloadEvidence.structure_checks.push('original-sha256-matches-upload');
      result.downloads.push(downloadEvidence);

      // Submit the exact same bytes again and require the visible conflict
      // branch; a second document must not silently appear.
      await page.getByRole('button', { name: '上传文件' }).click();
      dialog = page.getByRole('dialog');
      await dialog.locator('input[type="file"]').setInputFiles(sourceFixture!);
      await dialog.getByRole('button', { name: '上传并处理' }).click();
      await expect(page.getByRole('status')).toContainText('同名文件未保存', { timeout: 15_000 });
      result.actual_layer = 'real';
    });
  });

  test('P10-07-RECYCLE-AUTHZ delete and restore a real document', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const knowledgeBase = process.env.P10_EXISTING_KB;
    const folder = process.env.P10_EXISTING_FOLDER;
    const filename = process.env.P10_EXISTING_FILENAME;
    test.skip(!admin || !knowledgeBase || !folder || !filename, 'Admin Secret path and an existing successful browser document are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-RECYCLE-AUTHZ', async (_directory, result) => {
      await login(page, admin!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      await navigation.getByRole('button', { name: '知识库' }).click();
      await page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') }).click();
      await page.getByRole('button', { name: folder!, exact: true }).click();
      const row = page.getByRole('row').filter({ hasText: filename! });
      await expect(row).toContainText('已完成', { timeout: 15_000 });
      await row.getByRole('button', { name: `删除 ${filename}` }).click();
      await expect(page.getByRole('dialog')).toContainText('确认操作');
      await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
      await expect(row).toHaveCount(0, { timeout: 15_000 });

      await navigation.getByRole('button', { name: '回收站' }).click();
      const recycled = page.getByRole('row').filter({ hasText: filename! });
      await expect(recycled).toBeVisible({ timeout: 15_000 });
      await recycled.getByRole('button', { name: '恢复', exact: true }).click();
      const restoreDialog = page.getByRole('dialog');
      await restoreDialog.getByLabel('恢复后的名称').fill(filename!);
      await restoreDialog.getByRole('button', { name: '恢复', exact: true }).click();
      await expect(recycled).toHaveCount(0, { timeout: 15_000 });
      result.actual_layer = 'real';
    });
  });

  test('P10-07-MEMORY own Memory create update delete', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const subject = process.env.P10_MEMORY_SUBJECT;
    const content = process.env.P10_MEMORY_CONTENT;
    test.skip(!admin || !subject || !content, 'Admin Secret path and unique Memory fixture values are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-MEMORY', async (_directory, result) => {
      await login(page, admin!);
      await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '我的 Memory' }).click();
      await page.getByRole('button', { name: '新增记忆' }).click();
      let dialog = page.getByRole('dialog');
      await dialog.getByLabel('主题（可选）').fill(subject!);
      await dialog.getByLabel('内容').fill(content!);
      await dialog.getByRole('button', { name: '保存', exact: true }).click();
      let row = page.getByRole('row').filter({ hasText: subject! });
      await expect(row).toContainText(content!);

      await row.getByRole('button', { name: '编辑' }).click();
      dialog = page.getByRole('dialog');
      const revised = `${content!}；browser revision`;
      await dialog.getByLabel('内容').fill(revised);
      await dialog.getByRole('button', { name: '保存', exact: true }).click();
      row = page.getByRole('row').filter({ hasText: subject! });
      await expect(row).toContainText(revised);

      page.once('dialog', (browserDialog) => void browserDialog.accept());
      await row.getByRole('button', { name: '删除' }).click();
      await expect(row).toHaveCount(0);
      result.actual_layer = 'real';
    });
  });

  test('P10-07-RAG-NO-ANSWER-SCOPE live assistant reports missing evidence', async ({ page, browser }, testInfo) => {
    // The configured live planner may use three 120-second provider attempts.
    test.setTimeout(420_000);
    const admin = credentials('admin');
    const query = process.env.P10_RAG_NO_ANSWER_QUERY;
    test.skip(!admin || !query, 'Admin Secret path and a unique no-answer query are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-RAG-NO-ANSWER-SCOPE', async (_directory, result) => {
      await login(page, admin!);
      const composer = page.getByLabel('输入消息');
      await composer.fill(query!);
      await page.getByRole('button', { name: '发送消息' }).click();
      const assistant = page.locator('article.message.assistant').last();
      await expect(assistant).toBeVisible({ timeout: 390_000 });
      await expect(assistant).toContainText(/没有找到|缺少依据|无法给出|补充资料|缩小问题范围/, { timeout: 15_000 });
      await expect(assistant.locator('.sources')).toHaveCount(0);
      result.actual_layer = 'real';
    });
  });

  test('P10-07-OFFICE-VISUAL upload Office/PDF/image fixtures and record terminal processing', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const fixtures = (process.env.P10_OFFICE_FIXTURES ?? '').split('|').filter(Boolean);
    const knowledgeBase = process.env.P10_OFFICE_KNOWLEDGE_BASE;
    const folder = process.env.P10_OFFICE_FOLDER;
    test.skip(!admin || fixtures.length !== 4 || !knowledgeBase || !folder, 'Admin Secret path, four Office/PDF/image fixtures and unique destination names are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-OFFICE-VISUAL', async (_directory, result) => {
      await login(page, admin!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      await navigation.getByRole('button', { name: '知识库' }).click();
      await page.getByRole('button', { name: '创建知识库' }).click();
      let dialog = page.getByRole('dialog');
      await dialog.getByLabel('名称').fill(knowledgeBase!);
      await dialog.getByLabel('说明').fill('P10 Office/PDF/image browser fixture.');
      await dialog.getByLabel('知识库类型').selectOption('public');
      await dialog.getByRole('button', { name: '保存' }).click();
      await page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') }).click();
      await page.getByRole('button', { name: '新建文件夹' }).click();
      dialog = page.getByRole('dialog');
      await dialog.getByLabel('名称').fill(folder!);
      await dialog.getByRole('button', { name: '保存' }).click();
      await page.getByRole('button', { name: folder!, exact: true }).click();

      await page.getByRole('button', { name: '上传文件' }).click();
      dialog = page.getByRole('dialog');
      await dialog.locator('input[type="file"]').setInputFiles(fixtures);
      await dialog.getByRole('button', { name: '上传并处理' }).click();
      await expect(page.getByRole('status')).toContainText('4 个文件已提交处理');
      await page.getByRole('navigation', { name: '文件路径' }).getByRole('button', { name: '知识库', exact: true }).click();
      await page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') }).click();
      await page.getByRole('button', { name: folder!, exact: true }).click();

      const filenames = fixtures.map((fixture) => fixture.split(/[\\/]/).pop()!);
      const statuses: string[] = [];
      for (const filename of filenames) {
        const row = page.getByRole('row').filter({ hasText: filename });
        await expect(row).toBeVisible({ timeout: 20_000 });
        await expect(row).toContainText(/已完成|部分完成|失败/, { timeout: 60_000 });
        statuses.push(await row.innerText());
      }
      // Office/PDF parsing is deterministic in the Worker. Image OCR/Vision
      // may be an explicit provider failure; either terminal result is kept
      // in the screenshot and must not be mislabeled as successful rendering.
      expect(statuses.slice(0, 3).every((value) => value.includes('已完成'))).toBeTruthy();
      result.observations = statuses;
      result.actual_layer = 'real';
    });
  });

  test('P10-07-OFFICE-FAILURE-DETAIL inspect image processing failure reason', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const knowledgeBase = process.env.P10_OFFICE_FAILURE_KNOWLEDGE_BASE;
    const folder = process.env.P10_OFFICE_FAILURE_FOLDER;
    const imageName = process.env.P10_OFFICE_FAILURE_IMAGE;
    test.skip(!admin || !knowledgeBase || !folder || !imageName, 'Admin Secret path and the existing image failure fixture are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-OFFICE-FAILURE-DETAIL', async (directory, result) => {
      await login(page, admin!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      await navigation.getByRole('button', { name: '知识库' }).click();
      await page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') }).click();
      await page.getByRole('button', { name: folder!, exact: true }).click();
      const row = page.getByRole('row').filter({ hasText: imageName! });
      await expect(row).toBeVisible({ timeout: 20_000 });
      await expect(row).toContainText('失败', { timeout: 60_000 });
      await row.getByRole('button', { name: '查看原因' }).click();
      const detail = page.getByRole('dialog');
      await expect(detail).toBeVisible();
      const detailText = await detail.innerText();
      expect(detailText).toMatch(/处理详情|失败|OCR|Vision|服务|错误|不可用/);
      result.observations = [detailText];
      const detailScreenshot = path.join(directory, 'failure-detail.png');
      await page.screenshot({ path: detailScreenshot, fullPage: true });
      result.screenshot = path.relative(process.env.P10_BROWSER_EVIDENCE_DIR ?? '', detailScreenshot).replaceAll('\\', '/');
      result.actual_layer = 'real';
    });
  });

  test('P10-07-DOCUMENT-PREVIEW preview stored Office/PDF bytes in the browser', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const documents = (process.env.P10_PREVIEW_DOCUMENTS ?? '').split('|').filter(Boolean);
    const knowledgeBase = process.env.P10_PREVIEW_KNOWLEDGE_BASE;
    const folder = process.env.P10_PREVIEW_FOLDER;
    test.skip(!admin || documents.length !== 3 || !knowledgeBase || !folder, 'Admin Secret path, successful Office/PDF document names and destination are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-DOCUMENT-PREVIEW', async (_directory, result) => {
      await login(page, admin!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      await navigation.getByRole('button', { name: '知识库' }).click();
      await page.getByRole('button', { name: new RegExp(knowledgeBase!, 'i') }).click();
      await page.getByRole('button', { name: folder!, exact: true }).click();
      for (const document of documents) {
        const row = page.getByRole('row').filter({ hasText: document });
        await expect(row).toContainText('已完成', { timeout: 15_000 });
        await row.locator('button.file-name').click();
        const dialog = page.getByRole('dialog');
        await expect(dialog.getByRole('heading', { name: document, exact: true })).toBeVisible();
        await expect(dialog.getByRole('button', { name: '下载原文件' })).toBeVisible({ timeout: 20_000 });
        await dialog.getByRole('button', { name: '关闭' }).click();
      }
      result.actual_layer = 'real';
    });
  });

  test('P10-07-KB-PUBLIC-DEPARTMENT public KB visible, other-department KB hidden', async ({ page, browser }, testInfo) => {
    const admin = credentials('admin');
    const ordinary = credentials('user');
    const department = process.env.P10_OTHER_DEPARTMENT;
    const departmentBase = process.env.P10_OTHER_DEPARTMENT_KB;
    const publicBase = process.env.P10_PUBLIC_KB;
    test.skip(!admin || !ordinary || !department || !departmentBase || !publicBase, 'Admin/user Secret paths and unique KB/department fixtures are required.');
    await withBrowserCase(page, browser, testInfo, 'P10-07-KB-PUBLIC-DEPARTMENT', async (_directory, result) => {
      await login(page, admin!);
      const navigation = page.getByRole('navigation', { name: '主导航' });
      await navigation.getByRole('button', { name: '成员管理' }).click();
      await page.getByRole('button', { name: '部门', exact: true }).click();
      await page.getByRole('button', { name: '创建部门' }).click();
      let dialog = page.getByRole('dialog');
      await dialog.getByLabel('部门名称').fill(department!);
      await dialog.getByRole('button', { name: '保存' }).click();
      await expect(page.getByText(department!, { exact: true })).toBeVisible();

      await navigation.getByRole('button', { name: '知识库' }).click();
      await page.getByRole('button', { name: '创建知识库' }).click();
      dialog = page.getByRole('dialog');
      await dialog.getByLabel('名称').fill(departmentBase!);
      await dialog.getByLabel('说明').fill('P10 other-department authorization fixture.');
      await dialog.getByLabel('知识库类型').selectOption('department');
      await dialog.getByLabel('所属部门（部门知识库必选）').selectOption({ label: department! });
      await dialog.getByRole('button', { name: '保存' }).click();
      await expect(page.getByText(departmentBase!, { exact: true })).toBeVisible();

      await page.getByRole('button', { name: '退出登录' }).click();
      await login(page, ordinary!);
      await navigation.getByRole('button', { name: '知识库' }).click();
      await expect(page.getByText(publicBase!, { exact: true })).toBeVisible();
      await expect(page.getByText(departmentBase!, { exact: true })).toHaveCount(0);
      result.actual_layer = 'real';
    });
  });
});
