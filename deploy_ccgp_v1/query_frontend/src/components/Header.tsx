import { useState } from 'react'
import { BookOpen, Database, Settings2 } from 'lucide-react'
import { ModelSettings } from './ModelSettings'

export function Header({ onKnowledge, onSearch }: { onKnowledge?: () => void; onSearch?: () => void }) {
  const [settingsOpen, setSettingsOpen] = useState(false)
  return (
    <header className="site-header">
      <div className="header-inner">
        <a className="brand" href="#top" onClick={onSearch} aria-label="灵犀找标首页">
          <span className="brand-mark">灵</span>
          <span className="brand-copy">
            <strong>灵犀找标</strong>
            <small>LINGXI BID DATA</small>
          </span>
        </a>
        <div className="header-actions">
          <button className="model-settings-button" type="button" onClick={onKnowledge}><BookOpen size={16} /><span>企业知识库</span></button>
          <button className="model-settings-button" type="button" onClick={() => setSettingsOpen(true)} aria-haspopup="dialog"><Settings2 size={16} /><span>模型配置</span></button>
          <a className="doc-link" href="/api/docs" target="_blank" rel="noreferrer"><BookOpen size={16} /> API 文档</a>
          <span className="source-status"><Database size={15} /> 本地采集数据</span>
        </div>
      </div>
      {settingsOpen ? <ModelSettings onClose={() => setSettingsOpen(false)} /> : null}
    </header>
  )
}
