const FIELD_LABEL = {
  guard_required_mhz: '保护间隔',
  leakage_limit_dbm: '掩模泄漏限值',
  band_low_mhz: '可用频段下限',
  band_high_mhz: '可用频段上限',
}

function fmt(v, d = 3) {
  return v == null ? '—' : Number(v).toFixed(d)
}

function ChangePill({ children, tone = '' }) {
  return <span className={`change-pill ${tone}`}>{children}</span>
}

/** 结构化差异视图：载波频率移动 / 掩模 / 极化规则 / 保护间隔。 */
export default function ProposalDiff({ diff, compact = false }) {
  if (!diff) return null
  const s = diff.summary || {}
  if (s.total === 0) {
    return <div className="hint" style={{ color: 'var(--ok)' }}>与对比版本完全相同，无差异。</div>
  }

  const moved = (diff.carrier_changes || []).filter((c) => 'center_mhz' in c.changes)
  const otherChanges = (diff.carrier_changes || []).filter((c) => !('center_mhz' in c.changes))

  return (
    <div className="diff-view">
      <div className="diff-summary">
        <ChangePill tone="accent">频率移动 {s.carriers_moved}</ChangePill>
        <ChangePill>其他载波属性 {otherChanges.length}</ChangePill>
        <ChangePill tone="warn">掩模 {s.masks_changed}</ChangePill>
        <ChangePill tone="pending">极化规则 {s.policies_changed}</ChangePill>
        <ChangePill tone="ok">保护/限值 {s.rules_changed}</ChangePill>
        {(s.carriers_added > 0 || s.carriers_removed > 0) && (
          <ChangePill tone="danger">新增 {s.carriers_added} / 删除 {s.carriers_removed}</ChangePill>
        )}
      </div>

      {moved.length > 0 && (
        <table className="plan-table diff-table">
          <thead>
            <tr><th>载波</th><th>原中心 MHz</th><th>新中心 MHz</th><th>偏移</th></tr>
          </thead>
          <tbody>
            {moved.map((c) => {
              const ch = c.changes.center_mhz
              return (
                <tr key={c.carrier}>
                  <td>{c.carrier}</td>
                  <td className="muted">{fmt(ch.from)}</td>
                  <td>{fmt(ch.to)}</td>
                  <td className={ch.delta > 0 ? 'shift-pos' : 'shift-neg'}>
                    {ch.delta > 0 ? '+' : ''}{fmt(ch.delta)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {otherChanges.length > 0 && (
        <div className="diff-block">
          <h4>其他载波属性</h4>
          <table className="plan-table diff-table">
            <thead><tr><th>载波</th><th>字段</th><th>原值</th><th>新值</th></tr></thead>
            <tbody>
              {otherChanges.flatMap((c) =>
                Object.entries(c.changes).map(([field, ch]) => (
                  <tr key={`${c.carrier}-${field}`}>
                    <td>{c.carrier}</td>
                    <td>{FIELD_LABEL[field] || field}</td>
                    <td className="muted">{String(ch.from)}</td>
                    <td>{String(ch.to)}</td>
                  </tr>
                )))}
            </tbody>
          </table>
        </div>
      )}

      {(diff.carriers_added?.length > 0 || diff.carriers_removed?.length > 0) && (
        <div className="diff-block">
          <h4>载波增删</h4>
          {diff.carriers_added.map((x) => (
            <div key={`a-${x.carrier}`} className="meta-row add">＋ 新增 {x.carrier}（{fmt(x.after.center_mhz)} MHz）</div>
          ))}
          {diff.carriers_removed.map((x) => (
            <div key={`r-${x.carrier}`} className="meta-row del">－ 删除 {x.carrier}（原 {fmt(x.before.center_mhz)} MHz）</div>
          ))}
        </div>
      )}

      {(diff.mask_changes?.length > 0 || diff.polarization_changes?.length > 0) && (
        <div className="diff-block">
          <h4>掩模与极化复用规则</h4>
          {diff.mask_changes.map((m) => (
            <div key={`m-${m.carrier}`} className="meta-row">
              {m.carrier} 掩模：<span className="muted">{m.from}</span> → <b>{m.to}</b>
            </div>
          ))}
          {diff.polarization_changes.map((p) => (
            <div key={`p-${p.pair}`} className="meta-row">
              极化对 {p.pair}：<span className="muted">{p.from ?? '（未设置→待评估）'}</span> → <b>{p.to}</b>
            </div>
          ))}
        </div>
      )}

      {diff.rule_changes?.length > 0 && (
        <div className="diff-block">
          <h4>保护间隔与规则参数</h4>
          <table className="plan-table diff-table">
            <thead><tr><th>参数</th><th>原值</th><th>新值</th></tr></thead>
            <tbody>
              {diff.rule_changes.map((r) => (
                <tr key={r.field}>
                  <td>{r.label}</td>
                  <td className="muted">{fmt(r.from)}</td>
                  <td>{fmt(r.to)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export function shortHash(h, n = 10) {
  return h ? `${String(h).slice(0, n)}…` : '—'
}
