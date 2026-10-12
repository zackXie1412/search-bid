INSERT INTO app.notices (
  source_notice_id, canonical_url, title, published_at, region_code, region_raw,
  notice_type, category, source_list_url
) VALUES
('v1-test-001', 'https://www.ccgp.gov.cn/test/zhaobiao-v1-001.htm',
 '湖北省某医院医疗设备公开招标公告', '2026-09-19 10:00:00+08', '420000', '湖北',
 '公开招标', 'zygg', 'https://www.ccgp.gov.cn/cggg/zygg/index.htm'),
('v1-test-002', 'https://www.ccgp.gov.cn/test/zhaobiao-v1-002.htm',
 '广东省某学校信息化服务采购公告', '2026-09-18 09:30:00+08', '440000', '广东',
 '竞争性磋商', 'dfgg', 'https://www.ccgp.gov.cn/cggg/dfgg/index.htm'),
('v1-test-003', 'https://www.ccgp.gov.cn/test/zhaobiao-v1-003.htm',
 '湖北某单位服务器采购项目成交公告', '2026-09-17 14:00:00+08', '420000', '湖北',
 '成交公告', 'dfgg', 'https://www.ccgp.gov.cn/cggg/dfgg/index.htm')
ON CONFLICT (canonical_url) DO UPDATE SET
  title = excluded.title,
  published_at = excluded.published_at,
  region_code = excluded.region_code,
  region_raw = excluded.region_raw,
  notice_type = excluded.notice_type,
  category = excluded.category,
  source_list_url = excluded.source_list_url,
  last_seen_at = now(),
  updated_at = now();
