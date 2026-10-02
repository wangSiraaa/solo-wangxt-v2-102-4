/** 提案决定的审计流水（可回放）与基准版本链。 */
const EVENT_LABEL = {
  created: '创建提案',
  updated: '草稿修订',
  reset_to_draft: '改动后退回草稿',
  analysis_run: '运行分析',
  plan_run: '运行规划',
  reviewed: '评审通过',
  applied: '应用为基准',
  apply_rejected: '应用被拒',
  cancelled: '取消提案',
  rolled_back: '回退基准',
}
const EVENT_CLASS = {
  applied: 'ok', reviewed: 'ok', analysis_run: '', plan_run: '',
  apply_rejected: 'error', cancelled: 'muted', rolled_back: 'warning',
  reset_to_draft: 'warning',
}
const VERSION_LABEL = { baseline: '基准', applied: '应用', rollback: '回退' }

export function AuditTimeline({ events }) {
  if (!events?.length) return <div className="hint">暂无决定记录。</div>
  return (
    <ol className="audit">
      {events.map((e) => (
        <li key={e.id} className={EVENT_CLASS[e.type] || ''}>
          <div className="row">
            <span className="audit-seq">#{e.seq}</span>
            <b>{EVENT_LABEL[e.type] || e.type}</b>
            <span className="spacer" />
            <span className="muted">{e.actor}</span>
            {e.created_at && (
              <span className="muted">{new Date(e.created_at).toLocaleString()}</span>
            )}
          </div>
          {e.note && <div className="audit-note">{e.note}</div>}
          {!!e.detail && Object.keys(e.detail).length > 0 && (
            <AuditDetail type={e.type} detail={e.detail} />
          )}
        </li>
      ))}
    </ol>
  )
}

function AuditDetail({ type, detail }) {
  const rows = []
  if (type === 'apply_rejected') {
    rows.push(['拒绝原因', detail.reason])
    if (detail.actual_revision != null)
      rows.push(['基准版本', `期望 r${detail.expected_revision}，实际 r${detail.actual_revision}`])
    if (detail.current_revision != null && detail.plan_revision != null)
      rows.push(['规划绑定', `r${detail.plan_revision}，草稿已到 r${detail.current_revision}`])
    if (detail.post_check_counts)
      rows.push(['post-check', `冲突 ${detail.post_check_counts.error}`])
  } else if (type === 'analysis_run') {
    rows.push(['输入', `r${detail.revision} · ${shortHash(detail.input_hash)}`])
    rows.push(['结论', `${detail.status}（冲突 ${detail.counts?.error} / 警告 ${detail.counts?.warning} / 待评估 ${detail.counts?.pending}）`])
  } else if (type === 'plan_run') {
    rows.push(['输入', `r${detail.revision} · ${shortHash(detail.input_hash)}`])
    rows.push(['模式', detail.mode])
    rows.push(['结论', detail.feasible
      ? `可行（post-check 冲突 ${detail.post_check_counts?.error ?? '-'}）`
      : '不可行'])
  } else if (type === 'reviewed') {
    rows.push(['依据', `r${detail.revision} · ${shortHash(detail.input_hash)} · 分析 ${detail.analysis_status}`])
  } else if (type === 'applied') {
    rows.push(['输入', `草稿 r${detail.snapshot_revision} · ${shortHash(detail.input_hash)}`])
    rows.push(['规划', `${detail.plan_mode}（产物 #${detail.plan_artifact_id}）`])
    rows.push(['新版本', `基准 r${detail.applied_revision} · ${shortHash(detail.content_hash)}`])
    rows.push(['post-check', `冲突 ${detail.post_check_counts?.error}`])
  } else if (type === 'rolled_back') {
    rows.push(['恢复', `创建时基准 r${detail.restored_base_revision} · ${shortHash(detail.restored_base_hash)}`])
    rows.push(['新版本', `基准 r${detail.new_revision} · ${shortHash(detail.content_hash)}`])
  } else if (type === 'reset_to_draft') {
    rows.push(['新草稿', `r${detail.revision} · ${shortHash(detail.input_hash)}`])
  }
  if (!rows.length) return null
  return (
    <div className="audit-detail">
      {rows.map(([k, v]) => (
        <div key={k}><span className="muted">{k}：</span>{String(v)}</div>
      ))}
    </div>
  )
}

export function VersionChain({ versions, currentRevision, onPick, pickedRevision }) {
  if (!versions?.length) return null
  return (
    <ol className="version-chain">
      {versions.map((v) => (
        <li key={v.id} className={v.revision === pickedRevision ? 'sel' : ''}
            onClick={() => onPick?.(v.revision)}>
          <span className={`ver-kind ${v.kind}`}>{VERSION_LABEL[v.kind] || v.kind}</span>
          <b>r{v.revision}</b>
          <span className="muted" title={v.content_hash}>{shortHash(v.content_hash)}</span>
          <span className="spacer" />
          {v.revision === currentRevision && <span className="fresh-ok">当前</span>}
          {v.proposal_id != null && <span className="muted">提案 #{v.proposal_id}</span>}
        </li>
      ))}
    </ol>
  )
}

export function shortHash(h) {
  return h ? `${h.slice(0, 8)}…` : ''
}
