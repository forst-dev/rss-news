"""
Google Cloud Functions (HTTP): 정기 경제 RSS 리포트 및 텔레그램 웹훅 키워드 검색.
"""

from __future__ import annotations

import html
import logging
import os
import re
import unicodedata
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
KST = timezone(timedelta(hours=9))
_RSS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_log = logging.getLogger(__name__)


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


def _top_keywords(titles: list[str], k: int = 3) -> list[tuple[str, int]]:
    counter = Counter(_tokenize_titles_for_counter(titles))
    return counter.most_common(k)


def _split_headline_source(raw_title: str) -> tuple[str, str]:
    raw_title = (raw_title or "").strip()
    parts = raw_title.rsplit(" - ", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return raw_title, "출처 미상"


def _format_pub_time(published: datetime | None) -> str:
    if published is None:
        return "시간 정보 없음"
    local = published.astimezone(KST)
    return (
        f"{local.year:04d}년 {local.month:02d}월 {local.day:02d}일 "
        f"{local.hour:02d}시 {local.minute:02d}분"
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("value")
        if isinstance(inner, str):
            return inner
        return str(inner or "")
    return str(value)


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_match_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def _entry_searchable_text(entry: feedparser.FeedParserDict) -> str:
    """제목·요약·본문에서 키워드 매칭용 평문을 만든다 (HTML·공백 정리)."""
    chunks: list[str] = []
    chunks.append(_strip_html(_as_text(entry.get("title"))))
    chunks.append(_strip_html(_as_text(entry.get("summary"))))
    chunks.append(_strip_html(_as_text(entry.get("description"))))
    chunks.append(_strip_html(_as_text(entry.get("summary_detail"))))
    chunks.append(_strip_html(_as_text(entry.get("subtitle"))))
    for block in entry.get("content") or []:
        chunks.append(_strip_html(_as_text(block)))
    return _normalize_match_text(" ".join(c for c in chunks if c))


def _fetch_entries(url: str) -> list[feedparser.FeedParserDict]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _RSS_USER_AGENT},
            timeout=25,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        _log.warning("RSS HTTP fetch failed, falling back to feedparser URL: %s", exc)
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


def _matches_keyword(entry: feedparser.FeedParserDict, keyword: str) -> bool:
    """항목 제목·요약·본문 평문에 검색어(공백 분리 시 모두)가 포함되는지 확인한다."""
    raw = (keyword or "").strip()
    if not raw:
        return False
    if raw.startswith("/"):
        return False
    hay = _entry_searchable_text(entry)
    if not hay:
        return False
    terms = [t for t in re.split(r"\s+", raw.strip()) if t]
    if not terms:
        return False
    hay_nf = _normalize_match_text(hay)
    return all(_normalize_match_text(t) in hay_nf for t in terms)


def _economy_keyword_search_url(user_keyword: str) -> str:
    """섹션 피드만으로는 0건일 때 쓰는 보조 검색(경제 맥락 + 최근 1일)."""
    q = f"{user_keyword.strip()} 경제 when:1d"
    params = {"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    return f"{SEARCH_RSS_BASE}?{urllib.parse.urlencode(params)}"


def _filter_entries_by_keyword(
    entries: list[feedparser.FeedParserDict], keyword: str
) -> list[feedparser.FeedParserDict]:
    return [e for e in entries if _matches_keyword(e, keyword)]


def _normalized_articles(
    entries: list[feedparser.FeedParserDict],
) -> list[dict]:
    rows: list[dict] = []
    for e in entries:
        raw_title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not raw_title:
            continue
        published = _entry_published(e)
        if published is None:
            continue
        headline, outlet = _split_headline_source(raw_title)
        rows.append(
            {
                "raw_title": raw_title,
                "headline": headline,
                "outlet": outlet,
                "link": link,
                "published": published,
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
    keywords: list[tuple[str, int]],
    picks: list[dict],
    footer: str,
) -> str:
    if keywords:
        filled = (keywords + [("-", 0), ("-", 0), ("-", 0)])[:3]
        rendered = [f"{word}({count})" for word, count in filled]
        kw_line = f"📊 오늘의 핵심 키워드: {rendered[0]}, {rendered[1]}, {rendered[2]}"
    else:
        kw_line = "📊 오늘의 핵심 키워드: (추출할 제목이 없습니다)"

    body_lines: list[str] = []
    for i, a in enumerate(picks, start=1):
        pub_time = _format_pub_time(a["published"])
        line = f"[{i}] {a['outlet']} - {a['headline']} - {pub_time}"
        body_lines.append(line)

    if not body_lines:
        body_lines.append("(표시할 뉴스가 없습니다.)")

    # 한 줄 작성 후 빈 줄로 구분 (키워드 줄 / 각 뉴스 / 하단 문구)
    out: list[str] = [kw_line, ""]
    for bl in body_lines:
        out.extend([bl, ""])
    out.append(footer)
    return "\n".join(out)


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text or "(빈 메시지)"},
        timeout=30,
    )
    if not resp.ok:
        body = (resp.text or "")[:2000]
        _log.error(
            "Telegram sendMessage failed: status=%s body=%s",
            resp.status_code,
            body,
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
        kw = (keyword or "").strip()
        # 1) 경제 섹션 RSS → 24시간 → 키워드 (제목·요약 평문 매칭)
        section_entries = _filter_within_hours(_fetch_entries(DAILY_RSS_URL), 24)
        entries = _filter_entries_by_keyword(section_entries, kw)
        if not entries:
            # 2) 섹션 피드만으로는 최근 24시간·키워드 조합이 비는 경우가 많아
            #    동일 키워드로 '경제 + when:1d' 검색 RSS를 보조 소스로 사용한다.
            alt = _fetch_entries(_economy_keyword_search_url(kw))
            alt = _filter_within_hours(alt, 24)
            entries = _filter_entries_by_keyword(alt, kw)
            if entries:
                _log.info(
                    "keyword search: section feed had 0 hits; used economy search RSS "
                    "(section_24h=%s)",
                    len(section_entries),
                )
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
