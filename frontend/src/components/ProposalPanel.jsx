import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import FindingsList from './FindingsList.jsx'
import ProposalDiff, { shortHash } from './ProposalDiff.jsx'

const STATUS = {
  draft: { label: '草稿', cls: 'pending' },
  reviewed: { label: '已评审', cls: 'accent' },
  applied: { label: '已应用', cls: 'ok' },
  cancelled: { label: '已取消', cls: 'danger' },
  rolled_back: { label: '已回退', cls: 'warn' },
}
const EVENT_LABEL = {
  created: '创建提案', revised: '草稿修订', plan_attached: '运行规划',
  analysis_attached: '运行分析', plan_accepted: '采纳规划',
  reviewed: '评审通过', applied: '应用', apply_rejected: '应用被拒绝',
  cancelled: '取消', rolled_back: '回退', draft: '退回草稿',
}
const POLS = ['H', 'V', 'LHCP', 'RHCP']

export default function ProposalPanel({ scenario, masks, onScenarioChanged }) {
  const [list, setList] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [proposal, setProposal] = useState(null)
  const [versions, setVersions] = useState(null)
  const [tab, setTab] = useState('diff')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [planMode, setPlanMode] = useState('mask_aware')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(null)

  const refreshList = useCallback(() => {
    if (!scenario) return
    api.listProposals(scenario.id).then(setList).catch(() => setList([]))
  }, [scenario])

  useEffect(() => {
    setProposal(null); setSelectedId(null); setVersions(null); setEditing(false)
    refreshList()
  }, [scenario?.id, refreshList])

  const refreshProposal = useCallback(async (id) => {
    const p = await api.getProposal(id)
    setProposal(p)
    return p
  }, [])

  const openProposal = async (id) => {
    setBusy('load'); setError(''); setEditing(false); setTab('diff')
    try {
      setSelectedId(id)
      await refreshProposal(id)
      const vs = await api.listVersions(scenario.id)
      setVersions(vs)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const createProposal = async () => {
    if (!scenario || !title.trim()) { setError('请先选择场景并填写提案标题'); return }
    setBusy('create'); setError('')
    try {
      // 复制当前基准为无差异草稿（先建后改）
      const content = snapshotFromScenario(scenario)
      const p = await api.createProposal({
        scenario_id: scenario.id, title: title.trim(),
        description: '', content,
      })
      setTitle('')
      await refreshList()
      await openProposal(p.id)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const run = async (kind, fn) => {
    setBusy(kind); setError('')
    try {
      await fn()
      await refreshProposal(selectedId)
    } catch (e) {
      setError(e.message)
    } finally { setBusy('') }
  }

  const startEdit = () => {
    setDraft(snapshotContent(proposal.current_snapshot))
    setEditing(true)
  }

  const saveRevision = async () => {
    setBusy('revise'); setError('')
    try {
      const p = await api.reviseProposal(selectedId, { content: draft })
      setProposal(p)
      setEditing(false)
      await refreshList()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const decision = async (kind, fn, reloadScenario = false) => {
    setBusy(kind); setError('')
    try {
      const p = await fn()
      setProposal(p)
      await refreshList()
      const vs = await api.listVersions(scenario.id)
      setVersions(vs)
      if (reloadScenario) onScenarioChanged?.()
    } catch (e) {
      setError(e.message)
      await refreshProposal(selectedId).catch(() => {})
    } finally { setBusy('') }
  }

  const st = proposal ? STATUS[proposal.status] : null

  return (
    <div className="panel proposal-panel">
      <h2>版本化调频提案（草稿不会覆盖教学基准）</h2>

      {!scenario ? (
        <div className="hint">请先在左上方选择一个教学场景，再为它创建调频提案。</div>
      ) : (
        <>
          <div className="row">
            <input className="field" style={{ flex: 1 }} placeholder="提案标题，如：拆分三处冲突载波对"
                   value={title} onChange={(e) => setTitle(e.target.value)} />
            <button className="primary" onClick={createProposal} disabled={!!busy}>
              从当前场景创建草稿快照
            </button>
          </div>
          <div className="hint">
            基准锚定：{scenario.name} v{scenario.version} ·
            直接分析行为不受影响，提案只有在“已评审 + 规划有效 + post-check 通过”后才能应用。
          </div>

          <table className="plan-table" style={{ marginTop: 8 }}>
            <thead>
              <tr><th>#</th><th>标题</th><th>状态</th><th>基准</th><th>修订</th><th>差异</th><th></th></tr>
            </thead>
            <tbody>
              {list.map((p) => (
                <tr key={p.id} className={selectedId === p.id ? 'row-selected' : ''}>
                  <td>{p.id}</td>
                  <td>{p.title}</td>
                  <td><StatusPill status={p.status} /></td>
                  <td className="muted">v{p.base_version}</td>
                  <td>{p.revision_count}</td>
                  <td className="muted">
                    {p.diff_summary ? `移动 ${p.diff_summary.carriers_moved} · 共 ${p.diff_summary.total}` : '—'}
                  </td>
                  <td><button onClick={() => openProposal(p.id)}>查看</button></td>
                </tr>
              ))}
              {list.length === 0 && (
                <tr><td colSpan={7} className="muted">还没有提案</td></tr>
              )}
            </tbody>
          </table>

          {error && <div className="err-msg" style={{ marginTop: 8 }}>{error}</div>}

          {proposal && (
            <div className="proposal-detail">
              <div className="proposal-head">
                <h3>#{proposal.id} {proposal.title} <StatusPill status={proposal.status} /></h3>
                <div className="hash-line">
                  <span>基准 v{proposal.base_version} {shortHash(proposal.base_snapshot_hash)}</span>
                  <span>当前草稿 rev{proposal.current_revision} {shortHash(proposal.current_snapshot_hash)}</span>
                  <span>场景当前 v{proposal.scenario_current_version}</span>
                </div>
                {proposal.baseline_changed && proposal.status !== 'applied' && proposal.status !== 'rolled_back' && (
                  <div className="warn-banner">
                    ⚠ 基准已被他人推进到 v{proposal.scenario_current_version}（提案锚定 v{proposal.base_version}）。
                    应用将因乐观并发校验被整体拒绝，且不会部分写入频率。
                  </div>
                )}
                {proposal.status === 'rolled_back' && (
                  <div className="ok-banner">
                    ↩ 已回退：场景恢复为提案锚定的 v{proposal.base_version} 基准（新场景版本 v{proposal.rolled_back_to_version}）。
                  </div>
                )}
              </div>

              <div className="row action-bar">
                {proposal.status === 'draft' && !editing && (
                  <button onClick={startEdit} disabled={!!busy}>✎ 编辑草稿（产生新修订）</button>
                )}
                {proposal.status === 'draft' && (
                  <>
                    <button onClick={() => run('analysis', () => api.proposalAnalysis(selectedId))}
                            disabled={!!busy}>运行分析</button>
                    <span className="seg">
                      <button className={planMode === 'guard_only' ? 'on' : ''}
                              onClick={() => setPlanMode('guard_only')}>仅保护间隔</button>
                      <button className={planMode === 'mask_aware' ? 'on' : ''}
                              onClick={() => setPlanMode('mask_aware')}>掩模感知</button>
                    </span>
                    <button className="primary"
                            onClick={() => run('plan', () => api.proposalPlan(selectedId, planMode))}
                            disabled={!!busy}>{busy === 'plan' ? '求解中…' : '求解频率位置'}</button>
                    <button onClick={() => decision('accept', () => api.acceptPlan(selectedId))}
                            disabled={!!busy}>采纳规划为新修订</button>
                    <button onClick={() => decision('review', () => api.reviewProposal(selectedId, note))}
                            disabled={!!busy}>送评审</button>
                    <button className="danger"
                            onClick={() => decision('cancel', () => api.cancelProposal(selectedId, note))}
                            disabled={!!busy}>取消提案</button>
                  </>
                )}
                {proposal.status === 'reviewed' && (
                  <>
                    <button onClick={() => decision('reopen', () => api.reopenProposal(selectedId, note))}
                            disabled={!!busy}>退回草稿</button>
                    <button className="danger"
                            onClick={() => decision('cancel', () => api.cancelProposal(selectedId, note))}
                            disabled={!!busy}>取消提案</button>
                    <button className="primary"
                            onClick={() => decision('apply', () => api.applyProposal(selectedId, note), true)}
                            disabled={!!busy}>{busy === 'apply' ? '校验并应用…' : '应用提案（乐观校验 + post-check）'}</button>
                  </>
                )}
                {proposal.status === 'applied' && (
                  <button className="danger"
                          onClick={() => decision('rollback', () => api.rollbackProposal(selectedId, note), true)}
                          disabled={!!busy}>回退到基准（保留审计）</button>
                )}
                {(proposal.status === 'cancelled' || proposal.status === 'rolled_back') && (
                  <span className="muted">终态：{st?.label}。重复请求幂等，不会重复写入。</span>
                )}
              </div>
              <div className="row">
                <input className="field" style={{ flex: 1 }} placeholder="决定依据/备注（写入审计）"
                       value={note} onChange={(e) => setNote(e.target.value)} />
                <a className="export-link" href={api.exportProposalUrl(selectedId)}
                   target="_blank" rel="noreferrer">⬇ 导出审计包（基准 + 已应用版本 + 决定依据）</a>
              </div>

              {editing && draft && (
                <DraftEditor draft={draft} masks={masks} onChange={setDraft}
                             onCancel={() => setEditing(false)} onSave={saveRevision} busy={busy} />
              )}

              <div className="tabs" style={{ marginTop: 10 }}>
                <button className={tab === 'diff' ? 'on' : ''} onClick={() => setTab('diff')}>差异（vs 基准）</button>
                <button className={tab === 'artifacts' ? 'on' : ''} onClick={() => setTab('artifacts')}>分析/规划（绑定哈希）</button>
                <button className={tab === 'revisions' ? 'on' : ''} onClick={() => setTab('revisions')}>修订历史</button>
                <button className={tab === 'versions' ? 'on' : ''} onClick={() => setTab('versions')}>版本对照</button>
                <button className={tab === 'events' ? 'on' : ''} onClick={() => setTab('events')}>决定依据（审计）</button>
              </div>

              {tab === 'diff' && <ProposalDiff diff={proposal.diff_base} />}

              {tab === 'artifacts' && <ArtifactsView proposal={proposal} />}

              {tab === 'revisions' && (
                <div>
                  {proposal.revisions.slice().sort((a, b) => b.revision - a.revision).map((r) => (
                    <div key={r.revision} className="revision-card">
                      <div className="row">
                        <b>rev{r.revision}</b>
                        <span className="muted">{shortHash(r.snapshot_hash, 12)}</span>
                        {r.note && <span className="meta">{r.note}</span>}
                      </div>
                      <ProposalDiff diff={r.diff.vs_previous || r.diff} compact />
                    </div>
                  ))}
                </div>
              )}

              {tab === 'versions' && (
                <VersionsView versions={versions} proposal={proposal} />
              )}

              {tab === 'events' && <EventsView proposal={proposal} />}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function StatusPill({ status }) {
  const s = STATUS[status] || { label: status, cls: '' }
  return <span className={`prop-status ${s.cls}`}>{s.label}</span>
}

function snapshotFromScenario(sc) {
  return {
    name: sc.name, description: sc.description || '',
    band_low_mhz: sc.band_low_mhz, band_high_mhz: sc.band_high_mhz,
    guard_required_mhz: sc.guard_required_mhz,
    leakage_limit_dbm: sc.leakage_limit_dbm,
    reuse_policy: sc.reuse_policy || {},
    carriers: sc.carriers.map(({ id, ...c }) => ({ ...c })),
  }
}

function snapshotContent(snap) {
  return {
    name: snap.name, description: snap.description || '',
    band_low_mhz: snap.band_low_mhz, band_high_mhz: snap.band_high_mhz,
    guard_required_mhz: snap.guard_required_mhz,
    leakage_limit_dbm: snap.leakage_limit_dbm,
    reuse_policy: { ...snap.reuse_policy },
    carriers: snap.carriers.map((c) => ({ ...c })),
  }
}

function DraftEditor({ draft, masks, onChange, onSave, onCancel, busy }) {
  const upd = (i, patch) =>
    onChange({ ...draft, carriers: draft.carriers.map((c, j) => (j === i ? { ...c, ...patch } : c)) })
  return (
    <div className="draft-editor">
      <div className="hint">修改后“保存新修订”会推进修订号，并使旧修订上的规划哈希立即失效。</div>
      <div className="row" style={{ margin: '6px 0' }}>
        <label className="field-label">保护间隔 MHz</label>
        <input className="field num" type="number" step="0.1" value={draft.guard_required_mhz}
               onChange={(e) => onChange({ ...draft, guard_required_mhz: parseFloat(e.target.value) })} />
        <label className="field-label">泄漏限值 dBm</label>
        <input className="field num" type="number" step="0.5" value={draft.leakage_limit_dbm}
               onChange={(e) => onChange({ ...draft, leakage_limit_dbm: parseFloat(e.target.value) })} />
      </div>
      <table className="carriers">
        <thead>
          <tr><th>名称</th><th>中心 MHz</th><th>功率 dBm</th><th>极化</th><th>掩模</th></tr>
        </thead>
        <tbody>
          {draft.carriers.map((c, i) => (
            <tr key={c.name}>
              <td>{c.name}</td>
              <td><input className="num" type="number" step="0.001" value={c.center_mhz}
                         onChange={(e) => upd(i, { center_mhz: parseFloat(e.target.value) })} /></td>
              <td><input className="num" type="number" step="0.5" value={c.power_dbm}
                         onChange={(e) => upd(i, { power_dbm: parseFloat(e.target.value) })} /></td>
              <td>
                <select value={c.polarization} onChange={(e) => upd(i, { polarization: e.target.value })}>
                  {POLS.map((p) => <option key={p}>{p}</option>)}
                </select>
              </td>
              <td>
                <select value={c.mask_name} onChange={(e) => upd(i, { mask_name: e.target.value })}>
                  {masks.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 8 }}>
        <button className="primary" onClick={onSave} disabled={busy}>保存为新修订</button>
        <button onClick={onCancel} disabled={busy}>放弃编辑</button>
      </div>
    </div>
  )
}

function ArtifactsView({ proposal }) {
  const revs = proposal.revisions.slice().sort((a, b) => b.revision - a.revision)
  const latest = proposal.current_revision
  return (
    <div>
      {revs.map((r) => (
        <div key={r.revision} className="revision-card">
          <div className="row">
            <b>rev{r.revision}</b>
            <span className="muted">输入 {shortHash(r.snapshot_hash, 12)}</span>
            {r.revision === latest
              ? <span className="change-pill accent">当前修订</span>
              : <span className="change-pill danger">旧修订（规划不可用于应用）</span>}
          </div>
          {r.artifacts.length === 0 && <div className="hint">该修订还没有分析/规划产物。</div>}
          {r.artifacts.map((a) => (
            <ArtifactCard key={a.id} a={a} currentHash={proposal.current_snapshot_hash}
                          isCurrent={r.revision === latest} />
          ))}
        </div>
      ))}
    </div>
  )
}

function ArtifactCard({ a, currentHash, isCurrent }) {
  const [open, setOpen] = useState(false)
  const bound = a.input_snapshot_hash === currentHash && isCurrent
  if (a.kind === 'analysis') {
    const p = a.payload
    return (
      <div className={`artifact-card ${bound ? '' : 'stale'}`}>
        <div className="row">
          <b>分析</b>
          <span className="muted">{shortHash(a.input_snapshot_hash, 12)}</span>
          <span className={`status-pill ${p.status}`}>{p.status}</span>
          <span className="muted">冲突 {p.counts.error} · 警告 {p.counts.warning} · 待评估 {p.counts.pending}</span>
          <span className="spacer" />
          {!bound && <span className="change-pill danger">哈希不匹配当前草稿</span>}
          <button onClick={() => setOpen(!open)}>{open ? '收起' : '展开冲突定位'}</button>
        </div>
        {open && <FindingsList findings={p.findings} selectedPair={null} onSelect={() => {}} />}
      </div>
    )
  }
  const p = a.payload
  return (
    <div className={`artifact-card ${bound ? '' : 'stale'}`}>
      <div className="row">
        <b>规划（{a.mode}）</b>
        <span className="muted">rev{a.revision} · {shortHash(a.input_snapshot_hash, 12)}</span>
        {p.feasible
          ? <span className="status-pill ok">可行 {p.objective_khz?.toFixed(0)} kHz</span>
          : <span className="status-pill conflict">无解</span>}
        {p.post_check && (
          <span className="muted">post-check 冲突 {p.post_check.counts.error}</span>
        )}
        {!bound && <span className="change-pill danger">旧规划哈希已失效</span>}
        <span className="spacer" />
        {p.feasible && <button onClick={() => setOpen(!open)}>{open ? '收起' : '展开频点'}</button>}
      </div>
      {open && p.feasible && (
        <table className="plan-table" style={{ marginTop: 6 }}>
          <thead><tr><th>载波</th><th>原中心</th><th>新中心</th><th>偏移 MHz</th></tr></thead>
          <tbody>
            {p.assignments.map((x) => (
              <tr key={x.name}>
                <td>{x.name}</td>
                <td className="muted">{x.original_center_mhz.toFixed(3)}</td>
                <td>{x.center_mhz.toFixed(3)}</td>
                <td className={x.shift_mhz > 0 ? 'shift-pos' : x.shift_mhz < 0 ? 'shift-neg' : ''}>
                  {x.shift_mhz > 0 ? '+' : ''}{x.shift_mhz.toFixed(3)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function VersionsView({ versions, proposal }) {
  const appliedV = proposal.applied_version
  const baseV = proposal.base_version
  const base = versions?.find((v) => v.version === baseV)
  return (
    <div>
      <div className="hint">刷新后仍可同时查看原基准（v{baseV}）与已应用版本{appliedV != null ? `（v${appliedV}）` : ''}。</div>
      <table className="plan-table" style={{ marginTop: 6 }}>
        <thead><tr><th>版本</th><th>类型</th><th>快照哈希</th><th>来源提案</th><th>备注</th></tr></thead>
        <tbody>
          {versions?.map((v) => (
            <tr key={v.version} className={v.version === appliedV ? 'row-applied' : ''}>
              <td>v{v.version}</td>
              <td>{kindLabel(v.kind)}</td>
              <td className="mono">{shortHash(v.snapshot_hash, 14)}</td>
              <td>{v.proposal_id ? `#${v.proposal_id}` : '—'}</td>
              <td className="muted">{v.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {appliedV != null && (
        <div className="revision-card">
          <b>已应用版本 v{appliedV} 相对原基准 v{baseV} 的差异（每次决定的依据见“决定依据”页签）</b>
          <ProposalDiff diff={proposal.diff_base} />
        </div>
      )}
      {!base && <div className="hint">基准版本行缺失。</div>}
    </div>
  )
}

function EventsView({ proposal }) {
  const events = proposal.events || []
  return (
    <ul className="findings" style={{ maxHeight: 420 }}>
      {events.map((e) => (
        <li key={e.sequence} className={eventClass(e.event_type)}>
          <div>
            <span className="tag">{e.sequence}</span>
            <b>{EVENT_LABEL[e.event_type] || e.event_type}</b>
            {e.from_status && <span className="muted"> {e.from_status} → {e.to_status}</span>}
            {e.actor && <span className="muted"> · {e.actor}</span>}
          </div>
          {e.note && <div>“{e.note}”</div>}
          <EventDetail type={e.event_type} detail={e.detail} />
        </li>
      ))}
    </ul>
  )
}

function EventDetail({ type, detail }) {
  if (!detail || Object.keys(detail).length === 0) return null
  if (type === 'applied' || type === 'plan_attached') {
    const c = detail.post_check || detail.post_check_ok != null && { error: detail.post_check_ok ? 0 : 1 }
    return (
      <div className="meta">
        {detail.mode && <span className="meta">模式 {detail.mode} · </span>}
        {detail.input_snapshot_hash && <span className="mono">输入 {shortHash(detail.input_snapshot_hash, 12)} · </span>}
        {detail.post_check && <span>post-check：冲突 {detail.post_check.error} 警告 {detail.post_check.warning} 待评估 {detail.post_check.pending}</span>}
        {detail.base_version != null && <span> · 基准 v{detail.base_version} → 应用 v{detail.applied_version}（rev{detail.revision}）</span>}
      </div>
    )
  }
  if (type === 'apply_rejected') {
    return <div className="meta">拒绝原因 [{detail.reason}]：{detail.message}</div>
  }
  if (type === 'revised' || type === 'plan_accepted') {
    return <div className="meta mono">修订 rev{detail.new_revision ?? detail.revision} · 输入 {shortHash(detail.input_snapshot_hash, 12)}</div>
  }
  if (type === 'rolled_back') {
    return <div className="meta">恢复基准 v{detail.restored_base_version}：v{detail.from_version} → v{detail.new_version}</div>
  }
  if (type === 'reviewed') {
    return <div className="meta mono">评审依据分析输入 {shortHash(detail.analysis_snapshot_hash, 12)}</div>
  }
  if (type === 'analysis_attached') {
    return <div className="meta mono">rev{detail.revision} 输入 {shortHash(detail.input_snapshot_hash, 12)} · {detail.status}</div>
  }
  return null
}

function kindLabel(k) {
  return { baseline: '基准', proposal_applied: '提案应用', rollback: '回退' }[k] || k
}

function eventClass(t) {
  if (t === 'apply_rejected') return 'ev-warn'
  if (t === 'applied' || t === 'plan_accepted' || t === 'rolled_back') return 'ev-ok'
  if (t === 'cancelled') return 'ev-danger'
  return 'ev-info'
}
