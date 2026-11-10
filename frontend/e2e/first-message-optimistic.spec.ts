import { expect, test, type Page } from '@playwright/test';

const user = { username: 'first-send', role: 'employee', department_id: 7, department_name: '研发部' };
const kb = {
  name: 'hardware',
  kb_id: 7,
  department_id: 7,
  department_name: '研发部',
  permission: 'write',
  registered: true,
};

async function installChat(page: Page) {
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'first-send-test', user: sessionUser }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/kbs') return route.fulfill({ json: [kb] });
    if (path === '/api/v1/conversations' && request.method() === 'GET') return route.fulfill({ json: [] });
    if (path === '/api/v1/conversations' && request.method() === 'POST') {
      return route.fulfill({
        json: {
          id: 100,
          user_id: user.username,
          kb_name: kb.name,
          title: '新对话',
          created_at: '2026-10-27T00:00:00Z',
          updated_at: '2026-10-27T00:00:00Z',
        },
      });
    }
    if (path === '/api/v1/conversations/100/messages') {
      // Reproduce the first-send race: the history loads empty before the
      // create-turn request has persisted the user message.
      await new Promise((resolve) => setTimeout(resolve, 50));
      return route.fulfill({ json: [] });
    }
    if (path === '/api/v1/conversations/100/turns' && request.method() === 'POST') {
      await new Promise((resolve) => setTimeout(resolve, 250));
      return route.fulfill({
        json: {
          turn: {
            id: 'turn-first',
            session_id: 100,
            user_message_id: 11,
            assistant_message_id: 12,
            kb_name: kb.name,
            query: '首次消息不能消失',
            query_mode: 'deep',
            status: 'pending',
            cancel_requested: false,
            last_event_seq: 0,
            answer: '',
            summary: {},
            footer: '',
            metrics: {},
            error_message: '',
            created_at: '2026-10-27T00:00:00Z',
            started_at: null,
            finished_at: null,
          },
          user_message: {
            id: 11,
            session_id: 100,
            turn_id: 'turn-first',
            role: 'user',
            content: '首次消息不能消失',
            footer: '',
            created_at: '2026-10-27T00:00:00Z',
            memory_context: [],
            attachments: [],
            citations: [],
            trace_steps: [],
            plan: null,
            budget: null,
            verification: null,
            subagents: [],
          },
        },
      });
    }
    if (path === '/api/v1/turns/turn-first/start' && request.method() === 'POST') {
      return route.fulfill({ json: {} });
    }
    if (path === '/api/v1/turns/turn-first/events') {
      return route.fulfill({
        contentType: 'text/event-stream',
        body: `event: done\ndata: ${JSON.stringify({ answer: '首轮回答', summary: {}, footer: '' })}\n\n`,
      });
    }
    if (path === '/api/v1/conversations/100/artifacts') {
      return route.fulfill({ json: { items: [] } });
    }
    if (path === '/api/v1/conversations/100/memory-summary') {
      return route.fulfill({ json: { auto_extract_enabled: false, extracted_memories: 0 } });
    }
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/plugins/mcp') return route.fulfill({ json: { enabled: false, items: [] } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [], total: 0 } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/chat?kb=hardware');
  await expect(page.getByText(/当前检索范围：hardware/)).toBeVisible();
  await expect(page.locator('textarea').last()).toBeVisible();
}

test('首次发送的用户消息不会被会话消息加载清掉', async ({ page }) => {
  await installChat(page);

  const composer = page.locator('textarea').last();
  await composer.fill('首次消息不能消失');
  await page.getByRole('button', { name: '发送', exact: true }).click();

  // Wait for the turn to finish.  On the buggy path the empty history load
  // has already removed the optimistic user message by this point.
  await expect(page.getByText('首轮回答', { exact: true })).toBeVisible();
  const userBubble = page.getByRole('main')
    .locator('div.whitespace-pre-wrap')
    .filter({ hasText: '首次消息不能消失' });
  await expect(userBubble).toHaveCount(1);
  await expect(userBubble).toBeVisible();
});
