"""
웹 페이지 스크래퍼 (기업 Press Release, 블로그 등 직접 URL 지정).

RSS 피드를 제공하지 않는 페이지를 위한 스크래퍼.
목록 페이지에서 기사 링크를 추출한 뒤 각 기사의 본문을 수집한다.

accounts.yaml 설정 예시:
  - platform: press_release
    handle: OpenAI Blog
    url: https://openai.com/news          # 목록 페이지 URL
    article_selector: "a.news-article"   # [선택] 기사 링크 CSS 선택자
    active: true

article_selector 미지정 시 자동 탐색:
  <article>, <main>, [role=main] 내부의 <a> 태그를 후보로 사용.
"""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from scrapers.base_scraper import BaseScraper, RawPost

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TrendSensingBot/1.0; "
        "+https://github.com/your-org/trendsensing)"
    )
}
_TIMEOUT = 15          # 초
_MAX_ARTICLE_FETCH = 5 # 목록에서 한 번에 본문을 가져올 최대 기사 수


class WebScraper(BaseScraper):
    """웹 페이지(Press Release / 블로그) 스크래퍼."""

    platform = "press_release"

    def fetch_posts(
        self,
        account_cfg: Dict[str, Any],
        since_post_id: Optional[str] = None,
        max_results: int = 20,
    ) -> List[RawPost]:
        listing_url = account_cfg.get("url")
        handle      = account_cfg["handle"]
        selector    = account_cfg.get("article_selector")

        if not listing_url:
            logger.error(f"[Web] '{handle}' 항목에 url 필드가 없습니다.")
            return []

        # 1. 목록 페이지 가져오기
        html = self._get(listing_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")

        # 2. 기사 링크 추출
        article_links = self._extract_links(soup, listing_url, selector)
        if not article_links:
            logger.warning(f"[Web] '{handle}' 목록 페이지에서 기사 링크를 찾지 못했습니다.")
            return []

        # 3. 링크별 본문 수집 (최대 _MAX_ARTICLE_FETCH개)
        posts: List[RawPost] = []
        for link in article_links[: min(max_results, _MAX_ARTICLE_FETCH)]:
            post = self._fetch_article(link, handle)
            if post:
                posts.append(post)

        logger.info(f"[Web] {handle} — {len(posts)}건 수집")
        return posts

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────────

    def _get(self, url: str) -> Optional[str]:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logger.error(f"[Web] GET 실패 {url}: {e}")
            return None

    def _extract_links(
        self, soup: BeautifulSoup, base_url: str, selector: Optional[str]
    ) -> List[str]:
        """목록 페이지에서 기사 절대 URL 목록을 반환한다."""
        base_domain = urlparse(base_url).netloc

        if selector:
            candidates = soup.select(selector)
        else:
            # 자동 탐색: 주요 콘텐츠 영역 내 링크 우선
            container = (
                soup.find("article")
                or soup.find("main")
                or soup.find(attrs={"role": "main"})
                or soup.body
            )
            candidates = container.find_all("a", href=True) if container else []

        seen, links = set(), []
        for tag in candidates:
            href = tag.get("href", "").strip()
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue
            abs_url = urljoin(base_url, href)
            # 동일 도메인의 링크만 수집 (외부 링크 제외)
            if urlparse(abs_url).netloc != base_domain:
                continue
            if abs_url not in seen:
                seen.add(abs_url)
                links.append(abs_url)

        return links

    def _fetch_article(self, url: str, handle: str) -> Optional[RawPost]:
        """개별 기사 페이지의 본문을 추출해 RawPost로 반환한다."""
        html = self._get(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title   = self._extract_title(soup)
        content = self._extract_body(soup, title)
        pub_dt  = self._extract_date(soup)
        post_id = hashlib.sha256(url.encode()).hexdigest()[:24]

        return RawPost(
            platform=self.platform,
            account_handle=handle,
            account_display_name=handle,
            post_id=post_id,
            content=content,
            url=url,
            published_at=pub_dt,
            raw_data={"source_url": url},
        )

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        # og:title → <h1> → <title> 순으로 시도
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        title_tag = soup.find("title")
        return title_tag.get_text(strip=True) if title_tag else ""

    @staticmethod
    def _extract_body(soup: BeautifulSoup, title: str) -> str:
        """본문 텍스트를 추출한다. 제목 + 본문 앞 1000자."""
        container = (
            soup.find("article")
            or soup.find(attrs={"role": "main"})
            or soup.find("main")
        )
        if container:
            # 네비게이션/사이드바 제거
            for tag in container.find_all(["nav", "aside", "footer", "script", "style"]):
                tag.decompose()
            body = container.get_text(separator=" ", strip=True)
        else:
            body = soup.get_text(separator=" ", strip=True)

        # 제목 + 본문 앞부분 (분석 토큰 절약)
        combined = f"{title}\n\n{body}"
        return combined[:1500]

    @staticmethod
    def _extract_date(soup: BeautifulSoup) -> datetime:
        """발행 일시를 UTC naive datetime으로 반환한다.

        시도 순서:
        1. JSON-LD (datePublished / dateCreated)
        2. <meta> 태그 (article:published_time 등)
        3. <time datetime="…"> 태그
        4. URL 내 날짜 패턴 (/2026/04/15 등)
        시간 정보가 없으면 해당 날짜의 00:00 UTC를 사용한다.
        """
        import json as _json
        import re
        from dateutil.parser import parse as dateparse

        def _try_parse(raw: str) -> datetime | None:
            try:
                dt = dateparse(raw)
                if dt.tzinfo:
                    return dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except Exception:
                return None

        # 1. JSON-LD structured data
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = _json.loads(script.string or "")
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    for key in ("datePublished", "dateCreated"):
                        val = item.get(key)
                        if val:
                            dt = _try_parse(str(val))
                            if dt:
                                return dt
            except Exception:
                pass

        # 2. <meta> 태그
        for attr, name in [
            ("property", "article:published_time"),
            ("property", "og:article:published_time"),
            ("name",     "publication_date"),
            ("name",     "date"),
            ("name",     "DC.date.issued"),
            ("itemprop", "datePublished"),
        ]:
            tag = soup.find("meta", attrs={attr: name})
            if tag and tag.get("content"):
                dt = _try_parse(tag["content"])
                if dt:
                    return dt

        # 3. <time> 태그
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            dt = _try_parse(time_tag["datetime"])
            if dt:
                return dt

        # 4. URL 내 날짜 패턴 (게시 일자라도 확보)
        page_url = ""
        canon = soup.find("link", rel="canonical")
        if canon and canon.get("href"):
            page_url = canon["href"]
        elif soup.find("meta", property="og:url"):
            page_url = soup.find("meta", property="og:url").get("content", "")

        if page_url:
            m = re.search(r'/(\d{4})/(\d{1,2})/(\d{1,2})', page_url)
            if m:
                try:
                    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    pass

        return datetime.utcnow()
