"""
Google Cloud Functions (HTTP): 정기 경제 RSS 리포트 및 텔레그램 웹훅 키워드 검색.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import functions_framework
import requests

DAILY_RSS_URL = (
    "https://news.google.com/rss/sections/topic/"
    "CAAqIggKIhxDQklTR2dnTWFoWUtGbmRyYjI0dVpXNXpLQUFQAQ"
    "?hl=ko&gl=KR&ceid=KR:ko"
)
SEARCH_RSS_BASE = "https://news.google.com/rss/search"


def _telegram_credentials() -> tuple[str, str]:
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID 환경 변수가 필요합니다."
        )
    return token, chat_id


def _entry_published(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    tup = entry.get("published_p") or entry.get("updated_p")
    if tup:
        try:
            return datetime(
                tup.tm_year,
                tup.tm_mon,
                tup.tm_mday,
                tup.tm_hour,
                tup.tm_min,
                tup.tm_sec,
                tzinfo=timezone.utc,
            )
        except (TypeError, ValueError):
            pass
    return None


def _tokenize_titles_for_counter(titles: list[str]) -> list[str]:
    tokens: list[str] = []
    for t in titles:
        if not t:
            continue
        tokens += re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", t)
    return tokens


def _top_keywords(titles: list[str], k: int = 3) -> list[str]:
    counter = Counter(_tokenize_titles_for_counter(titles))
    return [word for word, _ in counter.most_common(k)]


def _split_headline_source(raw_title: str) -> tuple[str, str]:
    raw_title = (raw_title or "").strip()
    parts = raw_title.rsplit(" - ", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return raw_title, "출처 미상"


def _fetch_entries(url: str) -> list[feedparser.FeedParserDict]:
    parsed = feedparser.parse(url)
    return list(parsed.entries or [])


def _filter_within_hours(
    entries: list[feedparser.FeedParserDict], hours: int
) -> list[feedparser.FeedParserDict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept: list[feedparser.FeedParserDict] = []
    for e in entries:
        pub = _entry_published(e)
        if pub is None:
            continue
        if pub >= cutoff:
            kept.append(e)
    return kept


def _normalized_articles(
    entries: list[feedparser.FeedParserDict],
) -> list[dict]:
    rows: list[dict] = []
    for e in entries:
        raw_title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not raw_title:
            continue
        headline, outlet = _split_headline_source(raw_title)
        rows.append(
            {
                "raw_title": raw_title,
                "headline": headline,
                "outlet": outlet,
                "link": link,
                "published": _entry_published(e),
            }
        )
    return rows


def _dedupe_sort_latest(articles: list[dict], limit: int) -> list[dict]:
    deduped: dict[str, dict] = {}
    for a in articles:
        key = re.sub(r"\s+", " ", a["headline"]).strip().lower()
        if not key:
            continue
        prev = deduped.get(key)
        ts = a["published"]
        if prev is None:
            deduped[key] = a
            continue
        pts = prev["published"]
        if ts and (pts is None or ts > pts):
            deduped[key] = a
        elif pts is None and ts:
            deduped[key] = a

    def sort_key(x: dict) -> datetime:
        return x["published"] or datetime.min.replace(tzinfo=timezone.utc)

    ordered = sorted(deduped.values(), key=sort_key, reverse=True)
    return ordered[:limit]


def _format_message(
    keywords: list[str],
    picks: list[dict],
    footer: str,
) -> str:
    if keywords:
        k1, k2, k3 = (keywords + ["-", "-", "-"])[:3]
        kw_line = f"📊 오늘의 핵심 키워드: {k1}, {k2}, {k3}"
    else:
        kw_line = "📊 오늘의 핵심 키워드: (추출할 제목이 없습니다)"

    body_lines: list[str] = []
    for i, a in enumerate(picks, start=1):
        line = f"[{i}] {a['headline']} - {a['outlet']}"
        if a["link"]:
            line += f" ({a['link']})"
        body_lines.append(line)

    if not body_lines:
        body_lines.append("(표시할 뉴스가 없습니다.)")

    return "\n".join([kw_line, "", *body_lines, "", footer])


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    resp.raise_for_status()


def _parse_mode(request) -> tuple[str | None, str | None]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, None

    if data.get("action") == "daily_report":
        return "daily", None

    msg = data.get("message") or data.get("edited_message") or {}
    if isinstance(msg, dict):
        text = (msg.get("text") or "").strip()
        if text:
            return "search", text

    return None, None


@functions_framework.http
def telegram_news(request):
    """HTTP 엔트리포인트. Scheduler(JSON) 또는 텔레그램 웹훅 본문을 처리한다."""
    try:
        token, chat_id = _telegram_credentials()
    except RuntimeError as exc:
        return (str(exc), 500)

    mode, keyword = _parse_mode(request)
    if mode is None:
        return ("OK", 200)

    if mode == "daily":
        entries = _fetch_entries(DAILY_RSS_URL)
        entries = _filter_within_hours(entries, 24)
        footer = "오늘의 정기 리포트입니다."
    else:
        params = {
            "q": keyword,
            "hl": "ko",
            "gl": "KR",
            "ceid": "KR:ko",
        }
        url = f"{SEARCH_RSS_BASE}?{urllib.parse.urlencode(params)}"
        entries = _fetch_entries(url)
        footer = f"키워드 '{keyword}'에 대한 검색 결과입니다."

    articles = _normalized_articles(entries)
    all_titles = [a["raw_title"] for a in articles]
    top_kw = _top_keywords(all_titles, 3)
    picks = _dedupe_sort_latest(articles, 10)
    message = _format_message(top_kw, picks, footer)

    try:
        _send_telegram(token, chat_id, message)
    except requests.RequestException as exc:
        return (f"Telegram 전송 실패: {exc}", 502)

    return ("OK", 200)
