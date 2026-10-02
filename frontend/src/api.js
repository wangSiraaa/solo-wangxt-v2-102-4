const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    let payload = null
    try {
      payload = await res.json()
      const d = payload.detail
      detail = typeof d === 'string' ? d : d?.message ? `${d.code ? `[${d.code}] ` : ''}${d.message}` : JSON.stringify(d)
    } catch {
      /* ignore */
    }
    const err = new Error(detail)
    err.status = res.status
    err.payload = payload
    throw err
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  health: () => request('/health'),
  masks: () => request('/masks'),
  listScenarios: () => request('/scenarios'),
  getScenario: (id) => request(`/scenarios/${id}`),
  createScenario: (payload) =>
    request('/scenarios', { method: 'POST', body: JSON.stringify(payload) }),
  updateScenario: (id, payload) =>
    request(`/scenarios/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteScenario: (id) =>
    request(`/scenarios/${id}`, { method: 'DELETE' }),
  analyze: (payload) =>
    request('/analyze', { method: 'POST', body: JSON.stringify(payload) }),
  plan: (payload) =>
    request('/plan', { method: 'POST', body: JSON.stringify(payload) }),

  // ---- 版本化调频提案 ----
  listProposals: (scenarioId) =>
    request(`/proposals${scenarioId != null ? `?scenario_id=${scenarioId}` : ''}`),
  getProposal: (id) => request(`/proposals/${id}`),
  createProposal: (payload) =>
    request('/proposals', { method: 'POST', body: JSON.stringify(payload) }),
  reviseProposal: (id, payload) =>
    request(`/proposals/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  proposalAnalysis: (id) =>
    request(`/proposals/${id}/analysis`, { method: 'POST' }),
  proposalPlan: (id, mode) =>
    request(`/proposals/${id}/plan`, {
      method: 'POST', body: JSON.stringify({ mode }),
    }),
  acceptPlan: (id, note = '') =>
    request(`/proposals/${id}/accept-plan`, {
      method: 'POST', body: JSON.stringify({ note }),
    }),
  reviewProposal: (id, note = '') =>
    request(`/proposals/${id}/review`, { method: 'POST', body: JSON.stringify({ note }) }),
  reopenProposal: (id, note = '') =>
    request(`/proposals/${id}/reopen`, { method: 'POST', body: JSON.stringify({ note }) }),
  cancelProposal: (id, note = '') =>
    request(`/proposals/${id}/cancel`, { method: 'POST', body: JSON.stringify({ note }) }),
  applyProposal: (id, note = '') =>
    request(`/proposals/${id}/apply`, { method: 'POST', body: JSON.stringify({ note }) }),
  rollbackProposal: (id, note = '') =>
    request(`/proposals/${id}/rollback`, { method: 'POST', body: JSON.stringify({ note }) }),
  exportProposalUrl: (id) => `${BASE}/proposals/${id}/export`,
  listVersions: (scenarioId) =>
    request(`/scenarios/${scenarioId}/versions`),
  getVersion: (scenarioId, version) =>
    request(`/scenarios/${scenarioId}/versions/${version}`),
}
