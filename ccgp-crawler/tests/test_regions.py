from ccgp_crawler.regions import region_code_for


def test_region_aliases_and_unknown_values():
    assert region_code_for("湖北") == "420000"
    assert region_code_for("湖北省") == "420000"
    assert region_code_for("广西壮族自治区") == "450000"
    assert region_code_for("内蒙古自治区") == "150000"
    assert region_code_for("全国") is None
    assert region_code_for("广东/广西") is None
    assert region_code_for(None) is None
