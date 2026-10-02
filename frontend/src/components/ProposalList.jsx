import { useState } from 'react'
import { api } from '../api.js'

export const STATUS_LABEL = {
  draft: '草稿',
  reviewed: '已评审',
  applied: '已应用',
  cancelled: '已取消',
  rolled_back: '已回退',
}
export const STATUS_CLASS = {
  draft: 'attention',
  reviewed: 'ok',
  applied: 'ok',
  cancelled: 'muted',
  rolled_back: 'attention',
}

export function StatusPill({ status }) {
  return (
    <span className={`prop-status ${status}`} title={`状态：${STATUS_LABEL[status] || status}`}>
      {STATUS_LABEL[status] || status}
    </span>
  )
}

/**
 * 左列：从当前场景创建提案 + 该场景下的提案列表。
 */
export default function ProposalList({ scenarioId, scenario, proposals,
                                        selectedId, onSelect, onChanged }) {
  const [title, setTitle] = useState('调频提案')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const create = async () => {
    if (!scenarioId || !scenario) return
    setBusy(true); setError('')
    const content = {
      name: scenario.name, description: scenario.description || '',
      band_low_mhz: scenario.band_low_mhz, band_high_mhz: scenario.band_high_mhz,
      guard_required_mhz: scenario.guard_required_mhz,
      leakage_limit_dbm: scenario.leakage_limit_dbm,
      reuse_policy: scenario.reuse_policy || {},
      carriers: scenario.carriers.map(({ id, ...c }) => c),
    }
    try {
      const p = await api.createProposal({
        scenario_id: scenarioId, title: title || '调频提案',
        rationale: '', content, actor: '学生',
      })
      setTitle('调频提案')
      onChanged()
      onSelect(p.id)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <h2>版本化调频提案</h2>
      {!scenarioId ? (
        <div className="hint">请先在上方选择/保存一个基准场景，再从它创建提案。草稿不会改动基准。</div>
      ) : (
        <>
          <div className="row">
            <input className="field" style={{ flex: 1 }} value={title}
                   onChange={(e) => setTitle(e.target.value)} placeholder="提案标题" />
            <button className="primary" disabled={busy} onClick={create}>
              {busy ? '创建中…' : '从当前基准创建草稿'}
            </button>
          </div>
          <div className="hint">
            基准修订号 rev {scenario?.current_revision ?? 0}。应用时若基准已被他人修改，将整体拒绝。
          </div>
          {error && <div className="err-msg">{error}</div>}
          <ul className="prop-list">
            {proposals.map((p) => (
              <li key={p.id} className={p.id === selectedId ? 'sel' : ''}
                  onClick={() => onSelect(p.id)}>
                <div className="row">
                  <StatusPill status={p.status} />
                  <b>#{p.id} {p.title}</b>
                  <span className="spacer" />
                  <span className="muted">草稿 r{p.current_revision}</span>
                </div>
                <div className="row muted" style={{ fontSize: 11, marginTop: 2 }}>
                  <span>基准 r{p.base_revision}</span>
                  {p.diff_summary.moves > 0 && <span>· {p.diff_summary.moves} 移动</span>}
                  {p.diff_summary.mask_changes > 0 && <span>· {p.diff_summary.mask_changes} 掩模</span>}
                  {p.diff_summary.polarization_rule_changes > 0 &&
                    <span>· {p.diff_summary.polarization_rule_changes} 极化规则</span>}
                  {p.diff_summary.guard_changed && <span>· 保护间隔</span>}
                  {p.status === 'draft' && !p.has_analysis && <span className="fresh-no">· 未分析</span>}
                  {p.status === 'draft' && !p.plan_fresh && p.has_analysis
                    && <span className="fresh-no">· 规划过期</span>}
                  {p.plan_fresh && <span className="fresh-ok">· 规划有效</span>}
                  {p.applied_revision != null && <span>· 已应用 r{p.applied_revision}</span>}
                </div>
              </li>
            ))}
            {!proposals.length && <li className="muted" style={{ padding: '6px 2px' }}>该场景还没有提案。</li>}
          </ul>
        </>
      )}
    </div>
  )
}
