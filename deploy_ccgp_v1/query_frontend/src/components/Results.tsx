import { ArrowUpRight, ChevronLeft, ChevronRight, Clock3, Database, Inbox } from 'lucide-react'
import { NOTICE_PAGE_SIZE } from '../api'
import type { Notice } from '../data'

type ResultsProps = {
  items: Notice[]
  totalCount: number
  hasSearched: boolean
  loading: boolean
  error: string
  elapsedMs: number | null
  page: number
  hasNext: boolean
  onPageChange: (page: number) => void
}

function formatDate(value: string) {
  const date = new Date(value)
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date)
}

export function Results({ items, totalCount, hasSearched, loading, error, elapsedMs, page, hasNext, onPageChange }: ResultsProps) {
  const totalPages = Math.ceil(totalCount / NOTICE_PAGE_SIZE)
  return (
    <section className="results-section" aria-live="polite">
      <div className="results-header">
        <div>
          <h2>标讯结果</h2>
          <p>{loading ? '正在查询本地标讯…' : error ? '查询未完成' : hasSearched ? `共 ${totalCount} 条记录` : '选择地区、发布时间，或输入关键词开始查询'}</p>
        </div>
        {elapsedMs !== null ? (
          <div className="query-meta">
            <span><Database size={14} /> 本地采集数据</span>
            <span><Clock3 size={14} /> {elapsedMs} ms</span>
          </div>
        ) : null}
      </div>

      <div className="results-table-wrap">
        <table className="results-table">
          <thead>
            <tr>
              <th>公告名称</th>
              <th>公告类型</th>
              <th>地区</th>
              <th>发布时间（北京时间）</th>
            </tr>
          </thead>
          <tbody className={loading ? 'is-loading' : ''}>
            {items.map((notice) => (
              <tr key={notice.id}>
                <td>
                  <a href={notice.canonicalUrl} target="_blank" rel="noreferrer">
                    {notice.title}<ArrowUpRight size={15} />
                  </a>
                  <small>ID {notice.id}</small>
                </td>
                <td><span className="type-tag">公开招标公告</span></td>
                <td><span className="region-cell">{notice.region}</span></td>
                <td className="published-cell">{formatDate(notice.publishedAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && !error && hasSearched && items.length === 0 ? (
          <div className="empty-state">
            <Inbox size={34} />
            <strong>没有找到匹配的标讯</strong>
            <span>请减少关键词或放宽筛选条件</span>
          </div>
        ) : null}
        {loading || error ? (
          <div className="empty-state" role="status">
            <Database size={34} />
            <strong>{loading ? '正在查询本地标讯…' : '查询未完成'}</strong>
            <span>{loading ? '正在按所选条件读取公告' : '请调整条件或点击搜索重试'}</span>
          </div>
        ) : null}
        {!hasSearched ? (
          <div className="empty-state initial-state">
            <Database size={34} />
            <strong>准备查询最新标讯</strong>
            <span>仅查询本地已采集的公开招标公告，无需密钥</span>
          </div>
        ) : null}
      </div>

      {hasSearched && !loading && !error ? (
        <div className="pagination">
          <span>{totalPages > 0 ? `第 ${page} 页` : '暂无结果'}</span>
          <div>
            <span className="page-total">共 {totalPages} 页</span>
            <button aria-label="上一页" type="button" disabled={totalPages === 0 || page === 1} onClick={() => onPageChange(page - 1)}><ChevronLeft size={17} /></button>
            <button aria-label="下一页" type="button" disabled={!hasNext} onClick={() => onPageChange(page + 1)}><ChevronRight size={17} /></button>
          </div>
        </div>
      ) : null}
    </section>
  )
}
