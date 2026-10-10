const timestamp = String.raw`\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})`;
const instantPattern = new RegExp(`^${timestamp}$`, 'i');
const systemTimePattern = new RegExp(`((?:确认有效期至|查询时间|执行时间|执行)[:：]?\\s*)(${timestamp})`, 'gi');
const beijing = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', calendar: 'gregory', numberingSystem: 'latn',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

/** Format an API instant without depending on the device's timezone or locale. */
export function formatBeijingTime(value: string | null | undefined, fallback = '—'): string {
  if (!value || !instantPattern.test(value)) return fallback;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return fallback;
  const parts = Object.fromEntries(beijing.formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}（北京时间）`;
}

/** Support existing saved system prompts without rewriting user text or stored timestamps. */
export function formatSystemTimes(text: string): string {
  return text.replace(systemTimePattern, (match, label: string, value: string) => {
    const formatted = formatBeijingTime(value, '');
    return formatted ? label + formatted : match;
  });
}
