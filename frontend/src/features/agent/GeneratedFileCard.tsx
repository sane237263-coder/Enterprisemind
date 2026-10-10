import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Download, FileOutput, LoaderCircle } from 'lucide-react';
import { all, api, blob, download, mutation } from '../../shared/api';
import { Badge, ErrorMessage, Field, Modal } from '../../shared/ui';
import type { Base, Folder, GeneratedFileTaskPart, Pending } from '../../shared/types';
import { ChartPreviewCard, type ChartPreviewArtifact } from '../charts/ChartPreviewCard';

type GenerationTask = {
  task_id: string;
  kind: string;
  status: string;
  phase: string | null;
  format: GeneratedFileTaskPart['format'];
  scope: GeneratedFileTaskPart['scope'];
  processed_rows: number;
  total_rows: number | null;
  exported_rows: number | null;
  file_id: string | null;
  file_name?: string;
  size_bytes?: number;
  source_refs?: { source_title?: string; source_type?: string; queried_at?: string; executed_at?: string }[];
  artifacts?: { file_id: string; format: GeneratedFileTaskPart['format']; file_name: string; size_bytes: number; download_url: string; preview_url?: string }[];
  chart?: { title?: string; chart_type?: 'bar' | 'line' | 'pie'; scope?: string; statistic?: string };
  preview_url?: string;
  complete: boolean;
  error: { code: string | null; message: string | null } | null;
};

const formatName: Record<GeneratedFileTaskPart['format'], string> = {
  docx: 'DOCX', pdf: 'PDF', md: 'Markdown', xlsx: 'XLSX', png: 'PNG', csv: 'CSV',
};

export function GeneratedFileCard({ part, onPending }: { part: GeneratedFileTaskPart; onPending: (id: string) => void }) {
  const [task, setTask] = useState<GenerationTask>(); const [error, setError] = useState(''); const [importOpen, setImportOpen] = useState(false); const [cancelling, setCancelling] = useState(false); const [previewUrl, setPreviewUrl] = useState('');
  useEffect(() => {
    const controller = new AbortController(); let live = true; let polling = false; let timer: ReturnType<typeof setInterval> | undefined;
    async function refresh() {
      if (polling || controller.signal.aborted) return; polling = true;
      try { const next = (await api<GenerationTask>(`/api/v1/generation-tasks/${part.task_id}`, { signal: controller.signal })).data;
        if (live) { setTask(next); if (['succeeded', 'failed', 'cancelled'].includes(next.status) && timer) clearInterval(timer); }
      } catch (e) { if (live && !controller.signal.aborted) setError((e as Error).message); }
      finally { polling = false; }
    }
    void refresh(); timer = setInterval(() => void refresh(), 1500);
    return () => { live = false; controller.abort(); clearInterval(timer); };
  }, [part.task_id]);
  const pngArtifact = task?.artifacts?.find(artifact => artifact.format === 'png');
  const previewFileId = pngArtifact?.file_id || (task?.format === 'png' ? task.file_id : null);
  useEffect(() => {
    let live = true; let objectUrl = '';
    if (!task || task.status !== 'succeeded' || task.kind !== 'chart_render' || !previewFileId) {
      setPreviewUrl('');
      return () => undefined;
    }
    void blob(`/api/v1/generated-files/${previewFileId}/preview`).then(value => {
      if (!live) return;
      objectUrl = URL.createObjectURL(value); setPreviewUrl(objectUrl);
    }).catch(e => { if (live) setError((e as Error).message); });
    return () => { live = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [previewFileId, task?.kind, task?.status]);
  const name = task?.file_name ?? `生成文件.${part.format === 'md' ? 'md' : part.format}`;
  const terminal = !!task && ['succeeded', 'failed', 'cancelled'].includes(task.status);
  async function cancelTask() {
    if (cancelling) return; setCancelling(true); setError('');
    try { const next = await api<GenerationTask>(`/api/v1/generation-tasks/${part.task_id}/cancel`, mutation('POST')); setTask(next.data); }
    catch (e) { setError((e as Error).message); } finally { setCancelling(false); }
  }
  return <div className="generated-file-card">
    <div className="generated-file-heading"><FileOutput size={17} /><div><strong>{name}</strong><small>{part.kind === 'chart_render' && part.format === 'csv' ? '导出图表数据 CSV' : formatName[part.format]}{['database_export', 'chart_render'].includes(part.kind) ? ` · ${scopeLabel(task?.kind === 'chart_render' ? task.chart?.scope : task?.scope ?? part.scope)}` : ''}</small></div><Badge value={task?.status ?? part.status} /></div>
    {task?.status === 'running' && <p className="muted"><LoaderCircle size={15} className="spin" />{task.phase || '正在生成'}{['database_export', 'chart_render'].includes(task.kind) ? ` · 已处理 ${task.processed_rows} 行` : ''}</p>}
    {task?.status === 'failed' && <p className="muted">{task.error?.message || '文件生成失败。'}</p>}
    {task?.status === 'cancelled' && <p className="muted">任务已取消，未发布半成品文件。</p>}
    {task && ['queued', 'running'].includes(task.status) && <button className="text-button" onClick={() => void cancelTask()} disabled={cancelling}>{cancelling ? '正在取消…' : '取消任务'}</button>}
    {task?.status === 'succeeded' && <p className="muted generated-file-meta">{task.size_bytes == null ? '文件已发布' : formatBytes(task.size_bytes)}{['database_export', 'chart_render'].includes(task.kind) && task.exported_rows != null ? ` · 已处理 ${task.exported_rows} 行 · ${scopeLabel(task.kind === 'chart_render' ? task.chart?.scope : task.scope)}` : ''}{task.source_refs?.length ? ` · 来源 ${task.source_refs.length} 条` : ''}</p>}
    {previewUrl && task?.kind === 'chart_render' && task.file_id && <ChartPreviewCard
      title={task.chart?.title || '数据库图表'}
      chartType={task.chart?.chart_type || 'bar'}
      previewUrl={previewUrl}
      scopeLabel={scopeLabel(task.chart?.scope ?? task.scope)}
      statistic={task.chart?.statistic || '服务端冻结结果'}
      queriedAt={task.source_refs?.[0]?.executed_at || task.source_refs?.[0]?.queried_at || '以来源记录为准'}
      sourceRefs={(task.source_refs || []).map(source => source.source_title || source.source_type || '数据库来源')}
      artifacts={(task.artifacts?.length ? task.artifacts : [{ file_id: task.file_id, format: task.format, file_name: name, size_bytes: task.size_bytes || 0, download_url: `/api/v1/generated-files/${task.file_id}/download` }]).map(artifact => ({ format: artifact.format as ChartPreviewArtifact['format'], href: artifact.download_url, fileName: artifact.file_name, sizeBytes: artifact.size_bytes }))}
      onDownload={artifact => void download(artifact.href, artifact.fileName).catch(e => setError((e as Error).message))}
    />}
    {task?.status === 'succeeded' && task.file_id && <div className="generated-file-actions"><button className="secondary" onClick={() => void download(`/api/v1/generated-files/${task.file_id}/download`, name).catch(e => setError((e as Error).message))}><Download size={15} />下载</button>{task.kind !== 'chart_render' && <button onClick={() => setImportOpen(true)}>预览确认入库</button>}</div>}
    {terminal && !task?.file_id && <p className="muted">没有可下载的已发布文件。</p>}
    <ErrorMessage value={error} />
    {importOpen && task?.file_id && <GeneratedImportDialog fileId={task.file_id} fileName={name} close={() => setImportOpen(false)} onPending={onPending} />}
  </div>;
}

function GeneratedImportDialog({ fileId, fileName, close, onPending }: { fileId: string; fileName: string; close: () => void; onPending: (id: string) => void }) {
  const [bases, setBases] = useState<Base[]>([]); const [folders, setFolders] = useState<Folder[]>([]); const [baseId, setBaseId] = useState(''); const [folderId, setFolderId] = useState('');
  const [name, setName] = useState(fileName); const [description, setDescription] = useState(''); const [conflict, setConflict] = useState<'reject' | 'overwrite'>('reject'); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const importKey = useRef(crypto.randomUUID());
  useEffect(() => { void all<Base>('/api/v1/knowledge-bases').then(setBases).catch(e => setError((e as Error).message)); }, []);
  useEffect(() => { setFolders([]); setFolderId(''); if (!baseId) return; void all<Folder>(`/api/v1/knowledge-bases/${baseId}/folders`).then(setFolders).catch(e => setError((e as Error).message)); }, [baseId]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return; setBusy(true); setError('');
    try {
      const result = await api<Pending>(`/api/v1/generated-files/${fileId}/import-previews`, mutation('POST', {
        knowledge_base_id: baseId, folder_id: folderId || null, name: name.trim(), description: description.trim() || null, conflict_action: conflict,
      }, importKey.current));
      close(); onPending(result.data.id);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <Modal title="预览并确认入库" close={close} wide><form className="form" onSubmit={submit}><div className="modal-body"><p>将私有生成文件先绑定到目标，再经过确认写入。确认前不会创建知识库文档。</p><p className="attachment-chip"><FileOutput size={15} />{fileName}</p>
    <Field label="目标知识库"><select value={baseId} onChange={event => { setBaseId(event.target.value); importKey.current = crypto.randomUUID(); }} required><option value="">请选择</option>{bases.map(base => <option value={base.id} key={base.id}>{base.name} · {base.type === 'public' ? '企业公共' : '部门'}</option>)}</select></Field>
    <Field label="目标文件夹"><select value={folderId} onChange={event => { setFolderId(event.target.value); importKey.current = crypto.randomUUID(); }} disabled={!baseId}><option value="">知识库根目录</option>{folders.map(folder => <option value={folder.id} key={folder.id}>{folder.path ?? folder.name}</option>)}</select></Field>
    <Field label="文档名称"><input value={name} onChange={event => { setName(event.target.value); importKey.current = crypto.randomUUID(); }} maxLength={255} required /></Field>
    <Field label="说明"><textarea value={description} onChange={event => { setDescription(event.target.value); importKey.current = crypto.randomUUID(); }} maxLength={10000} /></Field>
    <Field label="同名处理"><select value={conflict} onChange={event => { setConflict(event.target.value as 'reject' | 'overwrite'); importKey.current = crypto.randomUUID(); }}><option value="reject">同名时拒绝</option><option value="overwrite">沿用既有覆盖确认</option></select></Field>
    <p className="muted">可先下载文件查看正文和来源；提交后还需在下一步确认，确认成功只表示已进入原文档解析流程。</p><ErrorMessage value={error} /></div><footer><button type="button" className="secondary" onClick={close} disabled={busy}>取消</button><button type="submit" disabled={busy || !baseId || !name.trim()}>{busy && <LoaderCircle size={16} className="spin" />}生成预览确认</button></footer></form></Modal>;
}

function scopeLabel(scope: string | null | undefined) {
  return scope === 'current_page'
    ? '当前页'
    : scope === 'first_n' || scope === 'top_n'
      ? '前 N 条'
      : scope === 'all_aggregated'
        ? '全部服务端聚合'
        : scope === 'all_matching'
          ? '全部符合条件'
          : '数据库结果';
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
