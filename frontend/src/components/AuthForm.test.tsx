import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AuthForm, validateCredentials } from './AuthForm';
import { accountMock, json } from '../test/account';

function fill(username = 'demo_123', password = 'demo_pass_123') {
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: username } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: password } });
}

describe('shared login/register form', () => {
  it.each([
    ['ab', '12345678', '用户名'], ['a'.repeat(21), '12345678', '用户名'],
    ['用户名', '12345678', '用户名'], ['has space', '12345678', '用户名'],
    ['abc-', '12345678', '用户名'], ['abc', 'x'.repeat(65), '密码'],
  ])('validates %s / password length before fetching', async (username, password, reason) => {
    const mock = accountMock(); render(<AuthForm onSuccess={vi.fn()} />);
    fill(username, password); fireEvent.click(screen.getByRole('button', { name: '登录' }));
    expect(screen.getByRole('alert')).toHaveTextContent(reason);
    expect(mock.fetchMock).not.toHaveBeenCalled();
  });
  it.each(['', '1', '1234567'])('submits short login passwords to server authentication (%j)', async password => {
    const mock = accountMock();
    mock.overrides['POST /auth/login'] = () => json({ code: 40103, message: '密码错误' }, 401);
    render(<AuthForm onSuccess={vi.fn()} />);
    fill('demo_123', password);
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('密码错误');
    expect(mock.calls('/auth/login')).toHaveLength(1);
    expect(JSON.parse(String(mock.calls('/auth/login')[0][1]?.body))).toEqual({ username: 'demo_123', password });
    expect(screen.queryByText(/8–64/)).not.toBeInTheDocument();
  });
  it.each([['Ab_', 'x'.repeat(8)], ['A'.repeat(20), 'x'.repeat(64)], ['abc', '😀'.repeat(8)]])('accepts boundary credentials for %s', (username, password) => {
    expect(validateCredentials(username, password)).toBeUndefined();
  });
  it.each([false, true])('shows format hints only on registration and keeps accessible errors (entry=%s)', entry => {
    render(<AuthForm onSuccess={vi.fn()} onGuest={entry ? vi.fn() : undefined} />);
    const expectLoginWithoutHints = () => {
      expect(screen.queryByText('3–20 位字母、数字或下划线。')).not.toBeInTheDocument();
      expect(screen.queryByText('8–64 位。')).not.toBeInTheDocument();
      expect(screen.queryByRole('list', { name: '注册密码要求' })).not.toBeInTheDocument();
      for (const label of ['用户名', '密码']) {
        const describedBy = screen.getByLabelText(label).getAttribute('aria-describedby')!;
        expect(describedBy).not.toContain('-hint');
        expect(document.getElementById(describedBy)).toBeInTheDocument();
      }
    };
    expectLoginWithoutHints();
    expect(screen.getByLabelText('用户名')).toHaveAttribute('autocomplete', 'username');
    expect(screen.getByLabelText('密码')).toHaveAttribute('type', 'password');
    expect(screen.getByLabelText('密码')).toHaveAttribute('autocomplete', 'current-password');
    fireEvent.click(screen.getByRole('tab', { name: '注册' }));
    expect(screen.queryByText('演示站点，请勿使用你常用的真实密码。')).not.toBeInTheDocument();
    expect(screen.getByText('3–20 位字母、数字或下划线。')).toBeVisible();
    expect(screen.getByRole('list', { name: '注册密码要求' })).toBeVisible();
    expect(screen.getByLabelText('密码')).toHaveAttribute('autocomplete', 'new-password');
    fireEvent.keyDown(screen.getByRole('tab', { name: '注册' }), { key: 'ArrowLeft' });
    expect(screen.getByRole('tab', { name: '登录' })).toHaveAttribute('aria-selected', 'true');
    expectLoginWithoutHints();
  });
  it.each([false, true])('shows all registration rules before submit and updates them while typing (entry=%s)', entry => {
    const mock = accountMock();
    render(<AuthForm onSuccess={vi.fn()} onGuest={entry ? vi.fn() : undefined} />);
    expect(screen.queryByRole('list', { name: '注册密码要求' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '注册' }));
    expect(screen.getByText('3–20 位字母、数字或下划线。')).toBeVisible();
    const rules = screen.getByRole('list', { name: '注册密码要求' });
    expect(rules.querySelectorAll('li')).toHaveLength(4);
    expect(rules).toHaveTextContent('8–64 位字符');
    expect(rules).toHaveTextContent('不能为纯数字');
    expect(rules).toHaveTextContent('不能与用户名相同（不区分大小写）');
    expect(rules).toHaveTextContent('不能使用常见弱口令');
    fill('demo_123', '12345678');
    expect(rules.querySelector('[data-rule="digits"]')).toHaveAttribute('data-state', 'unmet');
    expect(rules.querySelector('[data-rule="digits"]')).toHaveClass('password-rule-unmet');
    expect(rules.querySelector('[data-rule="length"]')).toHaveAttribute('data-state', 'satisfied');
    expect(screen.getByLabelText('密码')).toHaveAttribute('aria-invalid', 'true');
    expect(mock.fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: entry ? '注册并进入' : '注册' }));
    expect(screen.getByRole('alert')).toHaveTextContent('密码不能为纯数字');
    expect(mock.fetchMock).not.toHaveBeenCalled();
    fill('demo_123', 'dEMO_123');
    expect(rules.querySelector('[data-rule="different"]')).toHaveAttribute('data-state', 'unmet');
    // Editing the username also immediately reevaluates the password comparison.
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'another_user' } });
    expect(rules.querySelector('[data-rule="different"]')).toHaveAttribute('data-state', 'satisfied');
    fill('another_user', 'Password123');
    expect(rules.querySelector('[data-rule="common"]')).toHaveAttribute('data-state', 'unmet');
    fill('another_user', 'Unique_password!');
    for (const rule of rules.querySelectorAll('li')) {
      expect(rule).toHaveAttribute('data-state', 'satisfied');
      expect(rule).toHaveClass('password-rule-satisfied');
      expect(rule).toHaveTextContent('已满足');
    }
    expect(screen.getByLabelText('密码')).toHaveAttribute('aria-invalid', 'false');
    fireEvent.click(screen.getByRole('tab', { name: '登录' }));
    expect(screen.queryByRole('list', { name: '注册密码要求' })).not.toBeInTheDocument();
  });
  it('keeps the guest action secondary and separate from form submission in entry mode', () => {
    const mock = accountMock(); const onGuest = vi.fn(); const onSuccess = vi.fn();
    render(<AuthForm onSuccess={onSuccess} onGuest={onGuest} />);
    const guest = screen.getByRole('button', { name: '以访客进入' });
    expect(guest).toHaveAttribute('type', 'button');
    expect(screen.getByText('访客每天可提问 5 次，登录后每天可提问 50 次')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录' })).toHaveClass('entry-auth-submit');
    fireEvent.click(screen.getByRole('tab', { name: '注册' }));
    expect(screen.getByRole('button', { name: '注册并进入' })).toHaveClass('entry-auth-submit');
    expect(screen.getByLabelText('密码')).toHaveAttribute('autocomplete', 'new-password');
    fireEvent.click(guest);
    expect(onGuest).toHaveBeenCalledOnce(); expect(onSuccess).not.toHaveBeenCalled();
    expect(mock.fetchMock).not.toHaveBeenCalled();
  });
  for (const mode of ['login', 'register'] as const) {
    it.each([
      [40001, '请求参数非法'], [40002, '用户名须为3–20位英文字母、数字或下划线'],
      [40003, '密码长度须为8–64位'], [40003, '密码过于常见，请使用更安全的密码'],
      [40102, '账号不存在'], [40103, '密码错误'], [40901, '用户名已被使用'],
      [42903, '登录尝试过多'], [50301, '服务暂不可用'],
    ])(`${mode} displays business code %i and its Chinese reason`, async (code, message) => {
      const mock = accountMock();
      mock.overrides[`POST /auth/${mode}`] = () => json({ code, message, retry_after_seconds: 2 }, code === 42903 ? 429 : 400);
      const onSuccess = vi.fn(); render(<AuthForm onSuccess={onSuccess} />);
      if (mode === 'register') fireEvent.click(screen.getByRole('tab', { name: '注册' }));
      fill(); fireEvent.click(screen.getByRole('button', { name: mode === 'login' ? '登录' : '注册' }));
      expect(await screen.findByRole('alert')).toHaveTextContent(message);
      expect(onSuccess).not.toHaveBeenCalled();
      if (code === 42903) expect(screen.getByRole('status')).toHaveTextContent('2 秒后再试');
    });
  }
  it('counts down 42903 and then enables retry without another request', async () => {
    vi.useFakeTimers(); const mock = accountMock();
    mock.overrides['POST /auth/login'] = () => json({ code: 42903, message: '登录尝试过多', retry_after_seconds: 2 }, 429);
    render(<AuthForm onSuccess={vi.fn()} />); fill();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '登录' })); });
    expect(screen.getByRole('button', { name: '登录' })).toBeDisabled();
    await act(async () => { vi.advanceTimersByTime(1000); });
    expect(screen.getByRole('status')).toHaveTextContent('1 秒后再试');
    await act(async () => { vi.advanceTimersByTime(1000); });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录' })).toBeEnabled();
    expect(mock.fetchMock).toHaveBeenCalledOnce();
  });
  it('prevents repeated submit while pending and keeps the entered username after errors', async () => {
    const mock = accountMock(); let resolve!: (response: Response) => void;
    mock.overrides['POST /auth/login'] = () => new Promise<Response>(done => { resolve = done; });
    render(<AuthForm onSuccess={vi.fn()} />); fill();
    const form = screen.getByLabelText('密码').closest('form')!;
    fireEvent.submit(form); fireEvent.submit(form);
    expect(screen.getByRole('button', { name: '正在提交…' })).toBeDisabled();
    expect(mock.fetchMock).toHaveBeenCalledOnce();
    await act(async () => { resolve(json({ code: 40103, message: '密码错误' }, 401)); });
    expect(screen.getByLabelText('用户名')).toHaveValue('demo_123');
    expect(screen.getByRole('button', { name: '登录' })).toBeEnabled();
  });
});
