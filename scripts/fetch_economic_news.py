import datetime as dt
import html
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


GOOGLE_NEWS_SEARCH_RSS = "https://news.google.com/rss/search"
KST = dt.timezone(dt.timedelta(hours=9))
KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _strip_html(raw_text: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw_text or "")
    text = html.unescape(text).strip()
    return re.sub(r"\s+", " ", text)


def _parse_pub_date(pub_date: str) -> dt.datetime | None:
    if not pub_date:
        return None
    try:
        # RFC2822 format used in RSS, e.g. "Tue, 05 May 2026 14:20:00 GMT"
        return dt.datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None


def _format_pub_date(pub_date: dt.datetime | None, fallback: str) -> str:
    if not pub_date:
        return fallback

    local_time = pub_date.astimezone(KST)
    weekday = KOREAN_WEEKDAYS[local_time.weekday()]
    return (
        f"{local_time.year:04d}년 {local_time.month:02d}월 {local_time.day:02d}일 "
        f"{weekday}요일 {local_time.hour:02d}시 {local_time.minute:02d}분"
    )


def build_feed_url() -> str:
    # "when:1d" narrows search to the last 24 hours.
    query = "경제 when:1d"
    params = {
        "q": query,
        "hl": "ko",
        "gl": "KR",
        "ceid": "KR:ko",
    }
    return f"{GOOGLE_NEWS_SEARCH_RSS}?{urllib.parse.urlencode(params)}"


def fetch_recent_economic_news(limit: int = 10) -> list[dict[str, str]]:
    url = build_feed_url()
    with urllib.request.urlopen(url, timeout=20) as response:
        content = response.read()

    root = ET.fromstring(content)
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=1)

    items: list[dict[str, str]] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        summary = _strip_html(item.findtext("description") or "")
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate") or ""
        pub_date = _parse_pub_date(pub_date_raw)

        # Double-check recency in case feed returns older entries.
        if pub_date and pub_date < cutoff:
            continue

        if not title:
            continue

        items.append(
            {
                "title": title,
                "summary": summary if summary else "(요약 없음)",
                "link": link,
                "pub_date": _format_pub_date(pub_date, pub_date_raw),
            }
        )

        if len(items) >= limit:
            break

    return items


def main() -> None:
    news = fetch_recent_economic_news(limit=10)
    print("최근 24시간 경제 뉴스")
    print("=" * 40)

    if not news:
        print("조건에 맞는 뉴스가 없습니다.")
        return

    for idx, article in enumerate(news, start=1):
        print(f"{idx}. {article['title']}")
        print(f"   요약: {article['summary']}")
        print(f"   링크: {article['link']}")
        print(f"   발행일: {article['pub_date']}")
        print("-" * 40)


if __name__ == "__main__":
    main()
