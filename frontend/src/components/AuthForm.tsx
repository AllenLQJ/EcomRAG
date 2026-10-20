import { useEffect, useId, useRef, useState } from 'react';
import { accountAPI, accountError, type User } from '../api/account';
import { ChatError } from '../api/errors';
import { registrationPasswordRules, validateCredentials } from '../lib/credentials';
export { validateCredentials } from '../lib/credentials';

export function AuthForm({ onSuccess, onGuest }: { onSuccess: (user: User) => void; onGuest?: () => void }) {
  const entry = Boolean(onGuest);
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [passwordTouched, setPasswordTouched] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [retryAt, setRetryAt] = useState(0);
  const submitting = useRef(false);
  const id = useId();
  const label = mode === 'login' ? '登录' : entry ? '注册并进入' : '注册';
  const passwordRules = registrationPasswordRules(username, password);
  const invalidPassword = mode === 'register' && passwordTouched && passwordRules.some(rule => !rule.satisfied);
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
  return <div className="entry-auth">
    <div role="tablist" aria-label="登录或注册" className="entry-auth-tabs">
      {(['login', 'register'] as const).map(tab => <button type="button" role="tab" id={`${id}-${tab}`} aria-controls={`${id}-panel`}
        aria-selected={mode === tab} disabled={pending} key={tab} tabIndex={mode === tab ? 0 : -1}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 'login' : event.key === 'End' ? 'register' : mode === 'login' ? 'register' : 'login';
          setMode(next); setError(''); setPassword(''); setPasswordTouched(false); document.getElementById(`${id}-${next}`)?.focus();
        }} onClick={() => { setMode(tab); setError(''); setPassword(''); setPasswordTouched(false); }}
        className={`min-h-11 flex-1 border-b-2 text-sm ${mode === tab ? 'border-ink font-medium' : 'border-transparent text-muted'}`}>{tab === 'login' ? '登录' : '注册'}</button>)}
    </div>
    <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-${mode}`}>
      <form noValidate className="space-y-4" onSubmit={async event => {
        event.preventDefault();
        if (submitting.current || seconds > 0) return;
        const invalid = validateCredentials(username, password, mode);
        if (mode === 'register') setPasswordTouched(true);
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
        <div><label htmlFor={`${id}-username`} className="entry-field-label">用户名</label>
          <input id={`${id}-username`} name="username" autoComplete="username" value={username} disabled={pending}
            aria-describedby={mode === 'register' ? `${id}-username-hint ${id}-error` : `${id}-error`} className="entry-auth-input" onChange={event => { setUsername(event.target.value); setError(''); }} />
          {mode === 'register' && <p id={`${id}-username-hint`} className="mt-2 text-xs text-muted">3–20 位字母、数字或下划线。</p>}</div>
        <div><label htmlFor={`${id}-password`} className="entry-field-label">密码</label>
          <input id={`${id}-password`} name="password" type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            value={password} disabled={pending} aria-invalid={invalidPassword} aria-describedby={mode === 'register' ? `${id}-password-hint ${id}-error` : `${id}-error`} className="entry-auth-input"
            onBlur={() => setPasswordTouched(true)} onChange={event => { setPassword(event.target.value); setPasswordTouched(true); setError(''); }} />
          {mode === 'register' && <ul id={`${id}-password-hint`} aria-label="注册密码要求" aria-live="polite" className="mt-2 space-y-1 text-xs leading-relaxed">
            {passwordRules.map(rule => {
              const state = !passwordTouched ? 'pending' : rule.satisfied ? 'satisfied' : 'unmet';
              return <li key={rule.id} data-rule={rule.id} data-state={state}
                className={`password-rule ${state === 'satisfied' ? 'password-rule-satisfied' : state === 'unmet' ? 'password-rule-unmet' : 'password-rule-pending'}`}>
                <span aria-hidden="true">{state === 'satisfied' ? '✓ ' : state === 'unmet' ? '! ' : '· '}</span>
                {rule.label}<span className="sr-only">：{state === 'satisfied' ? '已满足' : state === 'unmet' ? '未满足' : '待输入'}</span>
              </li>;
            })}
          </ul>}</div>
        <div id={`${id}-error`} aria-live="polite" hidden={!error && seconds === 0}>
          {error && <p role="alert" className="text-sm text-fallback-text">{error}</p>}
          {seconds > 0 && <p role="status" className="mt-2 text-sm text-muted">请 {seconds} 秒后再试。</p>}
        </div>
        <button className="entry-auth-submit disabled:opacity-50" type="submit" disabled={pending || seconds > 0}>{pending ? '正在提交…' : label}</button>
      </form>
    </div>
    {onGuest && <>
      <div className="entry-auth-divider"><span aria-hidden="true" /><span>或</span><span aria-hidden="true" /></div>
      <button type="button" className="entry-auth-guest" onClick={onGuest}>以访客进入</button>
      <p className="entry-auth-quota">访客每天可提问 5 次，登录后每天可提问 50 次</p>
    </>}
  </div>;
}
