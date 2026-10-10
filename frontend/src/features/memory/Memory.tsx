import { formatBeijingTime } from '../../shared/datetime';
import { useEffect, useState } from 'react';
import { Brain, Pencil, Plus, Trash2 } from 'lucide-react';
import { api, mutation } from '../../shared/api';
import { Empty, ErrorMessage, Field, Form, Modal } from '../../shared/ui';
import type { User } from '../../shared/types';

type Category = 'preference' | 'work_context' | 'ongoing_context' | 'other';
type MemoryItem = {
  id: string;
  category: Category;
  subject_key: string | null;
  content: string;
  origin: string;
  revision: number;
  updated_at: string | null;
};

const labels: Record<Category, string> = {
  preference: '偏好', work_context: '工作背景', ongoing_context: '持续事项', other: '其他',
};

export function Memory({ user }: { user: User }) {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [editing, setEditing] = useState<MemoryItem | null | undefined>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try { setItems((await api<MemoryItem[]>('/api/v1/me/memories?limit=100')).data); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, [user.id]);

  async function remove(item: MemoryItem) {
    if (!window.confirm('删除这条 Memory？删除后不会再注入新的会话。')) return;
    setError('');
    try {
      await api(`/api/v1/me/memories/${item.id}`, mutation('DELETE', { expected_revision: item.revision }));
      await load();
    } catch (e) { setError((e as Error).message); }
  }

  return <section className="page">
    <div className="page-heading"><div><p className="eyebrow">PERSONAL MEMORY</p><h1>我的 Memory</h1>
      <p>只用于你的个性化上下文，不改变资料权限，也不代替企业事实。</p></div>
      <button onClick={() => setEditing(null)}><Plus size={17} />新增记忆</button></div>
    <ErrorMessage value={error} />
    {loading ? <div className="empty">正在读取…</div> : !items.length ? <Empty title="还没有保存的 Memory">你可以手动新增，也可以在对话中明确说“请记住”。</Empty> :
      <div className="table-wrap"><table><thead><tr><th>类别</th><th>主题</th><th>内容</th><th>更新</th><th /></tr></thead><tbody>
        {items.map(item => <tr key={item.id}><td><span className="badge active">{labels[item.category]}</span></td>
          <td>{item.subject_key ?? <span className="muted">通用</span>}</td><td><strong>{item.content}</strong></td>
          <td className="muted">{formatBeijingTime(item.updated_at)}</td>
          <td><div className="row-actions"><button className="icon-button small" aria-label="编辑" onClick={() => setEditing(item)}><Pencil size={15} /></button>
            <button className="icon-button small danger-text" aria-label="删除" onClick={() => void remove(item)}><Trash2 size={15} /></button></div></td></tr>)}
      </tbody></table></div>}
    {editing !== undefined && <MemoryEditor item={editing} close={() => setEditing(undefined)} saved={async () => { setEditing(undefined); await load(); }} />}
  </section>;
}

function MemoryEditor({ item, close, saved }: { item: MemoryItem | null; close: () => void; saved: () => Promise<void> }) {
  return <Modal title={item ? '编辑 Memory' : '新增 Memory'} close={close}>
    <Form label="保存" cancel={close} onSubmit={async (form, key) => {
      const body = { category: String(form.get('category')) as Category, subject_key: String(form.get('subject_key') ?? '').trim() || null, content: String(form.get('content') ?? ''), ...(item ? { expected_revision: item.revision } : {}) };
      await api(item ? `/api/v1/me/memories/${item.id}` : '/api/v1/me/memories', mutation(item ? 'PATCH' : 'POST', body, key));
      await saved();
    }}>
      <Field label="类别"><select name="category" defaultValue={item?.category ?? 'preference'}>{Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
      <Field label="主题（可选）"><input name="subject_key" maxLength={300} defaultValue={item?.subject_key ?? ''} placeholder="例如：写作风格" /></Field>
      <Field label="内容"><textarea name="content" required minLength={1} maxLength={1000} rows={5} defaultValue={item?.content ?? ''} placeholder="只填写稳定且安全的个人偏好或工作背景" /></Field>
      <p className="muted">不要保存密码、Token、密钥、数据库连接串或企业资料副本。</p>
    </Form>
  </Modal>;
}

export { Brain };
