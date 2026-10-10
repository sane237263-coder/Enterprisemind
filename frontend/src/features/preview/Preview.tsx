import { formatBeijingTime } from '../../shared/datetime';
import { useEffect, useRef, useState } from 'react';
import { Database, Download, ExternalLink, RefreshCw } from 'lucide-react';
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { api, blob, download, mutation } from '../../shared/api';
import type { DatabaseSourcePreview, Preview as PreviewData, Source } from '../../shared/types';
import { ErrorMessage, Modal } from '../../shared/ui';
import { relativeBox } from './geometry';

type Target = { documentId?: string; sourceId?: string; name: string };
export type PreviewTarget = Target;
export function Preview({ target, close }: { target: Target; close: () => void }) {
  const [data, setData] = useState<PreviewData | DatabaseSourcePreview>(); const [error, setError] = useState('');
  const [html, setHtml] = useState(''); const [pdf, setPdf] = useState<Blob>(); const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0); const [submitting, setSubmitting] = useState(false);
  const rebuilding = submitting || ['pending', 'processing'].includes(data?.rebuild_task?.status ?? '');
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined; let fingerprint = '';
    const urls: string[] = [];
    setData(undefined); setHtml(''); setPdf(undefined); setError(''); setLoading(true);
    async function load() {
      let keepPolling = false;
      try {
        const payload = target.sourceId ? (await api<DatabaseSourcePreview | (Source & { preview: PreviewData })>(`/api/v1/answer-sources/${target.sourceId}/preview`, { signal: controller.signal })).data
          : (await api<PreviewData>(`/api/v1/documents/${target.documentId}/preview`, { signal: controller.signal })).data;
        const result = 'preview' in payload ? payload.preview : payload;
        if (controller.signal.aborted) return;
        setData(result);
        if ('source_type' in result && result.source_type === 'database') return;
        const documentResult = result as PreviewData;
        keepPolling = ['pending', 'processing'].includes(documentResult.rebuild_task?.status ?? '');
        if (!documentResult.ready) return;
        const next = JSON.stringify([documentResult.artifacts, documentResult.visuals]);
        if (next === fingerprint) return;
        const artifact = documentResult.artifacts[0];
        const content = await blob(artifact.content_url, controller.signal);
        if (controller.signal.aborted) return;
        if (artifact.type === 'preview_pdf') setPdf(content);
        else {
          const document = new DOMParser().parseFromString(await content.text(), 'text/html');
          const policy = document.createElement('meta'); policy.httpEquiv = 'Content-Security-Policy';
          policy.content = "default-src 'none'; style-src 'unsafe-inline'; img-src data: blob:; base-uri 'none'; form-action 'none'";
          document.head.prepend(policy);
          for (const visual of documentResult.visuals ?? []) {
            const section = document.getElementById(visual.html_target);
            if (!section || section.querySelector('img')) continue;
            const image = await blob(visual.content_url, controller.signal);
            if (controller.signal.aborted) return;
            const url = URL.createObjectURL(image); urls.push(url);
            const element = document.createElement('img'); element.src = url; element.alt = visual.label;
            section.querySelector('[data-visual-pending]')?.remove(); section.append(element);
          }
          if (result.highlight) {
            const selected = document.getElementById(result.highlight.html_target);
            if (selected) { selected.setAttribute('data-source-highlight', 'true'); selected.style.background = '#fff0af'; selected.style.outline = '2px solid #b78218'; }
          }
          setHtml('<!doctype html>' + document.documentElement.outerHTML);
        }
        fingerprint = next;
      } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
      finally { if (!controller.signal.aborted) { setLoading(false); if (keepPolling) timer = setTimeout(() => void load(), 1500); } }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer); urls.forEach(url => URL.revokeObjectURL(url)); };
  }, [target.documentId, target.sourceId, refresh]);
  const databaseData = data && 'source_type' in data && data.source_type === 'database' ? data : undefined;
  const database = !!databaseData;
  async function save() { try { if (data && !database && data.download_url) await download(data.download_url, target.name); } catch (e) { setError((e as Error).message); } }
  async function rebuild() {
    if (!data || database || rebuilding) return;
    setSubmitting(true); setError('');
    try {
      await api(`/api/v1/documents/${data.document_id}/preview/rebuild`, mutation('POST', {
        row_version: data.row_version, revision_id: data.revision_id,
      }));
      setRefresh(value => value + 1);
    } catch (e) { setError((e as Error).message); }
    finally { setSubmitting(false); }
  }
  return <Modal title={target.name} close={close} wide>
    <div className="preview-toolbar"><span>{database ? <><Database size={15} />数据库来源</> : data?.highlight ? <><ExternalLink size={15} />已定位引用来源{data.highlight.text_origin === 'vision' ? ' · 图片语义说明' : data.highlight.text_origin === 'ocr' ? ' · 文字识别' : ''}</> : '文档预览'}</span>
    {!database && data?.can_rebuild && <button className="secondary" disabled={rebuilding} onClick={() => void rebuild()}><RefreshCw size={16} />{rebuilding ? '正在重建预览…' : data.ready ? '重建预览' : '生成预览'}</button>}
    {!database && data && <button className="secondary" onClick={() => void save()}><Download size={16} />下载原文件</button>}</div>
    <ErrorMessage value={error} />{loading && <p className="empty">正在加载预览…</p>}
    {error && <button className="secondary" onClick={() => setRefresh(value => value + 1)}>重新加载预览</button>}
    {!database && rebuilding && <p role="status">正在更新预览，完成后将自动刷新。</p>}
    {!database && data?.rebuild_task?.status === 'failed' && <p role="alert">{data.rebuild_task.error_message ?? '预览重建未完成，请重试。'}</p>}
    {!database && data?.rebuild_task?.status === 'success' && <p role="status">预览已更新。</p>}
    {!database && data && !data.ready && <p className="empty">预览尚未生成。可以稍后重试，或下载原文件查看。</p>}
    {!database && data?.highlight && <blockquote className="source-quote">{data.highlight.source_text}</blockquote>}
    {databaseData && <DatabaseSource data={databaseData} />}
    {html && <iframe title="文档内容" className="document-preview" sandbox="allow-same-origin" srcDoc={html}
      onLoad={event => { const doc = event.currentTarget.contentDocument;
        const selected = doc?.querySelector('[data-source-highlight]'); selected?.scrollIntoView({ block: 'center' });
        if (data?.highlight?.locator.boxes?.length && selected) {
          const img = selected.querySelector('img'); const h = data.highlight;
          if (img && h.width && h.height) {
            const wrap = doc!.createElement('div'); wrap.style.position = 'relative'; wrap.style.display = 'inline-block'; img.before(wrap); wrap.append(img);
            img.style.display = 'block';
            for (const b of h.locator.boxes ?? []) {
              const relative = relativeBox(b, h.image_bounds ?? { x: 0, y: 0, width: h.width, height: h.height });
              if (!relative) continue;
              const box = doc!.createElement('div'); Object.assign(box.style, {
              position: 'absolute', left: `${relative.x}%`, top: `${relative.y}%`, width: `${relative.width}%`, height: `${relative.height}%`,
              background: '#f4d64b55', outline: '2px solid #b78218', pointerEvents: 'none',
            }); wrap.append(box); }
          }
        }
      }} />}
    {pdf && <Pdf content={pdf} highlight={data?.highlight} error={setError} />}
  </Modal>;
}

function DatabaseSource({ data }: { data: DatabaseSourcePreview }) {
  const locator = data.locator;
  const columns = data.columns ?? [];
  return <div className="database-source-view"><div className="database-source-mark"><Database size={22} /><div><p className="eyebrow">真实查询来源</p><h3>{data.source_title ?? '数据库来源'}</h3></div></div>
    <dl className="database-source-meta"><div><dt>数据库</dt><dd>{locator?.database_name ?? '—'}</dd></div><div><dt>查询时间</dt><dd>{formatBeijingTime(data.queried_at)}</dd></div><div><dt>查询类型</dt><dd>{data.query_kind === 'aggregate' ? '聚合统计' : '明细查询'}</dd></div><div><dt>返回范围</dt><dd>{locator?.page_index === undefined ? '—' : `第 ${locator.page_index + 1} 页 · 最多 ${locator.page_size ?? '—'} 行`}</dd></div><div><dt>当前页行数</dt><dd>{data.returned_rows ?? '—'}</dd></div></dl>
    <section><p className="eyebrow">查询表或 View</p><div className="database-source-relations">{(data.referenced_relations ?? []).map(relation => <code key={relation}>{relation}</code>)}</div></section>
    <section><p className="eyebrow">条件说明</p><p className="database-source-conditions">{data.conditions_summary ?? '已按查询中的实际筛选条件过滤（具体条件值已隐藏）'}</p></section>
    <section><div className="database-result-heading"><p className="eyebrow">当前页结果</p><span>{data.result_available ? `${data.returned_rows ?? 0} 行` : '结果不可用'}</span></div>{data.result_available ? columns.length ? <div className="database-result-table"><table><thead><tr>{columns.map(column => <th key={column.name}>{column.name}<small>{column.data_type}</small></th>)}</tr></thead><tbody>{(data.rows ?? []).map((row, index) => <tr key={index}>{columns.map(column => <td key={column.name}>{databaseValue(row[column.name], column.data_type)}</td>)}</tr>)}</tbody></table></div> : <p className="muted">本页没有返回行。</p> : <p className="muted">当前页结果已过期或因进程重启不可用；仍保留来源元信息，重新查询即可获得新的结果。</p>}</section>
    {data.has_more && (data.locator?.continuation_available === false ? <p className="database-source-more">当前结果还有未返回数据，但未能证明稳定排序，无法安全续页；请缩小查询范围或提供主键、非空唯一键等稳定排序。</p> : data.can_continue ? <p className="database-source-more">当前结果还有下一页。可返回会话点击“下一页”。</p> : <p className="database-source-more">续页上下文已过期；请重新查询。</p>)}
    <p className="muted database-source-boundary">结果行只在进程内短期缓存，并受当前页上限约束；不会保存完整 SQL、参数或结果集，也不会从历史来源重新执行查询。</p>
  </div>;
}
function databaseValue(value: unknown, dataType: string): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string' && /^(?:timestamptz|timestamp(?:\(\d+\))? with time zone)$/i.test(dataType)) {
    return formatBeijingTime(value);
  }
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch { return '[无法显示]'; }
  }
  return String(value);
}
function Pdf({ content, highlight, error }: { content: Blob; highlight?: PreviewData['highlight']; error: (message: string) => void }) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let cancelled = false; let destroy: (() => void) | undefined;
    async function render() {
      try {
        const library = await import('pdfjs-dist');
        library.GlobalWorkerOptions.workerSrc = pdfWorker;
        const task = library.getDocument({ data: new Uint8Array(await content.arrayBuffer()), enableXfa: false });
        destroy = () => { void task.destroy(); }; const doc = await task.promise;
        if (cancelled || !root.current) return;
        root.current.replaceChildren();
        for (let number = 1; number <= doc.numPages; number++) {
          if (cancelled || !root.current) return;
          const page = await doc.getPage(number); const base = page.getViewport({ scale: 1 });
          const viewport = page.getViewport({ scale: Math.min(1.5, (root.current.clientWidth - 40) / base.width) });
          const wrapper = document.createElement('div'); wrapper.className = 'pdf-page'; wrapper.style.width = `${viewport.width}px`; wrapper.style.height = `${viewport.height}px`;
          const canvas = document.createElement('canvas'); canvas.width = viewport.width; canvas.height = viewport.height; canvas.setAttribute('aria-label', `第 ${number} 页`);
          wrapper.append(canvas); root.current.append(wrapper);
          await page.render({ canvas, viewport }).promise;
          if (number === (highlight?.locator.page ?? 0)) {
            for (const b of highlight?.locator.boxes ?? []) {
              const box = document.createElement('div'); box.className = 'source-box';
              const width = highlight?.width ?? base.width; const height = highlight?.height ?? base.height;
              Object.assign(box.style, { left: `${b.x / width * 100}%`, top: `${b.y / height * 100}%`, width: `${b.width / width * 100}%`, height: `${b.height / height * 100}%` }); wrapper.append(box);
            }
            wrapper.scrollIntoView({ block: 'start' });
          }
        }
      } catch (e) { if (!cancelled) error((e as Error).message); }
    }
    void render(); return () => { cancelled = true; destroy?.(); };
  }, [content, highlight]);
  return <div ref={root} className="pdf-preview" aria-label="PDF 文档页面" />;
}
