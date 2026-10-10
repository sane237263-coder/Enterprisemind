import { useEffect, useState } from 'react';
import { Plus, Users, Building2, Database, KeyRound, Pencil, Trash2 } from 'lucide-react';
import { all, api, mutation } from '../../shared/api';
import { Badge, Empty, ErrorMessage, Field, Form, Modal } from '../../shared/ui';
import type { ClientAction, Department, User } from '../../shared/types';
import { DatabaseAdmin } from './DatabaseAdmin';

type Editor = { kind: 'user' | 'department' | 'reset' | 'delete-department'; user?: User; department?: Department; action?: ClientAction };
export function Admin({ action, consumed }: { action?: ClientAction; consumed: () => void }) {
  const [tab, setTab] = useState<'users' | 'departments' | 'database'>('users');
  const [users, setUsers] = useState<User[]>([]); const [departments, setDepartments] = useState<Department[]>([]);
  const [editor, setEditor] = useState<Editor>(); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  async function refresh() {
    setError('');
    try { const [u, d] = await Promise.all([all<User>('/api/v1/admin/users'), all<Department>('/api/v1/admin/departments')]); setUsers(u); setDepartments(d); }
    catch (e) { setError((e as Error).message); } finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, []);
  useEffect(() => {
    if (!action || loading) return;
    let active = true;
    async function open() {
      try {
        if (!action?.available) throw new Error('此操作入口已失效，请在会话中重新发起。');
        await api<User>('/api/v1/auth/me').then(r => { if (r.data.role !== 'admin') throw new Error('需要管理员权限。'); });
        if (action.action === 'open_admin_user_create') { if (active) setEditor({ kind: 'user', action }); }
        else {
          if (!action.user_id) throw new Error('操作目标无效。');
          const target = (await api<User>(`/api/v1/admin/users/${action.user_id}`)).data;
          if (target.row_version !== action.row_version || target.username !== action.display_name) throw new Error('用户信息已变化，请重新发起密码重置。');
          if (active) setEditor({ kind: 'reset', user: target, action });
        }
      } catch (e) { if (active) setError((e as Error).message); }
      finally { if (active) consumed(); }
    }
    void open(); return () => { active = false; };
  }, [action, loading]);
  const close = () => setEditor(undefined);
  const prefill = editor?.action?.action === 'open_admin_user_create' ? editor.action.prefill : undefined;
  async function save(data: FormData, key: string) {
    if (!editor) return;
    const get = (name: string) => String(data.get(name) ?? '');
    if (editor.kind === 'reset') {
      if (get('password') !== get('confirm_password')) throw new Error('两次输入的密码不一致。');
      await api(`/api/v1/admin/users/${editor.user!.id}/password-reset`, mutation('POST', { row_version: editor.user!.row_version, new_password: get('password') }, key));
    } else if (editor.kind === 'user') {
      const body = { username: get('username'), role: get('role'), status: get('status'), department_id: get('department_id') || null };
      if (body.role === 'user' && !body.department_id) throw new Error('请选择用户所属部门。');
      await api(editor.user ? `/api/v1/admin/users/${editor.user.id}` : '/api/v1/admin/users', mutation(editor.user ? 'PATCH' : 'POST', {
        ...body, ...(editor.user ? { row_version: editor.user.row_version } : { password: get('password') }),
      }, key));
    } else if (editor.kind === 'delete-department') {
      await api(`/api/v1/admin/departments/${editor.department!.id}?row_version=${editor.department!.row_version}`, mutation('DELETE', undefined, key));
    } else {
      await api(editor.department ? `/api/v1/admin/departments/${editor.department.id}` : '/api/v1/admin/departments', mutation(editor.department ? 'PATCH' : 'POST', {
        name: get('name'), status: get('status'), ...(editor.department ? { row_version: editor.department.row_version } : {}),
      }, key));
    }
    close(); await refresh();
  }
  const isDatabase = tab === 'database';
  return <section className="page"><header className="page-heading"><div><p className="eyebrow">{isDatabase ? '结构化数据' : '组织与权限'}</p><h1>{isDatabase ? '数据库配置' : '成员管理'}</h1><p>{isDatabase ? '配置业务数据源、发布 Schema，并维护查询授权边界。' : '维护企业成员与部门，让知识在正确的范围内流转。'}</p></div>
    {!isDatabase && <button onClick={() => setEditor({ kind: tab === 'users' ? 'user' : 'department' })}><Plus size={17} />{tab === 'users' ? '创建用户' : '创建部门'}</button>}</header>
    <nav className="tabs" aria-label="管理分类"><button className={tab === 'users' ? 'selected' : ''} onClick={() => setTab('users')}><Users size={17} />用户</button>
    <button className={tab === 'departments' ? 'selected' : ''} onClick={() => setTab('departments')}><Building2 size={17} />部门</button>
    <button className={tab === 'database' ? 'selected' : ''} onClick={() => setTab('database')}><Database size={17} />数据库</button></nav>
    <ErrorMessage value={error} />
    {isDatabase ? <DatabaseAdmin departments={departments} /> : loading ? <Empty title="正在读取组织信息…" /> : <div className="table-wrap"><table><thead><tr><th>{tab === 'users' ? '用户名' : '部门名称'}</th>{tab === 'users' && <><th>角色</th><th>所属部门</th></>}<th>状态</th><th className="align-right">操作</th></tr></thead>
    <tbody>{tab === 'users' ? users.map(user => <tr key={user.id}><td><strong>{user.username}</strong></td><td>{user.role === 'admin' ? '管理员' : '普通用户'}</td><td>{user.department?.name ?? '—'}</td><td><Badge value={user.status} /></td>
    <td className="row-actions"><button className="text-button" onClick={() => setEditor({ kind: 'user', user })}><Pencil size={15} />编辑</button><button className="text-button" onClick={() => setEditor({ kind: 'reset', user })}><KeyRound size={15} />重置密码</button></td></tr>)
    : departments.map(department => <tr key={department.id}><td><strong>{department.name}</strong></td><td><Badge value={department.status} /></td><td className="row-actions">
      <button className="text-button" onClick={() => setEditor({ kind: 'department', department })}><Pencil size={15} />编辑</button>
      <button className="text-button danger-text" onClick={() => setEditor({ kind: 'delete-department', department })}><Trash2 size={15} />删除</button></td></tr>)}</tbody></table>
    {tab === 'departments' && !departments.length && <Empty title="还没有部门">创建部门后，即可为普通用户分配所属部门。</Empty>}</div>}
    {editor && <Modal title={editor.kind === 'reset' ? `重置 ${editor.user!.username} 的密码` : editor.kind === 'delete-department' ? '删除部门' : editor.kind === 'user' ? editor.user ? '编辑用户' : '创建用户' : editor.department ? '编辑部门' : '创建部门'} close={close}>
      <Form onSubmit={save} cancel={close} label={editor.kind === 'reset' ? '重置密码' : editor.kind === 'delete-department' ? '删除部门' : '保存'}>
      {editor.kind === 'delete-department' ? <p>删除“{editor.department!.name}”。有用户或知识库引用的部门无法删除。</p> : editor.kind === 'reset' ? <>
        <p className="muted">提交后，该用户已有登录凭据将失效。</p><Field label="新密码"><input name="password" type="password" required minLength={12} autoComplete="new-password" /></Field>
        <Field label="再次输入新密码"><input name="confirm_password" type="password" required minLength={12} autoComplete="new-password" /></Field>
      </> : editor.kind === 'user' ? <>
        <Field label="用户名"><input name="username" required maxLength={100} autoComplete="off" defaultValue={editor.user?.username ?? prefill?.username ?? ''} /></Field>
        <Field label="角色"><select name="role" defaultValue={editor.user?.role ?? prefill?.role ?? 'user'}><option value="user">普通用户</option><option value="admin">管理员</option></select></Field>
        <Field label="所属部门"><select name="department_id" defaultValue={editor.user?.department_id ?? prefill?.department_id ?? ''}><option value="">未选择</option>{departments.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></Field>
        {!editor.user && <Field label="初始密码"><input name="password" type="password" required minLength={12} autoComplete="new-password" /></Field>}
      </> : <Field label="部门名称"><input name="name" required maxLength={200} defaultValue={editor.department?.name ?? ''} /></Field>}
      {(editor.kind === 'user' || editor.kind === 'department') && <Field label="状态"><select name="status" defaultValue={editor.user?.status ?? editor.department?.status ?? prefill?.status ?? 'active'}><option value="active">启用</option><option value="disabled">停用</option></select></Field>}
      </Form>
    </Modal>}
  </section>;
}
