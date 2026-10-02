import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import CarrierTable from './CarrierTable.jsx'
import RulesPanel from './RulesPanel.jsx'
import FindingsList from './FindingsList.jsx'
import PowerSummary from './PowerSummary.jsx'
import DiffView from './DiffView.jsx'
import { StatusPill } from './ProposalList.jsx'
import { AuditTimeline, VersionChain, shortHash } from './ProposalAudit.jsx'

const TERMINAL = new Set(['applied', 'cancelled', 'rolled_back'])

function normalizePolicy(p) {
  const out = {}
  for (const [k, v] of Object.entries(p || {})) {
    const [a, b] = k.split('|')
    out[[a, b].sort().join('|')] = v
  }
  return out
}

const emptyDraft = (content) => ({
  ...content,
  reuse_policy: { ...(content.reuse_policy || {}) },
  carriers: content.carriers.map((c) => ({ ...c })),
})

export default function ProposalWorkspace({ proposalId, scenario, masks,
                                            onProposalChanged }) {
  const [p, setP] = useState(null)
  const [versions, setVersions] = useState([])
  const [tab, setTab] = useState('diff')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [mode, setMode] = useState('mask_aware')
  const [draft, setDraft] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [pickedVersion, setPickedVersion] = useState(null)
  const [versionContent, setVersionContent] = useState(null)

  const refresh = async () => {
    const [np, vs] = await Promise.all([
      api.getProposal(proposalId),
      api.listVersions(scenario.id),
    ])
    setP(np)
    setVersions(vs)
    setDraft((d) => (dirty ? d : emptyDraft(np.latest_snapshot.content)))
    onProposalChanged?.()
    return np
  }

  useEffect(() => {
    setP(null); setError(''); setTab('diff'); setPickedVersion(null)
    setVersionContent(null); setDirty(false)
    refresh().catch((e) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proposalId])

  const run = async (kind, fn) => {
    setBusy(kind); setError('')
    try {
      if (kind === 'save') await fn()
      else await fn()
      await refresh()
      setDirty(false)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const saveDraft = () => run('save', () =>
    api.saveDraft(proposalId, { content: draft, note: note || '草稿修订', actor: '学生' }))
  const doAnalyze = () => run('analyze', () => api.analyzeProposal(proposalId))
  const doPlan = () => run('plan', () => api.planProposal(proposalId, mode))
  const doReview = () => run('review', () => api.reviewProposal(proposalId, note || '评审通过'))
  const doApply = () => run('apply', () =>
    api.applyProposal(proposalId, { note, expected_base_revision: p.base_revision }))
  const doCancel = () => run('cancel', () => api.cancelProposal(proposalId, { note }))
  const doRollback = () => run('rollback', () => api.rollbackProposal(proposalId, { note }))

  const exportJson = async () => {
    setBusy('export'); setError('')
    try {
      const data = await api.exportProposal(proposalId)
      const blob = new Blob([JSON.stringify(data, null, 2)],
                           { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `proposal-${proposalId}-${data.proposal.status}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const editDraft = (patch) => {
    setDraft((d) => ({ ...d, ...patch }))
    setDirty(true)
  }
  const editRules = (r) => editDraft({
    guard_required_mhz: r.guard_required_mhz,
    leakage_limit_dbm: r.leakage_limit_dbm,
    reuse_policy: normalizePolicy(r.reuse_policy),
  })

  const pickVersion = async (rev) => {
    if (pickedVersion === rev) { setPickedVersion(null); setVersionContent(null); return }
    setBusy('version'); setError('')
    try {
      const v = await api.getVersion(scenario.id, rev)
      setPickedVersion(rev); setVersionContent(v)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const rulesOfDraft = draft ? {
    guard_required_mhz: draft.guard_required_mhz,
    leakage_limit_dbm: draft.leakage_limit_dbm,
    reuse_policy: normalizePolicy(draft.reuse_policy),
  } : null

  const baselineDrifted = p && scenario
    && scenario.current_revision !== p.base_revision
  const fresh = useMemo(() => {
    if (!p?.latest_snapshot) return { analysis: false, plan: false }
    const rev = p.latest_snapshot.revision
    const hash = p.latest_snapshot.content_hash
    return {
      analysis: p.analysis && p.analysis.revision === rev && p.analysis.input_hash === hash,
      plan: p.plan && p.plan.revision === rev && p.plan.input_hash === hash
        && p.plan.result.feasible,
    }
  }, [p])

  if (!p) return <div className="panel"><h2>调频提案</h2>{error ? <div className="err-msg">{error}</div> : '加载中…'}</div>

  const terminal = TERMINAL.has(p.status)
  const planCounts = p.plan?.result?.post_check?.counts

  return (
    <div className="panel proposal-workspace">
      <div className="row">
        <h2 style={{ margin: 0 }}>提案 #{p.id}：{p.title}</h2>
        <StatusPill status={p.status} />
        <span className="spacer" />
        <button onClick={exportJson} disabled={!!busy}>导出审计包 JSON</button>
      </div>
      <div className="hint" style={{ marginTop: 4 }}>{p.rationale || '（无说明）'}</div>

      <div className="baseline-box">
        <div>
          创建依据：基准 <b>r{p.base_revision}</b> <code>{shortHash(p.base_hash)}</code>
          {' '}→ 当前草稿 <b>r{p.current_revision}</b>{' '}
          <code>{shortHash(p.current_hash)}</code>
        </div>
        <div>
          基准现状：<b>r{scenario?.current_revision}</b>
          {baselineDrifted
            ? <span className="fresh-no"> ⚠ 基准已被他人修改，直接应用会被整体拒绝（可先查看版本链）</span>
            : <span className="fresh-ok"> · 与提案基线一致</span>}
        </div>
      </div>

      <div className="tabs">
        <button className={tab === 'diff' ? 'on' : ''} onClick={() => setTab('diff')}>差异 / 草稿</button>
        <button className={tab === 'analyze' ? 'on' : ''} onClick={() => setTab('analyze')}>
          分析{fresh.analysis ? '✓' : ''}
        </button>
        <button className={tab === 'plan' ? 'on' : ''} onClick={() => setTab('plan')}>
          规划{fresh.plan ? '✓' : ''}
        </button>
        <button className={tab === 'audit' ? 'on' : ''} onClick={() => setTab('audit')}>
          审计与版本（{p.events.length}）
        </button>
      </div>

      {error && <div className="err-msg">{error}</div>}

      {tab === 'diff' && (
        <>
          <DiffView diff={p.diff_vs_base} />
          <div className="row" style={{ marginTop: 8 }}>
            <input className="field" style={{ flex: 1 }} value={note}
                   placeholder="本次修订/决定的说明（进入审计）"
                   onChange={(e) => setNote(e.target.value)} />
          </div>
          {!terminal && draft && rulesOfDraft && (
            <details className="draft-editor" open={dirty}>
              <summary>{dirty ? '草稿有未保存修订（保存后旧分析/规划立即失效）' : '编辑草稿（载波 / 规则 / 频段）'}</summary>
              <div className="row" style={{ marginTop: 6 }}>
                <label className="field-label">可用频段</label>
                <input className="field" type="number" style={{ width: 80 }}
                       value={draft.band_low_mhz}
                       onChange={(e) => editDraft({ band_low_mhz: parseFloat(e.target.value) })} />
                <span className="muted">–</span>
                <input className="field" type="number" style={{ width: 80 }}
                       value={draft.band_high_mhz}
                       onChange={(e) => editDraft({ band_high_mhz: parseFloat(e.target.value) })} />
                <span className="muted">MHz</span>
              </div>
              <CarrierTable carriers={draft.carriers} masks={masks}
                            onChange={(cs) => editDraft({ carriers: cs })}
                            onAdd={() => editDraft({
                              carriers: [...draft.carriers, {
                                name: `C${draft.carriers.length + 1}`,
                                center_mhz: 100, bandwidth_mhz: 4, power_dbm: 20,
                                polarization: 'H', mask_name: 'strict',
                              }],
                            })}
                            onRemove={(i) => editDraft({
                              carriers: draft.carriers.filter((_, j) => j !== i),
                            })}
                            disabled={!!busy} />
              <div style={{ marginTop: 8 }}>
                <RulesPanel rules={rulesOfDraft}
                            onChange={(r) => editRules(r)} disabled={!!busy} />
              </div>
              <div className="row" style={{ marginTop: 8 }}>
                <button className="primary" disabled={!dirty || !!busy} onClick={saveDraft}>
                  {busy === 'save' ? '保存中…' : `保存为草稿 r${p.current_revision + 1}`}
                </button>
                {dirty && <span className="fresh-no">未保存的修订不会进入任何分析/规划</span>}
              </div>
            </details>
          )}
        </>
      )}

      {tab === 'analyze' && (
        <div>
          {!fresh.analysis && (
            <div className="fresh-no" style={{ marginBottom: 6 }}>
              当前草稿还没有绑定哈希 {shortHash(p.current_hash)} 的分析（旧修订的结果不展示）。
            </div>
          )}
          <div className="row" style={{ marginBottom: 8 }}>
            <button className="primary" disabled={terminal || !!busy} onClick={doAnalyze}>
              {busy === 'analyze' ? '分析中…' : `对草稿 r${p.current_revision} 运行完整分析`}
            </button>
            {fresh.analysis && (
              <span className="muted">
                绑定 r{p.analysis.revision} · {shortHash(p.analysis.input_hash)} ·
                状态 {p.analysis.result.status}
              </span>
            )}
          </div>
          {fresh.analysis && (
            <>
              <FindingsList findings={p.analysis.result.findings}
                            selectedPair={null} onSelect={() => {}} />
              <div style={{ marginTop: 8 }}>
                <PowerSummary summary={p.analysis.result.power_summary} />
              </div>
            </>
          )}
        </div>
      )}

      {tab === 'plan' && (
        <div>
          <div className="row" style={{ marginBottom: 8 }}>
            <span className="seg">
              <button className={mode === 'guard_only' ? 'on' : ''}
                      onClick={() => setMode('guard_only')}>仅保护间隔</button>
              <button className={mode === 'mask_aware' ? 'on' : ''}
                      onClick={() => setMode('mask_aware')}>掩模感知</button>
            </span>
            <button className="primary" disabled={terminal || !!busy} onClick={doPlan}>
              {busy === 'plan' ? '求解中…' : `为草稿 r${p.current_revision} 规划频率`}
            </button>
          </div>
          {p.plan && !fresh.plan && (
            <div className="fresh-no" style={{ marginBottom: 6 }}>
              以下规划基于旧草稿 r{p.plan.revision}（{shortHash(p.plan.input_hash)}），
              已对当前草稿失效，应用会被拒绝；请重新规划。
            </div>
          )}
          {p.plan && (
            <PlanBlock result={p.plan.result} valid={fresh.plan} />
          )}
          {!p.plan && <div className="hint">尚无规划结果。掩模感知模式会保证 post-check 零冲突。</div>}
        </div>
      )}

      {tab === 'audit' && (
        <>
          <div className="row" style={{ marginBottom: 6 }}>
            <b>基准版本链</b>
            <span className="spacer" />
            <span className="muted">点击任意版本查看完整内容（原基准 / 应用 / 回退）</span>
          </div>
          <VersionChain versions={versions}
                        currentRevision={scenario?.current_revision}
                        pickedRevision={pickedVersion} onPick={pickVersion} />
          {versionContent && (
            <div className="version-viewer">
              <div className="row">
                <b>r{versionContent.revision} · {versionContent.label}</b>
                <span className="spacer" />
                <code>{shortHash(versionContent.content_hash)}</code>
                <button onClick={() => { setPickedVersion(null); setVersionContent(null) }}>关闭</button>
              </div>
              <ReadOnlyCarriers content={versionContent.content} highlightAgainst={p.base_content} />
            </div>
          )}
          <h2 style={{ marginTop: 12 }}>决定流水（只追加，可回放）</h2>
          <AuditTimeline events={p.events} />
        </>
      )}

      {/* 状态机动作条 */}
      <div className="action-bar">
        {p.status === 'draft' && (
          <button className="primary" disabled={!!busy || !fresh.analysis}
                  title={fresh.analysis ? '' : '需先对当前草稿运行完整分析'}
                  onClick={doReview}>
            {busy === 'review' ? '提交中…' : '教师评审通过'}
          </button>
        )}
        {p.status === 'reviewed' && (
          <>
            <button className="primary" disabled={!!busy || !fresh.plan}
                    title={fresh.plan ? '' : '需先对当前草稿规划且方案可行'}
                    onClick={doApply}>
              {busy === 'apply' ? '应用中…' : '应用到基准（乐观并发 + post-check）'}
            </button>
            <button className="danger" disabled={!!busy} onClick={doCancel}>取消提案</button>
            <span className="hint">评审后再改草稿会自动退回“草稿”态。</span>
          </>
        )}
        {p.status === 'applied' && (
          <>
            <button className="primary" disabled={!!busy} onClick={doRollback}>
              {busy === 'rollback' ? '回退中…' : '回退到创建时基准'}
            </button>
            <button disabled title="已应用，重复应用幂等返回" onClick={doApply}>重复应用（幂等）</button>
            <span className="hint">已成为基准 r{p.applied_revision}；重复应用/回退请求均幂等。</span>
          </>
        )}
        {p.status === 'cancelled' && <span className="muted">提案已取消，基准未受影响。重复取消幂等。</span>}
        {p.status === 'rolled_back' && <span className="muted">已回退，基准恢复为创建时快照；全部审计与版本保留。重复回退幂等。</span>}
        {!!planCounts && p.status !== 'draft' && (
          <span className="spacer" />
        )}
        {!!planCounts && (
          <span className="muted">
            规划 post-check：冲突 {planCounts.error} / 警告 {planCounts.warning} / 待评估 {planCounts.pending}
          </span>
        )}
      </div>
    </div>
  )
}

function PlanBlock({ result, valid }) {
  if (!result.feasible) {
    return <div className="err-msg">✗ {result.status}：{result.message}</div>
  }
  const counts = result.post_check?.counts
  return (
    <div>
      <div className={valid ? 'fresh-ok' : 'fresh-no'} style={{ marginBottom: 4 }}>
        {valid ? '✓ 规划绑定当前草稿哈希' : '⚠ 规划不属于当前草稿'}
        {counts && ` · post-check 冲突 ${counts.error} / 警告 ${counts.warning} / 待评估 ${counts.pending}`}
        {result.post_check?.counts?.error > 0 && ' · 以此应用会被整体拒绝'}
      </div>
      <table className="plan-table">
        <thead>
          <tr><th>载波</th><th>原中心</th><th>新中心 MHz</th><th>偏移 MHz</th></tr>
        </thead>
        <tbody>
          {result.assignments.map((a) => (
            <tr key={a.name}>
              <td>{a.name} <span className="muted">{a.polarization}</span></td>
              <td>{a.original_center_mhz.toFixed(3)}</td>
              <td>{a.center_mhz.toFixed(3)}</td>
              <td className={a.shift_mhz > 0 ? 'shift-pos' : a.shift_mhz < 0 ? 'shift-neg' : ''}>
                {a.shift_mhz > 0 ? '+' : ''}{a.shift_mhz.toFixed(3)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ReadOnlyCarriers({ content, highlightAgainst }) {
  const base = Object.fromEntries((highlightAgainst?.carriers || []).map((c) => [c.name, c]))
  return (
    <table className="plan-table" style={{ marginTop: 6 }}>
      <thead>
        <tr><th>载波</th><th>中心 MHz</th><th>带宽</th><th>功率 dBm</th><th>极化</th><th>掩模</th></tr>
      </thead>
      <tbody>
        {content.carriers.map((c) => {
          const b = base[c.name]
          const moved = b && b.center_mhz !== c.center_mhz
          return (
            <tr key={c.name}>
              <td>{c.name}{moved && <span className="fresh-ok"> ●</span>}</td>
              <td className={moved ? 'shift-pos' : ''}>{c.center_mhz.toFixed(3)}</td>
              <td>{c.bandwidth_mhz}</td>
              <td>{c.power_dbm}</td>
              <td>{c.polarization}</td>
              <td>{c.mask_name}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
