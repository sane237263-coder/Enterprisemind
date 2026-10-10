import { useEffect, useState } from 'react';
import { BookOpen, MessageSquare, Trash2, Users, LogOut, Menu, ArrowRight, Brain } from 'lucide-react';
import { api, setCredential } from './shared/api';
import type { ClientAction, User } from './shared/types';
import { ErrorMessage } from './shared/ui';
import { BrandMark } from './shared/BrandMark';
import { Admin } from './features/admin/Admin';
import { Chat } from './features/agent/Chat';
import { Knowledge } from './features/knowledge/Knowledge';
import { Recycle } from './features/recycle-bin/Recycle';
import { Preview } from './features/preview/Preview';
import { Memory } from './features/memory/Memory';
import type { PreviewTarget } from './features/preview/Preview';

type Page = 'agent' | 'knowledge' | 'memory' | 'recycle' | 'admin';
const pages: { id: Page; label: string; icon: typeof BookOpen }[] = [
  { id: 'agent', label: '知识助手', icon: MessageSquare }, { id: 'knowledge', label: '知识库', icon: BookOpen },
  { id: 'memory', label: '我的 Memory', icon: Brain },
  { id: 'recycle', label: '回收站', icon: Trash2 }, { id: 'admin', label: '成员管理', icon: Users },
];
export default function App() {
  const [user, setUser] = useState<User>(); const [page, setPage] = useState<Page>('agent'); const [mobile, setMobile] = useState(false);
  const [preview, setPreview] = useState<PreviewTarget>(); const [action, setAction] = useState<ClientAction>(); const [expired, setExpired] = useState(false);
  function logout() { setCredential(''); setUser(undefined); setPreview(undefined); setAction(undefined); setPage('agent'); }
  useEffect(() => { const listener = () => { logout(); setExpired(true); }; window.addEventListener('session-expired', listener); return () => window.removeEventListener('session-expired', listener); }, []);
  useEffect(() => {
    if (!user) return;
    const timer = setInterval(() => { void api<User>('/api/v1/auth/me').then(result => setUser(result.data)).catch(() => {}); }, 15000);
    return () => clearInterval(timer);
  }, [user?.id]);
  useEffect(() => { if (user?.role !== 'admin' && page === 'admin') { setAction(undefined); setPage('agent'); } }, [user?.role, page]);
  if (!user) return <Login expired={expired} done={value => { setExpired(false); setUser(value); }} />;
  return <div className={`workspace ${mobile ? 'nav-open' : ''}`}><aside className="main-sidebar"><a href="#" className="brand" onClick={e => { e.preventDefault(); setPage('agent'); }}><BrandMark /><span>EnterpriseMind<small>企业知识工作台</small></span></a>
    <p className="sidebar-label">工作空间</p><nav aria-label="主导航">{pages.filter(item => item.id !== 'admin' || user.role === 'admin').map(item => <button key={item.id} className={page === item.id ? 'selected' : ''} aria-current={page === item.id ? 'page' : undefined} onClick={() => { setPage(item.id); setMobile(false); }}><item.icon size={19} />{item.label}</button>)}</nav>
    <div className="sidebar-bottom"><div className="user-avatar">{user.username.slice(0, 1)}</div><div><strong>{user.username}</strong><small>{user.role === 'admin' ? '管理员' : user.department?.name}</small></div><button className="icon-button" aria-label="退出登录" title="退出登录" onClick={logout}><LogOut size={17} /></button></div></aside>
    <main><header className="workspace-bar"><button className="icon-button mobile-menu" aria-label="打开导航" onClick={() => setMobile(value => !value)}><Menu size={20} /></button><span>企业空间 <span className="bar-separator">/</span> {pages.find(item => item.id === page)?.label}</span></header>
      {page === 'agent' && <Chat user={user} preview={setPreview} navigate={value => { if (user.role === 'admin') { setAction(value); setPage('admin'); } }} />}
      {page === 'knowledge' && <Knowledge user={user} preview={setPreview} />}
      {page === 'memory' && <Memory user={user} />}
      {page === 'recycle' && <Recycle user={user} />}
      {page === 'admin' && user.role === 'admin' && <Admin action={action} consumed={() => setAction(undefined)} />}
    </main>{preview && <Preview target={preview} close={() => setPreview(undefined)} />}
  </div>;
}
function Login({ done, expired }: { done: (user: User) => void; expired: boolean }) {
  const [username, setUsername] = useState(''); const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  return <div className="login-page"><section className="login-story"><div className="brand"><BrandMark />EnterpriseMind</div><div><p className="eyebrow">连接企业知识与日常工作</p><h1>让每一次回答，<br />都找得到依据。</h1><p>从资料中找到答案，从答案回到原文。<br />你的企业知识，在一个专注的工作空间里。</p><div className="login-proof"><span><BookOpen size={21} />资料</span><i /><span><MessageSquare size={21} />回答</span><i /><span><ArrowRight size={21} />来源</span></div></div><small>EnterpriseMind · 企业知识工作台</small></section>
    <section className="login-form"><div><p className="eyebrow">欢迎回来</p><h2>登录工作空间</h2><p className="muted">使用管理员提供的本地账号登录。</p><ErrorMessage value={error || (expired ? '登录已过期或账号权限已变化，请重新登录。' : '')} />
    <form onSubmit={async e => { e.preventDefault(); if (busy) return; setBusy(true); setError(''); try { const result = await api<{ user: User; access_token: string }>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }); setPassword(''); setCredential(result.data.access_token); done(result.data.user); } catch (err) { setError((err as Error).message); } finally { setBusy(false); } }}>
      <label className="field"><span>用户名</span><input name="username" value={username} onChange={e => setUsername(e.target.value)} required autoComplete="username" autoFocus /></label>
      <label className="field"><span>密码</span><input name="password" type="password" value={password} onChange={e => setPassword(e.target.value)} required autoComplete="current-password" /></label>
      <button disabled={busy} type="submit">{busy ? '正在登录…' : '登录'}<ArrowRight size={17} /></button>
    </form><p className="login-help">忘记密码？请联系管理员重置。</p></div></section></div>;
}
