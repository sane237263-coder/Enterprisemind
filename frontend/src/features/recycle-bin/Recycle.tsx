import { formatBeijingTime } from '../../shared/datetime';
import { useEffect, useState } from 'react';
import { RotateCcw, Trash2, Folder as FolderIcon, BookOpen, FileText } from 'lucide-react';
import { all, api, mutation } from '../../shared/api';
import { Confirmation, Empty, ErrorMessage, Field, Form, Modal } from '../../shared/ui';
import type { Base, Document, Folder, User } from '../../shared/types';

type Recycled = { id: string; name: string; path: 'documents' | 'folders' | 'knowledge-bases'; row_version: number; baseName: string; baseRecycled: boolean; purge_after?: string };
export function Recycle({ user }: { user: User }) {
  const [rows, setRows] = useState<Recycled[]>([]); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  const [restore, setRestore] = useState<Recycled>(); const [pending, setPending] = useState('');
  async function refresh() {
    setError('');
    try {
      const [active, deleted] = await Promise.all([all<Base>('/api/v1/knowledge-bases'), all<Base>('/api/v1/knowledge-bases?scope=recycle_bin')]);
      const results: Recycled[] = deleted.map(base => ({ ...base, path: 'knowledge-bases', baseName: '知识库及其内容', baseRecycled: false }));
      for (const base of [...active, ...deleted]) {
        const [folders, documents] = await Promise.all([all<Folder>(`/api/v1/knowledge-bases/${base.id}/folders?scope=recycle_bin`), all<Document>(`/api/v1/knowledge-bases/${base.id}/documents?scope=recycle_bin`)]);
        results.push(...folders.filter(row => row.is_deletion_root).map(row => ({ ...row, path: 'folders' as const, baseName: base.name, baseRecycled: base.status === 'recycle_bin' })),
          ...documents.filter(row => row.is_deletion_root).map(row => ({ ...row, path: 'documents' as const, baseName: base.name, baseRecycled: base.status === 'recycle_bin' })));
      }
      setRows(results);
    } catch (e) { setError((e as Error).message); } finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, []);
  async function purge(row: Recycled) { try { setPending((await api<{ id: string }>(`/api/v1/${row.path}/${row.id}/purge-requests`, mutation('POST', { row_version: row.row_version }))).data.id); } catch (e) { setError((e as Error).message); } }
  return <section className="page"><header className="page-heading"><div><p className="eyebrow">恢复与清理</p><h1>回收站</h1><p>删除内容保留 30 天。恢复文件夹或知识库时，会一并恢复同次删除的内容。</p></div></header><ErrorMessage value={error} />
    <div className="table-wrap"><table><thead><tr><th>名称</th><th>原所属位置</th><th>计划清理时间</th><th className="align-right">操作</th></tr></thead><tbody>{rows.map(row => <tr key={`${row.path}-${row.id}`}><td><span className="file-name">{row.path === 'knowledge-bases' ? <BookOpen size={20} /> : row.path === 'folders' ? <FolderIcon size={20} /> : <FileText size={20} />}{row.name}</span></td>
      <td>{row.baseName}{row.baseRecycled && <small>先恢复所属知识库</small>}</td><td>{formatBeijingTime(row.purge_after)}</td><td className="row-actions">
      <button className="text-button" disabled={(row.path === 'knowledge-bases' && user.role !== 'admin') || row.baseRecycled} onClick={() => setRestore(row)}><RotateCcw size={15} />恢复</button>
      <button className="text-button danger-text" disabled={row.path === 'knowledge-bases' && user.role !== 'admin'} onClick={() => void purge(row)}><Trash2 size={15} />永久删除</button></td></tr>)}</tbody></table>
      {!rows.length && <Empty title={loading ? '正在读取回收站…' : '回收站是空的'}>已删除的资料会在这里保留一段时间，方便恢复。</Empty>}</div>
    {restore && <Modal title={`恢复 ${restore.name}`} close={() => setRestore(undefined)}><Form label="恢复" cancel={() => setRestore(undefined)} onSubmit={async (data, key) => {
      await api(`/api/v1/${restore.path}/${restore.id}/restore`, mutation('POST', { row_version: restore.row_version, name: String(data.get('name')) }, key)); setRestore(undefined); await refresh();
    }}><Field label="恢复后的名称"><input name="name" defaultValue={restore.name} required maxLength={200} /></Field><p className="muted">默认恢复到原位置。若存在同名内容，请修改名称后恢复。</p></Form></Modal>}
    {pending && <Confirmation id={pending} done={() => { setPending(''); void refresh(); }} />}
  </section>;
}
