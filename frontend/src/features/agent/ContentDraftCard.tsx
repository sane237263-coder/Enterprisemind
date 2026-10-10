import { useEffect, useRef, useState } from 'react';
import { FileText, LoaderCircle } from 'lucide-react';
import { api, mutation } from '../../shared/api';
import { ErrorMessage, Field } from '../../shared/ui';
import type { ContentDraftPart, GeneratedFileTaskPart } from '../../shared/types';

type Draft = {
  id: string;
  title: string;
  body_markdown: string;
  source_refs: unknown[];
  revision: number;
  status: string;
};

export function ContentDraftCard({ part, onRendered }: { part: ContentDraftPart; onRendered: () => Promise<void> }) {
  const [draft, setDraft] = useState<Draft>();
  const [format, setFormat] = useState<GeneratedFileTaskPart['format']>('docx');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const renderKey = useRef(crypto.randomUUID());

  useEffect(() => {
    renderKey.current = crypto.randomUUID();
    const controller = new AbortController();
    void api<Draft>(`/api/v1/drafts/${part.draft_id}`, { signal: controller.signal })
      .then(result => setDraft(result.data))
      .catch(next => { if (!controller.signal.aborted) setError((next as Error).message); });
    return () => controller.abort();
  }, [part.draft_id, part.revision]);

  async function render() {
    if (!draft || busy) return;
    setBusy(true); setError('');
    try {
      await api(
        `/api/v1/drafts/${draft.id}/render`,
        mutation('POST', { expected_revision: draft.revision, format }, renderKey.current),
      );
      await onRendered();
    } catch (next) {
      setError((next as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <>
    <section className="content-draft-card">
      <div className="content-draft-heading"><FileText size={17} /><div><strong>{draft?.title ?? '当前草稿'}</strong><small>修订 {draft?.revision ?? part.revision} · {draft?.status === 'ready' ? '可导出' : '未完成'}</small></div></div>
      {draft && <div className="content-draft-body">{draft.body_markdown.slice(0, 20000)}{draft.body_markdown.length > 20000 ? '\n…正文预览已截断，请使用文件查看完整内容。' : ''}</div>}
      {draft?.source_refs.length ? <p className="muted">已绑定 {draft.source_refs.length} 条来源；文件会保留来源摘要。</p> : null}
      <div className="content-draft-actions">
        <Field label="导出格式"><select value={format} onChange={event => { setFormat(event.target.value as GeneratedFileTaskPart['format']); renderKey.current = crypto.randomUUID(); }} disabled={busy || !draft}><option value="docx">DOCX</option><option value="pdf">PDF</option><option value="md">Markdown</option><option value="xlsx">XLSX</option></select></Field>
        <button onClick={() => void render()} disabled={busy || !draft || draft.status !== 'ready'}>{busy && <LoaderCircle size={15} className="spin" />}生成文件</button>
      </div>
      <ErrorMessage value={error} />
    </section>
  </>;
}
