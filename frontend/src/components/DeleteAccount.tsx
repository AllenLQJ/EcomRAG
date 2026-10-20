import { useId, useRef, useState } from 'react';
import { accountError } from '../api/account';
import { Modal } from './Modal';

export function DeleteAccount({ onClose, onDelete }: { onClose: () => void; onDelete: (password: string) => Promise<void> }) {
  const id = useId();
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const submitting = useRef(false);
  return <Modal title="注销账号" onClose={onClose}>
    <p className="mb-4 text-sm leading-relaxed">确认注销账号？账号与服务器历史对话将被删除，此操作无法恢复。请输入密码确认。</p>
    <form className="space-y-4" onSubmit={async event => {
      event.preventDefault();
      if (submitting.current) return;
      if (!password) { setError('请输入密码。'); return; }
      submitting.current = true; setPending(true); setError('');
      try { await onDelete(password); }
      catch (failure) { setError(accountError(failure)); }
      finally { submitting.current = false; setPending(false); }
    }}>
      <label htmlFor={id} className="block text-sm">确认密码</label>
      <input className="form-input" id={id} type="password" autoComplete="current-password" value={password} disabled={pending}
        onChange={event => setPassword(event.target.value)} />
      {error && <p role="alert" className="text-sm text-fallback-text">{error}</p>}
      <div className="flex flex-wrap gap-3"><button type="button" className="secondary-button" onClick={onClose}>取消</button>
        <button type="submit" className="primary-button disabled:opacity-50" disabled={pending || !password}>{pending ? '正在注销…' : '确认注销'}</button></div>
    </form>
  </Modal>;
}
