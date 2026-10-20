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
    expect(within(entry).queryByText('3–20 位字母、数字或下划线。')).not.toBeInTheDocument();
    expect(within(entry).queryByText('8–64 位。')).not.toBeInTheDocument();
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
    expect(within(entry).getByText('3–20 位字母、数字或下划线。')).toBeVisible();
    expect(within(entry).getByRole('list', { name: '注册密码要求' }).children).toHaveLength(4);
    expect(within(entry).getByRole('button', { name: '注册并进入' })).toBeInTheDocument();
    expect(within(entry).queryByText('演示站点，请勿使用你常用的真实密码。')).not.toBeInTheDocument();
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
  it.each(['offline', '50301', 'timeout'])('keeps identity unknown after bounded me retries (%s), then allows manual recovery', async kind => {
    const mock = accountMock();
    mock.overrides['GET /auth/me'] = init => {
      if (kind === 'timeout') return new Promise<Response>((_resolve, reject) => {
        init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
      });
      if (kind === 'offline') throw new TypeError('offline');
      return json({ code: 50301, message: '服务暂不可用' }, 503);
    };
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1');
    await mount();
    expect(screen.getByRole('main', { name: '正在获取身份' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '输入问题' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '以访客进入' })).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(48_000); });
    expect(mock.calls('/auth/me')).toHaveLength(3);
    expect(screen.getByText('暂时无法确认身份，请稍后重试。')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(mock.calls('/auth/me')).toHaveLength(3);
    expect(document.querySelector('.welcome-splash')).toBeNull();
    mock.state.identity = userIdentity();
    delete mock.overrides['GET /auth/me'];
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新确认身份' })); });
    expect(screen.getByRole('main', { name: '客服对话' })).toBeInTheDocument();
    expect(screen.getByText(testUser.username)).toBeInTheDocument();
    expect(screen.getByText('今日剩余 50/50 次')).toBeInTheDocument();
    expect(mock.calls('/auth/me')).toHaveLength(4);
  });
  it('recovers an authenticated Cookie on automatic me retry without ever mounting guest chat', async () => {
    const mock = accountMock(userIdentity());
    let attempts = 0;
    mock.overrides['GET /auth/me'] = () => {
      if (++attempts === 1) throw new TypeError('offline');
      return json(mock.state.identity);
    };
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1');
    await mount();
    expect(mock.calls('/conversations')).toHaveLength(0);
    expect(screen.queryByRole('main', { name: '客服对话' })).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText(testUser.username)).toBeInTheDocument();
    expect(screen.queryByText(/5\/5/)).not.toBeInTheDocument();
    expect(mock.calls('/conversations')).toHaveLength(1);
    expect(mock.calls('/auth/me')).toHaveLength(2);
  });
  it('clears a scheduled identity retry on unmount', async () => {
    const mock = accountMock();
    mock.overrides['GET /auth/me'] = () => { throw new TypeError('offline'); };
    const view = await mount();
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(mock.calls('/auth/me')).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
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
    const modal = screen.getByRole('dialog', { name: '登录/注册' });
    expect(within(modal).queryByText('3–20 位字母、数字或下划线。')).not.toBeInTheDocument();
    expect(within(modal).queryByText('8–64 位。')).not.toBeInTheDocument();
    expect(within(modal).getByRole('heading', { name: '灵码' })).toBeInTheDocument();
    expect(within(modal).getByText(brandSlogan())).toBeInTheDocument();
    expect(modal.querySelector('.entry-logo')).toHaveAttribute('src', '/brand/lingma-b-seal.svg');
    expect(modal.querySelector('.entry-card .entry-auth-tabs')).toBeInTheDocument();
    expect(within(modal).getByRole('button', { name: '登录' })).toHaveClass('entry-auth-submit');
    expect(within(modal).queryByRole('button', { name: '以访客进入' })).not.toBeInTheDocument();
    fireEvent.click(within(modal).getByRole('tab', { name: '注册' }));
    expect(within(modal).queryByText('演示站点，请勿使用你常用的真实密码。')).not.toBeInTheDocument();
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
  it.each([false, true])('places authentication close at the top right above the logo with a touch-sized target (mobile=%s)', async mobile => {
    mockMobile(mobile);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); accountMock(); await mount();
    if (mobile) fireEvent.click(screen.getByRole('button', { name: '历史' }));
    fireEvent.click(screen.getByRole('button', { name: '登录/注册' }));
    const modal = screen.getByRole('dialog', { name: '登录/注册' });
    const close = within(modal).getByRole('button', { name: '关闭登录/注册' });
    expect(close).toHaveClass('min-h-11', 'min-w-11');
    expect(close.parentElement).toHaveClass('flex', 'justify-end');
    expect(close.parentElement?.nextElementSibling).toContainElement(modal.querySelector('.entry-logo'));
    fireEvent.click(within(modal).getByRole('tab', { name: '注册' }));
    const rules = within(modal).getByRole('list', { name: '注册密码要求' });
    expect(within(modal).getByText('3–20 位字母、数字或下划线。')).toBeVisible();
    expect(rules.children).toHaveLength(4);
    fireEvent.change(within(modal).getByLabelText('密码'), { target: { value: '12345678' } });
    expect(rules.querySelector('[data-rule="digits"]')).toHaveAttribute('data-state', 'unmet');
    fireEvent.click(close);
    expect(screen.queryByRole('dialog', { name: '登录/注册' })).not.toBeInTheDocument();
  });
  it('keeps one floating shop action through resizes and expanded quick questions', async () => {
    const resize = mockMobile(false);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); accountMock(); await mount();
    expect(screen.getAllByRole('button', { name: '店铺商品' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '店铺商品' }));
    expect(screen.getAllByRole('dialog', { name: '店铺商品' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '关闭店铺商品' }));
    act(() => resize(true));
    expect(screen.getAllByRole('button', { name: '店铺商品' })).toHaveLength(1);
    expect(screen.getByRole('button', { name: '店铺商品' }).closest('header, footer')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '常见问题' }));
    expect(screen.getByRole('button', { name: '店铺商品' })).toBeVisible();
    act(() => resize(false));
    expect(within(screen.getByRole('banner')).queryByRole('button', { name: '店铺商品' })).not.toBeInTheDocument();
    act(() => resize(true));
    expect(screen.getByRole('button', { name: '店铺商品' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '常见问题' }));
    fireEvent.click(screen.getByRole('button', { name: '店铺商品' }));
    expect(screen.getAllByRole('dialog', { name: '店铺商品' })).toHaveLength(1);
  });
  it.each(['0', '1'])('shows an origin-specific chat error (stream=%s)', async stream => {
    vi.stubEnv('VITE_STREAM', stream);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1');
    const mock = accountMock();
    mock.overrides[`POST /chat${stream === '1' ? '/stream' : ''}`] = () => json({ code: 40301, message: '请求来源校验失败' }, 403);
    await mount(); ask();
    await screen.findByText('访问来源未被允许，请使用正确的访问地址。');
    expect(screen.queryByText('服务有点忙，请稍后再试。')).not.toBeInTheDocument();
  });
  it.each([false, true])('keeps shop information available before and after sending and creating a conversation (mobile=%s)', async mobile => {
    mockMobile(mobile);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); accountMock();
    await mount();
    expect(screen.getByRole('button', { name: /店铺商品/ })).toBeInTheDocument();
    ask(); await screen.findByText('闲聊');
    expect(screen.getByRole('button', { name: /店铺商品/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '新对话' }));
    expect(screen.getByRole('button', { name: /店铺商品/ })).toBeInTheDocument();
  });
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
  it.each([false, true])('shows the brand and categories without policy sections and fills only the draft (mobile=%s)', async mobile => {
    mockMobile(mobile);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); const mock = accountMock(); await mount();
    const entry = screen.getByRole('button', { name: '店铺商品' });
    expect(entry).toHaveClass('shop-products-button');
    expect(entry.closest('header, footer, form, nav')).toBeNull();
    const headerLogo = screen.getByRole('banner').querySelector('img')!;
    fireEvent.click(entry);
    const shop = screen.getByRole('dialog', { name: '店铺商品' });
    expect(within(shop).queryByRole('heading', { name: '店铺商品' })).not.toBeInTheDocument();
    expect(within(shop).getByRole('heading', { name: shopInfo.name })).toHaveClass('font-serif');
    expect(within(shop).getByText(shopInfo.intro)).toBeVisible();
    const brand = shop.querySelector('.shop-products-brand')!;
    expect(brand.querySelector('img')).toHaveAttribute('src', headerLogo.getAttribute('src'));
    expect(brand.firstElementChild?.tagName).toBe('IMG');
    expect(brand.parentElement?.nextElementSibling).toHaveTextContent('商品品类');
    expect(within(shop).queryByText(shopInfo.disclaimer)).not.toBeInTheDocument();
    expect(within(shop).queryByText('客服信息')).not.toBeInTheDocument();
    expect(within(shop).queryByText('服务承诺')).not.toBeInTheDocument();
    for (const fact of [...shopInfo.support, ...shopInfo.promises]) expect(within(shop).queryByText(fact)).not.toBeInTheDocument();
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
  it.each([false, true])('keeps shop dialog keyboard focus inside and restores the floating entry (mobile=%s)', async mobile => {
    mockMobile(mobile);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); accountMock(); await mount();
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: '店铺商品' });
    expect(trigger).toHaveAttribute('type', 'button');
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
    expect(trigger.querySelector('.shop-products-dot')).toHaveAttribute('aria-hidden', 'true');
    trigger.focus();
    await user.keyboard('{Enter}');
    const dialog = screen.getByRole('dialog', { name: '店铺商品' });
    const close = within(dialog).getByRole('button', { name: '关闭店铺商品' });
    expect(close).toHaveTextContent('关闭');
    expect(close.parentElement).toHaveClass('justify-between', 'items-start');
    expect(close).toHaveFocus();
    expect(trigger.closest('[inert]')).not.toBeNull();
    await user.tab({ shift: true });
    expect(within(dialog).getByRole('button', { name: shopInfo.categories[shopInfo.categories.length - 1] })).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(trigger.closest('[inert]')).toBeNull();
    await user.click(trigger);
    await user.click(screen.getByRole('button', { name: '关闭店铺商品' }));
    expect(trigger).toHaveFocus();
  });
});
