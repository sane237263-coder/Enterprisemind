import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Folder as FolderIcon, FileText, Plus, Upload, Pencil, Trash2, RefreshCw, Download, ChevronRight, BookOpen, MoveRight } from 'lucide-react';
import { all, api, download, mutation, upload, websocketCredential } from '../../shared/api';
import { Badge, Confirmation, Empty, ErrorMessage, Field, Form, Modal, stageLabel } from '../../shared/ui';
import type { Base, Department, Document, Folder, Processing, User } from '../../shared/types';
import type { PreviewTarget } from '../preview/Preview';

type Editor = { kind: 'base' | 'folder' | 'document' | 'upload' | 'overwrite'; base?: Base; folder?: Folder; document?: Document };
export function Knowledge({ user, preview }: { user: User; preview: (target: PreviewTarget) => void }) {
  const [bases, setBases] = useState<Base[]>([]); const [baseId, setBaseId] = useState(''); const [folderId, setFolderId] = useState('');
  const [folders, setFolders] = useState<Folder[]>([]); const [documents, setDocuments] = useState<Document[]>([]);
  const [departments, setDepartments] = useState<Department[]>([]); const [progress, setProgress] = useState<Record<string, Processing>>({});
  const [editor, setEditor] = useState<Editor>(); const [pending, setPending] = useState(''); const [error, setError] = useState('');
  const [details, setDetails] = useState<{ name: string; processing: Processing }>();
  const [filter, setFilter] = useState(''); const [loading, setLoading] = useState(true); const [notice, setNotice] = useState('');
  const current = bases.find(base => base.id === baseId);
  const baseRef = useRef(baseId); baseRef.current = baseId;
  async function loadBases() { setBases(await all<Base>('/api/v1/knowledge-bases')); }
  async function loadContents(id = baseId) {
    if (!id) return;
    const [f, d] = await Promise.all([all<Folder>(`/api/v1/knowledge-bases/${id}/folders`), all<Document>(`/api/v1/knowledge-bases/${id}/documents`)]);
    if (baseRef.current === id) { setFolders(f); setDocuments(d); }
  }
  useEffect(() => { void loadBases().catch(e => setError(e.message)).finally(() => setLoading(false));
    if (user.role === 'admin') void all<Department>('/api/v1/admin/departments').then(setDepartments).catch(e => setError(e.message)); }, [user.role]);
  useEffect(() => { setFolders([]); setDocuments([]); setFolderId(''); setProgress({}); setError('');
    if (baseId) { setLoading(true); void loadContents(baseId).catch(e => setError(e.message)).finally(() => setLoading(false)); }
  }, [baseId]);
  const pendingIds = documents.filter(doc => ['pending', 'processing'].includes(doc.processing_status)
    || ['pending', 'processing'].includes(progress[doc.id]?.task_status)).map(doc => doc.id).join(',');
  useEffect(() => {
    if (!pendingIds) return;
    const ids = pendingIds.split(',').slice(0, 100); let live = true; let socket: WebSocket | undefined; const controller = new AbortController();
    async function refresh() {
      try { const statuses = await Promise.all(ids.map(id => api<Processing>(`/api/v1/documents/${id}/processing`, { signal: controller.signal })));
        if (!live) return; const values = Object.fromEntries(statuses.map(r => [r.data.document_id, r.data])); setProgress(p => ({ ...p, ...values }));
        if (statuses.some(r => !['pending', 'processing'].includes(r.data.processing_status))) await loadContents();
      } catch (e) { if (live) setError((e as Error).message); }
    }
    socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/ws/document-processing`);
    socket.onopen = () => socket?.send(JSON.stringify({ type: 'subscribe', access_token: websocketCredential(), document_ids: ids }));
    socket.onmessage = event => { try { const message = JSON.parse(event.data); if (message.type === 'document.processing') void refresh(); } catch { /* Polling reconciles state. */ } };
    const timer = setInterval(() => void refresh(), 3000); void refresh();
    return () => { live = false; controller.abort(); clearInterval(timer); socket?.close(); };
  }, [pendingIds, baseId]);
  async function done() { setEditor(undefined); await loadBases(); await loadContents(); }
  async function save(data: FormData, key: string) {
    if (!editor) return; const value = (name: string) => String(data.get(name) ?? '');
    if (editor.kind === 'upload' || editor.kind === 'overwrite') {
      const body = new FormData(); const files = data.getAll('files').filter((item): item is File => item instanceof File && !!item.name);
      if (!files.length) throw new Error('请选择要上传的文件。'); for (const file of files) body.append('files', file);
      if (editor.kind === 'overwrite') {
        body.append('row_version', String(editor.document!.row_version));
        setPending((await api<{ id: string }>(`/api/v1/documents/${editor.document!.id}/overwrite-requests`, upload(body, key))).data.id);
      } else {
        if (folderId) body.append('folder_id', folderId); if (value('name')) body.append('name', value('name'));
        const result = (await api<{ items: { status: string; filename?: string; name?: string; message?: string; document_id?: string }[] }>(`/api/v1/knowledge-bases/${baseId}/document-uploads`, upload(body, key))).data;
        const rejected = result.items.filter(item => item.status !== 'accepted');
        setNotice(`${result.items.filter(item => item.status === 'accepted').length} 个文件已提交处理。` + rejected.map(item => ` ${item.filename ?? item.name ?? '文件'}：${item.status === 'name_conflict' ? '同名文件未保存，请改名后重试，或在原文件菜单选择覆盖。' : item.message ?? '未上传'}`).join(''));
      }
    } else if (editor.kind === 'base') {
      await api(editor.base ? `/api/v1/knowledge-bases/${editor.base.id}` : '/api/v1/knowledge-bases', mutation(editor.base ? 'PATCH' : 'POST', {
        name: value('name'), description: value('description') || null,
        ...(editor.base ? { row_version: editor.base.row_version } : { type: value('type') }),
        ...((editor.base?.type ?? value('type')) === 'department' ? { department_id: value('department_id') || null } : {}),
      }, key));
    } else if (editor.kind === 'folder') {
      await api(editor.folder ? `/api/v1/folders/${editor.folder.id}` : `/api/v1/knowledge-bases/${baseId}/folders`, mutation(editor.folder ? 'PATCH' : 'POST', {
        name: value('name'), parent_id: value('parent_id') || null, ...(editor.folder ? { row_version: editor.folder.row_version } : {}),
      }, key));
    } else {
      await api(`/api/v1/documents/${editor.document!.id}`, mutation('PATCH', { row_version: editor.document!.row_version,
        name: value('name'), description: value('description') || null, folder_id: value('parent_id') || null }, key));
    }
    await done();
  }
  async function remove(path: string, row: { id: string; row_version: number }) {
    try { const response = (await api<{ id: string; action_type?: string }>(`/api/v1/${path}/${row.id}/deletion-requests`, mutation('POST', { row_version: row.row_version }))).data;
      if (response.action_type) setPending(response.id); else await done();
    } catch (e) { setError((e as Error).message); }
  }
  async function retry(doc: Document) { try { await api(`/api/v1/documents/${doc.id}/processing/retry`, mutation('POST', { row_version: doc.row_version }));
    const state = (await api<Processing>(`/api/v1/documents/${doc.id}/processing`)).data;
    setProgress(previous => ({ ...previous, [doc.id]: state })); await loadContents(); } catch (e) { setError((e as Error).message); } }
  async function inspect(doc: Document) { try { setDetails({ name: doc.name, processing: (await api<Processing>(`/api/v1/documents/${doc.id}/processing`)).data }); }
    catch (e) { setError((e as Error).message); } }
  async function move(kind: string, id: string, parent: string) {
    try {
      const item = kind === 'folder' ? folders.find(f => f.id === id) : documents.find(d => d.id === id); if (!item) return;
      await api(`/api/v1/${kind === 'folder' ? 'folders' : 'documents'}/${id}`, mutation('PATCH', { row_version: item.row_version, [kind === 'folder' ? 'parent_id' : 'folder_id']: parent || null })); await loadContents();
    } catch (e) { setError((e as Error).message); }
  }
  function drop(event: React.DragEvent, parent: string) { event.preventDefault(); const [kind, id] = event.dataTransfer.getData('application/x-enterprisemind').split(':'); if ((kind === 'folder' || kind === 'document') && id) void move(kind, id, parent); }
  const chain: Folder[] = []; let cursor = folderId; const seen = new Set<string>();
  while (cursor && !seen.has(cursor)) { seen.add(cursor); const f = folders.find(item => item.id === cursor); if (!f) break; chain.unshift(f); cursor = f.parent_id ?? ''; }
  const shownFolders = folders.filter(folder => (folder.parent_id ?? '') === folderId);
  const shownDocuments = documents.filter(doc => (doc.folder_id ?? '') === folderId && (!filter || doc.processing_status === filter));
  return <section className="page"><header className="page-heading"><div><p className="eyebrow">共享资料 · 有据可查</p><h1>{current?.name ?? '知识库'}</h1><p>{current?.description || '在公共知识和部门资料中，建立可追溯的工作依据。'}</p></div>
    <div className="button-group">{current ? <><button className="secondary" onClick={() => setEditor({ kind: 'folder' })}><Plus size={17} />新建文件夹</button><button onClick={() => setEditor({ kind: 'upload' })}><Upload size={17} />上传文件</button></> : user.role === 'admin' && <button onClick={() => setEditor({ kind: 'base' })}><Plus size={17} />创建知识库</button>}</div></header>
    <ErrorMessage value={error} />{notice && <div className="notice" role="status">{notice}<button className="text-button" onClick={() => setNotice('')}>收起</button></div>}
    {!current ? <div className="base-grid">{bases.map(base => <article className="base-card" key={base.id}><button className="base-open" onClick={() => setBaseId(base.id)}><div className={`base-icon ${base.type}`}><BookOpen size={24} /></div><span className="base-type">{base.type === 'public' ? '企业公共' : '部门知识'}</span><h2>{base.name}</h2><p>{base.description || '进入知识库，查看与管理资料。'}</p><span className="base-enter">打开知识库<ChevronRight size={17} /></span></button>
      {user.role === 'admin' && <div className="base-actions"><button className="text-button" onClick={() => setEditor({ kind: 'base', base })}><Pencil size={14} />编辑</button><button className="text-button" onClick={() => void remove('knowledge-bases', base)}><Trash2 size={14} />删除</button></div>}</article>)}
      {!bases.length && <Empty title={loading ? '正在读取知识库…' : '暂无可访问的知识库'}>{user.role === 'admin' ? '创建公共或部门知识库，开始整理企业资料。' : '联系管理员分配部门知识库。'}</Empty>}</div>
    : <><div className="content-toolbar"><nav className="breadcrumbs" aria-label="文件路径"><button onClick={() => setBaseId('')}><ArrowLeft size={16} />知识库</button><ChevronRight size={14} /><button onClick={() => setFolderId('')} onDragOver={e => e.preventDefault()} onDrop={e => drop(e, '')}>{current.name}</button>{chain.map(folder => <span key={folder.id}><ChevronRight size={14} /><button onClick={() => setFolderId(folder.id)}>{folder.name}</button></span>)}</nav>
    <label className="filter-label">处理状态<select value={filter} onChange={e => setFilter(e.target.value)}><option value="">全部</option><option value="success">已完成</option><option value="processing">处理中</option><option value="pending">等待处理</option><option value="partial_success">部分完成</option><option value="failed">失败</option></select></label></div>
    <div className="table-wrap"><table><thead><tr><th>名称</th><th>状态</th><th>文件大小</th><th className="align-right">操作</th></tr></thead><tbody>
      {shownFolders.map(folder => <tr key={`f${folder.id}`} draggable onDragStart={e => e.dataTransfer.setData('application/x-enterprisemind', `folder:${folder.id}`)} onDragOver={e => e.preventDefault()} onDrop={e => drop(e, folder.id)}>
        <td><button className="file-name" onClick={() => setFolderId(folder.id)}><FolderIcon className="folder-icon" size={23} />{folder.name}</button></td><td className="muted">文件夹</td><td>—</td><td className="row-actions"><button className="text-button" onClick={() => setEditor({ kind: 'folder', folder })}><Pencil size={14} />编辑 / 移动</button><button className="text-button" onClick={() => void remove('folders', folder)}><Trash2 size={14} />删除</button></td></tr>)}
      {shownDocuments.map(doc => <tr key={`d${doc.id}`} draggable onDragStart={e => e.dataTransfer.setData('application/x-enterprisemind', `document:${doc.id}`)}><td><button className="file-name" onClick={() => preview({ documentId: doc.id, name: doc.name })}><FileText size={22} /><span>{doc.name}<small>{doc.description || doc.file_extension.toUpperCase()}</small></span></button></td>
      <td><Badge value={progress[doc.id]?.processing_status ?? doc.processing_status} />{['pending', 'processing'].includes(progress[doc.id]?.task_status) && progress[doc.id]?.current_stage && <small className="progress-label">{stageLabel[progress[doc.id].current_stage!] ?? '处理中'} {progress[doc.id].stage_total ? `${progress[doc.id].stage_current ?? 0} / ${progress[doc.id].stage_total}` : ''}</small>}
      {['failed', 'partial_success'].includes(doc.processing_status) && <button className="text-button progress-detail" onClick={() => void inspect(doc)}>查看原因</button>}</td>
      <td className="muted">{doc.size_bytes < 1048576 ? `${Math.max(1, Math.round(doc.size_bytes / 1024))} KB` : `${(doc.size_bytes / 1048576).toFixed(1)} MB`}</td>
      <td><div className="row-actions"><button className="icon-button" title="下载原文件" aria-label={`下载 ${doc.name}`} onClick={() => void download(`/api/v1/documents/${doc.id}/download`, doc.name).catch(e => setError(e.message))}><Download size={16} /></button>
      <button className="text-button" onClick={() => setEditor({ kind: 'document', document: doc })}>编辑 / 移动</button><button className="text-button" onClick={() => setEditor({ kind: 'overwrite', document: doc })}>覆盖</button>
      {['failed', 'partial_success'].includes(doc.processing_status) && <button className="text-button" onClick={() => void retry(doc)}><RefreshCw size={14} />重试</button>}
      <button className="icon-button" aria-label={`删除 ${doc.name}`} onClick={() => void remove('documents', doc)}><Trash2 size={16} /></button></div></td></tr>)}</tbody></table>
      {!shownFolders.length && !shownDocuments.length && <Empty title={loading ? '正在加载资料…' : '这个文件夹还没有资料'}>上传文件后，助手会在处理完成后使用其中的内容。</Empty>}</div></>}
    {editor && <Modal title={editor.kind === 'base' ? editor.base ? '编辑知识库' : '创建知识库' : editor.kind === 'folder' ? editor.folder ? '编辑文件夹' : '新建文件夹' : editor.kind === 'document' ? '编辑文档' : editor.kind === 'overwrite' ? `覆盖 ${editor.document!.name}` : '上传文件'} close={() => setEditor(undefined)}>
      <Form onSubmit={save} cancel={() => setEditor(undefined)} label={editor.kind === 'upload' ? '上传并处理' : editor.kind === 'overwrite' ? '继续确认覆盖' : '保存'}>
      {['upload', 'overwrite'].includes(editor.kind) ? <><Field label="选择文件"><input type="file" name="files" required multiple={editor.kind === 'upload'} /></Field>
        {editor.kind === 'upload' && <Field label="保存名称（单文件可选）"><input name="name" maxLength={255} placeholder="留空时使用原文件名" /></Field>}
        <p className="muted">支持 PDF、Office、文本、CSV 和图片。{editor.kind === 'overwrite' ? '新内容处理成功前，原有内容继续可用。' : '每个文件会独立处理，可在列表查看当前阶段。'}</p></>
      : <><Field label="名称"><input name="name" required maxLength={200} defaultValue={editor.base?.name ?? editor.folder?.name ?? editor.document?.name ?? ''} /></Field>
      {(editor.kind === 'base' || editor.kind === 'document') && <Field label="说明"><textarea name="description" defaultValue={editor.base?.description ?? editor.document?.description ?? ''} /></Field>}
      {editor.kind === 'base' ? <>
        {!editor.base && <Field label="知识库类型"><select name="type"><option value="public">企业公共知识库</option><option value="department">部门知识库</option></select></Field>}
        {(!editor.base || editor.base.type === 'department') && <Field label="所属部门（部门知识库必选）"><select name="department_id" defaultValue={editor.base?.department_id ?? ''}><option value="">未选择</option>{departments.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></Field>}
      </> : <Field label={<span>所在文件夹 <MoveRight size={13} /></span>}><select name="parent_id" defaultValue={editor.folder?.parent_id ?? editor.document?.folder_id ?? folderId}><option value="">知识库根目录</option>{folders.filter(f => f.id !== editor.folder?.id).map(f => <option value={f.id} key={f.id}>{f.path ?? f.name}</option>)}</select></Field>}
      </>}
      </Form></Modal>}
    {pending && <Confirmation id={pending} done={() => { setPending(''); void done().catch(e => setError(e.message)); }} />}
    {details && <Modal title={`${details.name} · 处理详情`} close={() => setDetails(undefined)}><div className="modal-body">
      <Badge value={details.processing.processing_status} />
      {details.processing.failed_units.length ? <ul>{details.processing.failed_units.map((unit, index) => <li key={index}>
        <strong>{stageLabel[unit.stage] ?? '处理'}：</strong>{unit.error_message || processingError[unit.error_code ?? ''] || '此步骤未完成，请稍后重试。'}
        <small className="muted">{unit.error_code}</small>
      </li>)}</ul> : <p>没有失败步骤。任务可能正在等待处理。</p>}
      <p className="muted">重试会保留已成功处理的内容，只重新执行失败步骤及必要的后续步骤。</p>
    </div></Modal>}
  </section>;
}
const processingError: Record<string, string> = {
  OCR_PROVIDER_REJECTED: '文字识别服务拒绝了请求，请检查图片是否清晰、格式是否受支持。',
  MEDIA_PROVIDER_RATE_LIMITED: '图片理解或识别服务调用过于频繁，请稍后重试。',
  MEDIA_PROVIDER_UNAVAILABLE: '图片理解或识别服务暂时不可用，请稍后重试。',
  CHUNK_NO_CONTENT: '没有可用于检索的文本内容，请先处理识别失败的步骤。',
  NO_RETRIEVABLE_CONTENT: '尚未生成可检索内容，请检查前面的处理步骤。',
};
