import { useState } from 'react'
import { CalendarDays, ChevronRight, Search, SlidersHorizontal } from 'lucide-react'
import { regions, timeRanges } from '../data'
import { resolveDateRange, type QueryFilters } from '../api'

const resourceTabs = ['全部标讯', '合同临期项目', '拟建项目', '公司', '供应商', '采购商', '品牌', '招标代理机构']

type SearchFiltersProps = {
  filters: QueryFilters
  loading: boolean
  onChange: <K extends keyof QueryFilters>(key: K, value: QueryFilters[K]) => void
  onSearch: () => void
}

type FilterRowProps = {
  label: string
  children: React.ReactNode
}

function FilterRow({ label, children }: FilterRowProps) {
  return (
    <div className="filter-row">
      <span className="filter-label">{label}</span>
      <div className="filter-options">{children}</div>
    </div>
  )
}

export function SearchFilters({ filters, loading, onChange, onSearch }: SearchFiltersProps) {
  const [showAllRegions, setShowAllRegions] = useState(false)
  const visibleRegions = showAllRegions ? regions : regions.slice(0, 20)
  const effectiveDates = filters.timeRange === '全部' || filters.timeRange === '自定义'
    ? { startDate: filters.startDate, endDate: filters.endDate }
    : resolveDateRange(filters)
  function submit(event: React.FormEvent) {
    event.preventDefault()
    onSearch()
  }

  return (
    <section className="search-section" id="top">
      <form className="search-form" onSubmit={submit}>
        <div className="search-box">
          <Search size={22} aria-hidden="true" />
          <input
            value={filters.keyword}
            onChange={(event) => onChange('keyword', event.target.value)}
            placeholder="请输入关键词，多个关键词请用空格隔开"
            aria-label="搜索关键词"
          />
          <button type="submit" disabled={loading}>
            {loading ? '查询中' : '搜索'}
          </button>
        </div>
        <div className="hot-searches">
          <span>热门搜索</span>
          {['人工智能', '智慧城市', '信息化运维'].map((term) => (
            <button type="button" key={term} onClick={() => onChange('keyword', term)}>{term}</button>
          ))}
        </div>
      </form>

      <div className="filter-panel">
        <nav className="resource-tabs" aria-label="资源类型">
          {resourceTabs.map((tab, index) => (
            <button className={index === 0 ? 'active' : ''} type="button" key={tab}>{tab}</button>
          ))}
        </nav>
        <div className="filter-body">
          <FilterRow label="项目地区">
            {visibleRegions.map(([name, code]) => (
              <button
                type="button"
                className={filters.regionCode === code ? 'selected' : ''}
                aria-pressed={filters.regionCode === code}
                onClick={() => onChange('regionCode', code)}
                key={name}
              >{name}</button>
            ))}
            <button type="button" className="more-filter" aria-expanded={showAllRegions} onClick={() => setShowAllRegions((current) => !current)}>
              {showAllRegions ? '收起' : '更多'} <ChevronRight size={14} />
            </button>
          </FilterRow>
          <FilterRow label="发布时间">
            {timeRanges.map((range) => (
              <button
                type="button"
                className={filters.timeRange === range ? 'selected' : ''}
                aria-pressed={filters.timeRange === range}
                onClick={() => onChange('timeRange', range)}
                key={range}
              >{range}</button>
            ))}
            <div className="date-range">
              <CalendarDays size={15} />
              <input aria-label="开始日期" type="date" value={filters.startDate} onChange={(event) => onChange('startDate', event.target.value)} />
              <span>至</span>
              <input aria-label="结束日期" type="date" value={filters.endDate} onChange={(event) => onChange('endDate', event.target.value)} />
            </div>
          </FilterRow>
          <div className="filter-summary">
            <SlidersHorizontal size={15} />
            <span>已选条件</span>
            {filters.regionCode ? <b>{regions.find(([, code]) => code === filters.regionCode)?.[0]}</b> : null}
            {filters.timeRange !== '全部' && filters.timeRange !== '自定义' ? <b>{filters.timeRange}</b> : null}
            {effectiveDates.startDate || effectiveDates.endDate ? (
              <b aria-label="实际查询日期">{effectiveDates.startDate || '不限'} 至 {effectiveDates.endDate || '不限'}</b>
            ) : null}
            {!filters.regionCode && filters.timeRange === '全部' && !filters.startDate && !filters.endDate ? <em>暂无额外筛选</em> : null}
          </div>
        </div>
      </div>
    </section>
  )
}
