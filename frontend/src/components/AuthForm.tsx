import { useEffect, useId, useRef, useState } from 'react';
import { accountAPI, accountError, type User } from '../api/account';
import { ChatError } from '../api/errors';

export function validateCredentials(username: string, password: string): string | undefined {
  if (!/^[A-Za-z0-9_]{3,20}$/.test(username)) return '用户名须为 3–20 位，仅含字母、数字或下划线。';
  if (Array.from(password).length < 8 || Array.from(password).length > 64) return '密码须为 8–64 位。';
}

export function AuthForm({ onSuccess, onGuest }: { onSuccess: (user: User) => void; onGuest?: () => void }) {
  const entry = Boolean(onGuest);
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [retryAt, setRetryAt] = useState(0);
  const submitting = useRef(false);
  const id = useId();
  const label = mode === 'login' ? '登录' : entry ? '注册并进入' : '注册';
  useEffect(() => {
    if (!retryAt) return;
    const update = () => {
      const remaining = Math.max(0, Math.ceil((retryAt - Date.now()) / 1000));
      setSeconds(remaining);
      if (remaining === 0) setRetryAt(0);
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [retryAt]);
  return <div className={entry ? 'entry-auth' : 'mx-auto w-full max-w-md'}>
    <div role="tablist" aria-label="登录或注册" className={entry ? 'entry-auth-tabs' : 'mb-6 flex border-b border-line'}>
      {(['login', 'register'] as const).map(tab => <button type="button" role="tab" id={`${id}-${tab}`} aria-controls={`${id}-panel`}
        aria-selected={mode === tab} disabled={pending} key={tab} tabIndex={mode === tab ? 0 : -1}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 'login' : event.key === 'End' ? 'register' : mode === 'login' ? 'register' : 'login';
          setMode(next); setError(''); setPassword(''); document.getElementById(`${id}-${next}`)?.focus();
        }} onClick={() => { setMode(tab); setError(''); setPassword(''); }}
        className={`min-h-11 flex-1 border-b-2 text-sm ${mode === tab ? 'border-ink font-medium' : 'border-transparent text-muted'}`}>{tab === 'login' ? '登录' : '注册'}</button>)}
    </div>
    <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-${mode}`}>
      {mode === 'register' && <p className={entry ? 'entry-auth-warning' : 'mb-4 rounded-card border border-outline bg-card p-3 text-sm font-medium text-fallback-text'}>演示站点，请勿使用你常用的真实密码。</p>}
      <form noValidate className="space-y-4" onSubmit={async event => {
        event.preventDefault();
        if (submitting.current || seconds > 0) return;
        const invalid = validateCredentials(username, password);
        if (invalid) { setError(invalid); return; }
        submitting.current = true; setPending(true); setError('');
        try { const { user } = await accountAPI.authenticate(mode, username, password); setPassword(''); onSuccess(user); }
        catch (failure) {
          setError(accountError(failure));
          if (failure instanceof ChatError && failure.code === 42903) {
            const delay = Math.max(1, Math.ceil(failure.retryAfterSeconds || 60));
            setSeconds(delay); setRetryAt(Date.now() + delay * 1000);
          }
        } finally { submitting.current = false; setPending(false); }
      }}>
        <div><label htmlFor={`${id}-username`} className={entry ? 'entry-field-label' : 'mb-2 block text-sm'}>用户名</label>
          <input id={`${id}-username`} name="username" autoComplete="username" value={username} disabled={pending}
            aria-describedby={`${id}-hint ${id}-error`} className={entry ? 'entry-auth-input' : 'form-input'} onChange={event => setUsername(event.target.value)} />
          <p id={`${id}-hint`} className={entry ? 'sr-only' : 'mt-2 text-xs text-muted'}>3–20 位字母、数字或下划线；密码 8–64 位。</p></div>
        <div><label htmlFor={`${id}-password`} className={entry ? 'entry-field-label' : 'mb-2 block text-sm'}>密码</label>
          <input id={`${id}-password`} name="password" type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            value={password} disabled={pending} aria-describedby={`${id}-hint ${id}-error`} className={entry ? 'entry-auth-input' : 'form-input'} onChange={event => setPassword(event.target.value)} /></div>
        <div id={`${id}-error`} aria-live="polite" hidden={entry && !error && seconds === 0}>
          {error && <p role="alert" className="text-sm text-fallback-text">{error}</p>}
          {seconds > 0 && <p role="status" className="mt-2 text-sm text-muted">请 {seconds} 秒后再试。</p>}
        </div>
        <button className={entry ? 'entry-auth-submit disabled:opacity-50' : 'primary-button w-full disabled:opacity-50'} type="submit" disabled={pending || seconds > 0}>{pending ? '正在提交…' : label}</button>
      </form>
    </div>
    {onGuest && <>
      <div className="entry-auth-divider"><span aria-hidden="true" /><span>或</span><span aria-hidden="true" /></div>
      <button type="button" className="entry-auth-guest" onClick={onGuest}>以访客进入</button>
      <p className="entry-auth-quota">访客每天可提问 5 次，登录后每天可提问 50 次</p>
    </>}
  </div>;
}
