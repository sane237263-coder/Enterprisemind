import { formatBeijingTime } from '../../shared/datetime';
import { BarChart3, Download, LineChart, PieChart } from 'lucide-react';
import { useId } from 'react';
import type { ReactNode } from 'react';
import './chart-preview.css';

export type ChartPreviewType = 'bar' | 'line' | 'pie';
export type ChartDownloadFormat = 'png' | 'pdf' | 'xlsx' | 'csv';

export type ChartPreviewArtifact = {
  format: ChartDownloadFormat;
  href: string;
  fileName: string;
  sizeBytes?: number;
};

export type ChartPreviewCardProps = {
  title: string;
  chartType: ChartPreviewType;
  previewUrl: string | null;
  scopeLabel: string;
  statistic: string;
  queriedAt: string;
  sourceRefs: string[];
  artifacts: ChartPreviewArtifact[];
  onDownload?: (artifact: ChartPreviewArtifact) => void;
  emptyMessage?: string;
};

const chartNames: Record<ChartPreviewType, string> = {
  bar: '柱状图',
  line: '折线图',
  pie: '饼图',
};

const formatNames: Record<ChartDownloadFormat, string> = {
  png: 'PNG 图片',
  pdf: 'PDF 文件',
  xlsx: 'XLSX 原生图表',
  csv: '导出图表数据 CSV',
};

const AUTHORIZED_RESOURCE_PREFIXES = ['/api/v1/generated-files/', '/api/v1/chart-artifacts/'];

const chartIcons: Record<ChartPreviewType, ReactNode> = {
  bar: <BarChart3 size={18} aria-hidden="true" />,
  line: <LineChart size={18} aria-hidden="true" />,
  pie: <PieChart size={18} aria-hidden="true" />,
};

export function ChartPreviewCard({
  title,
  chartType,
  previewUrl,
  scopeLabel,
  statistic,
  queriedAt,
  sourceRefs,
  artifacts,
  onDownload,
  emptyMessage = '无可绘制数据',
}: ChartPreviewCardProps) {
  const titleId = useId();
  const safePreviewUrl = previewUrl ? safeResourceUrl(previewUrl) : null;
  const artifactByFormat = new Map(artifacts.map(artifact => [artifact.format, {
    ...artifact,
    safeHref: safeResourceUrl(artifact.href),
  }]));

  return <section className="chart-preview-card" aria-labelledby={titleId}>
    <header className="chart-preview-heading">
      <span className="chart-preview-icon">{chartIcons[chartType]}</span>
      <div>
        <p className="eyebrow">图表预览</p>
        <h2 id={titleId}>{title}</h2>
        <small>{chartNames[chartType]} · {scopeLabel}</small>
      </div>
    </header>
    <figure className="chart-preview-figure">
      {safePreviewUrl ? <img src={safePreviewUrl} alt={`${title}，${chartNames[chartType]}`} /> : <div className="chart-preview-empty" role="status">{previewUrl ? '预览链接不可用' : emptyMessage}<small>{scopeLabel}</small></div>}
      <figcaption>
        <span><strong>统计口径</strong>{statistic}</span>
        <span><strong>查询时间</strong>{formatBeijingTime(queriedAt, '以来源记录为准')}</span>
        <span><strong>来源</strong>{sourceRefs.length ? `${sourceRefs.length} 条已授权来源` : '无来源'}</span>
      </figcaption>
    </figure>
    {sourceRefs.length > 0 && <details className="chart-preview-sources">
      <summary>查看来源</summary>
      <ul>{sourceRefs.map(source => <li key={source}>{source}</li>)}</ul>
    </details>}
    <nav className="chart-preview-downloads" aria-label="图表下载">
      {(['png', 'pdf', 'xlsx', 'csv'] as ChartDownloadFormat[]).map(format => {
        const artifact = artifactByFormat.get(format);
        if (!artifact?.safeHref) return null;
        const label = <><Download size={14} aria-hidden="true" />{formatNames[format]}
          {artifact.sizeBytes != null && <small>{formatBytes(artifact.sizeBytes)}</small>}</>;
        return onDownload
          ? <button className="chart-preview-download" onClick={() => onDownload(artifact)} key={format}>{label}</button>
          : <a className="chart-preview-download" href={artifact.safeHref} download={artifact.fileName} key={format}>{label}</a>;
      })}
    </nav>
  </section>;
}

function safeResourceUrl(value: string) {
  if (value.startsWith('blob:')) return value;
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || !AUTHORIZED_RESOURCE_PREFIXES.some(prefix => url.pathname.startsWith(prefix))) return null;
    return `${url.pathname}${url.search}`;
  } catch {
    return null;
  }
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
