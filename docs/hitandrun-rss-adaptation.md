# H&R助手 RSS 订阅适配 — 改动与过程总结

> 本文档记录 MoviePilot-Plugins fork（warzook/MoviePilot-Plugins）中 H&R助手插件
> 针对 RSS 订阅场景的三轮改动，含调查结论、实现细节、验证方式与后续可继续修改的点。
> 后续继续改这个插件时，先读「主程序机制」和「三道门」两节，可避免重复排查。

- 编写日期：2026-10-13
- 当前版本：v3 `2.2.0` / v2 `1.7.0`；推送脚本 `v3.2.5`（v3.2.4 后经评审修复，见 §12）
- 改动基线：上游 `InfinityPacer/MoviePilot-Plugins`（本地原始文件与上游 hash 一致，上游未做此适配，全部为从零设计）

---

## 1. 背景与问题

用户需求：「H&R助手没有适配 rss订阅的情况，适配一下」。

排查发现两个独立缺陷：

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | 任务类型靠猜：`TaskType.NORMAL if torrent_info.description else TaskType.RSS_SUBSCRIBE` | 爬虫订阅、搜索下载同样没有 description，被误判为 RSS 订阅 |
| 2 | RSS 报文不携带 `hit_and_run` 标志 | RSS 订阅下载的 H&R 种子完全不被管理（除非站点全局 `hr_active`） |

---

## 2. 主程序机制（调查结论，改插件前必读）

### 2.1 H&R 管理的三道门

一个下载要进入 H&R 管理，顺序过三道检查（均在 `plugins.v*/hitandrun/__init__.py`）：

| 门 | 位置 | 判定 |
|---|---|---|
| ① 站点门 | `__process_torrent_task` 开头 | `torrent_task.site` 必须在 H&R 配置的站点列表内（即该站配了 `site_configs` 规则），否则跳过 |
| ② 类型门 | `TaskTypeHelper.resolve` + `__probe_hit_and_run` 守卫 | 任务类型需为 RSS_SUBSCRIBE / AUTO_SUBSCRIBE 才执行 H&R 探测 |
| ③ H&R 标志门 | `__init_hr_status` | 看 `torrent_info.hit_and_run`；RSS 报文不带此字段 |

### 2.2 主程序提供的来源信号

- `Context.resource_source`（`app/domain/context.py`）：`rss` / `spider` / `search` / `unknown`（默认 unknown）
- 事件 `source`：订阅下载为 `Subscribe|{json}`；`download_single(source=...)` 原样透传
- `DownloadAdded` 事件 payload（`app/chain/download/history.py` 的 `_settle_download_success`）：
  `hash` / `context` / `username` / `downloader` / `episodes` / `source`
- `TorrentsChain.rss()`（主程序自带站点 RSS 订阅）构造的 TorrentInfo 带 `site`/`site_name`/`hit_and_run`，事件有 `Subscribe|` 来源

### 2.3 各类下载路径的字段实况

| 下载路径 | site | hit_and_run | resource_source | 事件 source | username |
|---|---|---|---|---|---|
| 主程序站点 RSS 订阅 | ✅ 有 | ✅ 有 | `rss` | `Subscribe\|...` | — |
| 站点订阅（爬虫） | ✅ 有 | ✅ 有 | `spider` | `Subscribe\|...` | — |
| 搜索下载 | ✅ 有 | ✅ 有 | `search` | `Manual` | MP 登录用户名 |
| 官方 rsssubscribe「下载」动作 | ❌ 无 | ❌ 无 | unknown | None | `RSS订阅`（写死） |
| 推送脚本（`POST /api/v1/download/`） | ✅ 有（脚本自己查的） | ❌ 写死 false | unknown | `Manual`（路由写死） | MP 登录用户名 |

---

## 3. 第一轮改动：站点 RSS 订阅适配（v3 2.1.0 / v2 1.6.0）

### 3.1 `helper.py`：新增两个 Helper

**`TaskTypeHelper.resolve(context, torrent_info, source, username)`** —— 按真实来源解析任务类型，替换原来的 description 猜测：

```python
# 判定顺序（前面的命中即返回）：
# 1. resource_source == "rss"     → RSS_SUBSCRIBE
# 2. resource_source == "spider"  → AUTO_SUBSCRIBE
# 3. resource_source == "search"  → NORMAL
# 4. source 以 "Subscribe|" 开头   → AUTO_SUBSCRIBE
# 5. username == "RSS订阅"          → RSS_SUBSCRIBE   （第二轮加的，见 §4）
# 6. 兜底（旧版事件无任何来源）：有 description → NORMAL，否则 → RSS_SUBSCRIBE
```

**`SiteTorrentHelper.match_torrent(torrent_task, torrents)`** —— 站点检索结果与本地任务按资源身份匹配，三级优先级：
1. `page_url` 一致
2. `enclosure` 一致
3. `title` + `size` 均一致（RSS 报文携带 size）

### 3.2 `hnrconfig.py`：新增配置项

```python
probe_hr_for_subscribe: Optional[bool] = True  # 订阅来源种子H&R探测（RSS报文不携带H&R标志）
```

配置页对应「订阅H&R探测」开关（默认开启）；关闭仅跳过检索探测，不影响站点域名回填。

### 3.3 `__init__.py`：接入探测回填

- `handle_download_added_event`：改为调用 `TaskTypeHelper.resolve(context=..., torrent_info=..., source=...)`
- `__process_torrent_task`：在 `__init_hr_status` 前插入 `self.__probe_hit_and_run(torrent_task=torrent_task)`
- 新增方法：
  - `__probe_hit_and_run`：守卫条件 = 配置开启 & 未标记 H&R & 任务类型 ∈ {RSS_SUBSCRIBE, AUTO_SUBSCRIBE} & 站点未全局 `hr_active` & 有 site+title；命中后回填 `hit_and_run` 和缺失的 `description`
  - `__get_indexer_module`：懒加载 `ModuleManager().get_running_module("IndexerModule")`（v3: `app.application.module`；v2: `app.core.module`），异常返回 None
  - `__get_site_by_id`：`SitesHelper` 无按 id 查询接口，遍历 `get_indexers()`
  - `__search_site_torrents`：`indexer_module.search_torrents(site=site, keyword=keyword)`
- 所有探测异常只记日志，**绝不阻塞下载处理**

---

## 4. 第二轮改动：方案 A — 自定义订阅（RssSubscribe）适配（v3 2.2.0 / v2 1.7.0）

### 4.1 问题

官方 rsssubscribe 插件动作选「下载」时：`TorrentInfo` 不带 site（门①挂）、无来源标识且 description 非空导致判 NORMAL（门②挂，probe 不跑）。

### 4.2 方案选型

- **路线 A（已实施）**：只改 H&R，3 处改动，域名匹配前提见 §7
- 路线 B：patch rsssubscribe 显式带 site/resource_source（需维护第二个插件 fork，未实施）

### 4.3 改动内容

**① `helper.py` — `TaskTypeHelper.resolve` 加 username 分支**

```python
if username and str(username).strip() == TaskTypeHelper.RSS_SUBSCRIBE_USERNAME:
    # 自定义订阅插件（RssSubscribe）的下载不携带来源标识，按下载用户名识别
    return TaskType.RSS_SUBSCRIBE
```

`RSS_SUBSCRIBE_USERNAME = "RSS订阅"`（rsssubscribe 里写死的字符串，属隐性耦合）。

**② `helper.py` — 新增 `SiteMatchHelper.match_site_by_url(url)`**

```python
domain = StringUtils.get_url_domain(url)
site = siteshelper.get_indexer(domain)
return site.get("id"), site.get("name")   # 未匹配返回 (None, None)
```

与 H&R 原有的 tracker 反查站点是同一套调用。

**③ `__init__.py` — 接入**

- `handle_download_added_event`：读 `event.event_data["username"]` 传入 resolve
- `__process_torrent_task`：站点门检查前插入 `self.__backfill_site_from_url(torrent_task=torrent_task)`
- 新增 `__backfill_site_from_url`：site 为空时按 `page_url` → `enclosure` 顺序域名反查回填 site/site_name；已有 site 不覆盖；异常只记日志

---

## 5. 第三轮改动：推送脚本 H&R 回填（v3.2.3 → v3.2.4）

脚本：`~/Downloads/mp_seed_push_v3.user.js`（GreasyFork 599837 本地副本），
原版备份：`~/Downloads/mp_seed_push_v3.v3.2.3.bak.user.js`。

### 5.1 问题

脚本走 `POST /api/v1/download/`（主程序路由 `source="Manual"`），载荷里 `hit_and_run` 写死 false，
非全局 HR 站点下载的 H&R 种子不被管理。脚本自带的 site 信息是真的，差的只是 H&R 标志。

### 5.2 改动内容

在 `downloadTorrent` 之前插入三个函数（约 1700 行处）：

| 函数 | 作用 |
|---|---|
| `extractTorrentId(rawUrl)` | 从详情页 URL 提取资源 id：`id`/`torrentid`/`tid` 查询参数优先，其次 `/detail/<id>` 末级路径；空值返回空串 |
| `matchSiteTorrent(list, pageUrl, title)` | 按资源 id 匹配检索结果，标题一致兜底，确保回填的是当前这个种子 |
| `fetchSiteTorrentInfo(siteId, keyword, token)` | 检索站点索引：主接口 `GET /api/v1/site/resource/{siteId}?keyword=`（管理权限，返回带权威 `hit_and_run` 的 TorrentInfo），失败回退 `GET /api/v1/search/title?keyword=&sites=`；V3 信封/V2 直出双兼容；5 分钟内存缓存（v3.2.5 起仅缓存成功结果，失败可立即重试） |

`downloadTorrent` 主体重构：拿到 siteData 后先 `fetchSiteTorrentInfo` → `matchSiteTorrent` → 回填 `hit_and_run`；
检索失败不阻塞，按旧行为 false 继续；toast 显示「H&R 状态：是/否/未能确认」。

### 5.3 修过的 bug

`extractTorrentId` 初版在 `page_url` 为空时产出 `'p:null'`，两个空 page_url 会互相误匹配 → 空值返回空串 + 路径段限定字母数字。

---

## 6. 改动文件清单

仓库 10 个文件（`git diff --stat` 约 654 增 / 59 删）：

| 文件 | 内容 |
|---|---|
| `plugins.v3/hitandrun/helper.py` | TaskTypeHelper.resolve（含 username）、SiteTorrentHelper、SiteMatchHelper |
| `plugins.v3/hitandrun/hnrconfig.py` | `probe_hr_for_subscribe` 配置项 |
| `plugins.v3/hitandrun/__init__.py` | 事件接入、probe/backfill 及辅助方法、版本 2.2.0、配置页开关与提示文案 |
| `plugins.v2/hitandrun/*.py` | 以上三项镜像（v2 用 `app.core.*` 布局，版本 1.7.0） |
| `package.v3.json` / `package.v2.json` | version + history 条目 |
| `tests/v3/hitandrun/test_hitanrun.py` | 版本断言、来源解析、SiteMatchHelper、探测接入断言 |
| `README.md` | H&R助手 段落改为已适配说明 |

仓库外 1 个文件：

| 文件 | 内容 |
|---|---|
| `~/Downloads/mp_seed_push_v3.user.js` | v3.2.4，H&R 回填（§5） |

---

## 7. 已知限制与生效前提

### 生效前提（三条同时满足才触发）

1. 该站点在 H&R 配置里配了 `site_configs` 规则（门①是硬检查）
2. **域名能匹配上**：RSS 地址/详情页域名 = 主程序配置的站点域名。RSSHub、中转、代理域名匹配不到，仍不触发
3. `IndexerModule` 在运行（probe 检索依赖它）

### 未覆盖 / 遗留

- **rsssubscribe「订阅」动作**（走 SubscribeChain）：已天然覆盖（事件带 `Subscribe|`、TorrentInfo 带站点信息），无需改
- 脚本的 `uploadvolumefactor`/`downloadvolumefactor`/`pubdate` 仍是硬编码值（超出 H&R 范围，未动）
- `username == "RSS订阅"` 是与官方 rsssubscribe 的隐性耦合，对方改字符串即失效；更稳的特征判定（如「resource_source 为空 + 无 Subscribe| + 带 enclosure」）未实施
- 主程序 `TorrentInfo` 若补上自定义 RSS 的站点字段，门①的域名反查即可简化（路线 B/C 的基础）

---

## 8. 后续可继续修改的方向

| 方向 | 说明 | 代价 |
|---|---|---|
| 路线 B | fork rsssubscribe，构造 TorrentInfo 时显式带 `site`/`site_name`/`resource_source="rss"` | 中，需维护第二个 fork，但彻底摆脱域名匹配限制 |
| 路线 C | 把路线 B 代码提进 MoviePilot 主程序 fork，惠及所有用户 | 高 |
| 脚本遗留项 | 修 `uploadvolumefactor` 等硬编码；站点检索关键词过长时换 page_url 兜底检索 | 低 |
| 特征判定替代 | 用「带 enclosure + 无来源标识」替代 `username=="RSS订阅"` 硬编码 | 低 |
| 测试补强 | 仓库 pytest 需同级 `../MoviePilot` 后端，本机缺失；当前用 stub 环境验证（见 §9） | — |

---

## 9. 本地验证环境（无后端时如何继续改）

仓库 pytest 依赖同级 MoviePilot 后端源码，本机没有。验证方式是搭 stub 后端：

```
/tmp/hnr_stub/        # v3 stub：app/sdk、app/application/module、app/db/oper/site、app/modules、app/plugins/hitandrun（symlink）
/tmp/hnr_stub_v2/     # v2 stub：app/core、app/helper、app/log、app/utils、app/db、app/modules、app/plugins/hitandrun（symlink）
```

- `smoke_test.py`：直接执行插件逻辑（TaskTypeHelper.resolve / SiteTorrentHelper / SiteMatchHelper / probe 端到端 / backfill 端到端），v3 45 项、v2 35 项断言
- `run_contract_tests.py`：把 `tests/v3/hitandrun/test_hitanrun.py` 的用例在 stub 环境逐条执行（自带 patch/finally，不依赖 pytest fixture）
- 脚本逻辑测试：`/tmp/mp_script_test.js`（Node，桩掉 GM_xmlhttpRequest），15 项

上一轮验证结果（全部通过）：

- v3 冒烟 45 项 / v2 冒烟 35 项 / 合同测试 8 项 / 脚本 Node 测试 15 项
- 全部改动文件 `py_compile` 通过；两个 `package.*.json` JSON 合法
- 注意：stub 目录在 /tmp，重启后会丢，需要时按上面的结构重建（或从 transcript 恢复）

---

## 10. 主程序接口速查（改脚本/改插件会用到）

| 接口 | 用途 | 权限 |
|---|---|---|
| `GET /api/v1/site/resource/{site_id}?keyword=&page=` | 浏览站点资源，返回带权威 `hit_and_run` 的 TorrentInfo | 管理 |
| `GET /api/v1/search/title?keyword=&sites=` | 按标题搜索，返回 Context 列表（内含 torrent_info） | 登录 |
| `GET /api/v1/site/domain/{域名}` | 拿站点 id/name/cookie/proxy | 登录 |
| `POST /api/v1/download/` | 手动下载（`source="Manual"` 写死） | 登录 |
| `IndexerModule.search_torrents(site, keyword, ...)` | 插件侧站点检索（H&R probe 用） | 内部 |

SDK 字段：`TorrentInfo`（`site: int`、`site_name: str`、`hit_and_run: bool`、`page_url`、`enclosure`、`description`、`size`）；
`Context.resource_source` 可直接传入。

---

## 11. 累计补丁

- `/tmp/hitandrun-rss-adaptation.patch` —— 第一轮 RSS 适配（10 文件）
- `/tmp/hitandrun-rsssubscribe-adaptation.patch` —— 第一轮 + 第二轮累计（10 文件，含 §4 全部内容）
- 脚本改动 diff：`diff -u ~/Downloads/mp_seed_push_v3.v3.2.3.bak.user.js ~/Downloads/mp_seed_push_v3.user.js`

> /tmp 补丁重启会丢。仓库内改动未提交（`git status` 可见 10 个 M），建议尽快 commit；
> 提交时 git 身份按 remote 自动切换（GitHub → warzook，见用户级 MEMORY.md 约定）。

---

## 12. Review 修复轮（评审问题修正，插件版本号不变 / 脚本 v3.2.4 → v3.2.5）

| # | 问题 | 修复 |
|---|---|---|
| 1 | 探测检索（站点网络请求）在事件全局锁内执行，站点卡死会阻塞后续下载事件 | `__search_site_torrents` 改为线程池 + 15s 超时（类常量 `PROBE_SEARCH_TIMEOUT_SECONDS`），超时记警告并放弃本次探测 |
| 2 | `username == "RSS订阅"` 存在误判面：同名登录用户的手动下载可能被误判为订阅来源 | username 分支收紧为「事件无 `source`」时才生效（手动下载事件 `source=Manual`，不再命中） |
| 3 | 「订阅H&R探测」开关文案暗示关闭后跳过全部探测逻辑，实际域名回填不受影响 | 配置页 hint 改为「关闭仅跳过检索探测，不影响站点域名回填」，并注明默认开启、检索带超时保护 |
| 4 | 脚本 toast「未能确认」分支是死代码（`pushDownload` 只传 true/false） | 未匹配/检索失败/无站点 id 时改传 `null`，三态提示生效；提交载荷仍为 `Boolean(null)=false`，与旧行为一致 |
| 5 | 脚本缓存了 null 检索结果，站点抖动后 5 分钟内推送都回落 false 不重试 | `finish` 仅缓存成功结果，失败不写缓存可立即重试 |
| 6 | `probe_hr_for_subscribe` 默认 True 属默认开启的行为变更，未对用户明示 | package history、README、配置页 hint 均明示默认开启 |

测试同步：`tests/v3/hitandrun/test_hitanrun.py` 新增「带 `source` 时 username 分支不生效」断言与超时保护接入断言（`PROBE_SEARCH_TIMEOUT_SECONDS` / `FutureTimeoutError`）。

遗留（评审中确认可暂不处理）：`SiteTorrentHelper` 的 title+size 为精确浮点匹配，失配方向是保守不管理，安全；若实测漏管再考虑 ±1% 容差。
