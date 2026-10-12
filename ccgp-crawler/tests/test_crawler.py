import io
import json

import httpx
import pytest

from ccgp_crawler.crawler import CrawlOptions, CrawlStopped, crawl
from ccgp_crawler.local_store import get_checkpoint, open_database


def html_page(index: int) -> str:
    return f"""
        <ul class='c_list_bid'>
          <li><a href='./gkzb/202609/t20260918_2735671{index}.htm' title='第{index}条公开招标公告'>短标题</a>
              <em rel='bxlx'>公开招标</em> 发布时间：<em>2026-09-18 17:21</em>
              地域：<em>湖北</em></li>
        </ul>
    """


def mock_client(responses: list[tuple[int, str]], urls: list[str]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        status, body = responses.pop(0)
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_three_pages_are_bounded_and_resume_from_checkpoint(tmp_path):
    urls: list[str] = []
    db_path = tmp_path / "crawler.sqlite3"
    with open_database(db_path) as db:
        with mock_client([(200, html_page(i)) for i in range(3)], urls) as client:
            assert crawl(
                db,
                CrawlOptions("zygg", start_page=0, max_pages=3, delay_min=0, delay_max=0),
                client=client,
                output=io.StringIO(),
                sleep=lambda _: None,
            ) == 3
        assert len(urls) == 3
        assert urls[0].endswith("/index.htm")
        assert urls[-1].endswith("/index_2.htm")
        assert get_checkpoint(db, "zygg")["next_page"] == 3
        assert db.execute("SELECT count(*) FROM pending_notices").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM crawl_page_runs").fetchone()[0] == 3

    with open_database(db_path) as db:
        with mock_client([(200, html_page(3))], urls) as client:
            assert crawl(
                db,
                CrawlOptions("zygg", resume=True, max_pages=1),
                client=client,
                output=io.StringIO(),
            ) == 1
        assert urls[-1].endswith("/index_3.htm")
        assert get_checkpoint(db, "zygg")["next_page"] == 4


def test_dry_run_prints_region_fields_without_notice_type():
    urls: list[str] = []
    output = io.StringIO()
    with mock_client([(200, html_page(0))], urls) as client:
        assert crawl(None, CrawlOptions("zygg", max_pages=1, dry_run=True), client=client, output=output) == 1
    row = json.loads(output.getvalue())
    assert set(row) == {"canonical_url", "title", "published_at", "region_raw", "region_code", "category", "source_list_url"}
    assert row["region_code"] == "420000"


def test_soft_block_stops_without_retry_and_persists_cooldown(tmp_path):
    urls: list[str] = []
    with open_database(tmp_path / "crawler.sqlite3") as db:
        with mock_client([(200, "您的访问过于频繁，请稍后再试")], urls) as client:
            with pytest.raises(CrawlStopped, match="soft block"):
                crawl(db, CrawlOptions("dfgg", max_pages=3), client=client, output=io.StringIO())
        assert len(urls) == 1
        assert get_checkpoint(db, "dfgg")["status"] == "blocked"
        assert get_checkpoint(db, "dfgg")["next_page"] == 0
        assert db.execute("SELECT blocked FROM crawl_page_runs").fetchone()[0] == 1
        assert db.execute("SELECT blocked_count FROM domain_cooldown").fetchone()[0] == 1

        with mock_client([(200, html_page(0))], urls) as client:
            with pytest.raises(CrawlStopped, match="cooling down"):
                crawl(db, CrawlOptions("dfgg", resume=True), client=client, output=io.StringIO())
        assert len(urls) == 1


def test_empty_or_repeated_page_stops_and_keeps_last_success(tmp_path):
    urls: list[str] = []
    with open_database(tmp_path / "crawler.sqlite3") as db:
        with mock_client([(200, html_page(0)), (200, html_page(0))], urls) as client:
            with pytest.raises(CrawlStopped, match="same notice URLs"):
                crawl(
                    db,
                    CrawlOptions("zygg", max_pages=3, delay_min=0, delay_max=0),
                    client=client,
                    output=io.StringIO(),
                    sleep=lambda _: None,
                )
        assert len(urls) == 2
        assert get_checkpoint(db, "zygg")["next_page"] == 1
        assert db.execute("SELECT count(*) FROM pending_notices").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM crawl_page_runs").fetchone()[0] == 2


def test_parse_errors_save_page_artifact_without_expanding_payload(tmp_path):
    urls: list[str] = []
    html = """
      <ul class='c_list_bid'>
        <li><a href='./gkzb/202609/t20260918_123456.htm'>valid公开招标公告</a>
            发布时间：2026-09-18 10:00</li>
        <li><a href='./gkzb/202609/t20260918_123457.htm'>missing date</a>
            <em rel='bxlx'>公开招标</em></li>
      </ul>
    """
    failure_dir = tmp_path / "html_failures"
    with open_database(tmp_path / "crawler.sqlite3") as db:
        with mock_client([(200, html)], urls) as client:
            assert crawl(
                db,
                CrawlOptions(
                    "zygg",
                    max_pages=1,
                    failure_directory=failure_dir,
                ),
                client=client,
                output=io.StringIO(),
            ) == 1
        assert (failure_dir / "zygg_page_0.html").read_text() == html
        row = db.execute(
            "SELECT canonical_url,title,published_at,region_raw,region_code,category,source_list_url FROM pending_notices"
        ).fetchone()
        assert row is not None
        assert len(row) == 7


def test_all_filtered_page_advances_checkpoint(tmp_path):
    urls: list[str] = []
    filtered = """<ul class='c_list_bid'><li>
      <a href='./zbgg/202609/a.htm'>中标公告</a><em rel='bxlx'>中标公告</em>
      发布时间：2026-09-18 10:00 地域：湖北</li></ul>"""
    with open_database(tmp_path / "crawler.sqlite3") as db:
        with mock_client([(200, filtered), (200, html_page(1))], urls) as client:
            assert crawl(db, CrawlOptions("zygg", max_pages=2, delay_min=0, delay_max=0),
                         client=client, output=io.StringIO(), sleep=lambda _: None) == 2
        assert get_checkpoint(db, "zygg")["next_page"] == 2
        assert db.execute("SELECT count(*) FROM pending_notices").fetchone()[0] == 1
        assert db.execute(
            "SELECT filtered_count FROM crawl_page_runs WHERE page_no=0"
        ).fetchone()[0] == 1
