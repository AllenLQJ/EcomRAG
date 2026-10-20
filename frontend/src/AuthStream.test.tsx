import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App, { GUEST_ENTRY_KEY } from './App';
import { accountMock, json, testUser, userIdentity } from './test/account';
import { controlledSSE } from './test/streams';
import { fixtureFor } from './mocks/fixtures';

function ask(question: string) {
  fireEvent.change(screen.getByRole('textbox', { name: '输入问题' }), { target: { value: question } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
}
async function login() {
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: testUser.username } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'demo_pass_123' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '登录' })); });
}

describe('authentication changes during an active stream', () => {
  it('immediately aborts and blocks sends while logout is pending, then supports a clean login', async () => {
    const mock = accountMock(userIdentity());
    const stream = controlledSSE();
    mock.overrides['POST /chat/stream'] = () => stream.response;
    let finishLogout!: (response: Response) => void;
    mock.overrides['POST /auth/logout'] = () => new Promise<Response>(resolve => { finishLogout = resolve; });
    await act(async () => { render(<App />); });
    ask('登出前的问题');
    await waitFor(() => expect(mock.calls('/chat/stream')).toHaveLength(1));
    const signal = mock.calls('/chat/stream')[0][1]!.signal!;
    await act(async () => { stream.event({ event: 'delta', data: { text: '尚未完成的回答' } }); });
    expect(screen.getByText('尚未完成的回答')).toBeInTheDocument();
    const meCalls = mock.calls('/auth/me').length;
    fireEvent.click(screen.getByRole('button', { name: '退出登录' }));
    expect(signal.aborted).toBe(true);
    expect(screen.getByRole('button', { name: '正在退出…' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '输入问题' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '运费怎么算' }));
    fireEvent.submit(screen.getByRole('textbox', { name: '输入问题' }).closest('form')!);
    await waitFor(() => expect(stream.response.body?.locked).toBe(false));
    expect(mock.calls('/auth/me')).toHaveLength(meCalls);
    expect(mock.calls('/chat/stream')).toHaveLength(1);
    expect(mock.calls('/chat')).toHaveLength(0);
    expect(screen.queryByText('尚未完成的回答')).not.toBeInTheDocument();
    await act(async () => { finishLogout(json({})); });
    expect(screen.getByRole('main', { name: '入口登录页' })).toBeInTheDocument();
    await login();
    expect(screen.getByText(testUser.username)).toBeInTheDocument();
    expect(screen.queryByText('登出前的问题')).not.toBeInTheDocument();
    vi.stubEnv('VITE_STREAM', '0');
    ask('你好，重新登录的问题');
    await screen.findByText('闲聊');
    expect(mock.calls('/chat')).toHaveLength(1);
  });

  it('cancels the guest stream on login and keeps its pending reply out of account history', async () => {
    const mock = accountMock();
    const stream = controlledSSE();
    mock.overrides['POST /chat/stream'] = () => stream.response;
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1');
    await act(async () => { render(<App />); });
    ask('游客的问题');
    await waitFor(() => expect(mock.calls('/chat/stream')).toHaveLength(1));
    await act(async () => { stream.event({ event: 'delta', data: { text: '游客的部分回答' } }); });
    const signal = mock.calls('/chat/stream')[0][1]!.signal!;
    fireEvent.click(screen.getByRole('button', { name: '登录/注册' }));
    await login();
    expect(signal.aborted).toBe(true);
    await waitFor(() => expect(stream.response.body?.locked).toBe(false));
    expect(screen.queryByText('游客的部分回答')).not.toBeInTheDocument();
    expect(screen.queryByText('游客的问题')).not.toBeInTheDocument();
    expect(screen.getByText(testUser.username)).toBeInTheDocument();
    const next = controlledSSE();
    mock.overrides['POST /chat/stream'] = () => next.response;
    ask('账号的新问题');
    await waitFor(() => expect(mock.calls('/chat/stream')).toHaveLength(2));
    const request = JSON.parse(String(mock.calls('/chat/stream')[1][1]?.body));
    expect(request.conversation_id).toBe('server-1');
    expect(request.history).toEqual([]);
    await act(async () => { next.event({ event: 'done', data: fixtureFor(request) }); });
    expect(screen.getByRole('textbox', { name: '输入问题' })).toBeEnabled();
    expect(mock.calls('/chat')).toHaveLength(0);
  });
});
