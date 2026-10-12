import { ExternalLink, Inbox } from 'lucide-react'
import type { Notice } from '../types'
import { formatDateTime } from '../utils'

export function NoticeTable({ notices }: { notices: Notice[] }) {
  return (
    <section className="panel table-panel" aria-labelledby="notice-heading">
      <div className="section-heading compact">
        <div><p className="eyebrow">实时写入 SQLite</p><h2 id="notice-heading">最近采集结果</h2></div>
        <span className="result-count">{notices.length} 条已显示</span>
      </div>
      {notices.length === 0 ? (
        <div className="table-empty"><Inbox size={24} /><p>任务采集到的公告会出现在这里</p></div>
      ) : (
        <div className="table-scroll">
          <table>
            <thead><tr><th>发布时间</th><th>地区</th><th>公告标题</th><th>层级</th><th>状态</th></tr></thead>
            <tbody>{notices.map((notice) => (
              <tr key={notice.canonical_url}>
                <td className="nowrap">{formatDateTime(notice.published_at)}</td>
                <td>{notice.region_raw ?? '未标注'}</td>
                <td><a href={notice.canonical_url} target="_blank" rel="noreferrer">{notice.title}<ExternalLink size={14} /></a></td>
                <td><span className="category-chip">{notice.category === 'zygg' ? '中央' : '地方'}</span></td>
                <td><span className={notice.is_new ? 'new-chip' : 'duplicate-chip'}>{notice.is_new ? '新增' : '重复'}</span></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  )
}

