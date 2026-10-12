"""Province-level region normalization aligned with app.regions."""

REGION_NAMES = {
    "110000": "北京", "120000": "天津", "130000": "河北", "140000": "山西",
    "150000": "内蒙古", "210000": "辽宁", "220000": "吉林", "230000": "黑龙江",
    "310000": "上海", "320000": "江苏", "330000": "浙江", "340000": "安徽",
    "350000": "福建", "360000": "江西", "370000": "山东", "410000": "河南",
    "420000": "湖北", "430000": "湖南", "440000": "广东", "450000": "广西",
    "460000": "海南", "500000": "重庆", "510000": "四川", "520000": "贵州",
    "530000": "云南", "540000": "西藏", "610000": "陕西", "620000": "甘肃",
    "630000": "青海", "640000": "宁夏", "650000": "新疆", "710000": "台湾",
    "810000": "香港", "820000": "澳门",
}

# Exact aliases first; never infer a province from a project's title or an
# address embedded in a multi-region string.
ALIASES = {name: code for code, name in REGION_NAMES.items()}
ALIASES.update({
    "内蒙古自治区": "150000", "广西壮族自治区": "450000",
    "西藏自治区": "540000", "宁夏回族自治区": "640000",
    "新疆维吾尔自治区": "650000", "香港特别行政区": "810000",
    "澳门特别行政区": "820000",
})
for _code, _name in REGION_NAMES.items():
    if _name not in {"内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门"}:
        ALIASES[_name + ("市" if _name in {"北京", "天津", "上海", "重庆"} else "省")] = _code


def region_code_for(raw: str | None) -> str | None:
    return ALIASES.get(raw.strip()) if raw else None
