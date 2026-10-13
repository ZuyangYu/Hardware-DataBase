/** Opt-in live-account quality workflow drill. Credentials stay in a 0600 file.
 * HDB_LIVE_QUALITY_CREDENTIALS points to {username: password}; artifacts contain
 * only workflow facts/screenshots. Each phase logs in through the real UI.
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(path.join(root, 'frontend/package.json'));
const { chromium, expect } = require('@playwright/test');
const credentialsPath = process.env.HDB_LIVE_QUALITY_CREDENTIALS;
if (!credentialsPath || (fs.statSync(credentialsPath).mode & 0o077)) throw new Error('Use a private 0600 credentials file');
const credentials = JSON.parse(fs.readFileSync(credentialsPath, 'utf8'));
const artifact = path.resolve(process.env.HDB_LIVE_QUALITY_ARTIFACT || path.join(root, 'storage/evaluations/live-quality-accounts-20261009'));
fs.mkdirSync(artifact, { recursive: true, mode: 0o700 });
const resultPath = path.join(artifact, 'workflow.json');
const result = fs.existsSync(resultPath) ? JSON.parse(fs.readFileSync(resultPath, 'utf8')) : {
  test_kind: 'live_accounts_synthetic_feedback_drill', base_url: process.env.HDB_BROWSER_BASE_URL || 'http://172.16.0.8:5176',
  owner: 'test_dept', administrator: 'admin', kb_name: 'ADAS', phases: [],
};
const phase = process.argv[2] || 'capture';
const errors = [];
const browser = await chromium.launch({ headless: true });
const contexts = [];
function save() { fs.writeFileSync(resultPath, JSON.stringify(result, null, 2) + '\n', { mode: 0o600 }); }
async function login(username) {
  const context = await browser.newContext({ baseURL: result.base_url, viewport: { width: 1440, height: 1000 }, timezoneId: 'UTC' });
  contexts.push(context);
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByPlaceholder('用户名', { exact: true }).fill(username);
  await page.getByPlaceholder('密码', { exact: true }).fill(credentials[username]);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem('hdb_auth_session') || '{}').user?.username)).toBe(username);
  return page;
}
async function request(page, method, endpoint, body, expected = 200) {
  const token = await page.evaluate(() => JSON.parse(localStorage.getItem('hdb_auth_session')).token);
  const response = await page.context().request.fetch(endpoint, { method, headers: { Authorization: `Bearer ${token}` }, data: body });
  if (response.status() !== expected) throw new Error(`${method} ${endpoint}: HTTP ${response.status()} ${await response.text()}`);
  return response.json();
}
async function screenshot(page, name) { await page.screenshot({ path: path.join(artifact, `${name}.png`), fullPage: true }); }
async function openCase(page, caseId) {
  await page.goto(`/admin/quality?case=${encodeURIComponent(caseId)}`);
  await expect(page.getByRole('heading', { name: '固定案例与复核', exact: true })).toBeVisible();
  await expect(page.getByText('正在读取授权材料…', { exact: true })).toHaveCount(0);
}
async function sendQuestion(page, title) {
  const pending = result.pending_capture;
  const session = pending?.session || await request(page, 'POST', '/api/v1/conversations', { kb_name: result.kb_name, title });
  await page.evaluate(({ username, kb, sessionId }) => sessionStorage.setItem(`hdb_chat_session:${encodeURIComponent(username)}:${encodeURIComponent(kb)}`, String(sessionId)), { username: result.owner, kb: result.kb_name, sessionId: session.id });
  await page.goto(`/chat?kb=${encodeURIComponent(result.kb_name)}`);
  let turn = pending?.turn;
  if (!turn) {
    const createdResponse = page.waitForResponse((response) => /\/conversations\/\d+\/turns$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST');
    await page.locator('textarea').fill(result.question);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    const response = await createdResponse;
    if (!response.ok()) throw new Error(`Create turn: HTTP ${response.status()} ${await response.text()}`);
    turn = (await response.json()).turn;
    result.pending_capture = { session, turn }; save();
  }
  await expect(page.locator(`[data-message-id="${turn.assistant_message_id}"]`).getByRole('button', { name: '没帮助', exact: true })).toBeVisible({ timeout: 240000 });
  const state = await request(page, 'GET', `/api/v1/turns/${turn.id}`);
  if (state.status !== 'completed') throw new Error(`Chat ended with ${state.status}`);
  return { session, turn: state };
}
async function review(page, caseId, judgment, correctness, notes) {
  await openCase(page, caseId);
  await page.getByLabel('判断').selectOption(judgment);
  await page.getByLabel('事实正确性').selectOption(correctness);
  await page.getByLabel('根因').selectOption('presentation');
  await page.getByLabel('参考答案（经确认后填写）', { exact: true }).fill(result.reference_answer);
  await page.getByLabel('评分要点 / rubric', { exact: true }).fill('R1108 标称阻值应为 10K，即 10 kΩ；依据冻结原理图证据回答，不虚构其他参数。');
  await page.getByLabel('复核说明', { exact: true }).fill(notes);
  await page.getByRole('button', { name: '保存复核结论', exact: true }).click();
  await expect(page.getByText('已保存复核结论', { exact: true })).toBeVisible();
  return request(page, 'GET', `/api/v1/quality/cases/${caseId}`);
}
async function waitRun(page, runId, expectedStatus) {
  let state;
  await expect.poll(async () => {
    state = await request(page, 'GET', `/api/v1/quality/evaluation-runs/${runId}`);
    return [expectedStatus, 'failed', 'cancelled'].includes(state.status);
  }, { timeout: 600000, intervals: [1000, 3000, 5000] }).toBe(true);
  if (state.status !== expectedStatus) throw new Error(`Evaluation ${runId}: ${state.status} ${state.error_message}`);
  return state;
}

try {
  if (phase === 'capture') {
    if (result.source_turn) throw new Error('Capture already exists; use the next phase');
    result.question = '质量闭环流程测试：请仅依据 ADAS 原理图索引，查证电阻 R1108 的标称阻值，并说明对应的原始标记。回答控制在三句话以内；证据不足时明确说明。';
    const owner = await login(result.owner);
    const captured = await sendQuestion(owner, '[质量闭环测试] R1108 反馈与回归 2026-10-09');
    result.session_id = captured.session.id;
    result.source_turn = { id: captured.turn.id, assistant_message_id: captured.turn.assistant_message_id, answer: captured.turn.answer, finished_at: captured.turn.finished_at, evidence_count: captured.turn.summary?.evidence?.length || 0 };
    const feedback = await request(owner, 'GET', `/api/v1/chat/turns/${captured.turn.id}/feedback`);
    result.answer_revision = feedback.answer_revision;
    delete result.pending_capture;
    await screenshot(owner, '01-answer-and-feedback');
  } else if (phase === 'review') {
    if (!result.source_turn) throw new Error('Capture required');
    result.reference_answer = 'ADAS 原理图中电阻 R1108 的标称阻值标记为 10K，即 10 kΩ（10000 Ω）。';
    const owner = await login(result.owner);
    await owner.evaluate(({ username, kb, sessionId }) => sessionStorage.setItem(`hdb_chat_session:${encodeURIComponent(username)}:${encodeURIComponent(kb)}`, String(sessionId)), { username: result.owner, kb: result.kb_name, sessionId: result.session_id });
    await owner.goto(`/chat?kb=${encodeURIComponent(result.kb_name)}`);
    const bubble = owner.locator(`[data-message-id="${result.source_turn.assistant_message_id}"]`);
    if (!result.case_id) {
      await bubble.getByRole('button', { name: '没帮助', exact: true }).click();
      await owner.getByLabel('格式不符', { exact: true }).check();
      await owner.getByLabel('补充说明（可选）', { exact: true }).fill('【质量闭环流程测试】模拟格式不符反馈，用于验收反馈、复核、固定评测及回访链路；不作为真实硬件事实错误。');
      const submitted = owner.waitForResponse((response) => response.url().endsWith('/feedback') && response.request().method() === 'POST');
      await owner.getByRole('button', { name: '提交反馈', exact: true }).click();
      const response = await submitted;
      if (response.status() !== 200) throw new Error(`Feedback: HTTP ${response.status()}`);
      const event = await response.json();
      result.case_id = event.case_id; result.feedback_event_id = event.event_id; save();
    }
    await expect(bubble.getByRole('button', { name: '没帮助', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await screenshot(owner, '02-negative-feedback');
    const admin = await login(result.administrator);
    await openCase(admin, result.case_id);
    await expect(admin.getByText('当前仅有元数据访问权限，正文与证据未共享或共享已失效。', { exact: true })).toBeVisible();
    await expect(admin.getByRole('button', { name: '保存复核结论', exact: true })).toHaveCount(0);
    const adminView = await request(admin, 'GET', `/api/v1/quality/cases/${result.case_id}`);
    if (adminView.content_access !== 'hidden' || Object.keys(adminView.content).length) throw new Error('Administrator content isolation failed');
    result.admin_check = { content_access: adminView.content_access, rating: adminView.feedback?.rating, reason_codes: adminView.feedback?.reason_codes, status: adminView.status };
    await screenshot(admin, '03-administrator-metadata');
    const forbidden = await request(admin, 'POST', `/api/v1/quality/cases/${result.case_id}/reviews`, { row_version: adminView.row_version, client_request_id: crypto.randomUUID(), judgment: 'confirmed', correctness: 'incorrect', root_cause: 'unknown' }, 403);
    result.admin_review_denied = Boolean(forbidden.detail);
    const reviewed = result.review_id ? await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}`) : await review(owner, result.case_id, 'confirmed', 'correct', '【流程测试】模拟呈现问题确认。R1108=10K 与固定证据一致，事实正确性单独记为 correct；该演练不代表已发现硬件事实缺陷。');
    result.review_id = reviewed.reviews.at(-1).review_id;
    result.review_status = reviewed.status;
    save();
    await openCase(owner, result.case_id);
    await screenshot(owner, '04-owner-review');
    if (!result.dataset_version_id) {
      const frozen = owner.waitForResponse((response) => /\/quality\/datasets\/production-repair\/versions$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST');
      await owner.getByRole('button', { name: '冻结已确认案例版本', exact: true }).click();
      const frozenResponse = await frozen;
      if (frozenResponse.status() !== 200) throw new Error(`Freeze: HTTP ${frozenResponse.status()} ${await frozenResponse.text()}`);
      result.dataset_version_id = (await frozenResponse.json()).version_id; save();
    } else {
      await owner.getByLabel('数据集版本 ID', { exact: true }).fill(result.dataset_version_id);
    }
    await owner.getByRole('button', { name: '读取当前授权的固定评测材料', exact: true }).click();
    await expect(owner.getByText('评测样本 / 生产回答快照', { exact: true })).toBeVisible();
    const material = await request(owner, 'GET', `/api/v1/quality/dataset-versions/${result.dataset_version_id}?materialize=true`);
    result.dataset_check = { sample_count: material.samples.length, snapshot_count: material.snapshots.length, manifest_sha256: material.manifest_sha256 };
    await screenshot(owner, '05-frozen-dataset');
  } else if (phase === 'evaluate') {
    if (!result.dataset_version_id) throw new Error('Frozen dataset required');
    const owner = await login(result.owner);
    await openCase(owner, result.case_id);
    await owner.getByLabel('数据集版本 ID', { exact: true }).fill(result.dataset_version_id);
    if (!result.baseline_run_id) {
      const responsePromise = owner.waitForResponse((response) => response.url().endsWith('/evaluation-runs') && response.request().method() === 'POST');
      await owner.getByRole('button', { name: '评分固定生产回答', exact: true }).click();
      const response = await responsePromise;
      if (response.status() !== 200) throw new Error(`Baseline: HTTP ${response.status()} ${await response.text()}`);
      result.baseline_run_id = (await response.json()).run_id; save();
    }
    const baseline = await waitRun(owner, result.baseline_run_id, 'completed');
    result.baseline_gate = baseline.summary?.gate; save();
    if (!result.candidate_run_id) {
      const responsePromise = owner.waitForResponse((response) => response.url().endsWith('/evaluation-runs') && response.request().method() === 'POST');
      await owner.getByRole('button', { name: '固定证据生成候选回答', exact: true }).click();
      const response = await responsePromise;
      if (response.status() !== 200) throw new Error(`Candidate: HTTP ${response.status()} ${await response.text()}`);
      result.candidate_run_id = (await response.json()).run_id; save();
    }
    let candidate = await request(owner, 'GET', `/api/v1/quality/evaluation-runs/${result.candidate_run_id}`);
    if (!['scoring', 'reporting'].includes(candidate.stage)) {
      if (candidate.status !== 'completed') {
        candidate = await waitRun(owner, result.candidate_run_id, 'collected');
        await owner.getByRole('button', { name: '刷新评测进度', exact: true }).click();
        await expect(owner.getByRole('button', { name: '开始候选评分', exact: true })).toBeVisible();
        await owner.getByRole('button', { name: '开始候选评分', exact: true }).click();
      }
    }
    candidate = await waitRun(owner, result.candidate_run_id, 'completed');
    result.candidate_gate = candidate.summary?.gate;
    result.candidate_answer = candidate.results?.[0]?.response;
    save();
    if (result.candidate_gate?.verdict !== 'pass') throw new Error(`Candidate gate: ${result.candidate_gate?.verdict}`);
    await owner.getByRole('button', { name: '刷新评测进度', exact: true }).click();
    await owner.getByLabel('修改引用（commit / issue）', { exact: true }).fill('test://quality-loop/20261009/R1108');
    await owner.getByLabel('基线 run ID', { exact: true }).fill(result.baseline_run_id);
    await owner.getByLabel('候选 run ID', { exact: true }).fill(result.candidate_run_id);
    await owner.getByRole('button', { name: '校验并关联回归证据', exact: true }).click();
    await expect(owner.getByText('已校验并关联回归证据', { exact: true })).toBeVisible();
    const linked = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}`);
    result.regression_status = linked.status;
    result.changes = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}/changes`);
    await screenshot(owner, '06-paired-regression');
  } else if (phase === 'blocked') {
    if (result.candidate_gate?.verdict !== 'fail') throw new Error('A failed candidate gate is required for this negative-path check');
    const owner = await login(result.owner);
    const before = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}`);
    const changesBefore = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}/changes`);
    await openCase(owner, result.case_id);
    await owner.getByLabel('数据集版本 ID', { exact: true }).fill(result.dataset_version_id);
    await owner.getByLabel('修改引用（commit / issue）', { exact: true }).fill('test://quality-loop/20261009/R1108-failed-gate');
    await owner.getByLabel('基线 run ID', { exact: true }).fill(result.baseline_run_id);
    await owner.getByLabel('候选 run ID', { exact: true }).fill(result.candidate_run_id);
    const rejected = owner.waitForResponse((response) => response.url().endsWith(`/quality/cases/${result.case_id}/changes`) && response.request().method() === 'POST');
    await owner.getByRole('button', { name: '校验并关联回归证据', exact: true }).click();
    const response = await rejected;
    if (response.status() !== 409) throw new Error(`Failed gate association returned HTTP ${response.status()}`);
    const rejection = await response.json();
    if (!rejection.detail?.includes('not passed its gate')) throw new Error(`Unexpected rejection: ${rejection.detail}`);
    await expect(owner.getByText(rejection.detail, { exact: true })).toBeVisible();
    await screenshot(owner, '06-failed-gate-blocks-association');
    const release = JSON.parse(fs.readFileSync(path.join(artifact, 'existing-release.json'), 'utf8'));
    const closureRejected = await request(owner, 'POST', `/api/v1/quality/cases/${result.case_id}/changes`, {
      row_version: before.row_version, dataset_version_id: result.dataset_version_id,
      change_ref: 'test://quality-loop/20261009/R1108-failed-closure', baseline_run_id: result.baseline_run_id,
      candidate_run_id: result.candidate_run_id, release_ref: release.release_ref,
      deployed_at: release.deployed_at, validation_turn_id: result.source_turn.id,
    }, 409);
    if (!closureRejected.detail?.includes('not passed its gate')) throw new Error('Failed gate was not enforced before closure');
    const after = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}`);
    const changesAfter = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}/changes`);
    if (after.status !== before.status || after.row_version !== before.row_version || changesAfter.length !== changesBefore.length) throw new Error('Rejected association mutated the case');
    result.final_status = after.status;
    result.gate_blocking_check = { association_http: 409, closure_http: 409, detail: rejection.detail,
      status: after.status, row_version: after.row_version, change_event_count: changesAfter.length };
    const admin = await login(result.administrator);
    await openCase(admin, result.case_id);
    await expect(admin.getByText(/状态：已确认问题/)).toBeVisible();
    await screenshot(admin, '07-administrator-blocked-case');
  } else if (phase === 'close') {
    if (result.regression_status !== 'regression_verified') throw new Error('Verified paired regression required');
    const release = JSON.parse(fs.readFileSync(path.join(artifact, 'existing-release.json'), 'utf8'));
    const owner = await login(result.owner);
    if (!result.validation_turn) {
      const captured = await sendQuestion(owner, '[质量闭环测试] R1108 上线回访 2026-10-09');
      if (!/10\s*(?:k|千)/i.test(captured.turn.answer)) throw new Error('Validation answer requires manual fact review');
      result.validation_session_id = captured.session.id;
      result.validation_turn = { id: captured.turn.id, assistant_message_id: captured.turn.assistant_message_id, answer: captured.turn.answer, finished_at: captured.turn.finished_at };
      delete result.pending_capture; save();
    } else {
      await owner.evaluate(({ username, kb, sessionId }) => sessionStorage.setItem(`hdb_chat_session:${encodeURIComponent(username)}:${encodeURIComponent(kb)}`, String(sessionId)), { username: result.owner, kb: result.kb_name, sessionId: result.validation_session_id });
      await owner.goto(`/chat?kb=${encodeURIComponent(result.kb_name)}`);
    }
    if (!result.validation_case_id) {
      const submitted = owner.waitForResponse((response) => response.url().endsWith('/feedback') && response.request().method() === 'POST');
      await owner.locator(`[data-message-id="${result.validation_turn.assistant_message_id}"]`).getByRole('button', { name: '有帮助', exact: true }).click();
      const response = await submitted;
      if (response.status() !== 200) throw new Error(`Validation feedback: HTTP ${response.status()}`);
      result.validation_case_id = (await response.json()).case_id; save();
    }
    let validation = await request(owner, 'GET', `/api/v1/quality/cases/${result.validation_case_id}`);
    if (validation.manifest.code_revision !== release.release_ref || new Date(validation.created_at) < new Date(release.deployed_at)) throw new Error('Validation is not bound to the observed running deployment');
    if (!result.validation_review_id) {
      validation = await review(owner, result.validation_case_id, 'not_an_error', 'correct', '【流程测试回访】在实际运行版本上重新执行同一提问，R1108=10K 与原理图证据一致，回访确认为正确。');
      result.validation_review_id = validation.reviews.at(-1).review_id; save();
    }
    await screenshot(owner, '07-validation-review');
    await openCase(owner, result.case_id);
    await owner.getByLabel('数据集版本 ID', { exact: true }).fill(result.dataset_version_id);
    await owner.getByLabel('修改引用（commit / issue）', { exact: true }).fill('test://quality-loop/20261009/R1108');
    await owner.getByLabel('基线 run ID', { exact: true }).fill(result.baseline_run_id);
    await owner.getByLabel('候选 run ID', { exact: true }).fill(result.candidate_run_id);
    await owner.getByLabel('发布代码 revision（可选）', { exact: true }).fill(release.release_ref);
    await owner.getByLabel('已部署时间（可选）', { exact: true }).fill(new Date(release.deployed_at).toISOString().slice(0, 23));
    await owner.getByLabel('上线验证 turn ID（可选）', { exact: true }).fill(result.validation_turn.id);
    const linked = owner.waitForResponse((response) => response.url().endsWith(`/quality/cases/${result.case_id}/changes`) && response.request().method() === 'POST');
    await owner.getByRole('button', { name: '校验并关联回归证据', exact: true }).click();
    const response = await linked;
    if (response.status() !== 200) throw new Error(`Closure: HTTP ${response.status()} ${await response.text()}`);
    result.closure = await response.json();
    const closed = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}`);
    if (closed.status !== 'closed') throw new Error(`Final case status: ${closed.status}`);
    result.final_status = closed.status;
    result.release = release;
    result.changes = await request(owner, 'GET', `/api/v1/quality/cases/${result.case_id}/changes`);
    save();
    const admin = await login(result.administrator);
    await openCase(admin, result.case_id);
    await expect(admin.getByText(/状态：已上线验证/)).toBeVisible();
    await screenshot(admin, '08-closed-administrator-case');
  } else if (phase === 'report') {
    const admin = await login(result.administrator);
    await admin.goto('/admin/quality');
    const generated = admin.waitForResponse((response) => response.url().endsWith('/quality/analysis-runs') && response.request().method() === 'POST');
    await admin.getByRole('button', { name: '生成统计报告', exact: true }).click();
    const response = await generated;
    if (response.status() !== 200) throw new Error(`Report: HTTP ${response.status()} ${await response.text()}`);
    result.report_id = (await response.json()).report_id;
    result.report = await request(admin, 'GET', `/api/v1/quality/reports/${result.report_id}`);
    if (!result.report.verified_statistics.case_revisions.some(([caseId]) => caseId === result.case_id)) throw new Error('Generated report does not include the tested case');
    await expect(admin.locator('details summary').first()).toBeVisible();
    await admin.locator('details summary').first().click();
    await expect(admin.getByText(`输入版本：${result.report.input_manifest_sha256}`, { exact: true })).toBeVisible();
    await screenshot(admin, '09-administrator-report');
  } else {
    throw new Error(`Unsupported phase: ${phase}`);
  }
  if (errors.length) throw new Error(`Browser errors: ${errors.join('; ')}`);
  result.phases.push({ phase, status: 'passed', completed_at: new Date().toISOString() });
  delete result.last_error;
  save();
  console.log(JSON.stringify({ phase, status: 'passed', session_id: result.session_id, turn_id: result.source_turn?.id, case_id: result.case_id, dataset_version_id: result.dataset_version_id, baseline_run_id: result.baseline_run_id, candidate_run_id: result.candidate_run_id, regression_status: result.regression_status, final_status: result.final_status, report_id: result.report_id }));
} catch (error) {
  result.last_error = { phase, message: error.message, observed_at: new Date().toISOString() };
  save();
  throw error;
} finally {
  for (const context of contexts) {
    for (const page of context.pages()) {
      try { await request(page, 'POST', '/api/v1/logout'); } catch { /* Login can fail before a session exists. */ }
    }
    await context.close();
  }
  await browser.close();
}
