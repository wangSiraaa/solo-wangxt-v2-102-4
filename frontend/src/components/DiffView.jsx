/** 提案相对基准的结构化差异：频率移动 / 掩模 / 极化规则 / 保护间隔 / 其他。 */
const POL_LABEL = { forbidden: '禁止', allowed: '允许', unknown: '待评估' }

function fmt(v) {
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(3)
  return String(v)
}

export default function DiffView({ diff, compact = false }) {
  if (!diff) return null
  if (!diff.changed) {
    return <div className="hint" style={{ color: 'var(--ok)' }}>草稿与基准一致（无差异）。</div>
  }
  return (
    <div className="diff-view">
      <div className="hint" style={{ marginBottom: 6 }}>{diff.summary}</div>
      {!!diff.carrier_moves?.length && (
        <div className="diff-section">
          <div className="diff-head">📶 载波频率移动（{diff.carrier_moves.length}）</div>
          <table className="plan-table">
            <thead>
              <tr><th>载波</th><th>基准 MHz</th><th>草稿 MHz</th><th>偏移</th></tr>
            </thead>
            <tbody>
              {diff.carrier_moves.map((m) => (
                <tr key={m.carrier}>
                  <td>{m.carrier}</td>
                  <td>{fmt(m.from_center_mhz)}</td>
                  <td>{fmt(m.to_center_mhz)}</td>
                  <td className={m.shift_mhz > 0 ? 'shift-pos' : 'shift-neg'}>
                    {m.shift_mhz > 0 ? '+' : ''}{fmt(m.shift_mhz)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!!diff.mask_changes?.length && (
        <div className="diff-section">
          <div className="diff-head">🎭 掩模变更（{diff.mask_changes.length}）</div>
          {diff.mask_changes.map((m) => (
            <div key={m.carrier} className="diff-row">
              <b>{m.carrier}</b>
              <span className="muted"> {m.from_mask} </span>→ <span>{m.to_mask}</span>
            </div>
          ))}
        </div>
      )}

      {!!diff.polarization_rule_changes?.length && (
        <div className="diff-section">
          <div className="diff-head">🧲 极化复用规则变更（{diff.polarization_rule_changes.length}）</div>
          {diff.polarization_rule_changes.map((r) => (
            <div key={r.pair} className="diff-row">
              <b>{r.pair}</b>
              <span className="muted"> {POL_LABEL[r.from] || r.from} </span>→{' '}
              <span>{POL_LABEL[r.to] || r.to}</span>
            </div>
          ))}
        </div>
      )}

      {(diff.rule_changes?.guard_required_mhz || diff.rule_changes?.leakage_limit_dbm) && (
        <div className="diff-section">
          <div className="diff-head">📏 规则参数</div>
          {diff.rule_changes.guard_required_mhz && (
            <div className="diff-row">
              保护间隔 <span className="muted">{fmt(diff.rule_changes.guard_required_mhz.from)}</span>
              {' '}→ <b>{fmt(diff.rule_changes.guard_required_mhz.to)}</b> MHz
            </div>
          )}
          {diff.rule_changes.leakage_limit_dbm && (
            <div className="diff-row">
              泄漏限值 <span className="muted">{fmt(diff.rule_changes.leakage_limit_dbm.from)}</span>
              {' '}→ <b>{fmt(diff.rule_changes.leakage_limit_dbm.to)}</b> dBm
            </div>
          )}
        </div>
      )}

      {!compact && (
        <>
          {!!diff.carrier_changes?.length && (
            <div className="diff-section">
              <div className="diff-head">载波属性变更</div>
              {diff.carrier_changes.map((c) => (
                <div key={c.carrier} className="diff-row">
                  <b>{c.carrier}</b>
                  {Object.entries(c.changes).map(([k, v]) => (
                    <span key={k} className="diff-chip">
                      {k}：<span className="muted">{fmt(v.from)}</span> → {fmt(v.to)}
                    </span>
                  ))}
                </div>
              ))}
            </div>
          )}
          {!!diff.band_changes && Object.keys(diff.band_changes).length > 0 && (
            <div className="diff-section">
              <div className="diff-head">可用频段</div>
              {Object.entries(diff.band_changes).map(([k, v]) => (
                <div key={k} className="diff-row">
                  {k}：<span className="muted">{fmt(v.from)}</span> → {fmt(v.to)} MHz
                </div>
              ))}
            </div>
          )}
          {(!!diff.carriers_added?.length || !!diff.carriers_removed?.length) && (
            <div className="diff-section">
              <div className="diff-head">载波增删</div>
              {!!diff.carriers_added.length && (
                <div className="diff-row">新增：{diff.carriers_added.join(', ')}</div>
              )}
              {!!diff.carriers_removed.length && (
                <div className="diff-row">删除：{diff.carriers_removed.join(', ')}</div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
