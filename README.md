# 城市临时活动（全国版）

一个"现在 / 本周末这个城市有什么临时活动"的静态站点：展览、市集、快闪店、美食节。
覆盖一线 / 新一线 / 二线 / 三线热门共 75 城，按城市拆分数据，自动轮转更新。

- 线上：https://guoiijj.github.io/shanghai-events-demo/?city=sh
- 城市索引：`cities.json`（每城在架数、更新时间）
- 单城数据：`data/{code}.json`（如 `data/bj.json`）
- 运营后台：`ops/`（Python，零第三方依赖）

## 目录

```
index.html            站点（原生 JS，先读 cities.json，再按城市加载 data/{code}.json）
data/*.json           75 个城市的活动数据（脚本生成，不要手改）
cities.json           城市索引：分级、在架数
data.json / meta.json 兼容旧入口，内容 = 上海
ops/
  manage.py           入口：sweep / run / publish / stats / review / cities / hide / dedupe
  cities.json         城市注册表（code/name/tier/cadence/domain）
  sources.json        旧单城源配置（保留给 manual run --city 场景）
  fetchers.py         抓取层：本地宝移动站结构化接口
  extract.py          LLM 抽取（仅在文章源启用时用到）
  enrich.py           回原文补价格/营业时间
  store.py            主库（带 city 字段）、去重、状态、归档
  publish.py          按城市编译 data/*.json + cities.json，并用 GitHub API 推送
  city_probe.py       单城源体检
  rate_test.py        反爬阈值测试
  state/              events.json 主库、city_runs.json 轮转表、seen_urls.json
  reports/            日报（sweep-YYYY-MM-DD.md）
  logs/               计划任务日志
  run_daily.cmd       计划任务入口
运营手册.md           每日/每周/每月运营动作
```

## 常用命令

```bash
cd ops

python manage.py sweep --due --limit 15 --channels auto --gap 15 --publish --push
python manage.py sweep --cities bj,sh,gz --channels zhoumohd --gap 8 --publish
python manage.py run --city bj --enrich 1 --publish
python manage.py stats --city bj
python manage.py cities
python manage.py review --city bj
python manage.py publish --push
```

- `sweep` 是多城日常模式：外层频道、内层城市，把请求摊到 75 个城市域名上。
- `--channels auto` 按星期几轮换两个频道，避免一天把 5 个频道全扫。
- `--due` 按 `cities.json` 里的 cadence（一线 1 天 / 新一线 2 天 / 二线 3 天 / 三线 7 天）挑选到期城市。

## 数据来源与反爬（重要）

来源是本地宝各城市站（`m.{code}.bendibao.com`），同一套频道：

| 频道 | 接口 | 内容 |
|---|---|---|
| zhoumohd | `action=get_data` | 周末活动（量最大） |
| mianfeihd | `action=get_data` | 免费活动 |
| zhanlan | `action=get_all_activity` | 展览 |
| shiji | `action=get_all_activity` | 市集 |
| shilinghuodong | `action=get_all_activity` | 时令活动（花展/银杏等） |

反爬实测：
- 桌面站 `{code}.bendibao.com` 很快触发 302 `/challenge`（拼图验证），**不要用**。
- 移动站 `m.{code}.bendibao.com` 也有 progressive_lock，TTL 约 10 分钟，且**被封后继续请求会续期**。
- 被封时 `http_get` 会抛 `Blocked`，`sweep` 会跳过该城市；一旦触发，最好整体静默 15 分钟以上。
- 因此日常用 `--gap 15` 左右、每次只扫 15 城 × 2 频道，一天分多次跑。

## 数据规则

- 只上"还去得成"的：`end_time` 早于今天不发布；结束超过 45 天归档。
- 时间不确定的单列「待确认」，默认列表不展示。
- 每条都有 `source_url`；同一活动多源合并，来源数越多越靠前。
- 票务演出（演唱会/话剧/音乐会等）不收。
- 去重按城市分组；同 URL 不等于同一活动（汇总帖会抽多条），标题相似度是主判据。

## 环境

- Python 3.11+（只用标准库）；Windows 上计划任务固定用 `C:\Users\15647\anaconda3\python.exe`。
- 发布：本机 `git` 到 github.com:443 不通，用 `gh api`（Git Data API）提交；仓库需已 `gh auth login`。
- 本地 `webapp/` 是工作副本，不要 `git push`；发布走 `ops/publish.py`。
- GitHub Pages 有 CDN 缓存，前端请求已带 `?v=Date.now()`。
