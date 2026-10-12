import { useEffect, useRef, useState } from 'react'
import { AlertCircle } from 'lucide-react'
import { KnowledgePage } from './components/KnowledgePage'
import { Header } from './components/Header'
import { AiBidCheck } from './components/AiBidCheck'
import { Results } from './components/Results'
import { SearchFilters } from './components/SearchFilters'
import { queryNotices, resolveDateRange, type QueryFilters } from './api'
import type { Notice } from './data'

const initialFilters: QueryFilters = {
  keyword: '', regionCode: '', timeRange: '全部', startDate: '', endDate: '',
}

export default function App() {
  const [view, setView] = useState<'search' | 'knowledge'>('search')
  const [filters, setFilters] = useState<QueryFilters>(initialFilters)
  const [items, setItems] = useState<Notice[]>([])
  const [totalCount, setTotalCount] = useState(0)
  const [loading, setLoading] = useState(false)
  const [hasSearched, setHasSearched] = useState(false)
  const [error, setError] = useState('')
  const [elapsedMs, setElapsedMs] = useState<number | null>(null)
  const [page, setPage] = useState(1)
  const [hasNext, setHasNext] = useState(false)
  const [successfulFilters, setSuccessfulFilters] = useState<QueryFilters | null>(null)
  const submittedFilters = useRef<QueryFilters>(initialFilters)
  const pageCursors = useRef<(string | null)[]>([null])
  const requestId = useRef(0)
  const activeRequest = useRef<AbortController | null>(null)

  useEffect(() => () => activeRequest.current?.abort(), [])

  async function search(query: QueryFilters, nextPage = 1, cursor: string | null = null) {
    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    const id = ++requestId.current
    setError('')
    setLoading(true)
    setHasSearched(true)
    setItems([])
    setTotalCount(0)
    setHasNext(false)
    setElapsedMs(null)
    setPage(nextPage)
    try {
      const result = await queryNotices(query, { cursor, signal: controller.signal })
      if (id !== requestId.current || controller.signal.aborted) return
      setItems(result.items)
      setSuccessfulFilters({ ...query })
      setTotalCount(result.totalCount)
      setHasNext(Boolean(result.nextCursor))
      setElapsedMs(result.elapsedMs)
      pageCursors.current[nextPage] = result.nextCursor
    } catch (caught) {
      if (id !== requestId.current || controller.signal.aborted) return
      setError(caught instanceof Error ? caught.message : '查询失败，请稍后重试')
    } finally {
      if (id === requestId.current && !controller.signal.aborted) setLoading(false)
    }
  }

  function startSearch(query: QueryFilters) {
    // Freeze preset dates too, so pagination across midnight keeps the same query.
    try {
      const dates = resolveDateRange(query)
      submittedFilters.current = { ...query, ...dates, timeRange: '自定义' }
    } catch {
      submittedFilters.current = { ...query }
    }
    pageCursors.current = [null]
    void search(submittedFilters.current)
  }

  function updateFilter<K extends keyof QueryFilters>(key: K, value: QueryFilters[K]) {
    const next = { ...filters, [key]: value }
    if (key === 'timeRange') {
      next.startDate = ''
      next.endDate = ''
    } else if (key === 'startDate' || key === 'endDate') {
      next.timeRange = next.startDate || next.endDate ? '自定义' : '全部'
    }
    setFilters(next)
    if (key !== 'keyword') startSearch(next)
  }

  function changePage(nextPage: number) {
    const cursor = pageCursors.current[nextPage - 1]
    if (loading || nextPage < 1 || cursor === undefined || (nextPage > 1 && !cursor)) return
    void search(submittedFilters.current, nextPage, cursor)
  }

  return (
    <div className="app-shell">
      <Header onKnowledge={() => setView('knowledge')} onSearch={() => setView('search')} />
      <main>
        {view === 'knowledge' ? <KnowledgePage onBack={() => setView('search')} /> : <>
        <SearchFilters filters={filters} loading={loading} onChange={updateFilter} onSearch={() => startSearch(filters)} />
        {error ? <div className="error-banner" role="alert"><AlertCircle size={17} />{error}</div> : null}
        <Results
          items={items}
          totalCount={totalCount}
          hasSearched={hasSearched}
          loading={loading}
          error={error}
          elapsedMs={elapsedMs}
          page={page}
          hasNext={hasNext}
          onPageChange={changePage}
        />
        <AiBidCheck onManageKnowledge={() => setView('knowledge')} filters={successfulFilters} totalCount={totalCount} queryReady={hasSearched && !loading && !error && successfulFilters !== null} keywordDirty={filters.keyword.trim() !== (successfulFilters?.keyword || '').trim()} />
        </>}
      </main>
      <footer id="api-reference">
        <span>灵犀找标 · 公开招标公告查询</span>
        <code>GET /api/v1/notices</code>
      </footer>
    </div>
  )
}
