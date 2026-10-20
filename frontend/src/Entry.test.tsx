import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App, { GUEST_ENTRY_KEY } from './App';
import { WELCOME_KEY } from './components/WelcomeSplash';
import { accountMock, json, testUser, userIdentity } from './test/account';
import { saveConversations } from './lib/storage';
import { conversation } from './test/conversations';
import { mockMobile } from './test/media';
import { categoryQuestion, shopInfo } from './shopInfo';
import { brandSlogan } from './lib/config';

function credentials() {
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: testUser.username } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'demo_pass_123' } });
}
function ask(question = '你好') {
  fireEvent.change(screen.getByRole('textbox', { name: '输入问题' }), { target: { value: question } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
}
function motion(reduced = false) {
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query === '(min-width: 1024px)' || (reduced && query === '(prefers-reduced-motion: reduce)'), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
}
async function mount() { let view!: ReturnType<typeof render>; await act(async () => { view = render(<App />); }); return view; }
function skipSplash() {
  const splash = screen.queryByRole('dialog', { name: '欢迎来到灵码' });
  if (splash) { fireEvent.click(splash); act(() => vi.advanceTimersByTime(700)); }
}

describe('entry and welcome lifecycle', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.stubEnv('VITE_WELCOME_ANIMATION', '1'); motion(); });
  it('renders the confirmed entry branding and card without WelcomeSplash or entrance animation classes', async () => {
    vi.stubEnv('VITE_LOGO', 'a-spark');
    accountMock(); await mount();
    const entry = screen.getByRole('main', { name: '入口登录页' });
    expect(within(entry).getByRole('heading', { name: '灵码' })).toBeInTheDocument();
    expect(within(entry).getByText(brandSlogan())).toBeInTheDocument();
    expect(within(entry).getByText('智能客服')).toBeInTheDocument();
    expect(entry.querySelector('.entry-logo')).toHaveAttribute('src', '/brand/lingma-b-seal.svg');
    expect(entry.querySelector('.entry-card')).toContainElement(within(entry).getByRole('button', { name: '以访客进入' }));
    expect(within(entry).getByText('或')).toBeInTheDocument();
    expect(within(entry).getByText(shopInfo.disclaimer)).toHaveClass('entry-footer');
    expect(document.querySelector('.welcome-splash')).toBeNull();
    expect(screen.queryByRole('dialog', { name: '欢迎来到灵码' })).not.toBeInTheDocument();
    for (const element of [entry, ...entry.querySelectorAll('[class]')]) {
      expect(element.className).not.toMatch(/(?:^|\s)(?:[\w-]+:)*(?:animate-|welcome-|fade-in|slide-in)/);
    }
    expect(sessionStorage.getItem(WELCOME_KEY)).toBeNull();
    fireEvent.click(within(entry).getByRole('tab', { name: '注册' }));
    expect(within(entry).getByRole('button', { name: '注册并进入' })).toBeInTheDocument();
    expect(within(entry).getByText('演示站点，请勿使用你常用的真实密码。')).toHaveClass('entry-auth-warning');
    expect(document.querySelector('.welcome-splash')).toBeNull();
  });
  it('never mounts the splash on entry; guest entry resets it and remount stays at home without replay', async () => {
    const mock = accountMock();
    sessionStorage.setItem(WELCOME_KEY, '1');
    const view = await mount();
    expect(screen.getByRole('main', { name: '入口登录页' })).toBeInTheDocument();
    expect(document.querySelector('.welcome-splash')).toBeNull();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBe('1');
    fireEvent.click(screen.getByRole('button', { name: '以访客进入' }));
    expect(screen.getByRole('dialog', { name: '欢迎来到灵码' })).toBeInTheDocument();
    expect(sessionStorage.getItem(GUEST_ENTRY_KEY)).toBe('1');
    view.unmount(); await mount();
    expect(screen.getByRole('main', { name: '客服对话' })).toBeInTheDocument();
    expect(screen.queryByRole('main', { name: '入口登录页' })).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mock.calls('/auth/me')).toHaveLength(2);
  });
  it('authenticated startup goes straight home and plays once per browser session', async () => {
    accountMock(userIdentity());
    const view = await mount();
    expect(screen.getByRole('dialog', { name: '欢迎来到灵码' })).toBeInTheDocument();
    expect(screen.queryByRole('main', { name: '入口登录页' })).not.toBeInTheDocument();
    view.unmount(); await mount();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it.each(['login', 'register'] as const)('entry %s success plays, logout returns to entry and resets markers', async mode => {
    accountMock();
    await mount();
    if (mode === 'register') fireEvent.click(screen.getByRole('tab', { name: '注册' }));
    credentials();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: mode === 'login' ? '登录' : '注册并进入' })); });
    expect(screen.getByRole('dialog', { name: '欢迎来到灵码' })).toBeInTheDocument();
    skipSplash();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '退出登录' })); });
    expect(screen.getByRole('main', { name: '入口登录页' })).toBeInTheDocument();
    expect(document.querySelector('.welcome-splash')).toBeNull();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBeNull();
    expect(sessionStorage.getItem(GUEST_ENTRY_KEY)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '以访客进入' }));
    expect(screen.getByRole('dialog', { name: '欢迎来到灵码' })).toBeInTheDocument();
  });
  it.each(['offline', '50301'])('me failure (%s) leaves guest entry usable', async kind => {
    const mock = accountMock();
    mock.overrides['GET /auth/me'] = () => { if (kind === 'offline') throw new TypeError('offline'); return json({ code: 50301, message: '服务暂不可用' }, 503); };
    await mount();
    expect(document.querySelector('.welcome-splash')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '以访客进入' }));
    expect(screen.getByRole('main', { name: '客服对话' })).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: '欢迎来到灵码' })).toBeInTheDocument();
  });
  it.each(['off', 'reduced'])('entry transition respects %s', async mode => {
    accountMock();
    if (mode === 'off') vi.stubEnv('VITE_WELCOME_ANIMATION', '0'); else motion(true);
    await mount(); fireEvent.click(screen.getByRole('button', { name: '以访客进入' }));
    expect(screen.getByRole('main', { name: '客服对话' })).toBeInTheDocument();
    expect(document.querySelector('.welcome-splash')).toBeNull();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBe(mode === 'off' ? null : '1');
  });
  it('modal login reuses the form without replaying welcome; Esc closes it', async () => {
    accountMock(); await mount();
    fireEvent.click(screen.getByRole('button', { name: '以访客进入' })); skipSplash();
    fireEvent.click(screen.getByRole('button', { name: '登录/注册' }));
    expect(screen.getByRole('dialog', { name: '登录/注册' })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '登录/注册' })); credentials();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '登录' })); });
    expect(screen.getByText(testUser.username)).toBeInTheDocument();
    expect(document.querySelector('.welcome-splash')).toBeNull();
  });
  it('StrictMode startup waits for identity before mounting welcome', async () => {
    accountMock(userIdentity());
    await act(async () => { render(<StrictMode><App /></StrictMode>); });
    expect(screen.getAllByRole('dialog', { name: '欢迎来到灵码' })).toHaveLength(1);
  });
});

describe('quota, account and shop integration', () => {
  beforeEach(() => { vi.stubEnv('VITE_STREAM', '0'); });
  it.each([false, true])('shows and refreshes quota after successful and failed questions (user=%s)', async signedIn => {
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1');
    const mock = accountMock(signedIn ? userIdentity() : undefined);
    await mount();
    expect(screen.getByText(new RegExp(`今日剩余 ${signedIn ? '50/50' : '5/5'} 次`))).toBeInTheDocument();
    ask();
    await waitFor(() => expect(screen.getByText(new RegExp(`今日剩余 ${signedIn ? '49/50' : '4/5'} 次`))).toBeInTheDocument());
    expect(mock.calls('/auth/me')).toHaveLength(2);
    mock.overrides['POST /chat'] = () => json({ code: 50301, message: '服务暂不可用' }, 503);
    ask('继续');
    await screen.findByText('服务有点忙，请稍后再试。');
    await waitFor(() => expect(mock.calls('/auth/me')).toHaveLength(3));
  });
  it.each(['guest', 'user'] as const)('42902 shows a specific %s hint near composer and blocks further sends', async role => {
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1');
    const mock = accountMock(role === 'user' ? userIdentity() : undefined);
    mock.overrides['POST /chat'] = () => {
      mock.state.identity.quota.remaining = 0;
      return json({ code: 42902, message: '今日次数用完', limit_scope: role }, 429);
    };
    await mount(); ask();
    const hint = await screen.findByRole('status');
    expect(hint).toHaveTextContent(role === 'guest' ? '登录后每天可提问 50 次' : '请明天再试');
    expect(screen.queryByText('服务有点忙，请稍后再试。')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: '输入问题' }), { target: { value: '明天再问' } });
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '输入问题' })).toBeEnabled();
    if (role === 'guest') expect(within(hint).getByRole('button', { name: '登录/注册' })).toBeInTheDocument();
  });
  it('account deletion needs confirmation and a password; logout restores guest local history', async () => {
    saveConversations([conversation('游客旧历史')]);
    const mock = accountMock(userIdentity()); await mount();
    expect(screen.queryByRole('button', { name: '打开对话：游客旧历史' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '注销账号' }));
    const confirm = screen.getByRole('button', { name: '确认注销' });
    expect(confirm).toBeDisabled();
    expect(mock.calls('/auth/account')).toHaveLength(0);
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'demo_pass_123' } });
    fireEvent.click(confirm);
    await screen.findByRole('main', { name: '入口登录页' });
    expect(JSON.parse(String(mock.calls('/auth/account')[0][1]?.body))).toEqual({ password: 'demo_pass_123' });
    fireEvent.click(screen.getByRole('button', { name: '以访客进入' }));
    expect(screen.getByRole('button', { name: '打开对话：游客旧历史' })).toBeInTheDocument();
  });
  it.each([false, true])('shop facts render and a category fills the draft without sending (mobile=%s)', async mobile => {
    mockMobile(mobile);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); const mock = accountMock(); await mount();
    if (mobile) fireEvent.click(screen.getByRole('button', { name: '历史' }));
    fireEvent.click(screen.getByRole('button', { name: '店铺信息' }));
    const shop = screen.getByRole('dialog', { name: '店铺信息' });
    expect(within(shop).getByText(shopInfo.disclaimer)).toBeInTheDocument();
    for (const fact of [...shopInfo.support, ...shopInfo.promises]) expect(within(shop).getByText(fact)).toBeInTheDocument();
    for (const category of shopInfo.categories) expect(within(shop).getByRole('button', { name: category })).toBeInTheDocument();
    fireEvent.click(within(shop).getByRole('button', { name: shopInfo.categories[0] }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '输入问题' })).toHaveValue(categoryQuestion(shopInfo.categories[0]));
    expect(mock.calls('/chat')).toHaveLength(0);
  });
  it('modal traps keyboard focus and restores it on close', async () => {
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); accountMock(); await mount();
    const user = userEvent.setup(); const trigger = screen.getByRole('button', { name: '登录/注册' });
    await user.click(trigger);
    const dialog = screen.getByRole('dialog', { name: '登录/注册' });
    expect(within(dialog).getByRole('button', { name: '关闭登录/注册' })).toHaveFocus();
    await user.tab({ shift: true }); expect(within(dialog).getByRole('button', { name: '登录' })).toHaveFocus();
    await user.tab(); expect(within(dialog).getByRole('button', { name: '关闭登录/注册' })).toHaveFocus();
    await user.keyboard('{Escape}'); expect(trigger).toHaveFocus();
  });
});
