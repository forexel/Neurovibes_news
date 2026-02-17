import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'

const api = {
  async login(email, password) {
    const r = await fetch('/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    })
    if (!r.ok) throw new Error('login failed')
    return r.json()
  },
  async me(token) {
    const r = await fetch('/v1/me', { headers: { Authorization: `Bearer ${token}` } })
    if (!r.ok) throw new Error('auth failed')
    return r.json()
  },
  async listArticles(token, params) {
    const qs = new URLSearchParams(params)
    const r = await fetch(`/v1/articles?${qs.toString()}`, { headers: { Authorization: `Bearer ${token}` } })
    if (!r.ok) throw new Error('list failed')
    return r.json()
  },
  async score(token, id) {
    const r = await fetch(`/v1/articles/${id}/score-breakdown`, { headers: { Authorization: `Bearer ${token}` } })
    if (!r.ok) return null
    return r.json()
  },
  async versions(token, id) {
    const r = await fetch(`/v1/articles/${id}/versions`, { headers: { Authorization: `Bearer ${token}` } })
    if (!r.ok) return []
    return r.json()
  },
  async neighbors(token, id) {
    const r = await fetch(`/v1/articles/${id}/neighbors?top_k=5`, { headers: { Authorization: `Bearer ${token}` } })
    if (!r.ok) return []
    return r.json()
  },
  async bulkStatus(token, articleIds, status) {
    const r = await fetch('/v1/articles/bulk/status', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ article_ids: articleIds, status })
    })
    if (!r.ok) throw new Error('bulk failed')
    return r.json()
  },
  async feedback(token, id, body) {
    const r = await fetch(`/v1/articles/${id}/feedback`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
    if (!r.ok) throw new Error('feedback failed')
    return r.json()
  },
  async analytics(token) {
    const r = await fetch('/v1/analytics/overview', { headers: { Authorization: `Bearer ${token}` } })
    if (!r.ok) return null
    return r.json()
  }
}

function App() {
  const [token, setToken] = useState(localStorage.getItem('nv_token') || '')
  const [me, setMe] = useState(null)
  const [email, setEmail] = useState('admin@local')
  const [password, setPassword] = useState('admin123')
  const [data, setData] = useState({ items: [], page: 1, page_size: 20, total: 0 })
  const [status, setStatus] = useState('')
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState([])
  const [activeArticle, setActiveArticle] = useState(null)
  const [score, setScore] = useState(null)
  const [versions, setVersions] = useState([])
  const [neighbors, setNeighbors] = useState([])
  const [analytics, setAnalytics] = useState(null)

  useEffect(() => {
    if (!token) return
    api.me(token).then(setMe).catch(() => {
      setToken('')
      localStorage.removeItem('nv_token')
    })
  }, [token])

  useEffect(() => {
    if (!token) return
    loadArticles(1)
    api.analytics(token).then(setAnalytics)
  }, [token, status])

  const pages = useMemo(() => Math.max(1, Math.ceil((data.total || 0) / (data.page_size || 20))), [data])

  async function login(e) {
    e.preventDefault()
    const out = await api.login(email, password)
    setToken(out.access_token)
    localStorage.setItem('nv_token', out.access_token)
  }

  async function loadArticles(page) {
    const out = await api.listArticles(token, { page, page_size: 20, status, q })
    setData(out)
  }

  async function openArticle(a) {
    setActiveArticle(a)
    const [s, v, n] = await Promise.all([api.score(token, a.id), api.versions(token, a.id), api.neighbors(token, a.id)])
    setScore(s)
    setVersions(v)
    setNeighbors(n)
  }

  async function setBulk(statusValue) {
    if (!selected.length) return
    await api.bulkStatus(token, selected, statusValue)
    setSelected([])
    loadArticles(data.page)
  }

  async function sendFeedback() {
    if (!activeArticle) return
    await api.feedback(token, activeArticle.id, {
      explanation_text: 'Selected for strategic impact and relevance',
      reason_codes: ['strategic_impact', 'high_relevance'],
      confidence: 8,
      liked_aspects: 'global impact',
      disliked_aspects: 'limited source diversity'
    })
    alert('feedback saved')
  }

  if (!token) {
    return (
      <div style={styles.loginWrap}>
        <form onSubmit={login} style={styles.card}>
          <h2>Neurovibes Admin</h2>
          <input style={styles.input} value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email" />
          <input style={styles.input} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password" type="password" />
          <button style={styles.btn}>Login</button>
        </form>
      </div>
    )
  }

  return (
    <div style={styles.page}>
      <aside style={styles.sidebar}>
        <h3>Analytics</h3>
        <pre style={styles.pre}>{JSON.stringify(analytics, null, 2)}</pre>
        <h3>User</h3>
        <pre style={styles.pre}>{JSON.stringify(me, null, 2)}</pre>
      </aside>
      <main style={styles.main}>
        <div style={styles.toolbar}>
          <input style={styles.input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="search title" />
          <select style={styles.input} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">all</option>
            <option value="inbox">inbox</option>
            <option value="review">review</option>
            <option value="scored">scored</option>
            <option value="ready">ready</option>
            <option value="published">published</option>
            <option value="rejected">rejected</option>
          </select>
          <button style={styles.btn} onClick={() => loadArticles(1)}>Refresh</button>
          <button style={styles.btn} onClick={() => setBulk('review')}>Bulk to Review</button>
          <button style={styles.btn} onClick={() => setBulk('archived')}>Bulk Archive</button>
        </div>

        <table style={styles.table}>
          <thead><tr><th></th><th>ID</th><th>Status</th><th>Score</th><th>Title</th></tr></thead>
          <tbody>
            {data.items.map((a) => (
              <tr key={a.id} style={{ cursor: 'pointer' }} onClick={() => openArticle(a)}>
                <td>
                  <input type="checkbox" checked={selected.includes(a.id)} onChange={() => setSelected((prev) => prev.includes(a.id) ? prev.filter((x) => x !== a.id) : [...prev, a.id])} />
                </td>
                <td>{a.id}</td>
                <td>{a.status}</td>
                <td>{a.score ?? '-'}</td>
                <td>{a.ru_title || a.title}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div style={styles.pagination}>
          <button style={styles.btn} disabled={data.page <= 1} onClick={() => loadArticles(data.page - 1)}>Prev</button>
          <span>{data.page}/{pages}</span>
          <button style={styles.btn} disabled={data.page >= pages} onClick={() => loadArticles(data.page + 1)}>Next</button>
        </div>

        {activeArticle && (
          <section style={styles.details}>
            <h3>Article #{activeArticle.id}</h3>
            <p>{activeArticle.ru_title || activeArticle.title}</p>
            <a href={activeArticle.canonical_url} target="_blank">Source</a>
            <h4>Score breakdown</h4>
            <pre style={styles.pre}>{JSON.stringify(score, null, 2)}</pre>
            <h4>Similarity neighbors</h4>
            <pre style={styles.pre}>{JSON.stringify(neighbors, null, 2)}</pre>
            <h4>Version history</h4>
            <pre style={styles.pre}>{JSON.stringify(versions, null, 2)}</pre>
            <button style={styles.btn} onClick={sendFeedback}>Save structured feedback</button>
          </section>
        )}
      </main>
    </div>
  )
}

const styles = {
  page: { display: 'grid', gridTemplateColumns: '320px 1fr', minHeight: '100vh', background: '#0f172a', color: '#e2e8f0', fontFamily: 'Segoe UI, sans-serif' },
  sidebar: { borderRight: '1px solid #334155', padding: 16, background: '#111827' },
  main: { padding: 16 },
  loginWrap: { minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#0f172a', color: '#e2e8f0', fontFamily: 'Segoe UI, sans-serif' },
  card: { width: 360, background: '#111827', border: '1px solid #334155', borderRadius: 10, padding: 16, display: 'grid', gap: 8 },
  toolbar: { display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' },
  input: { padding: '8px 10px', borderRadius: 8, border: '1px solid #334155', background: '#0b1220', color: '#e2e8f0' },
  btn: { padding: '8px 10px', borderRadius: 8, border: '1px solid #475569', background: '#1e293b', color: '#e2e8f0', cursor: 'pointer' },
  table: { width: '100%', borderCollapse: 'collapse', background: '#111827' },
  pagination: { display: 'flex', gap: 10, alignItems: 'center', marginTop: 10 },
  details: { marginTop: 16, border: '1px solid #334155', borderRadius: 8, padding: 12, background: '#111827' },
  pre: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', background: '#0b1220', border: '1px solid #334155', padding: 10, borderRadius: 8, fontSize: 12 }
}

createRoot(document.getElementById('root')).render(<App />)
