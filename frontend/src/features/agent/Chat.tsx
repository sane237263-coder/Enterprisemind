import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Plus, Send, Paperclip, Square, Play, FileText, ArrowUpRight, Pencil, Trash2, X, MessageSquare } from 'lucide-react';
import { all, api, mutation, stream, upload } from '../../shared/api';
import { LatestRequest } from '../../shared/latest-request';
import { Badge, Confirmation, ErrorMessage, Field, Form, Modal } from '../../shared/ui';
import { BrandMark } from '../../shared/BrandMark';
import { formatSystemTimes } from '../../shared/datetime';
import type { Attachment, ClientAction, Conversation, Message, Run, Source, User } from '../../shared/types';
import type { PreviewTarget } from '../preview/Preview';
import { ContentDraftCard } from './ContentDraftCard';
import { GeneratedFileCard } from './GeneratedFileCard';

const activeStates = ['pending', 'running', 'waiting_confirmation'];
type OwnedRun = { conversationId: string; run: Run };
export function Chat({ user, preview, navigate }: { user: User; preview: (target: PreviewTarget) => void; navigate: (action: ClientAction) => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([]); const [selected, setSelected] = useState('');
  const [messages, setMessages] = useState<Message[]>([]); const [ownedRun, setOwnedRun] = useState<OwnedRun>();
  const [draft, setDraft] = useState(''); const [files, setFiles] = useState<Attachment[]>([]); const [delta, setDelta] = useState('');
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(''); const [rename, setRename] = useState<Conversation>();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [streamVersion, setStreamVersion] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null); const bottom = useRef<HTMLDivElement>(null);
  const messageKey = useRef(crypto.randomUUID()); const createKey = useRef(crypto.randomUUID());
  const messageLoads = useRef(new LatestRequest());
  const preserveDraftFor = useRef('');
  const selectedRef = useRef(selected); selectedRef.current = selected;
  const ownedRunRef = useRef(ownedRun); ownedRunRef.current = ownedRun;
  const loadingRef = useRef(loading); loadingRef.current = loading;
  const run = ownedRun?.conversationId === selected ? ownedRun.run : undefined;
  function selectConversation(id: string) {
    if (selectedRef.current === id) return;
    selectedRef.current = id;
    loadingRef.current = !!id;
    setLoading(!!id);
    setSelected(id);
  }
  function isCurrentRun(owner: string, runId: string) {
    const current = ownedRunRef.current;
    return selectedRef.current === owner && current?.conversationId === owner && current.run.id === runId;
  }
  function updateCurrentRun(owner: string, runId: string, update: (current: Run) => Run) {
    setOwnedRun(current => current?.conversationId === owner && current.run.id === runId
      ? { conversationId: owner, run: update(current.run) }
      : current);
  }
  async function loadList() { setConversations(await all<Conversation>('/api/v1/conversations')); }
  useEffect(() => { void loadList().catch(e => setError(e.message)); }, []);
  async function loadMessages(id: string, signal?: AbortSignal) {
    const version = messageLoads.current.begin();
    const next = await all<Message>(`/api/v1/conversations/${id}/messages`, signal);
    if (messageLoads.current.isCurrent(version) && selectedRef.current === id && !signal?.aborted) setMessages(next);
  }
  useEffect(() => {
    setMessages([]); setDelta(''); setError('');
    setOwnedRun(current => current?.conversationId === selected ? current : undefined);
    if (preserveDraftFor.current === selected && selected) preserveDraftFor.current = '';
    else { setFiles([]); setDraft(''); }
    if (!selected) { loadingRef.current = false; setLoading(false); return; }
    const c = new AbortController(); loadingRef.current = true; setLoading(true);
    Promise.all([loadMessages(selected, c.signal), api<Conversation>(`/api/v1/conversations/${selected}`, { signal: c.signal })])
      .then(async ([, conversation]) => { if (c.signal.aborted) return;
        if (conversation.data.latest_run_id) {
          const latest = await api<Run>(`/api/v1/agent-runs/${conversation.data.latest_run_id}`, { signal: c.signal });
          if (!c.signal.aborted && selectedRef.current === selected) setOwnedRun({ conversationId: selected, run: latest.data });
        }
      }).catch(e => { if (!c.signal.aborted && selectedRef.current === selected) setError(e.message); })
      .finally(() => { if (!c.signal.aborted && selectedRef.current === selected) { loadingRef.current = false; setLoading(false); } });
    return () => c.abort();
  }, [selected]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: 'end' }); }, [messages.length, delta]);
  useEffect(() => {
    if (!run || !activeStates.includes(run.status)) return;
    const controller = new AbortController(); const id = run.id; const conversation = selected; let polling = false;
    async function refresh() {
      if (polling || controller.signal.aborted) return; polling = true;
      try {
        const next = (await api<Run>(`/api/v1/agent-runs/${id}`, { signal: controller.signal })).data;
        if (controller.signal.aborted || !isCurrentRun(conversation, id)) return;
        setOwnedRun({ conversationId: conversation, run: next });
        if (!activeStates.includes(next.status)) {
          await loadMessages(conversation);
          if (controller.signal.aborted || !isCurrentRun(conversation, id)) return;
          setDelta(''); await loadList(); controller.abort();
        }
      } catch (e) { if (!controller.signal.aborted && isCurrentRun(conversation, id)) setError((e as Error).message); } finally { polling = false; }
    }
    void stream(`/api/v1/agent-runs/${id}/events`, controller.signal, (event, data) => {
      if (controller.signal.aborted || !isCurrentRun(conversation, id)) return;
      if (event === 'answer.delta' && typeof data.text === 'string') setDelta(previous => previous + data.text);
      else if (event === 'step.updated' && typeof data.step === 'string') {
        updateCurrentRun(conversation, id, previous => ({ ...previous, current_step: data.step as string }));
      }
      else if (['run.completed', 'run.failed', 'run.cancelled', 'pending_action.required'].includes(event)) void refresh();
    }).catch(() => { /* Current state and completed messages remain available via polling. */ });
    const timer = setInterval(() => void refresh(), 1500); void refresh();
    return () => { clearInterval(timer); controller.abort(); };
  }, [run?.id, selected, streamVersion]);
  async function createConversation() {
    const priorSelection = selectedRef.current;
    const result = await api<Conversation>('/api/v1/conversations', mutation('POST', { title: '新会话' }, createKey.current));
    createKey.current = crypto.randomUUID();
    if (selectedRef.current === priorSelection) {
      preserveDraftFor.current = result.data.id;
      selectConversation(result.data.id);
    }
    await loadList(); return result.data.id;
  }
  async function send(event: FormEvent) {
    event.preventDefault(); if (busy || loadingRef.current || active || (!draft.trim() && !files.length)) return;
    const priorSelection = selectedRef.current;
    let owner = priorSelection;
    const content = draft.trim(); const stagedUploadIds = files.map(file => file.staged_upload_id);
    setBusy(true); if (selectedRef.current === priorSelection) setError('');
    try {
      if (!owner) owner = await createConversation();
      const idempotencyKey = messageKey.current;
      const next = await api<Run>(`/api/v1/conversations/${owner}/messages`, mutation('POST', { content_text: content, staged_upload_ids: stagedUploadIds }, idempotencyKey));
      if (messageKey.current === idempotencyKey) messageKey.current = crypto.randomUUID();
      if (selectedRef.current !== owner) return;
      setOwnedRun({ conversationId: owner, run: next.data });
      setDraft(''); setFiles([]); setDelta(''); await loadMessages(owner);
    } catch (e) {
      if (selectedRef.current === owner || (!owner && selectedRef.current === priorSelection)) setError((e as Error).message);
    } finally { setBusy(false); }
  }
  async function continueDatabase(source: Source) {
    const owner = selectedRef.current;
    if (busy || loadingRef.current || active || !owner || !source.query_id || !source.result_id) return;
    setBusy(true); setError('');
    try {
      const idempotencyKey = messageKey.current;
      const next = await api<Run>(
        `/api/v1/conversations/${owner}/database-queries/${encodeURIComponent(source.query_id)}/pages`,
        mutation('POST', { query_id: source.query_id, result_id: source.result_id }, idempotencyKey),
      );
      if (messageKey.current === idempotencyKey) messageKey.current = crypto.randomUUID();
      if (selectedRef.current !== owner) return;
      setOwnedRun({ conversationId: owner, run: next.data }); setDelta(''); await loadMessages(owner);
    } catch (e) { if (selectedRef.current === owner) setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function attach(chosen: FileList | null) {
    if (!chosen?.length) return;
    if (busy || loadingRef.current || active) { if (fileInput.current) fileInput.current.value = ''; return; }
    const priorSelection = selectedRef.current;
    let owner = priorSelection;
    setBusy(true); if (selectedRef.current === priorSelection) setError('');
    try {
      if (files.length + chosen.length > 10) throw new Error('一条消息最多添加 10 个附件。');
      if (!owner) owner = await createConversation();
      const data = new FormData(); for (const file of chosen) data.append('files', file);
      const result = await api<{ items: Attachment[] }>(`/api/v1/conversations/${owner}/attachments`, upload(data));
      if (selectedRef.current !== owner) return;
      const accepted = result.data.items.filter(item => item.staged_upload_id);
      setFiles(previous => [...previous, ...accepted]);
      const rejected = result.data.items.filter(item => !item.staged_upload_id); if (rejected.length) setError(rejected.map(item => item.message ?? '附件未上传').join('；'));
      messageKey.current = crypto.randomUUID();
    } catch (e) {
      if (selectedRef.current === owner || (!owner && selectedRef.current === priorSelection)) setError((e as Error).message);
    } finally { setBusy(false); if (fileInput.current) fileInput.current.value = ''; }
  }
  async function control(kind: 'stop' | 'resume') {
    const owner = selectedRef.current;
    const target = run;
    if (!target || !isCurrentRun(owner, target.id)) return;
    setError('');
    try {
      await api(`/api/v1/agent-runs/${target.id}/${kind}`, mutation('POST'));
      const next = (await api<Run>(`/api/v1/agent-runs/${target.id}`)).data;
      if (!isCurrentRun(owner, target.id)) return;
      setOwnedRun({ conversationId: owner, run: next });
      if (kind === 'resume') { setDelta(''); setStreamVersion(version => version + 1); }
    } catch (e) { if (isCurrentRun(owner, target.id)) setError((e as Error).message); }
  }
  async function remove(conversation: Conversation) {
    try { const latest = (await api<Conversation>(`/api/v1/conversations/${conversation.id}`)).data;
      setPending((await api<{ id: string }>(`/api/v1/conversations/${conversation.id}/deletion-requests`, mutation('POST', { row_version: latest.row_version }))).data.id);
    } catch (e) { setError((e as Error).message); }
  }
  const current = conversations.find(c => c.id === selected); const active = run && activeStates.includes(run.status);
  const history = <><header><h2>我的会话</h2><button className="icon-button" aria-label="新建会话" onClick={() => { selectConversation(''); setMessages([]); setDraft(''); setFiles([]); setHistoryOpen(false); }}><Plus size={19} /></button></header>
    {conversations.map(item => <div className={`conversation-item ${selected === item.id ? 'selected' : ''}`} key={item.id}>
      <button className="conversation-name" onClick={() => { selectConversation(item.id); setHistoryOpen(false); }}><MessageSquare size={16} /><span>{item.title}</span></button>
      <button className="icon-button small" aria-label={`重命名 ${item.title}`} onClick={() => setRename(item)}><Pencil size={14} /></button><button className="icon-button small" aria-label={`删除 ${item.title}`} onClick={() => void remove(item)}><Trash2 size={14} /></button>
    </div>)}{!conversations.length && <p className="muted list-hint">你的对话会保存在这里。</p>}</>;
  return <section className="chat-layout"><aside className="conversation-list">{history}</aside>
    <div className="chat-main"><header className="chat-heading"><div><p className="eyebrow">企业知识助手</p><h1>{current?.title ?? '新的对话'}</h1></div><span className="access-note">仅使用你有权访问的资料</span><button className="secondary mobile-history" onClick={() => setHistoryOpen(true)}><MessageSquare size={16} />会话</button></header>
    <div className="messages" aria-label="会话消息">
      {!messages.length && !loading && <div className="chat-welcome"><BrandMark /><h2>把问题，交给你的企业知识。</h2><p>查找资料、核对依据，或让助手完成文档操作。<br />每一条企业事实，都有可回溯的出处。</p>
        <div className="suggestions">{['查找与差旅报销相关的制度', '总结我有权限查看的文档', '帮我整理知识库中的文件'].map(text => <button className="secondary" key={text} onClick={() => setDraft(text)}>{text}<ArrowUpRight size={16} /></button>)}</div></div>}
      {loading && <p className="muted">正在读取会话…</p>}
      {messages.filter(message => message.status !== 'streaming').map(message => <article className={`message ${message.role}`} key={message.id}><div className="message-author">{message.role === 'user' ? user.username : 'EnterpriseMind'}{message.status === 'failed' && <Badge value="failed" />}</div>
        {!!message.content_text?.trim() && <div className="message-body">{(message.role === 'assistant' ? formatSystemTimes(message.content_text ?? '') : message.content_text ?? '').split(/(\[\d+\])/).map((part, index) => {
          const source = message.sources.find(source => `[${source.ordinal}]` === part);
          return source ? <button key={index} className="inline-source" disabled={!source.available} onClick={() => preview({ sourceId: source.id, name: source.document_name ?? '引用来源' })}>{part}</button> : <span key={index}>{part}</span>;
        })}</div>}
        {message.content_parts?.map((part, index) => part.type === 'client_action' ? <button key={index} className="secondary client-action" disabled={!part.available || user.role !== 'admin'} onClick={() => navigate(part)}>{part.available ? part.action === 'open_admin_user_create' ? '打开用户创建页面' : '打开密码重置页面' : '此操作入口已失效'}<ArrowUpRight size={16} /></button>
          : part.type === 'database_page' ? null : part.type === 'generated_file_task' ? <GeneratedFileCard key={index} part={part} onPending={setPending} /> : part.type === 'content_draft' ? <ContentDraftCard key={index} part={part} onRendered={() => loadMessages(selected)} /> : <div className="attachment-chip" key={index}><FileText size={15} />{'file_name' in part ? part.file_name : '附件'}</div>)}
        {!!message.sources.length && <div className="sources">{message.sources.map(source => { const database = source.source_type === 'database'; return <div className="source-card-row" key={source.id}><button className="source-card" disabled={!source.available} onClick={() => preview({ sourceId: source.id, name: database ? source.source_title ?? '数据库来源' : source.document_name ?? '引用来源' })}><span className="source-number">{source.ordinal}</span><span className="source-card-content"><strong>{database ? source.source_title ?? '数据库来源' : source.document_name ?? '来源不可用'}</strong><small>{source.available ? (database ? databaseSourcePosition(source) : sourcePosition(source)) : '权限或来源已变化'}</small>
          {source.available && database ? <span className="source-excerpt">{source.conditions_summary ?? '数据库查询结果'}{source.has_more ? (source.locator?.continuation_available === false ? ' · 还有结果，但无法安全续页' : source.can_continue ? ' · 还有下一页结果' : ' · 续页上下文已过期') : ''}</span> : source.available && source.quote_text ? <span className="source-excerpt">{source.text_origin === 'vision' ? '图片语义：' : source.text_origin === 'ocr' ? '识别文字：' : ''}{source.quote_text}</span> : null}</span><ArrowUpRight size={16} /></button>{database && source.available && source.can_continue && source.query_id && source.result_id && <button type="button" className="text-button database-next-page" disabled={busy || loading || !!active} onClick={() => void continueDatabase(source)}>下一页</button>}</div>; })}</div>}
      </article>)}
      {delta && <article className="message assistant"><div className="message-author">EnterpriseMind</div><div className="message-body" aria-live="polite">{formatSystemTimes(delta)}<span className="stream-caret" /></div></article>}
      <div ref={bottom} />
    </div>
    <div className="composer-wrap"><ErrorMessage value={error} />
      {run && <div className="run-status"><span>{active ? <><span className="activity-dot" />{run.current_step || '正在处理'}</> : run.status === 'failed' ? run.failure_message : ''}</span>
        {run.status === 'waiting_confirmation' && run.pending_action_id && <button className="secondary" onClick={() => setPending(run.pending_action_id!)}>查看待确认操作</button>}
        {active && <button className="text-button" onClick={() => void control('stop')}><Square size={13} />停止</button>}
        {run.status === 'cancelled' && (run.started_at ? <button className="text-button" onClick={() => void control('resume')}><Play size={15} />继续任务</button> : <span>任务在开始前已停止，可重新发送消息。</span>)}</div>}
      <form className="composer" onSubmit={send}>{!!files.length && <div className="attachments">{files.map(file => <span className="attachment-chip" key={file.staged_upload_id}><FileText size={14} />{file.file_name}<button type="button" className="icon-button small" aria-label={`移除 ${file.file_name}`} onClick={() => { setFiles(items => items.filter(item => item !== file)); messageKey.current = crypto.randomUUID(); }}><X size={13} /></button></span>)}</div>}
      <textarea aria-label="输入消息" placeholder="提出问题，或描述你想完成的操作…" value={draft} disabled={busy || loading || !!active} onChange={e => { setDraft(e.target.value); messageKey.current = crypto.randomUUID(); }} onKeyDown={e => {
        if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
        e.preventDefault();
        if (!e.repeat) e.currentTarget.form?.requestSubmit();
      }} />
      <div className="composer-actions"><input type="file" multiple hidden ref={fileInput} onChange={e => void attach(e.target.files)} /><button type="button" className="text-button" disabled={busy || loading || !!active} onClick={() => fileInput.current?.click()}><Paperclip size={17} />添加附件</button><span>Enter 发送 · Shift+Enter 换行</span><button type="submit" disabled={busy || loading || !!active || (!draft.trim() && !files.length)} aria-label="发送消息"><Send size={17} />发送</button></div></form>
    </div></div>
    {pending && <Confirmation id={pending} done={() => { setPending(''); void loadList().then(() => { if (selectedRef.current) return loadMessages(selectedRef.current); }).catch(() => { selectConversation(''); }); }} />}
    {historyOpen && <Modal title="切换会话" close={() => setHistoryOpen(false)}><div className="conversation-list history-dialog">{history}</div></Modal>}
    {rename && <Modal title="重命名会话" close={() => setRename(undefined)}><Form cancel={() => setRename(undefined)} onSubmit={async (data, key) => { const latest = (await api<Conversation>(`/api/v1/conversations/${rename.id}`)).data; await api(`/api/v1/conversations/${rename.id}`, mutation('PATCH', { title: String(data.get('title')), row_version: latest.row_version }, key)); setRename(undefined); await loadList(); }}><Field label="会话名称"><input name="title" defaultValue={rename.title} required maxLength={300} /></Field></Form></Modal>}
  </section>;
}

function sourcePosition(source: Source) {
  const position = source.locator;
  if (!position) return '';
  if (position.page) return `第 ${position.page} 页`;
  if (position.slide) return `Slide ${position.slide}`;
  if (position.sheet) return `${position.sheet} ${position.cell_range ?? ''}`.trim();
  if (position.paragraph_ordinal) return `第 ${position.paragraph_ordinal} 段`;
  if (position.start_line) return `第 ${position.start_line}${position.end_line && position.end_line !== position.start_line ? `–${position.end_line}` : ''} 行`;
  return position.type === 'image' ? '图片' : '';
}

function databaseSourcePosition(source: Source) {
  const locator = source.locator;
  const page = locator?.page_index === undefined ? '' : `第 ${locator.page_index + 1} 页${locator.page_size ? ` · 每页最多 ${locator.page_size} 行` : ''}`;
  const rows = source.returned_rows === undefined ? '' : `本页 ${source.returned_rows} 行`;
  return [locator?.database_name, (source.referenced_relations ?? []).join('、'), page, rows].filter(Boolean).join(' · ') || '数据库查询';
}
