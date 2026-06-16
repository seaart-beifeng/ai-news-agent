#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
        normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= limit:
            break
    return links


def official_article_item(source: str, url: str, page: str) -> Item | None:
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
    category_titles = [
        "blog | product launch",
        "blog | research",
        "blog | enterprise ai",
        "blog | ai for developers",
        "newsroom",
        "press",
        "events",
    ]
    if lowered_title in category_titles:
        return True
    if re.fullmatch(r"(blog|news|research|announcements|press)(/[a-z0-9-]+)?", lowered_url) and len(summary) < 300:
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
            full_name = repo.get("full_name", "")
            html_url = repo.get("html_url", "")
            stars = repo.get("stargazers_count", 0)
            desc = repo.get("description") or ""
            pushed_at = repo.get("pushed_at") or repo.get("updated_at") or ""
            if full_name and html_url:
                items.append(
                    Item(
                        id=stable_id(html_url, full_name),
                        title=f"{full_name} ({stars:,} stars)",
                        url=html_url,
                        source="GitHub",
                        kind="github",
                        summary=truncate(desc, 500),
                        published_at=pushed_at,
                    )
                )
        time.sleep(0.8)
    return items


def fetch_newsapi(config: dict[str, Any]) -> list[Item]:
    newsapi = config.get("newsapi", {})
    key = os.environ.get("NEWSAPI_KEY", "")
    if not newsapi.get("enabled", False) or not key:
        return []
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
            if title and link:
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
        "发布",
        "模型",
        "论文",
        "开源",
        "融资",
        "监管",
        "安全",
        "智能体",
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


def call_openai_for_brief(items: list[Item], config: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or not items:
        return None, "未配置 OPENAI_API_KEY"
    model = os.environ.get("OPENAI_MODEL") or "gpt-5.5"
    max_items = int(config.get("max_items_in_report", 18))
    max_items_for_ai = int(config.get("max_items_for_ai", max_items))
    timeout = int(config.get("openai_timeout_seconds", 180))
    candidates = [
        {
            "id": item.id,
            "title": item.title,
            "source": item.source,
            "kind": item.kind,
            "url": item.url,
            "summary": item.summary,
            "details": item.details,
            "published_at": item.published_at,
            "raw_score": item.raw_score,
            "first_seen": item.first_seen,
        }
        for item in items[: min(len(items), max_items_for_ai)]
    ]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string"},
            "executive_summary": {"type": "string"},
            "themes": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
            },
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
                    "required": [
                        "id",
                        "score",
                        "importance",
                        "category",
                        "summary_zh",
                        "why_it_matters",
                        "action",
                    ],
                },
            },
        },
        "required": ["headline", "executive_summary", "themes", "items"],
    }
    prompt = {
        "task": "从候选信息中筛选真正重要的 AI 新闻、论文、产品和工程信息，生成中文日报。",
        "selection_rules": [
            "优先一手来源、重大模型/产品发布、AI Agent、基础设施、开源项目、研究突破、监管与商业动作。",
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
            "如果当日提交信息不足，要明确写“未从公开提交信息中识别到明确的当日重要提交”。",
            "如果 GitHub 项目的 first_seen=true，summary_zh 必须以“首次收录项目。”开头，项目介绍可以更充分；如果 first_seen=false，要减少基础介绍，重点写更新变化。",
            "新闻/论文要总结事实、背景、影响和可继续阅读的重点。",
            "why_it_matters 必须用中文说明对 AI 从业者、产品或技术决策的具体意义，不要限制字数。",
        ],
        "max_items": max_items,
        "candidates": candidates,
    }
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": "你是一个严谨的 AI 行业情报分析员。输出必须是有效 JSON，中文表达简洁具体。",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "daily_ai_news_brief",
                "schema": schema,
                "strict": True,
            },
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
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[warn] OpenAI failed: HTTP {exc.code}: {body[:1000]}", file=sys.stderr)
        return None, f"模型调用失败：HTTP {exc.code}"
    except Exception as exc:
        print(f"[warn] OpenAI failed: {exc}", file=sys.stderr)
        return None, f"模型调用失败：{exc}"

    text = extract_response_text(data)
    if not text:
        print("[warn] OpenAI returned no text", file=sys.stderr)
        return None, "模型调用失败：返回内容为空"
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        print(f"[warn] OpenAI JSON parse failed: {exc}: {text[:500]}", file=sys.stderr)
        return None, f"模型调用失败：JSON 解析失败：{exc}"


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
    if item.details or item.summary:
        return local_model_required_summary(item, model_error)
    if item.kind == "arxiv":
        return f"论文摘要：围绕“{clean_title(item.title)}”展开，建议结合原文判断方法和实验价值。"
    if item.kind == "github":
        prefix = "首次收录项目。" if item.first_seen else ""
        return f"{prefix}未配置模型，无法把 README 自动总结成中文项目功能大纲；请配置 OPENAI_API_KEY 后重新生成。项目：{clean_title(item.title)}。"
    return f"内容摘要：{clean_title(item.title)}。"


def local_model_required_summary(item: Item, model_error: str = "") -> str:
    reason = model_error or "模型未返回可用结果"
    if item.kind == "github":
        prefix = "首次收录项目。" if item.first_seen else ""
        return (
            f"{prefix}已抓取该 GitHub 项目的 README，但中文功能总结生成失败。"
            f"原因：{reason}。系统会在模型调用成功后总结项目功能、核心能力、适用场景和技术特点。"
        )
    if item.kind == "arxiv":
        return f"已抓取论文摘要，但中文论文大纲生成失败。原因：{reason}。"
    return f"已抓取原文内容，但中文内容大纲生成失败。原因：{reason}。"


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
        return html.escape(truncate(text, 170))

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

    grouped: dict[str, list[tuple[int, Item]]] = {}
    for index, item in enumerate(selected, start=1):
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
    json_file = output_dir / f"{report_date}.json"
    html_file.write_text(render_html(report_date, selected, brief, config), encoding="utf-8")
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

    print(f"[info] collecting items for {report_date}")
    items = collect_items(config)
    print(f"[info] collected {len(items)} unique items")
    ranked = filter_and_rank(items, config)
    print(f"[info] retained {len(ranked)} candidates")
    seen_after_run = mark_first_seen(ranked, project_dir)
    print("[info] fetching item details")
    enrich_item_details(ranked, config)
    brief, model_error = call_openai_for_brief(ranked, config)
    selected, brief = apply_ai_brief(ranked, brief, config, model_error)
    output_file = write_outputs(project_dir, report_date, selected, ranked, brief, config)
    save_seen_items(project_dir, seen_after_run)
    print(f"[info] wrote {output_file}")

    if not args.no_push:
        push_wecom(config, selected, brief, report_date, output_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
