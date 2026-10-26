# 上海临时活动（shanghai-events-demo）

一个"现在 / 本周末上海有什么临时活动"的静态站点：展览、市集、快闪店、美食节。
每日自动抓取公开信息源 → AI 结构化 → 去重 → 时效判断 → 发布到 GitHub Pages。

- 线上：https://guoiijj.github.io/shanghai-events-demo/
- 数据：`data.json`（在架活动）、`meta.json`（更新时间与计数）
- 运营后台：`ops/`（Python，零第三方依赖）

## 目录

```
index.html          站点（原生 JS，读取 data.json / meta.json）
data.json           发布数据（脚本生成，不要手改）
meta.json           更新时间、在架数量
ops/
  manage.py         运营入口（run / publish / stats / review / prune / hide / dedupe / import-legacy）
  sources.json      信息源注册表（改这里加源，不改代码）
  fetchers.py       抓取层（本地宝列表页 / 活动接口 / 资讯列表）
  extract.py        LLM 抽取（DeepSeek）
  enrich.py         回原文补价格、营业时间、地址
  store.py          主库、去重合并、时效状态、归档
  publish.py        生成 data.json / meta.json，并通过 GitHub API 推送
  state/            events.json 主库、archive.json 归档、seen_urls.json 抓取记录
  reports/          每日运营日报（Markdown）
  logs/             定时任务日志
  run_daily.cmd     Windows 计划任务入口
运营手册.md         每日/每周/每月运营动作
```

## 常用命令

```bash
cd ops

python manage.py stats                  # 看库里有多少、状态分布
python manage.py review                 # 需要人工确认的条目
python manage.py run --publish --push   # 抓取→抽取→合并→发布→推 GitHub
python manage.py publish --push         # 只重新发布
python manage.py dedupe                 # 全库去重
python manage.py prune --days 45        # 归档已结束超过 45 天的
python manage.py hide <id> --reason "场地取消"   # 人工下架
```

定时任务：Windows 计划任务 `ShanghaiEventsOps`，每天 08:30 执行 `ops/run_daily.cmd`。

## 数据规则

- **只上"还去得成"的**：`end_time` 早于今天 → 不发布；结束超过 45 天 → 进归档。
- **时间不确定的单独放**：`is_time_fuzzy=true` 进「待确认」标签，默认列表不展示。
- **来源可追溯**：每条至少一个 `source_url`，卡片底部直达原文。
- **多源合并**：同一活动按来源 URL / 标题相似度 / 场馆 + 日期重叠合并，来源数越多越靠前。
- **时间取并集**：同一活动多次抓取，起止区间取并集，避免把长展期缩成一个子活动。

## 环境

- Python 3.11+（脚本只用标准库），Windows 上固定用 `C:\Users\15647\anaconda3\python.exe`。
- 抽取模型：环境变量 `DEEPSEEK_API_KEY`（或 `OPENAI_API_KEY`）+ `OPENAI_BASE_URL`，默认 `deepseek-chat`。
- 发布：本机 `git` 到 github.com:443 不通，改用 `gh api`（Git Data API）提交，仓库需已 `gh auth login`。
- 本地 `webapp/` 只是工作副本，不要用 `git push`；所有发布走 `ops/publish.py`。
