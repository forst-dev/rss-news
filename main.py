"""
Google Cloud Functions (HTTP): 정기 경제 RSS 리포트 및 텔레그램 웹훅 키워드 검색.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass

import html
import json
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

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)

# 전체 문자열이 호스트명처럼 보이면 언론사로 보지 않음 (예: v.daum.net)
_HOSTNAME_LIKE = re.compile(r"^[\w.-]+\.[a-zA-Z]{2,}$")
# 언론사 문자열 안에 끼어 있는 도메인 패턴
_DOMAIN_SNIPPET = re.compile(
    r"\b[a-z0-9][a-z0-9.-]*\.(com|net|org|co\.kr|go\.kr|or\.kr|kr)\b",
    re.IGNORECASE,
)


def _is_valid_outlet(outlet: str) -> bool:
    """도메인·URL 형태가 아닌 실제 언론사명으로 보일 때만 True."""
    o = (outlet or "").strip()
    if not o or o == "출처 미상":
        return False
    if "://" in o or o.lower().startswith("www."):
        return False
    if _HOSTNAME_LIKE.fullmatch(o):
        return False
    if _DOMAIN_SNIPPET.search(o):
        return False
    return True


def _telegram_credentials() -> tuple[str, str]:
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID 환경 변수가 필요합니다."
        )
    return token, chat_id


def _entry_published(entry: feedparser.FeedParserDict) -> datetime | None:
    """feedparser 표준 키(published_parsed 등)까지 사용해 발행 시각을 얻는다."""
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, datetime):
            dt = raw
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                continue
            try:
                dt = parsedate_to_datetime(s)
            except (TypeError, ValueError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

    tup = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
        or entry.get("published_p")
        or entry.get("updated_p")
    )
    if tup:
        try:
            return datetime.fromtimestamp(calendar.timegm(tup), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _tokenize_titles_for_counter(titles: list[str]) -> list[str]:
    tokens: list[str] = []
    for t in titles:
        if not t:
            continue
        tokens += re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", t)
    return tokens


def _token_norm_for_compare(tok: str) -> str:
    t = unicodedata.normalize("NFKC", (tok or "").strip())
    if re.fullmatch(r"[a-zA-Z]+", t):
        return t.lower()
    return t


def _query_exclusion_keys(query: str) -> frozenset[str]:
    """검색어에서 나온 토큰(핵심 키워드 집계에서 제외할 대상)."""
    q = unicodedata.normalize("NFKC", (query or "").strip())
    if not q:
        return frozenset()
    keys: set[str] = set()
    for part in re.split(r"\s+", q):
        part = part.strip()
        if not part:
            continue
        for t in re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", part):
            keys.add(_token_norm_for_compare(t))
        if re.fullmatch(r"[가-힣]{2,}", part):
            keys.add(_token_norm_for_compare(part))
        if re.fullmatch(r"[a-zA-Z]{2}", part):
            keys.add(part.lower())
    return frozenset(keys)


def _top_keywords(
    titles: list[str],
    k: int = 3,
    *,
    exclude_query: str | None = None,
) -> list[tuple[str, int]]:
    counter = Counter(_tokenize_titles_for_counter(titles))
    if exclude_query:
        banned = _query_exclusion_keys(exclude_query)
        if banned:
            for word in list(counter):
                if _token_norm_for_compare(word) in banned:
                    del counter[word]
    return counter.most_common(k)


def _split_headline_source(raw_title: str) -> tuple[str, str]:
    """구글 뉴스 제목은 '제목 - 언론사' 또는 en/em dash·파이프 구분을 쓰는 경우가 많다."""
    raw_title = (raw_title or "").strip()
    if not raw_title:
        return "", "출처 미상"
    for sep in (" - ", "\u2013", "\u2014", " | "):
        if sep not in raw_title:
            continue
        left, mid, right = raw_title.rpartition(sep)
        if mid and right.strip():
            return left.strip(), right.strip()
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


@dataclass(frozen=True)
class RSSFetchReport:
    http_ok: bool
    http_error: str | None
    entry_count: int
    feed_bozo: bool
    feed_bozo_message: str | None


def _fetch_entries_with_report(url: str) -> tuple[list[feedparser.FeedParserDict], RSSFetchReport]:
    http_ok = True
    http_error: str | None = None
    parsed = None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _RSS_USER_AGENT},
            timeout=25,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        http_ok = False
        http_error = str(exc)
        _log.warning("RSS HTTP fetch failed, falling back to feedparser URL: %s", exc)
        parsed = feedparser.parse(url)
    entries = list(parsed.entries or [])
    bozo = bool(getattr(parsed, "bozo", False))
    bozo_msg: str | None = None
    if bozo:
        exc = getattr(parsed, "bozo_exception", None)
        bozo_msg = repr(exc) if exc else "unknown"
    report = RSSFetchReport(
        http_ok=http_ok,
        http_error=http_error,
        entry_count=len(entries),
        feed_bozo=bozo,
        feed_bozo_message=bozo_msg,
    )
    return entries, report


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


def _economy_keyword_search_url(user_keyword: str | None = None) -> str:
    """경제 맥락 + 최근 1일"""
    q = f"{user_keyword or ''} 경제 when:1d"
    params = {"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    return f"{SEARCH_RSS_BASE}?{urllib.parse.urlencode(params)}"


def _filter_entries_by_keyword(
    entries: list[feedparser.FeedParserDict], keyword: str
) -> list[feedparser.FeedParserDict]:
    return [e for e in entries if _matches_keyword(e, keyword)]


def _normalized_articles(
    entries: list[feedparser.FeedParserDict],
) -> tuple[list[dict], dict[str, int]]:
    rows: list[dict] = []
    stats = {
        "no_title": 0,
        "no_pub": 0,
        "bad_outlet": 0,
        "kept": 0,
    }
    for e in entries:
        raw_title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not raw_title:
            stats["no_title"] += 1
            continue
        published = _entry_published(e)
        if published is None:
            stats["no_pub"] += 1
            continue
        headline, outlet = _split_headline_source(raw_title)
        if not _is_valid_outlet(outlet):
            stats["bad_outlet"] += 1
            continue
        stats["kept"] += 1
        rows.append(
            {
                "raw_title": raw_title,
                "headline": headline,
                "outlet": outlet,
                "link": link,
                "published": published,
            }
        )
    return rows, stats


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


def _pipeline_diagnostic_line(
    mode: str,
    fetch: RSSFetchReport | None,
    n_after_hours: int,
    norm_stats: dict[str, int],
    n_picks: int
) -> str:
    """표시 뉴스 0건일 때 텔레그램에 붙이는 한 블록 요약."""
    bits: list[str] = []
    if fetch is not None:
        bits.append(f"RSS {fetch.entry_count}건")
        if not fetch.http_ok:
            bits.append(f"HTTP실패({fetch.http_error or 'unknown'})")
        if fetch.feed_bozo and fetch.feed_bozo_message:
            bits.append(f"파싱경고({fetch.feed_bozo_message[:80]})")
    bits.append(f"24h이내 {n_after_hours}건")
    bits.append(
        f"표시준비 {norm_stats.get('kept', 0)}건 "
        f"(제목없음 {norm_stats.get('no_title', 0)}, "
        f"시각없음 {norm_stats.get('no_pub', 0)}, "
        f"언론사제외 {norm_stats.get('bad_outlet', 0)})"
    )
    bits.append(f"노출 {n_picks}건")

    if fetch is None:
        verdict = "→ RSS 수집 메타를 확인하지 못했습니다."
    elif fetch.entry_count == 0 and not fetch.http_ok:
        verdict = "→ HTTP 오류 등으로 RSS 항목을 받지 못했을 가능성이 큽니다."
    elif fetch.entry_count == 0:
        verdict = "→ RSS에 항목이 없습니다(차단·빈 응답·URL 문제 등)."
    elif n_after_hours == 0:
        verdict = "→ 최근 24시간·발행시각 조건에 맞는 기사가 없습니다."
    elif norm_stats.get("kept", 0) == 0:
        verdict = "→ 유효 언론사·시각 필터 후 남은 기사가 없습니다."
    else:
        verdict = "→ 중복 제거 등으로 표시 줄이 비었을 수 있습니다."

    return "[진단] " + " | ".join(bits) + "\n" + verdict


def _format_message(
    keywords: list[tuple[str, int]],
    picks: list[dict],
    footer: str,
    diagnostic: str | None = None,
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
    if diagnostic:
        out.extend([diagnostic, ""])
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


def _parse_json_body(request) -> dict | None:
    """Cloud Scheduler 등이 Content-Type 없이 JSON만 보낼 때도 본문을 dict로 읽는다."""
    data = request.get_json(force=True, silent=True)
    if isinstance(data, dict):
        return data
    raw = request.get_data(cache=True)
    if not raw:
        return None
    try:
        text = raw.decode("utf-8-sig").strip()
        if not text:
            return None
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_mode_data(data: dict | None) -> tuple[str | None, str | None]:
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
    payload = _parse_json_body(request)
    if isinstance(payload, dict) and payload.get("action") == "daily_report":
        mode, keyword = "daily", None
    else:
        mode, keyword = _parse_mode_data(payload)

    try:
        token, chat_id = _telegram_credentials()
    except RuntimeError as exc:
        return (str(exc), 500)
    if mode is None:
        return ("OK", 200)

    fetch_report: RSSFetchReport | None = None
    n_after_hours = 0

    if mode == "daily":
        entries, fetch_report = _fetch_entries_with_report(_economy_keyword_search_url())
        n_after_hours = len(entries)
        footer = "오늘의 정기 리포트입니다."
    else:
        kw = (keyword or "").strip()
        # 1) 경제 섹션 RSS → 이전 하루 → 키워드 (제목·요약 평문 매칭)
        section_entries, fetch_report = _fetch_entries_with_report(_economy_keyword_search_url(kw))
        n_after_hours = len(section_entries)
        entries = _filter_entries_by_keyword(section_entries, kw)
        if entries:
            _log.info(
                "keyword search: section feed had 0 hits; used economy search RSS "
                "(section_1d=%s)",
                len(section_entries),
            )
        footer = f"키워드 '{keyword}'에 대한 검색 결과입니다."

    articles, norm_stats = _normalized_articles(entries)
    headlines = [a["headline"] for a in articles]
    if mode == "search":
        top_kw = _top_keywords(headlines, 3, exclude_query=keyword or "")
    else:
        top_kw = _top_keywords(headlines, 3)
    picks = _dedupe_sort_latest(articles, 10)

    diagnostic: str | None = None
    if not picks:
        diagnostic = _pipeline_diagnostic_line(
            mode,
            fetch_report,
            n_after_hours,
            norm_stats,
            len(picks)
        )

    _log.info(
        "report pipeline mode=%s rss_entries=%s after_24h=%s norm_kept=%s picks=%s",
        mode,
        fetch_report.entry_count if fetch_report else None,
        n_after_hours,
        norm_stats.get("kept", 0),
        len(picks),
    )

    message = _format_message(top_kw, picks, footer, diagnostic=diagnostic)

    try:
        _send_telegram(token, chat_id, message)
    except requests.RequestException as exc:
        return (f"Telegram 전송 실패: {exc}", 502)

    return ("OK", 200)
