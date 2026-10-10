import { formatBeijingTime } from '../../shared/datetime';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Database, RefreshCw, Save, ShieldCheck } from 'lucide-react';
import { ApiError, api, mutation } from '../../shared/api';
import { Badge, Empty, ErrorMessage, Field, Form, Modal } from '../../shared/ui';
import type { DatabaseSource, Department, RelationPolicy, SchemaCatalog, SchemaRefresh, SchemaRelation } from '../../shared/types';

const terminalRefreshStates = new Set(['active', 'failed', 'retired', 'ready']);
const refreshLabels: Record<string, string> = { building: '刷新中', ready: '已就绪', active: '已生效', failed: '失败', retired: '已归档' };

export function DatabaseAdmin({ departments }: { departments: Department[] }) {
  const [source, setSource] = useState<DatabaseSource | null>();
  const [refresh, setRefresh] = useState<SchemaRefresh>();
  const [lastSuccessfulRefresh, setLastSuccessfulRefresh] = useState<SchemaRefresh>();
  const [relations, setRelations] = useState<SchemaRelation[]>([]);
  const [policies, setPolicies] = useState<RelationPolicy[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [testing, setTesting] = useState(false);
  const [savingPolicy, setSavingPolicy] = useState('');
  const [editorOpen, setEditorOpen] = useState(false);
  const [error, setError] = useState('');
  const [testMessage, setTestMessage] = useState('');
  const refreshStatusRef = useRef<SchemaRefresh['status'] | undefined>(undefined);
  const syncedRefreshIdRef = useRef<string | undefined>(undefined);

  async function optional<T>(path: string): Promise<T | undefined> {
    try { return (await api<T>(path)).data; }
    catch (caught) {
      if (caught instanceof ApiError && ['RESOURCE_NOT_FOUND', 'SCHEMA_SNAPSHOT_UNAVAILABLE', 'DATABASE_SOURCE_UNAVAILABLE'].includes(caught.code)) return undefined;
      throw caught;
    }
  }

  async function load() {
    setLoading(true); setLoadFailed(false); setError('');
    try {
      const nextSource = (await api<DatabaseSource | null>('/api/v1/admin/database-mcp/source')).data;
      setSource(nextSource);
      if (!nextSource) {
        setRefresh(undefined); setLastSuccessfulRefresh(undefined); setRelations([]); setPolicies([]); return;
      }
      const activeRefresh = nextSource.active_snapshot_id
        ? optional<SchemaRefresh>(`/api/v1/admin/database-mcp/schema-refreshes/${nextSource.active_snapshot_id}`)
        : Promise.resolve(undefined);
      const [catalog, nextPolicies, latestRefresh, successfulRefresh] = await Promise.all([
        optional<SchemaCatalog>('/api/v1/admin/database-mcp/schema'),
        optional<RelationPolicy[]>('/api/v1/admin/database-mcp/relation-policies'),
        optional<SchemaRefresh>('/api/v1/admin/database-mcp/schema-refresh/latest'),
        activeRefresh,
      ]);
      setRelations(catalog?.relations ?? []); setPolicies(nextPolicies ?? []);
      setRefresh(latestRefresh); setLastSuccessfulRefresh(successfulRefresh);
    } catch (caught) { setLoadFailed(true); setError((caught as Error).message); }
    finally { setLoading(false); }
  }

  useEffect(() => { void load(); }, []);

  async function reloadAfterRefresh(refreshId: string) {
    if (syncedRefreshIdRef.current === refreshId) return;
    syncedRefreshIdRef.current = refreshId;
    await load();
  }

  useEffect(() => {
    const previousStatus = refreshStatusRef.current;
    refreshStatusRef.current = refresh?.status;
    if (previousStatus === 'building' && refresh && terminalRefreshStates.has(refresh.status)) {
      void reloadAfterRefresh(refresh.id);
    }
    if (refresh?.status !== 'building') return;
    const timer = window.setInterval(() => {
      void optional<SchemaRefresh>('/api/v1/admin/database-mcp/schema-refresh/latest')
        .then(next => { if (next) setRefresh(next); })
        .catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [refresh?.status]);

  async function saveSource(data: FormData, key: string) {
    const get = (name: string) => String(data.get(name) ?? '').trim();
    const body: Record<string, unknown> = {
      name: get('name'), host: get('host'), port: Number(get('port')), database_name: get('database_name'),
      ssl_mode: get('ssl_mode'), status: get('status'),
    };
    const secretRef = get('secret_ref');
    if (secretRef || !source) body.secret_ref = secretRef;
    if (source) {
      await api('/api/v1/admin/database-mcp/source', mutation('PATCH', { ...body, row_version: source.row_version }, key));
    } else {
      await api('/api/v1/admin/database-mcp/source', mutation('PUT', body, key));
    }
    setTestMessage('');
    setEditorOpen(false); await load();
  }

  async function startRefresh() {
    if (refreshing) return;
    setRefreshing(true); setError('');
    try {
      let next = (await api<SchemaRefresh>('/api/v1/admin/database-mcp/schema-refresh', mutation('POST'))).data;
      setRefresh(next);
      for (let attempt = 0; attempt < 35 && !terminalRefreshStates.has(next.status); attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        next = (await api<SchemaRefresh>(`/api/v1/admin/database-mcp/schema-refreshes/${next.id}`)).data;
        setRefresh(next);
      }
      if (terminalRefreshStates.has(next.status)) await reloadAfterRefresh(next.id);
      else await load();
    } catch (caught) { setError((caught as Error).message); }
    finally { setRefreshing(false); }
  }

  async function testConnection() {
    if (testing || !source) return;
    setTesting(true); setError(''); setTestMessage('');
    try {
      const result = await api<{ reachable: boolean; relation_count: number; message: string }>(
        '/api/v1/admin/database-mcp/test-connection',
        mutation('POST'),
      );
      setTestMessage(`${result.data.message} 当前可见关系对象 ${result.data.relation_count} 个。`);
    } catch (caught) { setError((caught as Error).message); }
    finally { setTesting(false); }
  }

  const policyByName = useMemo(() => new Map(policies.map(policy => [`${policy.schema_name}.${policy.relation_name}`, policy])), [policies]);
  function policyFor(relation: SchemaRelation): RelationPolicy {
    return policyByName.get(`${relation.schema_name}.${relation.relation_name}`) ?? {
      id: '0', database_source_id: source?.id ?? '', schema_name: relation.schema_name, relation_name: relation.relation_name,
      relation_type: relation.relation_type, enabled: false, department_ids: [], row_version: 0, created_at: '', updated_at: '',
    };
  }
  async function savePolicy(relation: SchemaRelation, enabled: boolean, departmentIds: string[]) {
    const current = policyFor(relation); const name = `${relation.schema_name}.${relation.relation_name}`;
    setSavingPolicy(name); setError('');
    try {
      const result = await api<RelationPolicy>('/api/v1/admin/database-mcp/relation-policies', mutation('PUT', {
        schema_name: relation.schema_name, relation_name: relation.relation_name, enabled,
        department_ids: departmentIds, ...(current.id !== '0' ? { row_version: current.row_version } : {}),
      }));
      setPolicies(previous => [...previous.filter(item => item.id !== current.id && `${item.schema_name}.${item.relation_name}` !== name), result.data]);
    } catch (caught) { setError((caught as Error).message); }
    finally { setSavingPolicy(''); }
  }

  if (loading) return <Empty title="正在读取数据源配置…" />;
  if (loadFailed && source == null) return <div className="database-admin"><ErrorMessage value={error} /><div className="database-empty"><Database size={26} /><h2>暂时无法读取数据源配置</h2><p>当前状态未确认，页面不会把读取失败误判为“未配置”。请重试。</p><button onClick={() => void load()}>重试</button></div></div>;
  return <div className="database-admin">
    <ErrorMessage value={error} />
    {!source ? <div className="database-empty"><Database size={26} /><h2>还没有配置业务数据库</h2><p>配置一套只读 PostgreSQL 数据源后，才能刷新 Schema 并让助手回答业务数据问题。</p><button onClick={() => setEditorOpen(true)}>配置数据源</button></div> : <>
      <section className="database-card database-source-card">
        <div className="database-card-heading"><div><p className="eyebrow">企业业务数据</p><h2>{source.name}</h2><p className="muted">{source.host}:{source.port} · {source.database_name} · {source.ssl_mode}</p></div><div className="database-card-actions"><Badge value={source.status} /><button className="secondary" disabled={testing || source.status !== 'active'} onClick={() => void testConnection()}>{testing ? '正在检查…' : '测试连接'}</button><button className="secondary" onClick={() => setEditorOpen(true)}>编辑配置</button></div></div>
        <p className="database-safety"><ShieldCheck size={16} />密码只通过部署配置的 Secret 引用读取；此页面不显示或保存密码。</p>
        {testMessage && <p className="success-text" role="status">{testMessage}</p>}
      </section>
      <section className="database-card">
        <div className="database-card-heading"><div><p className="eyebrow">结构快照</p><h2>Schema 刷新</h2><p className="muted">先刷新元数据，再决定哪些表或 View 可以被查询。</p></div><button onClick={() => void startRefresh()} disabled={refreshing || source.status !== 'active'}><RefreshCw size={16} className={refreshing ? 'spin' : ''} />{refreshing ? '正在刷新…' : '刷新 Schema'}</button></div>
        {refresh && <div className="database-refresh-status"><Badge value={refresh.status} /><span>第 {refresh.snapshot_no} 个快照 · {refreshLabels[refresh.status] ?? refresh.status}</span>{lastSuccessfulRefresh?.captured_at && <span>最近成功 {formatBeijingTime(lastSuccessfulRefresh.captured_at)}</span>}{refresh.error_message && <span className="danger-text">{refresh.error_message}</span>}</div>}
        {!refresh && !relations.length && <p className="muted database-hint">尚未发布 Schema 快照。</p>}
        {source.status === 'active' && !source.active_snapshot_id && <p className="muted database-hint" role="status">数据源已启用，但当前没有活动 Schema 快照。停用后重新启用也必须重新刷新 Schema，之后再恢复白名单和部门授权。</p>}
      </section>
      <section className="database-card database-policy-card">
        <div className="database-card-heading"><div><p className="eyebrow">查询边界</p><h2>白名单与部门授权</h2><p className="muted">普通用户只能查询“全局白名单”与所属部门授权的交集；管理员也不能绕过全局白名单。</p></div></div>
        {!relations.length ? <Empty title="暂无可配置的表或 View">刷新并发布 Schema 后，这里会显示关系权限。</Empty> : <div className="database-relations">{relations.map(relation => {
          const policy = policyFor(relation); const name = `${relation.schema_name}.${relation.relation_name}`;
          return <details className="database-relation" key={name}><summary><span><strong>{name}</strong><small>{relation.relation_type === 'view' ? 'View' : '表'} · {relation.columns.length} 个字段</small></span><label className="policy-switch" onClick={event => event.stopPropagation()}><input type="checkbox" checked={policy.enabled} disabled={savingPolicy === name} onChange={event => void savePolicy(relation, event.target.checked, policy.department_ids)} /><span>全局白名单{policy.enabled && policy.department_ids.length ? ` · 停用将清理 ${policy.department_ids.length} 个部门授权` : ''}</span></label></summary>
              <div className="database-relation-body"><div className="database-columns"><span className="eyebrow">字段</span>{relation.columns.map(column => <span key={column.id}><code>{column.column_name}</code><small>{column.data_type}{column.is_unique ? ' · 单列唯一键' : ''}{column.is_nullable === false ? ' · 非空' : column.is_nullable === true ? ' · 可空' : ''}</small></span>)}</div>
              <Field label={<span id={`department-grants-label-${relation.id}`}>{name} 的部门授权（按住 Ctrl/Command 可多选）</span>}><select id={`department-grants-${relation.id}`} aria-labelledby={`department-grants-label-${relation.id}`} multiple value={policy.department_ids} disabled={savingPolicy === name} onChange={event => void savePolicy(relation, policy.enabled, Array.from(event.currentTarget.selectedOptions, option => option.value))}>{departments.map(department => <option key={department.id} value={department.id}>{department.name}</option>)}</select></Field>
              <p className="muted database-policy-note"><Save size={14} />权限修改会立即作用于新的查询；进行中的查询仍会在执行前重新校验授权。</p>
            </div>
          </details>;
        })}</div>}
      </section>
    </>}
    {editorOpen && <Modal title={source ? '编辑数据源配置' : '配置业务数据源'} close={() => setEditorOpen(false)}>
      <Form onSubmit={saveSource} cancel={() => setEditorOpen(false)} label="保存配置">
        <p className="muted">只支持一套外部 PostgreSQL 业务数据源。这里保存连接元数据和 Secret 引用，不填写密码或连接串；实际只读账号仍由独立 MCP 执行端的部署 Secret 配置，保存此表单不会直接修改企业数据库账号。</p>
        <Field label="名称"><input name="name" required maxLength={200} defaultValue={source?.name ?? ''} /></Field>
        <div className="form-grid"><Field label="主机"><input name="host" required maxLength={255} defaultValue={source?.host ?? ''} /></Field><Field label="端口"><input name="port" type="number" required min={1} max={65535} defaultValue={source?.port ?? 5432} /></Field></div>
        <Field label="数据库名"><input name="database_name" required maxLength={200} defaultValue={source?.database_name ?? ''} /></Field>
        <div className="form-grid"><Field label="SSL 模式"><select name="ssl_mode" defaultValue={source?.ssl_mode ?? 'verify-full'}><option value="disable">disable</option><option value="require">require</option><option value="verify-ca">verify-ca</option><option value="verify-full">verify-full</option></select></Field><Field label="状态"><select name="status" defaultValue={source?.status === 'disabled' ? 'disabled' : 'active'}><option value="active">启用</option><option value="disabled">停用</option></select></Field></div>
        {source && <p className="muted" role="note">停用数据源会撤销当前活动 Schema 快照；重新启用后必须重新刷新 Schema，白名单和部门授权需要在新快照上重新确认。</p>}
        <Field label={source ? 'Secret 引用（留空表示不变）' : 'Secret 引用'}><input name="secret_ref" required={!source} maxLength={500} placeholder={source ? '保持现有引用' : '例如 secret://business-readonly'} /></Field>
      </Form>
    </Modal>}
  </div>;
}
