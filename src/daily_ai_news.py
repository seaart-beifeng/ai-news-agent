#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


USER_AGENT = "ai-news-agent/0.1 (+local daily brief)"
WECOM_MARKDOWN_MAX_BYTES = 3900

SELECTION_RULES = [
    "优先一手来源、重大模型/产品发布、AI Agent、AI 工程化（框架/SDK/工具链/部署/推理优化/RAG/向量数据库/评测/可观测性/MLOps）、基础设施、开源项目、研究突破、监管与商业动作。",
    "降低营销稿、重复转载、纯观点、缺少事实依据的信息权重。",
    "不要编造候选列表之外的信息。",
    "summary_zh 必须基于 summary/details 用中文写出内容大纲，覆盖用途、核心能力、适用场景、主要结论和值得继续看的点。",
    "不要限制 summary_zh 字数；内容复杂就写充分，内容简单就写简洁，避免空话。",
    "GitHub 项目必须按固定结构写：项目介绍、当日重要提交、近期版本/发布、关注点。",
    "项目介绍要总结 README 中的项目功能、核心能力、使用场景、技术特点，不要只翻译仓库 description。",
    "当日重要提交必须用列表逐条列出，格式类似：- 2026-06-09 sha：这次提交做了什么、为什么重要。",
    "当日重要提交只能使用 details 中 Important commits 的日期、sha、message；不要把过滤统计、Recent commits 或低信号体验项改写成重要提交。",
    "UI/UX、文案、样式、图标、预览、普通错误提示、文件名冲突、README、格式化、依赖小升级通常不算重要提交，除非明确影响安全、数据正确性、RAG/索引、性能、API 兼容、模型/推理能力、Agent 行为或生产稳定性。",
    "近期版本/发布要基于 Recent releases 总结 release 名称、日期和重点变化。",
    '如果当日提交信息不足，要明确写"未从公开提交信息中识别到明确的当日重要提交"。',
    '如果 GitHub 项目的 first_seen=true，summary_zh 必须以"首次收录项目。"开头，项目介绍可以更充分；如果 first_seen=false，要减少基础介绍，重点写更新变化。',
    "新闻/论文要总结事实、背景、影响和可继续阅读的重点。",
    "AI 工程化内容（如推理框架、部署方案、RAG 架构、评测工具、Agent 框架、SDK 更新、向量数据库、可观测性工具等）对从业者有很高实用价值，选题时应适当倾斜。",
    "why_it_matters 必须用中文说明对 AI 从业者、产品或技术决策的具体意义，不要限制字数。",
    'category 必须使用简短中文标签（如"模型发布"、"开源项目"、"研究论文"、"AI Agent"、"行业动态"、"安全/治理"）。全部条目合计不超过 6 个分类；相近主题必须合并到同一分类，不要每个条目创建独立分类。',
]


@dataclasses.dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    kind: str
    summary: str = ""
    published_at: str = ""
    raw_score: int = 0
    ai_score: int | None = None
    importance: str = "medium"
    reason: str = ""
    category: str = "AI"
    details: str = ""
    first_seen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def local_now(tz_name: str) -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(tz_name))
    except Exception:
        return dt.datetime.now()


def parse_date(value: str) -> dt.datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        pass
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
    return None


def format_date(value: str) -> str:
    parsed = parse_date(value)
    if not parsed:
        return value[:19] if value else ""
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style).*?</\1>", " ", text or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[\[file:[^\]]+\]\]", " ", text, flags=re.I)
    text = re.sub(r"\[\[https?://[^\]]+\]\[([^\]]+)\]\]", r"\1", text, flags=re.I)
    text = re.sub(r"\[\[https?://[^\]]+\]\]", " ", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*#\+(title|author|date|description):.*$", " ", text, flags=re.I | re.M)
    text = re.sub(r"^\s*[-*+]\s+", "- ", text, flags=re.M)
    text = re.sub(r"\|", " ", text)
    text = re.sub(r"\b(file|https?)[:=]\S+", " ", text, flags=re.I)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def compact_sentences(text: str, limit: int) -> str:
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?。！？])\s+", text)
    picked: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = " ".join(picked + [sentence])
        if len(candidate) > limit and picked:
            break
        picked.append(sentence)
        if len(candidate) >= limit * 0.65:
            break
    return truncate(" ".join(picked) or text, limit)


def stable_id(url: str, title: str) -> str:
    key = (url or title).strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def request_json(url: str, headers: dict[str, str] | None = None, timeout: int = 25, retries: int = 2) -> Any:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            if attempt >= retries:
                raise
        except Exception:
            if attempt >= retries:
                raise
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Request failed: {url}")


def request_text(url: str, headers: dict[str, str] | None = None, timeout: int = 25, retries: int = 2) -> str:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return data.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            if attempt >= retries:
                raise
        except Exception:
            if attempt >= retries:
                raise
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Request failed: {url}")


def request_bytes(url: str, headers: dict[str, str] | None = None, timeout: int = 25, retries: int = 2) -> bytes:
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            if attempt >= retries:
                raise
        except Exception:
            if attempt >= retries:
                raise
        time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Request failed: {url}")


def openai_responses_url() -> str:
    explicit = os.environ.get("OPENAI_RESPONSES_URL", "").strip()
    if explicit:
        return explicit
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    return base_url.rstrip("/") + "/responses"


def post_json(url: str, payload: dict[str, Any], timeout: int = 25) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def fetch_rss_feeds(config: dict[str, Any]) -> list[Item]:
    feeds = config.get("rss_feeds", [])
    max_items = int(config.get("max_items_per_source", 20))
    items: list[Item] = []
    for feed in feeds:
        name = feed.get("name") or feed.get("url", "RSS")
        url = feed.get("url")
        if not url:
            continue
        try:
            text = request_text(url)
            root = ET.fromstring(text)
        except Exception as exc:
            print(f"[warn] RSS failed: {name}: {exc}", file=sys.stderr)
            continue

        feed_items = root.findall(".//item")
        atom_entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        parsed_count = 0

        for node in feed_items:
            if parsed_count >= max_items:
                break
            title = child_text(node, "title")
            link = child_text(node, "link")
            if not link:
                guid = child_text(node, "guid")
                link = guid if guid.startswith("http") else ""
            summary = child_text(node, "description") or child_text(node, "summary")
            published = child_text(node, "pubDate") or child_text(node, "published")
            if title and link:
                items.append(
                    Item(
                        id=stable_id(link, title),
                        title=strip_html(title),
                        url=link.strip(),
                        source=name,
                        kind="rss",
                        summary=truncate(strip_html(summary), 600),
                        published_at=published.strip(),
                    )
                )
                parsed_count += 1

        for node in atom_entries:
            if parsed_count >= max_items:
                break
            title = ns_child_text(node, "title")
            link = ""
            for link_node in node.findall("{http://www.w3.org/2005/Atom}link"):
                href = link_node.attrib.get("href", "")
                rel = link_node.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                    break
            summary = ns_child_text(node, "summary") or ns_child_text(node, "content")
            published = ns_child_text(node, "published") or ns_child_text(node, "updated")
            if title and link:
                items.append(
                    Item(
                        id=stable_id(link, title),
                        title=strip_html(title),
                        url=link.strip(),
                        source=name,
                        kind="rss",
                        summary=truncate(strip_html(summary), 600),
                        published_at=published.strip(),
                    )
                )
                parsed_count += 1
    return items


def fetch_official_pages(config: dict[str, Any]) -> list[Item]:
    official = config.get("official_pages", {})
    if not official.get("enabled", False):
        return []
    max_links = int(official.get("max_links_per_source", 8))
    items: list[Item] = []
    for source in official.get("sources", []):
        name = source.get("name") or source.get("url", "Official")
        url = source.get("url", "")
        if not url:
            continue
        try:
            page = request_text(url, timeout=20, retries=1)
            links = extract_official_article_links(page, url, source, max_links)
        except Exception as exc:
            print(f"[warn] official source failed: {name}: {exc}", file=sys.stderr)
            continue
        for link in links:
            try:
                article = request_text(link, timeout=20, retries=1)
                item = official_article_item(name, link, article)
                if item:
                    items.append(item)
            except Exception as exc:
                print(f"[warn] official article failed: {name}: {link}: {exc}", file=sys.stderr)
            time.sleep(0.2)
        time.sleep(0.5)
    return items


def extract_official_article_links(page: str, base_url: str, source: dict[str, Any], limit: int) -> list[str]:
    include_paths = [str(path).lower() for path in source.get("include_paths", [])]
    if not include_paths:
        include_paths = ["/news/", "/blog/", "/research/", "/announcements/"]
    exclude_paths = [str(path).lower() for path in source.get("exclude_paths", [])]
    base = urllib.parse.urlsplit(base_url)
    allowed_hosts = {base.netloc.lower()}
    allowed_hosts.update(str(host).lower() for host in source.get("allowed_hosts", []))

    candidates: list[str] = []
    normalized_page = page.replace("\\/", "/").replace("\\u002F", "/")
    candidates.extend(re.findall(r"""href=["']([^"']+)["']""", normalized_page))
    candidates.extend(re.findall(r"""href=\\["']([^"']+)\\["']""", normalized_page))
    candidates.extend(re.findall(r"""["']((?:https?://|/)[A-Za-z0-9][^"'<>\s]{2,180})["']""", normalized_page))

    links: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = html.unescape(candidate).strip()
        if not candidate or candidate.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(base_url, candidate)
        parsed = urllib.parse.urlsplit(absolute)
        path = parsed.path.rstrip("/")
        lowered_path = path.lower()
        if parsed.netloc.lower() not in allowed_hosts:
            continue
        if path == base.path.rstrip("/"):
            continue
        if any(lowered_path.endswith(ext) for ext in [".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".pdf"]):
            continue
        if include_paths and not any(part in lowered_path for part in include_paths):
            continue
        if exclude_paths and any(part in lowered_path for part in exclude_paths):
            continue
        normalized = clean_url(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")))
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= limit:
            break
    return links


def official_article_item(source: str, url: str, page: str) -> Item | None:
    url = clean_url(url)
    title = (
        meta_content(page, "og:title")
        or meta_content(page, "twitter:title")
        or title_from_html(page)
    )
    title = clean_official_title(strip_html(title), source)
    if not title:
        return None
    description = (
        meta_content(page, "description")
        or meta_content(page, "og:description")
        or meta_content(page, "twitter:description")
    )
    body = article_body_excerpt(page, 900)
    summary = truncate(" ".join(part for part in [strip_html(description), body] if part), 900)
    if looks_like_official_index_page(title, url, summary):
        return None
    published = article_published_at(page)
    return Item(
        id=stable_id(url, title),
        title=title,
        url=url,
        source=source,
        kind="official",
        summary=summary,
        published_at=published,
    )


def meta_content(page: str, key: str) -> str:
    for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.I):
        if not re.search(rf"""\b(?:name|property)=["']{re.escape(key)}["']""", tag, flags=re.I):
            continue
        match = re.search(r"""\bcontent=["']([^"']*)["']""", tag, flags=re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def title_from_html(page: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.I | re.S)
    return html.unescape(match.group(1)).strip() if match else ""


def clean_official_title(title: str, source: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    source_tokens = [re.escape(part) for part in re.split(r"\s+", source) if len(part) > 2]
    if source_tokens:
        source_pattern = "|".join(source_tokens)
        title = re.sub(rf"\s*(?:\\|\||-|–)\s*(?:{source_pattern}).*$", "", title, flags=re.I).strip()
    return title


def looks_like_official_index_page(title: str, url: str, summary: str) -> bool:
    lowered_title = title.lower()
    lowered_url = urllib.parse.urlsplit(url).path.strip("/").lower()
    if re.search(r"\bblog\s*\|", lowered_title):
        return True
    category_titles = [
        "newsroom",
        "press",
        "events",
    ]
    if lowered_title in category_titles:
        return True
    index_segments = r"(blog|news|research|announcements|press|tag|tags|category|categories|page|archive|archives)"
    if re.fullmatch(rf"{index_segments}(/{index_segments}|/[a-z0-9-]+){{0,2}}", lowered_url) and len(summary) < 300:
        return True
    return False


def article_body_excerpt(page: str, limit: int) -> str:
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", page, flags=re.I | re.S)
    cleaned = [strip_html(p) for p in paragraphs]
    cleaned = [p for p in cleaned if len(p) >= 40 and not looks_like_boilerplate(p)]
    return truncate(" ".join(cleaned[:8]), limit)


def article_published_at(page: str) -> str:
    for key in ["article:published_time", "publish_date", "date", "datePublished", "publishedAt"]:
        value = meta_content(page, key)
        if value:
            return value
    patterns = [
        r"""datetime=["']([^"']+)["']""",
        r'"(?:datePublished|publishedAt|publishDate|date)":\s*"([^"]+)"',
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, page, flags=re.I)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return ""


def child_text(node: ET.Element, tag: str) -> str:
    found = node.find(tag)
    if found is not None and found.text:
        return found.text
    for child in list(node):
        if child.tag.endswith("}" + tag) and child.text:
            return child.text
    return ""


def ns_child_text(node: ET.Element, tag: str) -> str:
    found = node.find("{http://www.w3.org/2005/Atom}" + tag)
    return found.text if found is not None and found.text else ""


def fetch_arxiv(config: dict[str, Any]) -> list[Item]:
    arxiv = config.get("arxiv", {})
    if not arxiv.get("enabled", False):
        return []
    queries = arxiv.get("queries", [])
    max_results = int(arxiv.get("max_results", 20))
    items: list[Item] = []
    for query in queries:
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        url = f"https://export.arxiv.org/api/query?{params}"
        try:
            text = request_text(url)
            root = ET.fromstring(text)
        except Exception as exc:
            print(f"[warn] arXiv failed: {query}: {exc}", file=sys.stderr)
            continue
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            title = strip_html(ns_child_text(entry, "title"))
            summary = strip_html(ns_child_text(entry, "summary"))
            published = ns_child_text(entry, "published")
            link = ""
            for link_node in entry.findall("{http://www.w3.org/2005/Atom}link"):
                href = link_node.attrib.get("href", "")
                if href and link_node.attrib.get("rel", "alternate") == "alternate":
                    link = href
                    break
            if title and link:
                items.append(
                    Item(
                        id=stable_id(link, title),
                        title=title,
                        url=link,
                        source=f"arXiv {query}",
                        kind="arxiv",
                        summary=truncate(summary, 900),
                        published_at=published,
                    )
                )
        time.sleep(0.8)
    return items


def fetch_github(config: dict[str, Any]) -> list[Item]:
    github = config.get("github", {})
    if not github.get("enabled", False):
        return []
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    items: list[Item] = []
    items.extend(fetch_github_watch_repositories(github, headers))
    max_results = int(github.get("max_results", 10))
    for query in github.get("queries", []):
        params = urllib.parse.urlencode(
            {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": max_results,
            }
        )
        url = f"https://api.github.com/search/repositories?{params}"
        try:
            data = request_json(url, headers=headers)
        except Exception as exc:
            print(f"[warn] GitHub failed: {query}: {exc}", file=sys.stderr)
            continue
        for repo in data.get("items", []):
            item = github_repo_item(repo)
            if item:
                items.append(item)
        time.sleep(0.8)
    return items


def fetch_github_watch_repositories(github: dict[str, Any], headers: dict[str, str]) -> list[Item]:
    items: list[Item] = []
    for full_name in github.get("repositories", []):
        full_name = str(full_name).strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
            continue
        api_url = f"https://api.github.com/repos/{full_name}"
        try:
            repo = request_json(api_url, headers=headers, timeout=15, retries=1)
        except Exception as exc:
            print(f"[warn] GitHub watch repository failed: {full_name}: {exc}", file=sys.stderr)
            continue
        item = github_repo_item(repo, source="GitHub Watchlist")
        if item:
            items.append(item)
        time.sleep(0.3)
    return items


def github_repo_item(repo: dict[str, Any], source: str = "GitHub") -> Item | None:
    full_name = repo.get("full_name", "")
    html_url = repo.get("html_url", "")
    if not full_name or not html_url:
        return None
    stars = repo.get("stargazers_count", 0)
    desc = repo.get("description") or ""
    homepage = repo.get("homepage") or ""
    topics = repo.get("topics") or []
    summary_parts = [desc]
    if homepage:
        summary_parts.append(f"Homepage: {homepage}")
    if topics:
        summary_parts.append("Topics: " + ", ".join(str(topic) for topic in topics[:8]))
    pushed_at = repo.get("pushed_at") or repo.get("updated_at") or ""
    return Item(
        id=stable_id(html_url, full_name),
        title=f"{full_name} ({stars:,} stars)",
        url=html_url,
        source=source,
        kind="github",
        summary=truncate(" ".join(part for part in summary_parts if part), 600),
        published_at=pushed_at,
    )


def fetch_newsapi(config: dict[str, Any]) -> list[Item]:
    newsapi = config.get("newsapi", {})
    key = os.environ.get("NEWSAPI_KEY", "")
    if not newsapi.get("enabled", False) or not key:
        return []
    exclude_domains = set(str(d).lower() for d in config.get("exclude_domains", []))
    items: list[Item] = []
    page_size = int(newsapi.get("page_size", 20))
    for query in newsapi.get("queries", []):
        params = urllib.parse.urlencode(
            {
                "q": query,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": page_size,
                "apiKey": key,
            }
        )
        url = f"https://newsapi.org/v2/everything?{params}"
        try:
            data = request_json(url)
        except Exception as exc:
            print(f"[warn] NewsAPI failed: {query}: {exc}", file=sys.stderr)
            continue
        for article in data.get("articles", []):
            title = article.get("title") or ""
            link = article.get("url") or ""
            source = (article.get("source") or {}).get("name") or "NewsAPI"
            summary = article.get("description") or article.get("content") or ""
            published = article.get("publishedAt") or ""
            if not title or not link:
                continue
            link_host = urllib.parse.urlsplit(link).netloc.lower()
            if any(domain in link_host for domain in exclude_domains):
                continue
            items.append(
                Item(
                    id=stable_id(link, title),
                    title=strip_html(title),
                    url=link,
                    source=source,
                    kind="news",
                    summary=truncate(strip_html(summary), 600),
                    published_at=published,
                )
            )
    return items


def enrich_item_details(items: list[Item], config: dict[str, Any]) -> None:
    detail_config = config.get("detail_fetch", {})
    if not detail_config.get("enabled", True):
        return
    max_items = int(detail_config.get("max_items", 18))
    max_chars = int(detail_config.get("max_chars", 3000))
    for item in items[:max_items]:
        try:
            if item.kind == "github":
                item.details = fetch_github_readme(item.url, max_chars, config)
            elif item.kind in {"rss", "news"}:
                item.details = fetch_article_text(item.url, max_chars)
            else:
                item.details = truncate(strip_html(item.summary), max_chars)
        except Exception as exc:
            print(f"[warn] detail fetch failed: {item.title}: {exc}", file=sys.stderr)
        time.sleep(0.3)


def fetch_github_readme(repo_url: str, max_chars: int, config: dict[str, Any]) -> str:
    owner_repo = github_owner_repo(repo_url)
    if not owner_repo:
        return ""
    owner, repo = owner_repo
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github.raw"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_url = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/readme"
    readme = ""
    try:
        text = request_text(api_url, headers=headers)
        readme = truncate(strip_markdown(text), max_chars)
    except Exception as exc:
        print(f"[warn] GitHub README failed: {owner}/{repo}: {exc}", file=sys.stderr)
    updates = fetch_github_updates(owner, repo, token, config)
    if updates:
        if readme:
            return truncate(updates + "\n\nProject README overview:\n" + readme, max_chars)
        return truncate(updates, max_chars)
    return readme


def github_owner_repo(repo_url: str) -> tuple[str, str] | None:
    parsed = urllib.parse.urlsplit(repo_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.lower() != "github.com" or len(parts) < 2:
        return None
    return parts[0], parts[1]


def classify_github_commit(message: str) -> tuple[int, str]:
    subject = re.sub(r"\s+", " ", (message or "").splitlines()[0]).strip()
    lowered = subject.lower()
    if not lowered:
        return 0, "空提交信息"

    score = 0
    reasons: list[str] = []

    important_rules = [
        (r"\b(cve|vulnerab|security|exploit|auth|permission|privilege|sandbox|secret leak|token leak)\b", 6, "安全/权限"),
        (r"\b(crash|panic|deadlock|data loss|corrupt|timeout|outage|production|incident|rollback)\b", 5, "生产稳定性"),
        (r"\b(breaking|api|sdk|protocol|provider|integration|webhook|endpoint|schema|migration)\b", 4, "API/兼容性"),
        (r"\b(perf|performance|latency|throughput|memory leak|optimi[sz]e|cache|scalability|concurrency)\b", 4, "性能/扩展性"),
        (r"\b(rag|retrieval|embedding|vector (index|store|database|db|search)|indexing|indexer|rerank|chunking)\b", 4, "RAG/检索/索引"),
        (r"\b(model|llm|inference|serving|vllm|cuda|quantization|quantize|tokenizer|multimodal|checkpoint|fine[- ]?tuning)\b", 4, "模型/推理"),
        (r"\b(agent|planner|executor|workflow|tool calling|function call|mcp|multi-agent)\b", 4, "Agent/工具链"),
        (r"\b(database|storage|queue|streaming|observability|telemetry|tracing|metrics)\b", 3, "数据/基础设施"),
        (r"\b(implement|introduce|add support|enable|migrate|rewrite)\b", 2, "新增能力"),
    ]
    for pattern, weight, reason in important_rules:
        if re.search(pattern, lowered):
            score += weight
            reasons.append(reason)

    if re.search(r"\bvector\b", lowered) and re.search(r"\b(mismatch|incorrect|wrong|missing|corrupt|consistency|accuracy)\b", lowered):
        score += 5
        reasons.append("向量数据正确性")

    low_signal_patterns = [
        r"\b(ui|ux|style|layout|color|icon|button|modal|sidebar|navbar|page|screen|preview|tooltip|theme|font|css|tailwind|padding|margin)\b",
        r"\b(copy|wording|text|message|readme|docs?|documentation|typo|lint|format|formatting|prettier|eslint|ruff|black|badge|logo)\b",
        r"\b(example|demo|sample|screenshot|animation|loading|toast|file name conflict|filename conflict)\b",
    ]
    low_signal = any(re.search(pattern, lowered) for pattern in low_signal_patterns)

    maintenance_only = bool(
        re.match(r"^(docs?|style|chore|test|tests|ci|build)(\(.+?\))?:", lowered)
        or re.search(r"\b(bump|upgrade|update)\b.*\b(deps|dependencies|package|lockfile|package-lock|pnpm-lock|yarn.lock|go.mod)\b", lowered)
        or re.fullmatch(r"(update|updates|fix|fixes|minor fixes|misc fixes|cleanup|clean up|wip|changes?)\.?", lowered)
    )

    if maintenance_only and score < 6:
        return 0, "维护/文档/依赖类低信号"

    if low_signal and score < 6:
        return 0, "界面/文案/体验类低信号"
    if low_signal:
        score -= 2
        reasons.append("包含体验项但有核心影响")

    if re.match(r"^(feat|fix|perf|refactor|security)(\(.+?\))?:", lowered) and score:
        score += 1

    if not reasons:
        return 0, "未命中核心影响规则"
    return score, "、".join(dict.fromkeys(reasons))


def fetch_github_updates(owner: str, repo: str, token: str, config: dict[str, Any]) -> str:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    sections: list[str] = []
    repo_api = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
    github_config = config.get("github", {})
    lookback_hours = int(config.get("lookback_hours", 30))
    commit_fetch_limit = int(github_config.get("commit_fetch_limit", 20))
    important_commit_limit = int(github_config.get("important_commit_limit", 6))
    cutoff = utc_now() - dt.timedelta(hours=lookback_hours)

    try:
        repo_data = request_json(repo_api, headers=headers, timeout=15, retries=1)
        pushed_at = repo_data.get("pushed_at") or ""
        updated_at = repo_data.get("updated_at") or ""
        language = repo_data.get("language") or ""
        open_issues = repo_data.get("open_issues_count")
        sections.append(
            "Repository status: "
            + "; ".join(
                part
                for part in [
                    f"pushed_at={pushed_at}" if pushed_at else "",
                    f"updated_at={updated_at}" if updated_at else "",
                    f"language={language}" if language else "",
                    f"open_issues={open_issues}" if open_issues is not None else "",
                ]
                if part
            )
        )
    except Exception as exc:
        print(f"[warn] GitHub repo metadata failed: {owner}/{repo}: {exc}", file=sys.stderr)

    try:
        releases_url = repo_api + "/releases?per_page=5"
        releases = request_json(releases_url, headers=headers, timeout=15, retries=1)
        lines = []
        for release in releases[:5]:
            name = release.get("name") or release.get("tag_name") or ""
            published = release.get("published_at") or ""
            body = truncate(strip_markdown(release.get("body") or ""), 280)
            if name:
                lines.append(f"- {name} ({published}): {body}")
        if lines:
            sections.append("Recent releases:\n" + "\n".join(lines))
    except Exception as exc:
        print(f"[warn] GitHub releases failed: {owner}/{repo}: {exc}", file=sys.stderr)

    try:
        commits_url = repo_api + f"/commits?per_page={commit_fetch_limit}"
        commits = request_json(commits_url, headers=headers, timeout=15, retries=1)
        important_lines = []
        reviewed = 0
        skipped_old = 0
        filtered_low_signal = 0
        for commit in commits[:commit_fetch_limit]:
            commit_data = commit.get("commit") or {}
            message = truncate((commit_data.get("message") or "").splitlines()[0], 160)
            date = ((commit_data.get("committer") or {}).get("date") or "")
            sha = (commit.get("sha") or "")[:7]
            if not message:
                continue
            commit_date = parse_date(date)
            if commit_date and commit_date < cutoff:
                skipped_old += 1
                continue
            reviewed += 1
            importance_score, reason = classify_github_commit(message)
            if importance_score >= 4:
                important_lines.append(f"- {date} {sha} [{reason}]: {message}")
            else:
                filtered_low_signal += 1
        commit_header = (
            f"Commit review window: last {lookback_hours} hours; "
            f"reviewed={reviewed}; filtered_low_signal={filtered_low_signal}; skipped_old={skipped_old}."
        )
        if important_lines:
            sections.append(
                commit_header
                + "\nImportant commits:\n"
                + "\n".join(important_lines[:important_commit_limit])
            )
        else:
            sections.append(commit_header + "\nNo clearly important commits identified from recent public commits.")
    except Exception as exc:
        print(f"[warn] GitHub commits failed: {owner}/{repo}: {exc}", file=sys.stderr)

    return "\n\n".join(section for section in sections if section.strip())


def fetch_article_text(url: str, max_chars: int) -> str:
    page = request_text(url, timeout=20, retries=1)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.I | re.S)
    title = strip_html(title_match.group(1)) if title_match else ""
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", page, flags=re.I | re.S)
    cleaned = [strip_html(p) for p in paragraphs]
    cleaned = [p for p in cleaned if len(p) >= 40 and not looks_like_boilerplate(p)]
    text = " ".join(cleaned[:8])
    if title:
        text = f"{title}. {text}"
    return truncate(text, max_chars)


def looks_like_boilerplate(text: str) -> bool:
    lowered = text.lower()
    terms = ["cookie", "privacy policy", "subscribe", "newsletter", "sign up", "all rights reserved"]
    return any(term in lowered for term in terms)


def dedupe_items(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    unique: list[Item] = []
    for item in items:
        normalized_url = normalize_url(item.url)
        title_key = re.sub(r"\W+", "", item.title.lower())[:80]
        key = normalized_url or title_key
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def load_published_history(project_dir: Path, report_date: str, config: dict[str, Any]) -> dict[str, Any]:
    state_dir = project_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "published_items.json"
    history = {"version": 1, "items": {}}
    if state_file.exists():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded.get("items"), dict):
                history["items"] = normalize_published_history_records(loaded["items"])
        except Exception:
            pass

    history_config = config.get("history", {})
    if history_config.get("bootstrap_from_output", True):
        for key, record in bootstrap_published_history_from_outputs(project_dir, report_date).items():
            history["items"].setdefault(key, record)
    return history


def normalize_published_history_records(records: dict[str, Any]) -> dict[str, dict[str, str]]:
    normalized: dict[str, dict[str, str]] = {}
    for record in records.values():
        if not isinstance(record, dict):
            continue
        url = clean_url(str(record.get("url") or ""))
        title = str(record.get("title") or "")
        key = published_key_from_url_title(url, title)
        if not key:
            continue
        existing = normalized.get(key)
        first_published = str(record.get("first_published_at") or record.get("published_at") or "")
        last_published = str(record.get("last_published_at") or first_published)
        if existing:
            existing["first_published_at"] = min(existing.get("first_published_at", first_published), first_published)
            existing["last_published_at"] = max(existing.get("last_published_at", last_published), last_published)
            continue
        normalized[key] = {
            "first_published_at": first_published,
            "last_published_at": last_published,
            "title": title,
            "url": normalize_url(url),
            "kind": str(record.get("kind") or "unknown"),
            "source": str(record.get("source") or ""),
        }
    return normalized


def bootstrap_published_history_from_outputs(project_dir: Path, report_date: str) -> dict[str, dict[str, str]]:
    output_dir = project_dir / "output"
    if not output_dir.exists():
        return {}
    records: dict[str, dict[str, str]] = {}
    for html_file in sorted(output_dir.glob("*.html")):
        published_on = html_file.stem
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_on) or published_on >= report_date:
            continue
        try:
            page = html_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"[warn] published history bootstrap skipped: {html_file.name}: {exc}", file=sys.stderr)
            continue
        for url, title in extract_output_links(page):
            key = published_key_from_url_title(url, title)
            if not key:
                continue
            records.setdefault(
                key,
                {
                    "first_published_at": published_on,
                    "last_published_at": published_on,
                    "title": title,
                    "url": normalize_url(url),
                    "kind": "unknown",
                    "source": "output",
                },
            )
    return records


def extract_output_links(page: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, flags=re.I | re.S):
        url = html.unescape(match.group(1)).strip()
        title = strip_html(match.group(2))
        if not url or not title or not url.startswith(("http://", "https://")):
            continue
        key = published_key_from_url_title(url, title)
        if not key or key in seen:
            continue
        seen.add(key)
        links.append((url, title))
    return links


def filter_previously_published(
    items: list[Item],
    history: dict[str, Any],
    report_date: str,
    config: dict[str, Any],
) -> tuple[list[Item], int]:
    history_config = config.get("history", {})
    if not history_config.get("exclude_previously_published", True):
        return items, 0

    filter_kinds = set(history_config.get("filter_kinds", ["rss", "news", "official", "arxiv"]))
    records = history.get("items", {})
    fresh: list[Item] = []
    skipped = 0
    for item in items:
        if item.kind not in filter_kinds:
            fresh.append(item)
            continue
        record = records.get(published_key(item))
        if not record:
            fresh.append(item)
            continue
        first_published = str(record.get("first_published_at") or record.get("published_at") or "")
        if first_published >= report_date:
            fresh.append(item)
            continue
        skipped += 1
    return fresh, skipped


def save_published_history(project_dir: Path, history: dict[str, Any], selected: list[Item], report_date: str) -> None:
    state_dir = project_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    records = history.setdefault("items", {})
    for item in selected:
        key = published_key(item)
        record = records.get(key) or {}
        first_published = record.get("first_published_at") or report_date
        records[key] = {
            "first_published_at": min(str(first_published), report_date),
            "last_published_at": report_date,
            "title": item.title,
            "url": normalize_url(item.url),
            "kind": item.kind,
            "source": item.source,
        }
    state_file = state_dir / "published_items.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": utc_now().isoformat(),
                "items": dict(sorted(records.items())),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def published_key(item: Item) -> str:
    return published_key_from_url_title(item.url, item.title)


def published_key_from_url_title(url: str, title: str) -> str:
    normalized_url = normalize_url(url) if url else ""
    if normalized_url:
        return f"url:{normalized_url}"
    title_key = re.sub(r"\W+", "", (title or "").lower())[:120]
    return f"title:{title_key}" if title_key else ""


def parse_yyyy_mm_dd(value: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(value[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def mark_first_seen(items: list[Item], project_dir: Path) -> set[str]:
    state_dir = project_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "seen_items.json"
    if state_file.exists():
        try:
            seen = set(json.loads(state_file.read_text(encoding="utf-8")).get("ids", []))
        except Exception:
            seen = set()
    else:
        seen = set()

    current_ids: set[str] = set()
    for item in items:
        key = seen_key(item)
        current_ids.add(key)
        item.first_seen = key not in seen
    return seen | current_ids


def save_seen_items(project_dir: Path, seen: set[str]) -> None:
    state_dir = project_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "seen_items.json"
    state_file.write_text(json.dumps({"ids": sorted(seen)}, ensure_ascii=False, indent=2), encoding="utf-8")


def seen_key(item: Item) -> str:
    if item.kind == "github":
        parsed = urllib.parse.urlsplit(item.url)
        parts = [part.lower() for part in parsed.path.split("/") if part]
        if parsed.netloc.lower() == "github.com" and len(parts) >= 2:
            return f"github:{parts[0]}/{parts[1]}"
    return stable_id(normalize_url(item.url), item.title)


def normalize_url(url: str) -> str:
    url = clean_url(url)
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    query = [(k, v) for k, v in query if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            urllib.parse.urlencode(query),
            "",
        )
    )


def clean_url(url: str) -> str:
    return html.unescape(url or "").strip().strip("\\'\"")


def score_item(item: Item, config: dict[str, Any], cutoff: dt.datetime) -> int:
    text = f"{item.title} {item.summary}".lower()
    for keyword in config.get("exclude_keywords", []):
        if str(keyword).lower() in text:
            item.raw_score = -100
            return item.raw_score

    keywords = [str(k).lower() for k in config.get("keywords", [])]
    score = 0
    for keyword in keywords:
        if keyword and keyword in text:
            score += 2

    title = item.title.lower()
    high_signal_terms = [
        "release",
        "launch",
        "model",
        "benchmark",
        "paper",
        "agent",
        "funding",
        "acquisition",
        "open source",
        "safety",
        "policy",
        "regulation",
        "framework",
        "sdk",
        "inference",
        "deploy",
        "rag",
        "vector",
        "embedding",
        "fine-tune",
        "finetune",
        "mlops",
        "observability",
        "evaluation",
        "tool chain",
        "toolchain",
        "发布",
        "模型",
        "论文",
        "开源",
        "融资",
        "监管",
        "安全",
        "智能体",
        "框架",
        "推理",
        "部署",
        "微调",
        "向量",
        "评测",
        "工具链",
    ]
    score += sum(1 for term in high_signal_terms if term in title)

    parsed = parse_date(item.published_at)
    if parsed:
        if parsed >= cutoff:
            score += 3
        elif parsed >= cutoff - dt.timedelta(hours=24):
            score += 1

    official_source = item.kind == "official" or item.source.lower().startswith("official")
    model_vendor_terms = [
        "openai",
        "anthropic",
        "claude",
        "google deepmind",
        "gemini",
        "mistral",
        "meta ai",
        "llama",
        "cohere",
        "command",
        "xai",
        "grok",
        "qwen",
        "deepseek",
        "moonshot",
        "kimi",
        "zhipu",
        "z.ai",
        "glm",
        "minimax",
        "hunyuan",
        "ernie",
    ]
    release_terms = ["release", "launch", "introducing", "announce", "model", "preview", "available", "发布", "推出", "上线", "模型"]
    if any(term in text for term in model_vendor_terms) and any(term in text for term in release_terms):
        score += 6
    if official_source and any(term in text for term in model_vendor_terms):
        score += 4

    if item.kind in {"rss", "news", "official"}:
        score += 1
    if item.source == "GitHub Watchlist":
        score += 4
    if item.kind == "github" and re.search(r"\(([0-9,]+) stars\)", item.title):
        match = re.search(r"\(([0-9,]+) stars\)", item.title)
        stars = int(match.group(1).replace(",", "")) if match else 0
        if stars >= 5000:
            score += 4
        elif stars >= 1000:
            score += 2
    item.raw_score = score
    return score


def filter_and_rank(items: list[Item], config: dict[str, Any]) -> list[Item]:
    cutoff = utc_now() - dt.timedelta(hours=int(config.get("lookback_hours", 30)))
    stale_without_date = bool(config.get("exclude_undated_items", False))
    fresh_items: list[Item] = []
    for item in items:
        parsed = parse_date(item.published_at)
        if parsed is None:
            if stale_without_date:
                continue
        elif parsed < cutoff:
            continue
        fresh_items.append(item)

    for item in fresh_items:
        score_item(item, config, cutoff)
    ranked = sorted(fresh_items, key=lambda i: (i.raw_score, parse_date(i.published_at) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)), reverse=True)
    min_score = int(config.get("min_score", 4))
    filtered = [item for item in ranked if item.raw_score >= min_score]
    limit = int(config.get("max_items_before_ai", 80))
    return diversify_items(filtered, config.get("candidate_kind_limits", {}), limit)


def diversify_items(items: list[Item], kind_limits: dict[str, Any], total_limit: int) -> list[Item]:
    if not kind_limits:
        return items[:total_limit]
    selected: list[Item] = []
    counts: dict[str, int] = {}
    deferred: list[Item] = []
    for item in items:
        limit = int(kind_limits.get(item.kind, total_limit))
        count = counts.get(item.kind, 0)
        if count < limit:
            selected.append(item)
            counts[item.kind] = count + 1
        else:
            deferred.append(item)
        if len(selected) >= total_limit:
            return selected

    for item in deferred:
        if len(selected) >= total_limit:
            break
        selected.append(item)
    return selected


def _make_api_request(payload: dict[str, Any], timeout: int) -> tuple[dict[str, Any] | None, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    req = urllib.request.Request(
        openai_responses_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[warn] API failed: HTTP {exc.code}: {body[:500]}", file=sys.stderr)
        return None, f"HTTP {exc.code}"
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        print(f"[warn] API failed: {exc}", file=sys.stderr)
        return None, f"timed out: {exc}"
    except Exception as exc:
        print(f"[warn] API failed: {exc}", file=sys.stderr)
        return None, str(exc)

    text = extract_response_text(data)
    if not text:
        return None, "返回内容为空"
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        print(f"[warn] API JSON parse failed: {exc}: {text[:500]}", file=sys.stderr)
        return None, f"JSON 解析失败：{exc}"


def _build_single_payload(
    candidates: list[dict[str, Any]], max_items: int, model: str,
    quality_examples: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string"},
            "executive_summary": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "items": {
                "type": "array",
                "maxItems": max_items,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "score": {"type": "integer", "minimum": 1, "maximum": 10},
                        "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                        "category": {"type": "string"},
                        "summary_zh": {"type": "string"},
                        "why_it_matters": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["id", "score", "importance", "category", "summary_zh", "why_it_matters", "action"],
                },
            },
        },
        "required": ["headline", "executive_summary", "themes", "items"],
    }
    prompt: dict[str, Any] = {
        "task": "从候选信息中筛选真正重要的 AI 新闻、论文、产品和工程信息，生成中文日报。",
        "selection_rules": list(SELECTION_RULES),
        "max_items": max_items,
        "candidates": candidates,
    }
    if quality_examples:
        prompt["quality_examples"] = quality_examples
    return {
        "model": model,
        "input": [
            {"role": "system", "content": "你是一个严谨的 AI 行业情报分析员。输出必须是有效 JSON，中文表达简洁具体。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {"type": "json_schema", "name": "daily_ai_news_brief", "schema": schema, "strict": True},
            "verbosity": "low",
        },
        "reasoning": {"effort": "low"},
    }


def _items_to_candidates(items: list["Item"], limit: int) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id, "title": item.title, "source": item.source,
            "kind": item.kind, "url": item.url, "summary": item.summary,
            "details": item.details, "published_at": item.published_at,
            "raw_score": item.raw_score, "first_seen": item.first_seen,
        }
        for item in items[:limit]
    ]


def _is_retryable_error(error: str) -> bool:
    return any(tag in error for tag in ("HTTP 502", "HTTP 503", "HTTP 504", "timed out"))


def _call_openai_single(
    items: list["Item"], config: dict[str, Any], quality_cases: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or not items:
        return None, "未配置 OPENAI_API_KEY"
    model = os.environ.get("OPENAI_MODEL") or "gpt-5.5"
    max_items = int(config.get("max_items_in_report", 18))
    max_items_for_ai = int(config.get("max_items_for_ai", max_items))
    timeout = int(config.get("openai_timeout_seconds", 180))

    quality_examples: dict[str, Any] | None = None
    if quality_cases:
        quality_examples = select_cases_for_prompt(quality_cases, items, config)

    attempts = [max_items_for_ai]
    reduced = max(5, max_items_for_ai // 2)
    if reduced < max_items_for_ai:
        attempts.append(reduced)
        if reduced > 5:
            attempts.append(5)

    last_error = ""
    for attempt_limit in attempts:
        candidates = _items_to_candidates(items, attempt_limit)
        examples = quality_examples if attempt_limit == attempts[0] else None
        payload = _build_single_payload(candidates, max_items, model, examples)
        result, error = _make_api_request(payload, timeout)
        if result:
            return result, ""
        last_error = f"模型调用失败：{error}"
        if _is_retryable_error(error) and attempt_limit != attempts[-1]:
            next_limit = attempts[attempts.index(attempt_limit) + 1]
            print(f"[info] retrying with {next_limit} candidates (was {attempt_limit})", file=sys.stderr)
            continue
        return None, last_error

    return None, last_error


def _summarize_batch(
    batch: list[dict[str, Any]], model: str, timeout: int,
    quality_examples: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "score": {"type": "integer", "minimum": 1, "maximum": 10},
                        "importance": {"type": "string", "enum": ["high", "medium", "low"]},
                        "category": {"type": "string"},
                        "summary_zh": {"type": "string"},
                        "why_it_matters": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["id", "score", "importance", "category", "summary_zh", "why_it_matters", "action"],
                },
            },
        },
        "required": ["items"],
    }
    prompt: dict[str, Any] = {
        "task": "为以下 AI 相关信息生成中文摘要和评分。",
        "selection_rules": list(SELECTION_RULES),
        "candidates": batch,
    }
    if quality_examples:
        prompt["quality_examples"] = quality_examples
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": "你是一个严谨的 AI 行业情报分析员。输出必须是有效 JSON，中文表达简洁具体。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {"type": "json_schema", "name": "batch_summary", "schema": schema, "strict": True},
            "verbosity": "low",
        },
        "reasoning": {"effort": "low"},
    }
    result, error = _make_api_request(payload, timeout)
    if result and isinstance(result.get("items"), list):
        return result["items"]
    print(f"[warn] batch summarize failed: {error}", file=sys.stderr)
    return []


def _synthesize_brief(
    item_results: list[dict[str, Any]], model: str, timeout: int,
) -> dict[str, Any] | None:
    summaries = []
    for r in item_results:
        summaries.append({
            "id": r.get("id", ""),
            "title": r.get("title", ""),
            "category": r.get("category", ""),
            "score": r.get("score", 5),
            "summary_preview": (r.get("summary_zh", "") or "")[:100],
        })
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string"},
            "executive_summary": {"type": "string"},
            "themes": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "category_adjustments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string"},
                    },
                    "required": ["id", "category"],
                },
            },
        },
        "required": ["headline", "executive_summary", "themes", "category_adjustments"],
    }
    prompt = {
        "task": "根据以下已生成的 AI 日报条目摘要，生成整体标题、摘要和主题标签。如果分类标签需要合并调整（不超过6个分类），在 category_adjustments 中给出。",
        "items": summaries,
    }
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": "你是一个严谨的 AI 行业情报分析员。输出必须是有效 JSON，中文表达简洁具体。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {"type": "json_schema", "name": "brief_synthesis", "schema": schema, "strict": True},
            "verbosity": "low",
        },
        "reasoning": {"effort": "low"},
    }
    for attempt in range(2):
        result, error = _make_api_request(payload, timeout)
        if result:
            return result
        print(f"[warn] synthesis failed (attempt {attempt+1}/2): {error}", file=sys.stderr)
    return None


def _call_openai_concurrent(
    items: list["Item"], config: dict[str, Any],
    quality_cases: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    model = os.environ.get("OPENAI_MODEL") or "gpt-5.5"
    max_items_for_ai = int(config.get("max_items_for_ai", int(config.get("max_items_in_report", 18))))
    timeout = int(config.get("openai_timeout_seconds", 180))
    batch_size = int(config.get("concurrent_batch_size", 3))
    max_workers = int(config.get("concurrent_max_workers", 3))

    quality_examples: dict[str, Any] | None = None
    if quality_cases:
        quality_examples = select_cases_for_prompt(quality_cases, items, config)

    candidates = _items_to_candidates(items, max_items_for_ai)
    batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
    print(f"[info] concurrent mode: {len(candidates)} items in {len(batches)} batches (batch_size={batch_size})", file=sys.stderr)

    item_by_id = {it.id: it for it in items}
    all_results: list[dict[str, Any]] = []
    summarized_ids: set[str] = set()
    batch_members: list[list[dict[str, Any]]] = list(batches)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_summarize_batch, batch, model, timeout, quality_examples): idx
            for idx, batch in enumerate(batches)
        }
        for future in concurrent.futures.as_completed(futures):
            idx = futures[future]
            try:
                batch_items = future.result()
                all_results.extend(batch_items)
                summarized_ids.update(r.get("id", "") for r in batch_items)
                print(f"[info] batch {idx+1}/{len(batches)}: got {len(batch_items)} items", file=sys.stderr)
            except Exception as exc:
                print(f"[warn] batch {idx+1}/{len(batches)} failed: {exc}", file=sys.stderr)

    for batch_cands in batch_members:
        for cand in batch_cands:
            cid = cand.get("id", "")
            if cid not in summarized_ids:
                item = item_by_id.get(cid)
                fallback_summary = local_summary(item) if item else cand.get("title", "")
                all_results.append({
                    "id": cid,
                    "score": cand.get("raw_score", 5),
                    "importance": "medium",
                    "category": "行业动态",
                    "summary_zh": fallback_summary,
                    "why_it_matters": "该信息命中了 AI 重点关键词，建议结合原文判断影响范围。",
                    "action": "关注",
                })
                print(f"[info] fallback local_summary for: {cid}", file=sys.stderr)

    if not all_results:
        return None, "模型调用失败：所有并发批次均失败"

    candidate_map = {c["id"]: c for c in candidates}
    for r in all_results:
        cand = candidate_map.get(r.get("id", ""))
        if cand:
            r["title"] = cand.get("title", "")

    print(f"[info] phase 1 done: {len(summarized_ids)}/{len(candidates)} items summarized ({len(all_results) - len(summarized_ids)} fallback)", file=sys.stderr)

    synthesis = _synthesize_brief(all_results, model, timeout)

    if synthesis:
        cat_adj = {a["id"]: a["category"] for a in synthesis.get("category_adjustments", [])}
        for r in all_results:
            if r.get("id") in cat_adj:
                r["category"] = cat_adj[r["id"]]

    brief: dict[str, Any] = {
        "headline": (synthesis or {}).get("headline", "今日 AI 信息简报"),
        "executive_summary": (synthesis or {}).get("executive_summary", "基于并发模式生成。"),
        "themes": (synthesis or {}).get("themes", ["模型与产品", "研究论文", "开源项目"]),
        "items": all_results,
    }
    return brief, ""


def call_openai_for_brief(items: list[Item], config: dict[str, Any], quality_cases: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or not items:
        return None, "未配置 OPENAI_API_KEY"

    return _call_openai_concurrent(items, config, quality_cases)


def extract_response_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for output in data.get("output", []):
        for content in output.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(content["text"])
    if parts:
        return "\n".join(parts)
    if data.get("output_text"):
        return str(data["output_text"])
    return ""


def apply_ai_brief(items: list[Item], brief: dict[str, Any] | None, config: dict[str, Any], model_error: str = "") -> tuple[list[Item], dict[str, Any]]:
    max_items = int(config.get("max_items_in_report", 18))
    by_id = {item.id: item for item in items}
    selected: list[Item] = []
    if brief:
        for ranked in brief.get("items", []):
            item = by_id.get(ranked.get("id", ""))
            if not item:
                continue
            item.ai_score = int(ranked.get("score") or item.raw_score)
            item.importance = ranked.get("importance") or "medium"
            item.category = ranked.get("category") or item.category
            item.summary = ranked.get("summary_zh") or local_summary(item, model_error)
            item.reason = ranked.get("why_it_matters") or ranked.get("action") or fallback_reason(item)
            selected.append(item)
    if not selected:
        selected = diversify_items(items, config.get("report_kind_limits", {}), max_items)
        for item in selected:
            item.ai_score = min(10, max(1, item.raw_score))
            item.importance = "high" if item.raw_score >= 9 else "medium"
            item.category = guess_category(item)
            item.summary = local_summary(item, model_error)
            item.reason = fallback_reason(item)
        brief = {
            "headline": "今日 AI 信息简报",
            "executive_summary": f"基于本地规则筛选生成；{model_error or '模型未返回可用结果'}。",
            "themes": ["模型与产品", "研究论文", "开源项目"],
        }
    return selected[:max_items], brief or {}


def local_summary(item: Item, model_error: str = "") -> str:
    content = _best_available_text(item)
    if content:
        prefix = "首次收录项目。" if item.kind == "github" and item.first_seen else ""
        return prefix + outline_summary(content, 600)
    if item.kind == "arxiv":
        return f"论文：{clean_title(item.title)}。建议结合原文判断方法和实验价值。"
    if item.kind == "github":
        prefix = "首次收录项目。" if item.first_seen else ""
        return f"{prefix}{clean_title(item.title)}。"
    return f"{clean_title(item.title)}。"


def _best_available_text(item: Item) -> str:
    for text in (item.details, item.summary):
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        if len(cleaned) >= 40:
            return cleaned
    return ""


def clean_title(title: str) -> str:
    return re.sub(r"\s+\([0-9,]+ stars\)$", "", title).strip()


def outline_summary(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?。！？])\s+", text)
    picked: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence or len(sentence) < 12:
            continue
        if looks_like_summary_noise(sentence):
            continue
        if any(sentence.lower().startswith(prefix) for prefix in ["copyright", "all rights reserved"]):
            continue
        candidate = " ".join(picked + [sentence])
        if len(candidate) > limit and picked:
            break
        picked.append(sentence)
        if len(candidate) >= limit * 0.75:
            break
    return truncate(" ".join(picked) or text, limit)


def looks_like_summary_noise(text: str) -> bool:
    lowered = text.lower()
    noisy_terms = [
        "github sponsors",
        "support this work",
        "badge.svg",
        "melpa.org",
        "weekly meeting",
        "latest news",
        "documentation #",
    ]
    return any(term in lowered for term in noisy_terms)


def guess_category(item: Item) -> str:
    text = f"{item.title} {item.summary}".lower()
    if item.kind == "arxiv" or "paper" in text or "论文" in text:
        return "研究"
    if item.kind == "github" or "open source" in text or "开源" in text:
        return "开源/工程"
    if any(term in text for term in ["policy", "regulation", "监管", "政策", "safety"]):
        return "政策/安全"
    if any(term in text for term in ["launch", "release", "model", "发布", "模型"]):
        return "产品/模型"
    return "行业动态"


def fallback_reason(item: Item) -> str:
    if item.kind == "github":
        return "该项目近期活跃，建议重点看 README、示例和 issue 活跃度，判断是否适合纳入现有 AI 工程链路。"
    if item.kind == "arxiv":
        return "该论文与 AI 研究方向相关，适合进一步判断是否有方法或趋势价值。"
    return "该信息命中了 AI 重点关键词，建议结合原文判断影响范围。"


def render_html(report_date: str, selected: list[Item], brief: dict[str, Any], config: dict[str, Any]) -> str:
    headline = html.escape(brief.get("headline") or "今日 AI 信息简报")
    raw_executive_summary = brief.get("executive_summary") or ""
    executive_summary = html.escape(raw_executive_summary)
    themes = [html.escape(str(t)) for t in brief.get("themes", []) if t]
    generated_at = local_now(config.get("timezone", "Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    source_names = []
    for item in selected:
        if item.source and item.source not in source_names:
            source_names.append(item.source)
    source_text = " · ".join(source_names[:10]) or "本地信息源"
    if len(source_names) > 10:
        source_text += f" · +{len(source_names) - 10}"

    def item_meta_html(index: int, item: Item) -> str:
        score = item.ai_score if item.ai_score is not None else item.raw_score
        badge_class = "high" if item.importance == "high" else "medium" if item.importance == "medium" else "low"
        importance_label = {"high": "重点", "medium": "关注", "low": "观察"}.get(item.importance, item.importance)
        parts = [
            f'<span class="rank">#{index}</span>',
            f'<span class="badge {badge_class}">{html.escape(importance_label)}</span>',
            f"<span>{html.escape(item.source)}</span>",
            f"<span>{html.escape(format_date(item.published_at))}</span>" if item.published_at else "",
            f"<span>Score {score}</span>",
        ]
        return "".join(part for part in parts if part)

    def signal_text(item: Item) -> str:
        text = re.sub(r"\s+", " ", item.reason or item.summary or "").strip()
        return html.escape(compact_sentences(text, 280))

    priority_items = []
    for index, item in enumerate(selected[:3], start=1):
        priority_items.append(
            f"""
            <li>
              <div class="priority-meta">{item_meta_html(index, item)}</div>
              <a href="{html.escape(item.url)}" target="_blank" rel="noreferrer">{html.escape(item.title)}</a>
              <p>{signal_text(item)}</p>
            </li>
            """
        )

    priority_ids = {item.id for item in selected[:3]}

    grouped: dict[str, list[tuple[int, Item]]] = {}
    for index, item in enumerate(selected, start=1):
        if item.id in priority_ids:
            continue
        grouped.setdefault(item.category or "其他", []).append((index, item))

    group_sections = []
    for category, entries in grouped.items():
        entry_html = []
        for index, item in entries:
            entry_html.append(
                f"""
                <article class="entry">
                  <div class="entry-meta">{item_meta_html(index, item)}</div>
                  <h3><a href="{html.escape(item.url)}" target="_blank" rel="noreferrer">{html.escape(item.title)}</a></h3>
                  <div class="entry-summary">{html.escape(item.summary)}</div>
                  <div class="entry-reason"><strong>价值判断</strong><span>{html.escape(item.reason)}</span></div>
                </article>
                """
            )
        group_sections.append(
            f"""
            <section class="group">
              <div class="section-title">
                <h2>{html.escape(category)}</h2>
                <span>{len(entries)} 条</span>
              </div>
              {''.join(entry_html)}
            </section>
            """
        )

    theme_html = "".join(f"<span>{theme}</span>" for theme in themes)
    priority_html = "".join(priority_items)
    description = html.escape(truncate(raw_executive_summary, 180))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{headline} - {html.escape(report_date)}</title>
  <meta name="description" content="{description}">
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0c0c0d;
      --panel: #111114;
      --panel-soft: #151519;
      --hover: #1b1b21;
      --line: #29292f;
      --line-strong: #3b3b45;
      --text: #e7e7ea;
      --strong: #fafafa;
      --muted: #90909b;
      --accent: #4ade80;
      --accent-soft: rgba(74, 222, 128, .08);
      --accent-line: rgba(74, 222, 128, .24);
      --warn: #f0b35a;
      --danger: #f87171;
      --ok: #6ee7b7;
      --radius: 8px;
    }}
    * {{ box-sizing: border-box; }}
    @media (prefers-color-scheme: light) {{
      :root {{
        color-scheme: light;
        --bg: #f6f7f9;
        --panel: #ffffff;
        --panel-soft: #f0f2f5;
        --hover: #eceff3;
        --line: #d9dee7;
        --line-strong: #c3cad6;
        --text: #2c3442;
        --strong: #10151f;
        --muted: #667084;
        --accent: #0f8a58;
        --accent-soft: rgba(15, 138, 88, .08);
        --accent-line: rgba(15, 138, 88, .24);
        --warn: #9a620f;
        --danger: #b42318;
        --ok: #047857;
      }}
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .wrap {{
      max-width: 940px;
      margin: 0 auto;
      padding-left: 28px;
      padding-right: 28px;
    }}
    .topbar {{
      border-bottom: 1px solid var(--line);
      padding: 22px 0 16px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      color: var(--strong);
      font-weight: 700;
    }}
    .brand-mark {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 12px var(--accent);
    }}
    .namespace {{
      color: var(--accent);
      background: var(--accent-soft);
      border: 1px solid var(--accent-line);
      border-radius: 999px;
      padding: 3px 10px;
      font-size: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: nowrap;
    }}
    .hero {{
      padding-top: 34px;
      padding-bottom: 22px;
    }}
    h1 {{
      margin: 0 0 8px;
      color: var(--strong);
      font-size: clamp(28px, 4vw, 44px);
      line-height: 1.15;
      letter-spacing: 0;
    }}
    .date {{
      color: var(--muted);
      margin: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
    }}
    .brief {{
      border-left: 3px solid var(--accent);
      background: var(--accent-soft);
      border-radius: 0 var(--radius) var(--radius) 0;
      padding: 14px 18px;
      margin-top: 24px;
    }}
    .brief p {{
      margin: 0;
      color: var(--text);
    }}
    .themes {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }}
    .themes span {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 10px;
      color: var(--muted);
      background: var(--panel);
      font-size: 13px;
    }}
    .source-strip {{
      color: var(--muted);
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      padding: 12px 0;
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    main {{
      padding-top: 26px;
      padding-bottom: 56px;
    }}
    .priority {{
      margin-bottom: 34px;
    }}
    .section-title {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 14px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 8px;
      margin: 34px 0 2px;
    }}
    .section-title h2 {{
      margin: 0;
      color: var(--strong);
      font-size: 18px;
      line-height: 1.35;
      letter-spacing: 0;
    }}
    .section-title span {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .priority-list {{
      list-style: none;
      padding: 0;
      margin: 12px 0 0;
    }}
    .priority-list li {{
      border-bottom: 1px solid var(--line);
      padding: 16px 0;
    }}
    .priority-list a {{
      display: inline-block;
      color: var(--strong);
      font-size: 19px;
      font-weight: 700;
      line-height: 1.35;
      margin-top: 6px;
    }}
    .priority-list p {{
      margin: 8px 0 0;
      color: var(--text);
    }}
    .entry {{
      border-bottom: 1px solid var(--line);
      padding: 18px 0 20px;
    }}
    .entry:hover {{
      background: linear-gradient(90deg, transparent, var(--accent-soft), transparent);
    }}
    .entry-meta,
    .priority-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      align-items: center;
      font-size: 12px;
    }}
    .rank {{
      font-weight: 700;
      color: var(--accent);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .badge {{
      border-radius: 999px;
      padding: 1px 7px;
      border: 1px solid var(--line-strong);
      font-weight: 700;
    }}
    .badge.high {{ color: var(--danger); border-color: rgba(248, 113, 113, .35); }}
    .badge.medium {{ color: var(--warn); border-color: rgba(240, 179, 90, .35); }}
    .badge.low {{ color: var(--ok); border-color: rgba(110, 231, 183, .35); }}
    h3 {{
      font-size: 18px;
      line-height: 1.4;
      margin: 8px 0 10px;
      letter-spacing: 0;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
      overflow-wrap: anywhere;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .entry-summary {{
      color: var(--text);
      white-space: pre-line;
      overflow-wrap: anywhere;
    }}
    .entry-reason {{
      margin: 10px 0 0;
      display: grid;
      grid-template-columns: 72px 1fr;
      gap: 10px;
      color: var(--muted);
      background: var(--panel-soft);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px 12px;
    }}
    .entry-reason strong {{
      color: var(--strong);
    }}
    footer {{
      color: var(--muted);
      font-size: 13px;
      border-top: 1px solid var(--line);
      padding-top: 18px;
      padding-bottom: 28px;
    }}
    @media (max-width: 640px) {{
      .wrap {{
        padding-left: 16px;
        padding-right: 16px;
      }}
      .hero {{
        padding-top: 26px;
      }}
      .entry-reason {{
        grid-template-columns: 1fr;
        gap: 4px;
      }}
      .priority-list a,
      h3 {{
        font-size: 17px;
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="wrap brand">
      <div class="brand-mark">AI News Agent <span class="dot"></span></div>
      <div class="namespace">daily</div>
    </div>
  </header>
  <section class="hero">
    <div class="wrap">
      <h1>{headline}</h1>
      <p class="date">{html.escape(report_date)} · 生成时间 {html.escape(generated_at)}</p>
      <div class="brief"><p>{executive_summary}</p></div>
      <div class="themes">{theme_html}</div>
    </div>
  </section>
  <section class="source-strip">
    <div class="wrap">
      数据源：{html.escape(source_text)} · 已筛选 {len(selected)} 条
    </div>
  </section>
  <main class="wrap">
    <section class="priority">
      <div class="section-title"><h2>优先阅读</h2><span>Top {len(priority_items)}</span></div>
      <ol class="priority-list">{priority_html}</ol>
    </section>
    {''.join(group_sections)}
  </main>
  <footer class="wrap">
    本简报由本地 AI News Agent 生成。重要信息请以原文为准。
  </footer>
</body>
</html>
"""


def markdown_summary(
    report_date: str,
    selected: list[Item],
    brief: dict[str, Any],
    public_url: str = "",
    item_limit: int = 8,
    item_summary_chars: int = 80,
    executive_summary_chars: int = 220,
) -> str:
    headline = brief.get("headline") or "今日 AI 信息简报"
    summary = truncate(brief.get("executive_summary") or "", executive_summary_chars)
    lines = [f"**{headline}**", f"> 日期：{report_date}"]
    if summary:
        lines.append(f"> {summary}")
    if public_url:
        lines.append(f"> [打开完整 HTML 简报]({public_url})")
    lines.append("")
    for index, item in enumerate(selected[:item_limit], start=1):
        score = item.ai_score if item.ai_score is not None else item.raw_score
        lines.append(f"{index}. [{escape_wecom_md(item.title)}]({item.url})")
        lines.append(f"   {escape_wecom_md(item.category)} · {escape_wecom_md(item.source)} · 评分 {score}")
        if item.summary:
            lines.append(f"   {escape_wecom_md(truncate(item.summary, item_summary_chars))}")
    return "\n".join(lines)


def escape_wecom_md(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_utf8(text: str, max_bytes: int, suffix: str = "…") -> str:
    if utf8_len(text) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return suffix_bytes[:max_bytes].decode("utf-8", errors="ignore")
    clipped = text.encode("utf-8")[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return clipped.rstrip() + suffix


def fit_wecom_markdown(content: str, max_bytes: int) -> str:
    if utf8_len(content) <= max_bytes:
        return content
    notice = "\n\n…内容过长，已截断。请打开 HTML 查看完整简报。"
    return truncate_utf8(content, max_bytes - utf8_len(notice), "") + notice


def build_public_url(config: dict[str, Any], output_file: Path) -> str:
    base = (config.get("public_base_url") or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/{urllib.parse.quote(output_file.name)}"


def push_wecom(config: dict[str, Any], selected: list[Item], brief: dict[str, Any], report_date: str, output_file: Path) -> None:
    wecom = (config.get("push") or {}).get("wecom") or {}
    webhook = os.environ.get("WECOM_WEBHOOK_URL", "")
    if not wecom.get("enabled", False) or not webhook:
        return
    if not selected:
        print("[info] no items selected; skipping WeCom push", file=sys.stderr)
        return
    mode = wecom.get("mode", "markdown")
    public_url = build_public_url(config, output_file)
    mentioned_mobile_list = wecom.get("mentioned_mobile_list") or []
    if mode == "news":
        if not public_url:
            print("[warn] WeCom news mode requires public_base_url; fallback to markdown", file=sys.stderr)
            push_wecom_markdown(webhook, report_date, selected, brief, "", mentioned_mobile_list, wecom)
            return
        push_wecom_news(webhook, report_date, selected, brief, public_url, config)
    elif mode == "file":
        push_wecom_markdown(webhook, report_date, selected, brief, public_url, mentioned_mobile_list, wecom)
        push_wecom_file(webhook, output_file)
    else:
        push_wecom_markdown(webhook, report_date, selected, brief, public_url, mentioned_mobile_list, wecom)


def push_wecom_markdown(
    webhook: str,
    report_date: str,
    selected: list[Item],
    brief: dict[str, Any],
    public_url: str,
    mentioned_mobile_list: list[str],
    wecom: dict[str, Any] | None = None,
) -> None:
    wecom = wecom or {}
    max_bytes = int(wecom.get("markdown_max_bytes", WECOM_MARKDOWN_MAX_BYTES))
    item_limit = int(wecom.get("markdown_item_limit", 8))
    item_summary_chars = int(wecom.get("markdown_item_summary_chars", 80))
    executive_summary_chars = int(wecom.get("markdown_executive_summary_chars", 220))
    mention_suffix = ""
    if mentioned_mobile_list:
        mention_suffix = "\n\n" + " ".join(f"<@{mobile}>" for mobile in mentioned_mobile_list)
    content = markdown_summary(
        report_date,
        selected,
        brief,
        public_url,
        item_limit=item_limit,
        item_summary_chars=item_summary_chars,
        executive_summary_chars=executive_summary_chars,
    )
    content = fit_wecom_markdown(content, max_bytes - utf8_len(mention_suffix)) + mention_suffix
    result = post_json(webhook, {"msgtype": "markdown", "markdown": {"content": content}})
    check_wecom_result(result, "markdown")


def push_wecom_news(webhook: str, report_date: str, selected: list[Item], brief: dict[str, Any], public_url: str, config: dict[str, Any]) -> None:
    title = brief.get("headline") or f"{report_date} AI 简报"
    description = brief.get("executive_summary") or "今日 AI 重点信息已整理完成。"
    payload = {
        "msgtype": "news",
        "news": {
            "articles": [
                {
                    "title": truncate(title, 64),
                    "description": truncate(description, 120),
                    "url": public_url,
                    "picurl": config.get("cover_image_url", ""),
                }
            ]
        },
    }
    result = post_json(webhook, payload)
    check_wecom_result(result, "news")


def push_wecom_file(webhook: str, output_file: Path) -> None:
    key = urllib.parse.parse_qs(urllib.parse.urlsplit(webhook).query).get("key", [""])[0]
    if not key:
        print("[warn] WeCom file mode requires webhook key", file=sys.stderr)
        return
    media_id = upload_wecom_file(key, output_file)
    result = post_json(webhook, {"msgtype": "file", "file": {"media_id": media_id}})
    check_wecom_result(result, "file")


def upload_wecom_file(key: str, output_file: Path) -> str:
    boundary = f"----ai-news-agent-{int(time.time())}"
    content = output_file.read_bytes()
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{output_file.name}"\r\n'
        "Content-Type: text/html\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header + content + footer
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={urllib.parse.quote(key)}&type=file"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    check_wecom_result(result, "upload file")
    media_id = result.get("media_id")
    if not media_id:
        raise RuntimeError(f"WeCom upload returned no media_id: {result}")
    return media_id


def check_wecom_result(result: Any, label: str) -> None:
    if isinstance(result, dict) and result.get("errcode") not in (None, 0):
        raise RuntimeError(f"WeCom {label} failed: {result}")


def load_quality_cases(project_dir: Path) -> dict[str, Any]:
    path = project_dir / "state" / "quality_cases.json"
    if not path.exists():
        return {"version": 1, "updated_at": "", "good_cases": [], "bad_cases": [], "evaluation_history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[warn] failed to load quality cases: {exc}", file=sys.stderr)
        return {"version": 1, "updated_at": "", "good_cases": [], "bad_cases": [], "evaluation_history": []}


def save_quality_cases(project_dir: Path, cases: dict[str, Any]) -> None:
    state_dir = project_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cases["updated_at"] = utc_now().isoformat()
    path = state_dir / "quality_cases.json"
    path.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[info] saved quality cases to {path}", file=sys.stderr)


def rotate_cases(case_list: list[dict[str, Any]], max_count: int, max_per_kind: int, max_age_days: int, today: str) -> list[dict[str, Any]]:
    try:
        today_date = dt.date.fromisoformat(today)
    except Exception:
        today_date = dt.date.today()
    result = []
    for case in case_list:
        added = case.get("added_at", "")
        try:
            age = (today_date - dt.date.fromisoformat(added)).days
        except Exception:
            age = 999
        if age <= max_age_days:
            result.append(case)
    result.sort(key=lambda c: c.get("added_at", ""), reverse=True)
    kind_counts: dict[str, int] = {}
    filtered = []
    for case in result:
        kind = case.get("kind", "unknown")
        if kind_counts.get(kind, 0) >= max_per_kind:
            continue
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        filtered.append(case)
        if len(filtered) >= max_count:
            break
    return filtered


def select_cases_for_prompt(cases: dict[str, Any], items: list["Item"], config: dict[str, Any]) -> dict[str, Any] | None:
    quality_config = config.get("quality", {})
    max_examples = int(quality_config.get("max_prompt_examples", 5))
    good_cases = cases.get("good_cases", [])
    bad_cases = cases.get("bad_cases", [])
    if not good_cases and not bad_cases:
        return None
    item_kinds = {item.kind for item in items}
    max_good = min(3, max_examples)
    max_bad = max_examples - max_good

    def pick(case_list: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        matched = [c for c in case_list if c.get("kind") in item_kinds]
        unmatched = [c for c in case_list if c.get("kind") not in item_kinds]
        return (matched + unmatched)[:limit]

    good_picked = pick(good_cases, max_good)
    bad_picked = pick(bad_cases, max_bad)
    if not good_picked and not bad_picked:
        return None
    examples: dict[str, Any] = {"instruction": "参考以上好案例的写法标准和坏案例的常见问题，提升输出质量。"}
    if good_picked:
        examples["good_examples"] = [
            {"input": c["input_snippet"], "expected_output": c["output_snippet"], "why_good": c.get("reason", "")}
            for c in good_picked
        ]
    if bad_picked:
        examples["bad_examples"] = [
            {"input": c["input_snippet"], "actual_output": c["output_snippet"], "why_bad": c.get("reason", "")}
            for c in bad_picked
        ]
    return examples


def evaluate_report_quality(
    selected: list["Item"], brief: dict[str, Any], candidates: list["Item"], config: dict[str, Any]
) -> dict[str, Any] | None:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or not selected:
        return None
    model = os.environ.get("OPENAI_MODEL") or "gpt-5.5"
    quality_config = config.get("quality", {})
    timeout = int(quality_config.get("evaluation_timeout_seconds", 120))

    brief_items_by_id = {}
    for bi in brief.get("items", []):
        brief_items_by_id[bi.get("id", "")] = bi

    eval_items = []
    for item in selected:
        bi = brief_items_by_id.get(item.id, {})
        eval_items.append({
            "id": item.id,
            "title": item.title,
            "kind": item.kind,
            "source": item.source,
            "original_summary": (item.details or item.summary or "")[:500],
            "first_seen": item.first_seen,
            "ai_summary_zh": bi.get("summary_zh", item.summary),
            "ai_why_it_matters": bi.get("why_it_matters", item.reason),
            "ai_category": bi.get("category", item.category),
            "ai_score": bi.get("score", item.ai_score or item.raw_score),
        })

    eval_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "report_scores": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "headline_quality": {"type": "integer", "minimum": 1, "maximum": 5},
                    "executive_summary_quality": {"type": "integer", "minimum": 1, "maximum": 5},
                    "category_diversity": {"type": "integer", "minimum": 1, "maximum": 5},
                    "importance_distribution": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["headline_quality", "executive_summary_quality", "category_diversity", "importance_distribution"],
            },
            "item_evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "summary_accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
                        "summary_specificity": {"type": "integer", "minimum": 1, "maximum": 5},
                        "value_judgment": {"type": "integer", "minimum": 1, "maximum": 5},
                        "category_correctness": {"type": "integer", "minimum": 1, "maximum": 5},
                        "format_compliance": {"type": "integer", "minimum": 1, "maximum": 5},
                        "score_calibration": {"type": "integer", "minimum": 1, "maximum": 5},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "summary_accuracy", "summary_specificity", "value_judgment",
                                 "category_correctness", "format_compliance", "score_calibration", "reason"],
                },
            },
        },
        "required": ["report_scores", "item_evaluations"],
    }

    prompt = {
        "task": "你是 AI 日报质量评审员。评估以下已生成日报的质量，对每条内容逐项打分(1-5)。",
        "scoring_criteria": {
            "summary_accuracy": "summary_zh 是否忠实反映原文内容（original_summary），不添加、不遗漏关键事实",
            "summary_specificity": "是否包含具体事实（数字、方法名、模型名、版本号），而非空话套话",
            "value_judgment": "why_it_matters 是否有针对性地说明对 AI 从业者的意义，而非泛泛而谈",
            "category_correctness": "分类标签是否恰当，是否与内容主题匹配",
            "format_compliance": "GitHub 项目是否按结构写（项目介绍/提交/版本/关注点），论文是否总结方法和结论",
            "score_calibration": "AI 打分是否合理（重大发布应高分，普通更新不应满分）",
        },
        "report_criteria": {
            "headline_quality": "标题是否概括了当日最重要的主题",
            "executive_summary_quality": "摘要是否精炼且有信息量",
            "category_diversity": "分类是否合理，不超过6个且无过度碎片化",
            "importance_distribution": "重要性标注分布是否合理",
        },
        "headline": brief.get("headline", ""),
        "executive_summary": brief.get("executive_summary", ""),
        "themes": brief.get("themes", []),
        "items": eval_items,
    }

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": "你是严谨的 AI 日报质量评审员。按评分标准逐项打分，给出具体理由。输出 JSON。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {"type": "json_schema", "name": "quality_evaluation", "schema": eval_schema, "strict": True},
            "verbosity": "low",
        },
        "reasoning": {"effort": "low"},
    }

    req = urllib.request.Request(
        openai_responses_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[warn] quality evaluation failed: {exc}", file=sys.stderr)
        return None

    text = extract_response_text(data)
    if not text:
        print("[warn] quality evaluation returned no text", file=sys.stderr)
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[warn] quality evaluation JSON parse failed: {exc}", file=sys.stderr)
        return None


def classify_and_store_cases(
    project_dir: Path,
    evaluation: dict[str, Any],
    selected: list["Item"],
    brief: dict[str, Any],
    report_date: str,
    config: dict[str, Any],
) -> None:
    quality_config = config.get("quality", {})
    max_good = int(quality_config.get("max_good_cases", 20))
    max_bad = int(quality_config.get("max_bad_cases", 20))
    max_per_kind = int(quality_config.get("max_per_kind", 6))
    max_age_days = int(quality_config.get("max_age_days", 30))

    cases = load_quality_cases(project_dir)
    items_by_id = {item.id: item for item in selected}
    brief_items_by_id = {bi.get("id", ""): bi for bi in brief.get("items", [])}

    good_count = 0
    bad_count = 0

    for item_eval in evaluation.get("item_evaluations", []):
        item_id = item_eval.get("id", "")
        item = items_by_id.get(item_id)
        if not item:
            continue
        bi = brief_items_by_id.get(item_id, {})

        scores = [
            item_eval.get("summary_accuracy", 3),
            item_eval.get("summary_specificity", 3),
            item_eval.get("value_judgment", 3),
            item_eval.get("category_correctness", 3),
            item_eval.get("format_compliance", 3),
            item_eval.get("score_calibration", 3),
        ]
        avg = sum(scores) / len(scores)
        min_score = min(scores)

        case_entry = {
            "added_at": report_date,
            "report_date": report_date,
            "kind": item.kind,
            "input_snippet": {
                "title": item.title,
                "source": item.source,
                "kind": item.kind,
                "summary": (item.details or item.summary or "")[:300],
                "first_seen": item.first_seen,
            },
            "output_snippet": {
                "summary_zh": bi.get("summary_zh", item.summary),
                "why_it_matters": bi.get("why_it_matters", item.reason),
                "category": bi.get("category", item.category),
                "score": bi.get("score", item.ai_score or item.raw_score),
            },
            "reason": item_eval.get("reason", ""),
        }

        if min_score <= 2:
            cases["bad_cases"].append(case_entry)
            bad_count += 1
        elif min_score >= 3 and avg >= 3.5:
            cases["good_cases"].append(case_entry)
            good_count += 1

    cases["good_cases"] = rotate_cases(cases["good_cases"], max_good, max_per_kind, max_age_days, report_date)
    cases["bad_cases"] = rotate_cases(cases["bad_cases"], max_bad, max_per_kind, max_age_days, report_date)

    history = cases.setdefault("evaluation_history", [])
    history.append({
        "report_date": report_date,
        "item_count": len(selected),
        "good_count": good_count,
        "bad_count": bad_count,
        "report_scores": evaluation.get("report_scores", {}),
    })
    if len(history) > 90:
        cases["evaluation_history"] = history[-90:]

    save_quality_cases(project_dir, cases)
    print(f"[info] quality: {good_count} good, {bad_count} bad cases from {len(selected)} items", file=sys.stderr)


def print_evaluation_report(evaluation: dict[str, Any], report_date: str) -> None:
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  日报质量评估报告 - {report_date}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    report_scores = evaluation.get("report_scores", {})
    if report_scores:
        print("\n  整体评分:", file=sys.stderr)
        labels = {
            "headline_quality": "标题质量",
            "executive_summary_quality": "摘要质量",
            "category_diversity": "分类多样性",
            "importance_distribution": "重要性分布",
        }
        for key, label in labels.items():
            score = report_scores.get(key, "-")
            print(f"    {label}: {score}/5", file=sys.stderr)

    item_evals = evaluation.get("item_evaluations", [])
    if item_evals:
        print(f"\n  逐条评分 ({len(item_evals)} 条):", file=sys.stderr)
        dims = ["summary_accuracy", "summary_specificity", "value_judgment",
                "category_correctness", "format_compliance", "score_calibration"]
        dim_labels = ["准确性", "具体性", "价值判断", "分类", "格式", "打分"]
        print(f"    {'ID':<40} {'  '.join(f'{l:>4}' for l in dim_labels)}  avg", file=sys.stderr)
        for ie in item_evals:
            scores = [ie.get(d, 0) for d in dims]
            avg = sum(scores) / len(scores) if scores else 0
            short_id = ie.get("id", "?")[:38]
            scores_str = "  ".join(f"{s:>4}" for s in scores)
            print(f"    {short_id:<40} {scores_str}  {avg:.1f}", file=sys.stderr)

    print(f"\n{'='*60}\n", file=sys.stderr)


def run_check_mode(project_dir: Path, config: dict[str, Any], check_date: str) -> int:
    json_path = project_dir / "output" / f"{check_date}.json"
    if not json_path.exists():
        print(f"[error] {json_path} not found. Run with output.write_json=true first.", file=sys.stderr)
        return 1
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[error] failed to load {json_path}: {exc}", file=sys.stderr)
        return 1

    brief = data.get("brief", {})
    selected_dicts = data.get("selected", [])
    selected = []
    for d in selected_dicts:
        item = Item(
            id=d.get("id", ""),
            title=d.get("title", ""),
            url=d.get("url", ""),
            source=d.get("source", ""),
            kind=d.get("kind", ""),
            summary=d.get("summary", ""),
            published_at=d.get("published_at", ""),
            raw_score=d.get("raw_score", 0),
            ai_score=d.get("ai_score"),
            importance=d.get("importance", "medium"),
            reason=d.get("reason", ""),
            category=d.get("category", "AI"),
            details=d.get("details", ""),
            first_seen=d.get("first_seen", False),
        )
        selected.append(item)

    candidates_dicts = data.get("ranked_candidates", [])
    candidates = []
    for d in candidates_dicts:
        item = Item(
            id=d.get("id", ""),
            title=d.get("title", ""),
            url=d.get("url", ""),
            source=d.get("source", ""),
            kind=d.get("kind", ""),
            summary=d.get("summary", ""),
            published_at=d.get("published_at", ""),
            raw_score=d.get("raw_score", 0),
            ai_score=d.get("ai_score"),
            importance=d.get("importance", "medium"),
            reason=d.get("reason", ""),
            category=d.get("category", "AI"),
            details=d.get("details", ""),
            first_seen=d.get("first_seen", False),
        )
        candidates.append(item)

    print(f"[info] evaluating report for {check_date} ({len(selected)} items)", file=sys.stderr)
    evaluation = evaluate_report_quality(selected, brief, candidates, config)
    if not evaluation:
        print("[error] quality evaluation failed", file=sys.stderr)
        return 1

    print_evaluation_report(evaluation, check_date)
    classify_and_store_cases(project_dir, evaluation, selected, brief, check_date, config)
    return 0


def generate_optimization_report(
    history: list[dict[str, Any]],
    bad_cases: list[dict[str, Any]],
    good_cases: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("[error] OPENAI_API_KEY required for --review", file=sys.stderr)
        return None
    model = os.environ.get("OPENAI_MODEL") or "gpt-5.5"
    timeout = int(config.get("quality", {}).get("evaluation_timeout_seconds", 120))

    recent_history = history[-7:]
    dims = ["headline_quality", "executive_summary_quality", "category_diversity", "importance_distribution"]
    dim_avgs: dict[str, float] = {}
    for dim in dims:
        vals = [h.get("report_scores", {}).get(dim, 0) for h in recent_history if h.get("report_scores", {}).get(dim)]
        dim_avgs[dim] = round(sum(vals) / len(vals), 2) if vals else 0

    trend_data = {
        "days_covered": len(recent_history),
        "report_dimension_averages": dim_avgs,
        "daily_good_bad": [
            {"date": h.get("report_date", ""), "items": h.get("item_count", 0),
             "good": h.get("good_count", 0), "bad": h.get("bad_count", 0)}
            for h in recent_history
        ],
    }

    bad_summaries = []
    for c in bad_cases[:20]:
        bad_summaries.append({
            "kind": c.get("kind", ""),
            "title": c.get("input_snippet", {}).get("title", ""),
            "output_summary_zh": c.get("output_snippet", {}).get("summary_zh", "")[:200],
            "output_why_it_matters": c.get("output_snippet", {}).get("why_it_matters", "")[:200],
            "reason": c.get("reason", ""),
        })

    good_summaries = []
    for c in good_cases[:5]:
        good_summaries.append({
            "kind": c.get("kind", ""),
            "title": c.get("input_snippet", {}).get("title", ""),
            "reason": c.get("reason", ""),
        })

    numbered_rules = [{"index": i, "rule": r} for i, r in enumerate(SELECTION_RULES)]

    review_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "overall_assessment": {"type": "string"},
            "trend": {"type": "string", "enum": ["improving", "stable", "declining"]},
            "top_issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "issue": {"type": "string"},
                        "affected_dimension": {"type": "string"},
                        "frequency": {"type": "string"},
                        "example_titles": {"type": "array", "items": {"type": "string"}},
                        "root_cause": {"type": "string"},
                    },
                    "required": ["issue", "affected_dimension", "frequency", "example_titles", "root_cause"],
                },
            },
            "rule_suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "modify", "delete"]},
                        "target_rule_index": {"type": ["integer", "null"]},
                        "current_rule": {"type": "string"},
                        "suggested_rule": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["action", "target_rule_index", "current_rule", "suggested_rule", "reason"],
                },
            },
            "config_suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string"},
                        "current_value": {"type": "string"},
                        "suggested_value": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["key", "current_value", "suggested_value", "reason"],
                },
            },
        },
        "required": ["overall_assessment", "trend", "top_issues", "rule_suggestions", "config_suggestions"],
    }

    prompt = {
        "task": "你是 AI 日报系统的 prompt 优化顾问。根据近期质量评估数据和 bad case 分析，给出具体的 prompt 规则修改建议。",
        "trend_data": trend_data,
        "bad_cases": bad_summaries,
        "good_cases_reference": good_summaries,
        "current_selection_rules": numbered_rules,
        "instructions": [
            "分析 bad case 的共性问题，找出最常出现的质量缺陷模式。",
            "针对每个问题，判断是否可以通过修改 selection_rules 来解决。",
            "rule_suggestions 中 target_rule_index 必须对应 current_selection_rules 中的 index；add 时设为 null。",
            "config_suggestions 用于建议调整 config.json 中的参数（如 min_score、report_kind_limits 等）。",
            "只建议有明确证据支持的修改，不要泛泛而谈。",
            "如果质量已经很好，可以返回空的 rule_suggestions 和 config_suggestions。",
        ],
    }

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": "你是 AI 日报系统的 prompt 优化顾问。分析质量数据，输出结构化优化建议。输出 JSON。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {"type": "json_schema", "name": "optimization_report", "schema": review_schema, "strict": True},
            "verbosity": "low",
        },
        "reasoning": {"effort": "medium"},
    }

    req = urllib.request.Request(
        openai_responses_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[error] optimization report generation failed: {exc}", file=sys.stderr)
        return None

    text = extract_response_text(data)
    if not text:
        print("[error] optimization report returned no text", file=sys.stderr)
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[error] optimization report JSON parse failed: {exc}", file=sys.stderr)
        return None


def render_review_markdown(report: dict[str, Any], report_date: str) -> str:
    trend_label = {"improving": "上升", "stable": "持平", "declining": "下降"}.get(
        report.get("trend", "stable"), "持平"
    )
    lines = [
        f"# AI 日报优化建议报告 - {report_date}",
        "",
        "## 总体评估",
        "",
        f"**趋势**: {trend_label}",
        "",
        report.get("overall_assessment", ""),
        "",
    ]

    top_issues = report.get("top_issues", [])
    if top_issues:
        lines.append("## 主要问题")
        lines.append("")
        for i, issue in enumerate(top_issues, 1):
            lines.append(f"### {i}. {issue.get('issue', '')}")
            lines.append("")
            lines.append(f"- **影响维度**: {issue.get('affected_dimension', '')}")
            lines.append(f"- **出现频率**: {issue.get('frequency', '')}")
            lines.append(f"- **根因分析**: {issue.get('root_cause', '')}")
            examples = issue.get("example_titles", [])
            if examples:
                lines.append(f"- **涉及条目**: {', '.join(examples[:5])}")
            lines.append("")

    rule_suggestions = report.get("rule_suggestions", [])
    if rule_suggestions:
        lines.append("## Prompt 规则修改建议")
        lines.append("")
        for i, s in enumerate(rule_suggestions, 1):
            action = {"add": "新增", "modify": "修改", "delete": "删除"}.get(s.get("action", ""), s.get("action", ""))
            lines.append(f"### {i}. [{action}] 规则")
            lines.append("")
            if s.get("current_rule"):
                idx = s.get("target_rule_index")
                idx_str = f" (index {idx})" if idx is not None else ""
                lines.append(f"**当前{idx_str}**:")
                lines.append(f"> {s['current_rule']}")
                lines.append("")
            if s.get("suggested_rule"):
                lines.append("**建议**:")
                lines.append(f"> {s['suggested_rule']}")
                lines.append("")
            lines.append(f"**理由**: {s.get('reason', '')}")
            lines.append("")
        lines.append("**操作方式**: 修改 `src/daily_ai_news.py` 中的 `SELECTION_RULES` 列表。")
        lines.append("")

    config_suggestions = report.get("config_suggestions", [])
    if config_suggestions:
        lines.append("## Config 调参建议")
        lines.append("")
        lines.append("| 参数 | 当前值 | 建议值 | 理由 |")
        lines.append("|------|--------|--------|------|")
        for s in config_suggestions:
            lines.append(f"| `{s.get('key', '')}` | {s.get('current_value', '')} | {s.get('suggested_value', '')} | {s.get('reason', '')} |")
        lines.append("")
        lines.append("**操作方式**: 修改 `config.json` 中对应的字段。")
        lines.append("")

    if not rule_suggestions and not config_suggestions:
        lines.append("## 建议")
        lines.append("")
        lines.append("当前质量表现良好，暂无需要修改的规则或配置。")
        lines.append("")

    lines.append("---")
    lines.append("*本报告由 AI 自动生成，请人工审核后再合入修改。*")
    lines.append("")
    return "\n".join(lines)


def run_review_mode(project_dir: Path, config: dict[str, Any]) -> int:
    cases = load_quality_cases(project_dir)
    history = cases.get("evaluation_history", [])
    bad_cases = cases.get("bad_cases", [])
    good_cases = cases.get("good_cases", [])

    if len(history) < 3:
        print("[error] need at least 3 days of evaluation history for --review. Run daily reports first.", file=sys.stderr)
        return 1

    if not bad_cases:
        print("[info] no bad cases found. Quality is good, no optimization needed.", file=sys.stderr)
        return 0

    report_date = local_now(config.get("timezone", "Asia/Shanghai")).strftime("%Y-%m-%d")
    print(f"[info] generating optimization report ({len(bad_cases)} bad cases, {len(history)} days of history)", file=sys.stderr)

    report = generate_optimization_report(history, bad_cases, good_cases, config)
    if not report:
        print("[error] failed to generate optimization report", file=sys.stderr)
        return 1

    md = render_review_markdown(report, report_date)
    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"review-{report_date}.md"
    output_path.write_text(md, encoding="utf-8")
    print(f"[info] wrote {output_path}", file=sys.stderr)
    print(md)
    return 0


def collect_items(config: dict[str, Any]) -> list[Item]:
    items: list[Item] = []
    items.extend(fetch_rss_feeds(config))
    items.extend(fetch_official_pages(config))
    items.extend(fetch_arxiv(config))
    items.extend(fetch_github(config))
    items.extend(fetch_newsapi(config))
    return dedupe_items(items)


def write_outputs(project_dir: Path, report_date: str, selected: list[Item], all_ranked: list[Item], brief: dict[str, Any], config: dict[str, Any]) -> Path:
    output_dir = project_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    html_file = output_dir / f"{report_date}.html"
    html_file.write_text(render_html(report_date, selected, brief, config), encoding="utf-8")
    output_config = config.get("output", {})
    if output_config.get("write_json", False):
        json_file = output_dir / f"{report_date}.json"
        json_file.write_text(
            json.dumps(
                {
                    "date": report_date,
                    "brief": brief,
                    "selected": [item.to_dict() for item in selected],
                    "ranked_candidates": [item.to_dict() for item in all_ranked],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return html_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a daily AI news HTML brief.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--check", nargs="?", const="today", default=None,
                        help="Evaluate quality of a generated report. Pass a date (YYYY-MM-DD) or omit for today.")
    parser.add_argument("--review", action="store_true",
                        help="Generate weekly optimization report based on accumulated bad cases.")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    config_path = Path(args.config)
    env_path = Path(args.env)
    if not config_path.is_absolute():
        config_path = project_dir / config_path
    if not env_path.is_absolute():
        env_path = project_dir / env_path

    load_env(env_path)
    config = load_config(config_path)
    (project_dir / "logs").mkdir(exist_ok=True)
    report_date = local_now(config.get("timezone", "Asia/Shanghai")).strftime("%Y-%m-%d")

    if args.check is not None:
        check_date = report_date if args.check == "today" else args.check
        return run_check_mode(project_dir, config, check_date)

    if args.review:
        return run_review_mode(project_dir, config)

    print(f"[info] collecting items for {report_date}")
    items = collect_items(config)
    print(f"[info] collected {len(items)} unique items")
    published_history = load_published_history(project_dir, report_date, config)
    items, skipped_published = filter_previously_published(items, published_history, report_date, config)
    if skipped_published:
        print(f"[info] skipped {skipped_published} previously published items")
    ranked = filter_and_rank(items, config)
    print(f"[info] retained {len(ranked)} candidates")
    seen_after_run = mark_first_seen(ranked, project_dir)
    print("[info] fetching item details")
    enrich_item_details(ranked, config)
    quality_cases = load_quality_cases(project_dir)
    brief, model_error = call_openai_for_brief(ranked, config, quality_cases)
    selected, brief = apply_ai_brief(ranked, brief, config, model_error)
    output_file = write_outputs(project_dir, report_date, selected, ranked, brief, config)
    save_seen_items(project_dir, seen_after_run)
    save_published_history(project_dir, published_history, selected, report_date)
    print(f"[info] wrote {output_file}")

    quality_config = config.get("quality", {})
    if quality_config.get("enabled", True) and brief and not model_error:
        evaluation = evaluate_report_quality(selected, brief, ranked, config)
        if evaluation:
            classify_and_store_cases(project_dir, evaluation, selected, brief, report_date, config)
            print_evaluation_report(evaluation, report_date)

    if not args.no_push:
        push_wecom(config, selected, brief, report_date, output_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
