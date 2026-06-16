# AI News Agent

本地 AI 信息简报 Agent：每天抓取 AI 相关新闻、论文、GitHub 项目和行业信息，筛选后生成 HTML，并可推送到企业微信。

## 1. 初始化

```bash
cd /Users/mac372/Desktop/beifeng/ai-news-agent
cp config.example.json config.json
cp .env.example .env
```

编辑 `.env`：

```bash
# OpenAI 或 OpenAI-compatible 服务的 API Key。
# 为空时不会调用模型，日报会使用本地规则生成短摘要。
OPENAI_API_KEY=你的模型服务 API Key

# 用于总结、筛选和打分的模型名称。
OPENAI_MODEL=gpt-5.5

# 模型服务基础地址，不含最后的 /responses。
OPENAI_BASE_URL=https://api.openai.com/v1

# 可选：完整覆盖 Responses API endpoint。
# 填了它会优先使用；不填则使用 OPENAI_BASE_URL + /responses。
OPENAI_RESPONSES_URL=

# 企业微信群机器人 Webhook，用于推送日报。
WECOM_WEBHOOK_URL=企业微信群机器人 Webhook

# NewsAPI.org 的 API Key。可选。
NEWSAPI_KEY=可选，NewsAPI key

# GitHub token。可选但建议配置，用于抓项目最近 commits/releases。
GITHUB_TOKEN=可选，GitHub Personal Access Token
```

编辑 `config.json`：

```json
"push": {
  "wecom": {
    "enabled": true,
    "mode": "file"
  }
}
```

## 2. 本地运行

```bash
python3 src/daily_ai_news.py --config config.json --env .env
```

输出目录：

```text
/Users/mac372/Desktop/beifeng/ai-news-agent/output/YYYY-MM-DD.html
```

默认只生成 HTML。需要调试筛选结果时，可以把 `config.json` 里的 `output.write_json` 改为 `true`，额外生成 JSON。

## 3. 运行方式

脚本入口是：

```bash
python3 src/daily_ai_news.py --config config.json --env .env
```

运行时会按这个顺序执行：

1. 读取 `.env`，加载 OpenAI、企业微信、NewsAPI 等密钥。
2. 读取 `config.json`，拿到 RSS、arXiv、GitHub、NewsAPI、推送方式等配置。
3. 抓取信息源并去重。
4. 按关键词、时间、来源类型打分筛选。
5. 如果 `.env` 里有有效 `OPENAI_API_KEY`，调用 OpenAI Responses API 生成中文摘要、重要性评分和价值判断。
6. 如果没有有效 `OPENAI_API_KEY`，使用本地规则生成短摘要兜底。
7. 写入 `output/YYYY-MM-DD.html`。如果 `output.write_json=true`，额外写入 `output/YYYY-MM-DD.json` 供调试复盘。
8. 如果开启企业微信推送，则发送 markdown、news 卡片或 HTML 文件。

只生成本地文件、不推送：

```bash
python3 src/daily_ai_news.py --config config.json --env .env --no-push
```

## 4. 模型配置

模型在 `.env` 里配置：

```bash
OPENAI_API_KEY=你的模型服务 API Key
OPENAI_MODEL=gpt-5.5
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_RESPONSES_URL=
```

这几个变量的含义：

- `OPENAI_API_KEY`：模型服务密钥。为空时不会调用模型，会走本地规则摘要。
- `OPENAI_MODEL`：模型名。官方 OpenAI 可以用 `gpt-5.5`；第三方网关要填它支持的模型名。
- `OPENAI_BASE_URL`：模型服务基础地址，例如 `https://api.openai.com/v1`。
- `OPENAI_RESPONSES_URL`：完整 Responses API 地址。一般留空；如果你的服务路径不是 `{base}/responses`，再填写它。
- `GITHUB_TOKEN`：GitHub Personal Access Token。建议配置，否则匿名 API 容易 rate limit，影响 GitHub 项目的当日提交/发布信息抓取。

默认请求地址是：

```text
OPENAI_BASE_URL + /responses
```

例如官方 OpenAI：

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_RESPONSES_URL=
```

最终会请求：

```text
https://api.openai.com/v1/responses
```

如果你的模型服务不是 Responses API，而是只支持 Chat Completions `/chat/completions`，当前代码还不能直接用，需要再加一个 Chat Completions 适配分支。

## 5. 企业微信推送模式

支持三种模式：

- `markdown`：群里直接显示今日摘要和重点链接，最稳定；受企业微信 4096 字节限制，只适合预览，不适合放完整日报。
- `news`：群里显示图文卡片，点击跳转 HTML 链接。需要 `public_base_url` 配成企业微信可访问的地址。
- `file`：先发一条短摘要，再上传完整 HTML 文件。企业微信里通常显示为文件，不是聊天框原生网页预览，但能拿到完整内容。

企业微信聊天框里想“可预览”，建议用：

```json
"public_base_url": "https://你的域名或内网地址/ai-news-agent/output",
"push": {
  "wecom": {
    "enabled": true,
    "mode": "news"
  }
}
```

如果没有可访问 URL，建议用 `file`；如果有可访问 URL，建议用 `news`。

## 6. 每天 10 点定时

macOS 推荐使用 `launchd`：

```bash
./scripts/enable_schedule.sh
```

关闭定时任务：

```bash
./scripts/disable_schedule.sh
```

查看日志：

```bash
tail -f /Users/mac372/Desktop/beifeng/ai-news-agent/logs/launchd.out.log
tail -f /Users/mac372/Desktop/beifeng/ai-news-agent/logs/launchd.err.log
```

## 7. 信息源说明

第一版已支持：

- RSS / RSSHub
- 官方模型厂商页面：OpenAI、Anthropic、Google Gemini/DeepMind、Mistral、Meta AI、Cohere、xAI、Qwen、DeepSeek、Moonshot、智谱、MiniMax、腾讯混元、百度文心等
- arXiv
- GitHub Search
- NewsAPI

公众号和 X 更适合后续通过 RSSHub、授权 API 或你自己的订阅源接入。直接爬取不稳定，也容易遇到平台限制。

`candidate_kind_limits` 和 `report_kind_limits` 用来避免某一类来源刷屏。例如 GitHub 项目更新频率很高，默认只让它占日报的一部分。
