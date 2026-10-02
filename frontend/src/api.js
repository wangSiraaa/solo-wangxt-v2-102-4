const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string'
        ? body.detail
        : body.detail?.message || JSON.stringify(body.detail)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
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

  // 版本化调频提案
  listProposals: (scenarioId) =>
    request(`/proposals${scenarioId ? `?scenario_id=${scenarioId}` : ''}`),
  getProposal: (id) => request(`/proposals/${id}`),
  createProposal: (payload) =>
    request('/proposals', { method: 'POST', body: JSON.stringify(payload) }),
  saveDraft: (id, payload) =>
    request(`/proposals/${id}/draft`, { method: 'PUT', body: JSON.stringify(payload) }),
  analyzeProposal: (id, actor = '学生') =>
    request(`/proposals/${id}/analyze`, { method: 'POST', body: JSON.stringify({ actor }) }),
  planProposal: (id, mode, actor = '学生') =>
    request(`/proposals/${id}/plan`, {
      method: 'POST', body: JSON.stringify({ mode, actor }),
    }),
  reviewProposal: (id, note = '评审通过', actor = '教师') =>
    request(`/proposals/${id}/review`, {
      method: 'POST', body: JSON.stringify({ note, actor }),
    }),
  applyProposal: (id, payload = {}) =>
    request(`/proposals/${id}/apply`, {
      method: 'POST',
      body: JSON.stringify({ note: '', actor: '教师', ...payload }),
    }),
  cancelProposal: (id, payload = {}) =>
    request(`/proposals/${id}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ note: '', actor: '教师', ...payload }),
    }),
  rollbackProposal: (id, payload = {}) =>
    request(`/proposals/${id}/rollback`, {
      method: 'POST',
      body: JSON.stringify({ note: '', actor: '教师', ...payload }),
    }),
  exportProposal: (id) => request(`/proposals/${id}/export`),
  listVersions: (scenarioId) => request(`/scenarios/${scenarioId}/versions`),
  getVersion: (scenarioId, revision) =>
    request(`/scenarios/${scenarioId}/versions/${revision}`),
}
