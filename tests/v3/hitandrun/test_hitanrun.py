"""HitAndRun V3 的公开边界、索引和配置模型合同测试。"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

from app.plugins.hitandrun import HitAndRun
from app.plugins.hitandrun.entities import TaskType, TorrentTask
from app.plugins.hitandrun.helper import SiteMatchHelper, SiteTorrentHelper, TaskTypeHelper, TorrentHelper
from app.plugins.hitandrun.hnrconfig import HNRConfig


REPO_ROOT = Path(__file__).parents[3]
PLUGIN_SOURCE = REPO_ROOT / "plugins.v3/hitandrun/__init__.py"


def test_v3_source_uses_public_boundaries_and_relative_modules():
    """V3 实现不依赖旧兼容路径或插件包的宿主内部导入。"""
    source = PLUGIN_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(PLUGIN_SOURCE))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.network",
        "app.sdk.plugins",
        "app.sdk.services",
        "app.sdk.utilities",
    } <= imported_modules
    assert "app.db.oper.site" in imported_modules
    assert not any(
        module.startswith(("app.compat", "app.core", "app.helper", "app.log", "app.utils", "app.db.models"))
        for module in imported_modules
    )
    assert "app.plugins.hitandrun" not in source


def test_v3_metadata_and_empty_capabilities_are_explicit():
    """V3 版本索引与插件元数据一致，空能力直接返回空列表。"""
    package_v3 = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    package_v2 = json.loads((REPO_ROOT / "package.v2.json").read_text(encoding="utf-8"))

    assert HitAndRun.plugin_version == "2.2.0"
    assert package_v3["HitAndRun"]["version"] == HitAndRun.plugin_version
    assert package_v3["HitAndRun"]["system_version"] == ">=3.0.0"
    assert package_v2["HitAndRun"]["v3"] is False
    assert HitAndRun.get_command() == []
    assert HitAndRun().get_api() == []


def test_v3_supports_pydantic_v2_model_serialization():
    """配置模型可在 Pydantic V2 下完成 JSON 序列化。"""
    config = HNRConfig()

    assert config.model_dump()
    assert config.model_dump_json()


def test_task_type_helper_resolves_download_source():
    """任务类型按主程序下载来源解析，不再依赖种子详情猜测。"""
    rss_context = SimpleNamespace(resource_source="rss")
    spider_context = SimpleNamespace(resource_source="spider")
    search_context = SimpleNamespace(resource_source="search")
    unknown_context = SimpleNamespace(resource_source="unknown")

    assert TaskTypeHelper.resolve(context=rss_context) == TaskType.RSS_SUBSCRIBE
    assert TaskTypeHelper.resolve(context=spider_context) == TaskType.AUTO_SUBSCRIBE
    assert TaskTypeHelper.resolve(context=search_context) == TaskType.NORMAL
    # 来源为 RSS 订阅时，即使携带副标题也应识别为 RSS 订阅
    assert TaskTypeHelper.resolve(context=rss_context, torrent_info=SimpleNamespace(description="副标题")) \
        == TaskType.RSS_SUBSCRIBE
    # 订阅来源但无法区分 RSS / 站点订阅时，按事件来源前缀处理
    assert TaskTypeHelper.resolve(context=unknown_context, source="Subscribe|{\"id\": 1}") \
        == TaskType.AUTO_SUBSCRIBE
    # 自定义订阅插件（RssSubscribe）的下载不携带来源标识，按下载用户名识别
    assert TaskTypeHelper.resolve(context=unknown_context, username="RSS订阅") == TaskType.RSS_SUBSCRIBE
    # 事件带有来源时，即使下载用户名匹配也不按自定义订阅识别（避免同名登录用户手动下载被误判）
    assert TaskTypeHelper.resolve(context=unknown_context, source="Manual", username="RSS订阅",
                                  torrent_info=SimpleNamespace(description="副标题")) == TaskType.NORMAL
    # 用户名不匹配时不影响兜底判断
    assert TaskTypeHelper.resolve(context=unknown_context, username="其他用户",
                                  torrent_info=SimpleNamespace(description="副标题")) == TaskType.NORMAL
    # 旧版事件无来源信息时，回退到种子详情兜底判断
    assert TaskTypeHelper.resolve(torrent_info=SimpleNamespace(description="")) == TaskType.RSS_SUBSCRIBE
    assert TaskTypeHelper.resolve(torrent_info=SimpleNamespace(description="副标题")) == TaskType.NORMAL


def test_site_torrent_helper_matches_by_resource_identity():
    """站点检索结果按页面链接、下载链接或标题+大小与种子任务匹配。"""
    torrent_task = TorrentTask(
        site=1,
        site_name="ExampleSite",
        title="Some.Movie.2024",
        enclosure="https://example.com/download/123",
        page_url="https://example.com/detail/123",
        size=123456789.0,
    )

    by_page_url = SimpleNamespace(
        title="Some.Movie.2024",
        enclosure="https://example.com/download/123",
        page_url="https://example.com/detail/123",
        size=123456789.0,
    )
    by_enclosure = SimpleNamespace(
        title="Some.Movie.2024",
        enclosure="https://example.com/download/123",
        page_url="https://example.com/detail/456",
        size=123456789.0,
    )
    by_title_and_size = SimpleNamespace(
        title="Some.Movie.2024",
        enclosure="",
        page_url="",
        size=123456789.0,
    )
    different_size = SimpleNamespace(
        title="Some.Movie.2024",
        enclosure="",
        page_url="",
        size=987654321.0,
    )

    assert SiteTorrentHelper.match_torrent(torrent_task, [by_page_url]) is by_page_url
    assert SiteTorrentHelper.match_torrent(torrent_task, [by_enclosure]) is by_enclosure
    assert SiteTorrentHelper.match_torrent(torrent_task, [by_title_and_size]) is by_title_and_size
    assert SiteTorrentHelper.match_torrent(torrent_task, [different_size]) is None
    assert SiteTorrentHelper.match_torrent(torrent_task, []) is None
    assert SiteTorrentHelper.match_torrent(None, [by_page_url]) is None


def test_probe_config_and_probe_call_are_present():
    """RSS 订阅适配：探测配置项、站点检索与回填逻辑均已接入。"""
    config = HNRConfig()

    assert config.probe_hr_for_subscribe is True

    source = PLUGIN_SOURCE.read_text(encoding="utf-8")
    assert "TaskTypeHelper.resolve(context=context" in source
    assert "self.__probe_hit_and_run(torrent_task=torrent_task)" in source
    assert "SiteTorrentHelper.match_torrent" in source
    assert "search_torrents" in source
    # 站点检索带超时保护，避免长时间占用事件全局锁
    assert "PROBE_SEARCH_TIMEOUT_SECONDS" in source
    assert "FutureTimeoutError" in source
    # 自定义订阅适配：站点信息回填调用均已接入
    assert "self.__backfill_site_from_url(torrent_task=torrent_task)" in source
    assert "SiteMatchHelper.match_site_by_url" in source


def test_site_match_helper_matches_site_by_url():
    """自定义订阅适配：按资源链接域名反查已配置站点，未匹配时返回空。"""
    import app.plugins.hitandrun.helper as hitandrun_helper

    fake_sites = {
        "example.com": {"id": 1, "name": "ExampleSite"},
    }

    class _FakeSitesHelper:
        def get_indexer(self, domain):
            return fake_sites.get(domain)

    original = hitandrun_helper.siteshelper
    hitandrun_helper.siteshelper = _FakeSitesHelper()
    try:
        assert SiteMatchHelper.match_site_by_url("https://example.com/details.php?id=1") == (1, "ExampleSite")
        assert SiteMatchHelper.match_site_by_url("https://example.com/download.php?id=1&passkey=x") \
            == (1, "ExampleSite")
        assert SiteMatchHelper.match_site_by_url("https://other.com/download/1") == (None, None)
        assert SiteMatchHelper.match_site_by_url("") == (None, None)
        assert SiteMatchHelper.match_site_by_url(None) == (None, None)
    finally:
        hitandrun_helper.siteshelper = original


def test_transmission_hash_prefers_current_rpc_field():
    """Transmission 新字段优先读取，避免 V3 运行时触发旧属性告警。"""
    torrent = SimpleNamespace(hash_string="new-hash", hashString="legacy-hash")

    assert TorrentHelper._get_transmission_hash(torrent) == "new-hash"
