import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App, { GUEST_ENTRY_KEY } from './App';
import { accountMock, json, userIdentity } from './test/account';
import { CONVERSATIONS_KEY, saveConversations } from './lib/storage';
import { conversation } from './test/conversations';
import { accountAPI } from './api/account';
import { fixtureFor } from './mocks/fixtures';
import { controlledSSE } from './test/streams';

function ask(question: string) {
  fireEvent.change(screen.getByRole('textbox', { name: '输入问题' }), { target: { value: question } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
}
function seed(mock: ReturnType<typeof accountMock>) {
  mock.state.items = ['a', 'b'].map(id => ({ id, title: `服务器${id}`, updated_at: new Date().toISOString(), message_count: 2 }));
  for (const { id, title } of mock.state.items) mock.state.details[id] = { id, title, messages: [
    { role: 'user', content: `旧问题${id}`, created_at: '2026-10-19T10:00:00Z' },
    { role: 'assistant', content: `旧回答${id}[1]`, route: 'faq', sources: [], created_at: '2026-10-19T10:00:01Z' },
  ] };
}

describe('server conversation history', () => {
  beforeEach(() => { vi.stubEnv('VITE_STREAM', '0'); });
  it.each([undefined, null, 6000, 0])('restores known timing and hides unknown timing (%s)', async elapsed_ms => {
    const mock = accountMock(userIdentity()); seed(mock);
    mock.state.details.a.messages[1].elapsed_ms = elapsed_ms;
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '打开对话：服务器a' }));
    await screen.findByText('旧回答a[1]');
    if (elapsed_ms == null) expect(screen.queryByText(/秒$/)).not.toBeInTheDocument();
    else expect(screen.getByText(`${(elapsed_ms / 1000).toFixed(1)} 秒`)).toBeInTheDocument();
  });

  it.each(['json', 'sse'])('saves the browser elapsed time and restores it after login (%s)', async transport => {
    vi.stubEnv('VITE_STREAM', transport === 'sse' ? '1' : '0');
    const mock = accountMock(userIdentity()); seed(mock);
    let clock = 1000;
    vi.spyOn(performance, 'now').mockImplementation(() => clock);
    if (transport === 'sse') mock.overrides['POST /chat/stream'] = init => {
      const request = JSON.parse(String(init.body));
      const response = fixtureFor(request);
      const detail = mock.state.details.a;
      detail.messages.push({ role: 'user', content: request.message, created_at: '' },
        { role: 'assistant', content: response.answer, route: response.route, sources: response.sources, created_at: '' });
      mock.state.items[0].title = '继续';
      const stream = controlledSSE(); stream.event({ event: 'done', data: response }); stream.close();
      return stream.response;
    };
    const original = mock.fetchMock.getMockImplementation()!;
    mock.fetchMock.mockImplementation(async (url, init = {}) => {
      const response = await original(url, init);
      if (url.endsWith('/chat') || url.endsWith('/chat/stream')) clock = 7000;
      if (url.endsWith('/messages')) clock = 19000; // 回写耗时不计入回答耗时。
      return response;
    });
    const view = render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '打开对话：服务器a' }));
    await screen.findByText('旧回答a[1]');
    ask('继续');
    await screen.findByText('6.0 秒');
    await waitFor(() => expect(mock.state.details.a.messages.at(-1)?.elapsed_ms).toBe(6000));
    const body = JSON.parse(String(mock.calls('/conversations/a/messages', 'POST')[0][1]?.body));
    expect(body).toMatchObject({ elapsed_ms: 6000, request_id: expect.any(String), route: 'rag' });
    expect(mock.calls('/conversations/a/messages')[0][1]?.headers).toMatchObject({ 'X-Requested-With': 'ecomrag' });
    view.unmount();
    await accountAPI.logout(); await accountAPI.authenticate('login', 'demo_user', 'demo_pass_123');
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '打开对话：继续' }));
    await screen.findByText('6.0 秒');
    expect(screen.queryByText('0.0 秒')).not.toBeInTheDocument();
  });

  it('keeps the completed answer and reports a timing persistence failure', async () => {
    const mock = accountMock(userIdentity()); seed(mock);
    mock.overrides['POST /conversations/a/messages'] = () => json({ code: 50301, message: '服务暂不可用' }, 503);
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '打开对话：服务器a' }));
    await screen.findByText('旧回答a[1]'); ask('继续');
    await screen.findByText(/回答耗时保存失败/);
    expect(screen.getByText('知识库回答')).toBeInTheDocument();
    expect(mock.calls('/chat')).toHaveLength(1);
  });
  it('lists only server entries, opens on demand, and keeps existing multi-turn history assembly', async () => {
    saveConversations([conversation('游客')]); const before = localStorage.getItem(CONVERSATIONS_KEY);
    const mock = accountMock(userIdentity()); seed(mock); render(<App />);
    await screen.findByRole('button', { name: '打开对话：服务器a' });
    expect(screen.queryByRole('button', { name: '打开对话：游客' })).not.toBeInTheDocument();
    expect(mock.calls('/conversations/a')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '打开对话：服务器a' }));
    await screen.findByText('旧回答a[1]');
    ask('继续'); await screen.findByText('知识库回答');
    const body = JSON.parse(String(mock.calls('/chat')[0][1]?.body));
    expect(body).toMatchObject({ conversation_id: 'a', history: [
      { role: 'user', content: '旧问题a' }, { role: 'assistant', content: '旧回答a' },
    ] });
    expect(mock.calls('/conversations', 'POST')).toHaveLength(0);
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBe(before);
  });
  it('renames inline, confirms deletion, and opens the next entry after deleting the current conversation', async () => {
    const mock = accountMock(userIdentity()); seed(mock); render(<App />);
    await screen.findByRole('button', { name: '打开对话：服务器a' });
    fireEvent.click(screen.getByRole('button', { name: '重命名对话：服务器a' }));
    fireEvent.change(screen.getByLabelText('对话标题'), { target: { value: '自定标题' } });
    fireEvent.click(screen.getByRole('button', { name: '保存标题' }));
    await screen.findByRole('button', { name: '打开对话：自定标题' });
    expect(JSON.parse(String(mock.calls('/conversations/a', 'PATCH')[0][1]?.body))).toEqual({ title: '自定标题' });
    fireEvent.click(screen.getByRole('button', { name: '打开对话：自定标题' }));
    await screen.findByText('旧回答a[1]');
    fireEvent.click(screen.getByRole('button', { name: '删除对话：自定标题' }));
    expect(mock.calls('/conversations/a', 'DELETE')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await screen.findByText('旧回答b[1]');
    expect(screen.queryByRole('button', { name: '打开对话：自定标题' })).not.toBeInTheDocument();
    expect(mock.calls('/conversations/a', 'DELETE')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: '清空全部历史' })).not.toBeInTheDocument();
  });
  it('delays creation until first send, reuses the id, and refreshes server-generated titles', async () => {
    const mock = accountMock(userIdentity()); render(<App />);
    await screen.findByRole('textbox', { name: '输入问题' });
    fireEvent.click(screen.getByRole('button', { name: '新对话' }));
    expect(mock.calls('/conversations', 'POST')).toHaveLength(0);
    ask('你好');
    await screen.findByRole('button', { name: '打开对话：你好' });
    expect(mock.calls('/conversations', 'POST')).toHaveLength(1);
    expect(JSON.parse(String(mock.calls('/chat')[0][1]?.body)).conversation_id).toBe('server-1');
    ask('继续');
    await screen.findByRole('button', { name: '打开对话：继续' });
    expect(mock.calls('/conversations', 'POST')).toHaveLength(1);
    expect(JSON.parse(String(mock.calls('/chat')[1][1]?.body))).toMatchObject({ conversation_id: 'server-1', history: [
      { role: 'user', content: '你好' }, { role: 'assistant', content: expect.any(String) },
    ] });
    expect(mock.calls('/conversations', 'GET').length).toBeGreaterThanOrEqual(3);
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBeNull();
  });
  it('guest chat never sends conversation_id or creates server history; logout restores guest history', async () => {
    saveConversations([conversation('本地旧对话')]); sessionStorage.setItem(GUEST_ENTRY_KEY, '1');
    const mock = accountMock(userIdentity()); render(<App />);
    await screen.findByRole('button', { name: '退出登录' });
    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));
    await screen.findByRole('button', { name: '以访客进入' });
    fireEvent.click(screen.getByRole('button', { name: '以访客进入' }));
    expect(screen.getByRole('button', { name: '打开对话：本地旧对话' })).toBeInTheDocument();
    ask('你好'); await screen.findByText('闲聊');
    expect(JSON.parse(String(mock.calls('/chat')[0][1]?.body))).not.toHaveProperty('conversation_id');
    expect(mock.calls('/conversations', 'POST')).toHaveLength(0);
  });
  it.each(['list', 'open', 'create', 'rename', 'delete'] as const)('keeps the draft and gives a friendly error on %s failure', async failure => {
    const mock = accountMock(userIdentity()); seed(mock);
    const target = { list: 'GET /conversations', open: 'GET /conversations/a', create: 'POST /conversations', rename: 'PATCH /conversations/a', delete: 'DELETE /conversations/a' }[failure];
    mock.overrides[target] = () => json({ code: 50301, message: '服务暂不可用' }, 503);
    render(<App />); await screen.findByRole('textbox', { name: '输入问题' });
    if (failure !== 'list') await screen.findByRole('button', { name: '打开对话：服务器a' });
    fireEvent.change(screen.getByRole('textbox', { name: '输入问题' }), { target: { value: '保留草稿' } });
    if (failure === 'open') fireEvent.click(screen.getByRole('button', { name: '打开对话：服务器a' }));
    if (failure === 'create') fireEvent.click(screen.getByRole('button', { name: '发送' }));
    if (failure === 'rename') {
      fireEvent.click(screen.getByRole('button', { name: '重命名对话：服务器a' }));
      fireEvent.change(screen.getByLabelText('对话标题'), { target: { value: '保留的新标题' } });
      fireEvent.click(screen.getByRole('button', { name: '保存标题' }));
    }
    if (failure === 'delete') {
      fireEvent.click(screen.getByRole('button', { name: '删除对话：服务器a' }));
      fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    }
    await screen.findByText(/失败|无法创建|无法打开/);
    await waitFor(() => expect(screen.getByRole('textbox', { name: '输入问题' })).toHaveValue('保留草稿'));
    if (failure === 'rename') expect(screen.getByLabelText('对话标题')).toHaveValue('保留的新标题');
    expect(mock.calls('/chat')).toHaveLength(0);
  });
  it('ignores a late conversation response after a newer selection', async () => {
    const mock = accountMock(userIdentity()); seed(mock);
    let resolve!: (response: Response) => void;
    mock.overrides['GET /conversations/a'] = () => new Promise<Response>(done => { resolve = done; });
    render(<App />); await screen.findByRole('button', { name: '打开对话：服务器a' });
    fireEvent.click(screen.getByRole('button', { name: '打开对话：服务器a' }));
    fireEvent.click(screen.getByRole('button', { name: '打开对话：服务器b' }));
    await screen.findByText('旧回答b[1]');
    await act(async () => { resolve(json(mock.state.details.a)); });
    expect(screen.queryByText('旧回答a[1]')).not.toBeInTheDocument();
    expect(screen.getByText('旧回答b[1]')).toBeInTheDocument();
  });
});
