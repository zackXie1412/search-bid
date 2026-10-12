from pathlib import Path

from ccgp_crawler.parser import parse_list_page_result


PAGE_URL = "https://www.ccgp.gov.cn/cggg/zygg/index.htm"
FIXTURE = Path(__file__).parent / "fixtures" / "zygg_index.html"


def test_parser_extracts_regions_and_only_tenders():
    parsed = parse_list_page_result(PAGE_URL, FIXTURE.read_text(encoding="utf-8"), "zygg")
    assert len(parsed.notices) == 3
    # duplicate URL, missing date, and non-CCGP javascript href are all
    # rejected before persistence.
    assert parsed.skipped_count == 3
    assert parsed.filtered_count == 1
    first, second, third = parsed.notices
    assert first.as_dict() == {
        "canonical_url": "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260918_27356711.htm",
        "title": "长江水利委员会机关服务中心（本级）医疗设备公开招标公告",
        "published_at": "2026-09-18T17:21:00+08:00",
        "region_raw": "湖北",
        "region_code": "420000",
        "category": "zygg",
        "source_list_url": PAGE_URL,
    }
    assert second.title == "第三条邀请招标公告"
    assert second.published_at == "2026-09-16T00:00:00+08:00"
    assert second.region_raw == "广西壮族自治区"
    assert second.region_code == "450000"
    assert third.region_raw == "未知区域"
    assert third.region_code is None


def test_fallback_to_li_and_no_navigation():
    html = """
    <nav><li><a href='/cggg/zygg/index.htm'>导航</a>发布时间：2026-09-18</li></nav>
    <li><a href='./gkzb/202609/t20260918_123456.htm'>真实公开招标公告</a>
        发布时间：2026-09-18 10:00</li>
    """
    notices = parse_list_page_result(PAGE_URL, html, "zygg").notices
    assert len(notices) == 1
    assert notices[0].title == "真实公开招标公告"


def test_result_and_correction_with_bid_words_are_not_tenders():
    html = """<ul class='c_list_bid'>
      <li><a href='./gkzb/202609/a.htm'>设备公开招标公告</a>
          <em rel='bxlx'>更正公告</em>发布时间：2026-09-18 地域：广东</li>
      <li><a href='./zbgg/202609/b.htm'>项目公开招标中标公告</a>
          <em rel='bxlx'>中标公告</em>发布时间：2026-09-18 地域：广东</li>
    </ul>"""
    parsed = parse_list_page_result(PAGE_URL, html, "zygg")
    assert parsed.notices == []
    assert parsed.filtered_count == 2
