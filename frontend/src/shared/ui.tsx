import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode, FormEvent } from 'react';
import { X, LoaderCircle, AlertCircle } from 'lucide-react';
import { api, mutation } from './api';
import { formatSystemTimes } from './datetime';
import type { Pending } from './types';

export function ErrorMessage({ value }: { value: string }) {
  return value ? <div role="alert" className="error"><AlertCircle size={17} />{value}</div> : null;
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><h3>{title}</h3><p>{children}</p></div>;
}
export function Modal({ title, children, close, wide = false }: { title: string; children: ReactNode; close: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => { ref.current?.close(); previous?.focus(); };
  }, []);
  return <dialog ref={ref} aria-labelledby={titleId} className={`modal ${wide ? 'wide' : ''}`} onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2 id={titleId}>{title}</h2><button className="icon-button" onClick={close} aria-label="关闭"><X size={20} /></button></header>{children}
  </dialog>;
}
export function Form({ children, onSubmit, label = '保存', cancel }: {
  children: ReactNode; onSubmit: (form: FormData, key: string) => Promise<void>; label?: string; cancel: () => void;
}) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const key = useRef(crypto.randomUUID());
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError('');
    try { await onSubmit(form, key.current); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <form onSubmit={submit} onChange={() => { key.current = crypto.randomUUID(); }} className="form"><fieldset disabled={busy}>{children}</fieldset><ErrorMessage value={error} />
    <footer><button type="button" className="secondary" onClick={cancel} disabled={busy}>取消</button>
    <button type="submit" disabled={busy}>{busy && <LoaderCircle size={16} className="spin" />}{busy ? '正在处理…' : label}</button></footer></form>;
}
export function Field({ label, children }: { label: ReactNode; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}
export function Confirmation({ id, done }: { id: string; done: () => void }) {
  const [pending, setPending] = useState<Pending>(); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [selection, setSelection] = useState(''); const keys = useRef({ accept: crypto.randomUUID(), reject: crypto.randomUUID() });
  useEffect(() => { const c = new AbortController(); api<Pending>(`/api/v1/pending-actions/${id}`, { signal: c.signal })
    .then(r => setPending(r.data)).catch(e => { if (!c.signal.aborted) setError(e.message); }); return () => c.abort(); }, [id]);
  async function respond(decision: 'accept' | 'reject') {
    setBusy(true); setError('');
    try { await api(`/api/v1/pending-actions/${id}/responses`, mutation('POST', { decision,
      ...(selection ? { selected_target_id: selection } : {}) }, keys.current[decision])); done(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const active = pending?.status === 'pending' && Date.parse(pending.expires_at) > Date.now();
  return <Modal title={pending?.action_type === 'disambiguation' ? '选择操作目标' : '确认操作'} close={done}>
    <div className="modal-body"><p>{pending ? formatSystemTimes(pending.prompt_text) : '正在读取操作信息…'}</p>
    {pending?.preview && <div className="preview-summary"><strong>入库预览</strong><span>文件：{pending.preview.source_file_name ?? '生成文件'} · {pending.preview.source_format?.toUpperCase() ?? '文件'} · {pending.preview.source_size_bytes ?? 0} 字节</span><span>目标：{pending.preview.target?.knowledge_base_name ?? '未指定'}{pending.preview.target?.folder_name ? ` / ${pending.preview.target.folder_name}` : ''} · {pending.preview.target?.name ?? '未命名'}</span><span>来源：{pending.preview.source_refs?.length ?? 0} 条；{pending.preview.conflict_action === 'overwrite' ? '将沿用既有覆盖确认' : '同名时拒绝'}</span></div>}
    {pending?.action_type === 'disambiguation' && <fieldset className="choices"><legend>选择一个目标</legend>{pending.targets.map(target =>
      <label key={target.id}><input type="radio" name="target" value={target.id} checked={selection === target.id} onChange={() => setSelection(target.id)} />
      <span>{target.display_name}<small>{target.path}</small></span></label>)}</fieldset>}
    {pending && !active && <p className="muted">此操作已处理或已过期，请重新发起。</p>}<ErrorMessage value={error} /></div>
    <footer><button className="secondary" disabled={busy} onClick={() => active ? void respond('reject') : done()}>{active ? '取消操作' : '关闭'}</button>
    <button disabled={busy || !active || (pending?.action_type === 'disambiguation' && !selection)} onClick={() => void respond('accept')}>确认</button></footer>
  </Modal>;
}
export const stateLabel: Record<string, string> = { pending: '等待处理', queued: '等待生成', processing: '处理中', success: '已完成', succeeded: '已完成',
  partial_success: '部分完成', failed: '失败', active: '启用', disabled: '停用', recycle_bin: '已回收',
  completed: '已完成', cancelled: '已停止', running: '进行中', waiting_confirmation: '等待确认' };
export const stageLabel: Record<string, string> = { parse: '解析', ocr: '文字识别', vision: '图片理解', table: '表格解析',
  code: '代码提取', chunk: '内容分块', embedding: '向量处理', keyword_index: '建立索引', preview: '生成预览', activate: '完成入库' };
export function Badge({ value }: { value: string }) { return <span className={`badge ${value}`}>{stateLabel[value] ?? value}</span>; }
