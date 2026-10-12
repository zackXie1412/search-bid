from ccgp_crawler.normalize import canonicalize_url, list_url, parse_published_at


def test_list_page_url_and_category_validation():
    assert list_url("zygg", 0) == "https://www.ccgp.gov.cn/cggg/zygg/index.htm"
    assert list_url("dfgg", 2) == "https://www.ccgp.gov.cn/cggg/dfgg/index_2.htm"


def test_canonical_url():
    source = "https://www.ccgp.gov.cn/cggg/zygg/index.htm"
    assert canonicalize_url(source, "./gkzb/202609/example.htm#main") == (
        "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/example.htm"
    )
    assert canonicalize_url(source, "http://www.ccgp.gov.cn/cggg/dfgg/x.htm#foo") == (
        "https://www.ccgp.gov.cn/cggg/dfgg/x.htm"
    )


def test_date_formats_and_invalid_date():
    assert parse_published_at("发布时间：2026-09-18 17:21") == "2026-09-18T17:21:00+08:00"
    assert parse_published_at("发布时间:2026-09-18 17:21:06") == "2026-09-18T17:21:06+08:00"
    assert parse_published_at("发布时间：2026-09-18") == "2026-09-18T00:00:00+08:00"
    assert parse_published_at("发布时间：2026-13-18 17:21") is None
