#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import (
    parse_qs,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://tamilyogi.cx"
MOVIES_URL = f"{BASE_URL}/movie"
DEFAULT_OUTPUT = Path("movies.json")

REQUEST_TIMEOUT = 30
PAGE_DELAY_MIN = 1.0
PAGE_DELAY_MAX = 2.0
MOVIE_DELAY_MIN = 0.4
MOVIE_DELAY_MAX = 0.9

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ta;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}


class ScraperError(RuntimeError):
    pass


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_url(url: str, base_url: str = BASE_URL) -> str:
    absolute_url = urljoin(base_url, html.unescape(url.strip()))
    parsed = urlparse(absolute_url)

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def create_session(proxy_url: str | None = None) -> requests.Session:
    session = requests.Session()

    # Optional single outbound proxy. Configure with --proxy or SCRAPER_PROXY.
    # Standard HTTP_PROXY / HTTPS_PROXY environment variables are also
    # supported by requests automatically.
    if proxy_url:
        session.proxies.update({
            "http": proxy_url,
            "https": proxy_url,
        })

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)

    return session


def fetch_response(
    session: requests.Session,
    url: str,
    allow_redirects: bool = True,
) -> requests.Response:
    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=allow_redirects,
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        return response

    except requests.RequestException as exc:
        raise ScraperError(f"Request failed: {url}: {exc}") from exc


def build_listing_page_url(page_number: int) -> str:
    if page_number <= 1:
        return MOVIES_URL

    parsed = urlparse(MOVIES_URL)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_number)]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            "",
        )
    )


def is_movie_detail_url(url: str) -> bool:
    parsed = urlparse(url)

    return (
        parsed.netloc == urlparse(BASE_URL).netloc
        and parsed.path.startswith("/movie/watch-")
    )


def build_watch_entry_url(movie_url: str) -> str:
    """
    Input:
    https://tamilyogi.cx/movie/watch-ek-din-hd-833748

    Output:
    https://tamilyogi.cx/watch-movie/watch-ek-din-hd-833748
    """
    parsed = urlparse(movie_url)
    path = parsed.path

    if path.startswith("/movie/"):
        path = "/watch-movie/" + path.removeprefix("/movie/")

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path.rstrip("/"),
            "",
            "",
            "",
        )
    )


def find_movie_container(anchor: Tag) -> Tag:
    selectors = (
        ".flw-item",
        ".film_list-wrap",
        ".film-poster",
        ".movie-item",
        "article",
        ".card",
        ".item",
        "li",
    )

    for selector in selectors:
        parent = anchor.find_parent(selector)

        if not isinstance(parent, Tag):
            continue

        movie_links = parent.select('a[href*="/movie/watch-"]')

        if 1 <= len(movie_links) <= 3:
            return parent

    current: Tag = anchor

    for _ in range(6):
        parent = current.parent

        if not isinstance(parent, Tag):
            break

        movie_links = parent.select('a[href*="/movie/watch-"]')

        if 1 <= len(movie_links) <= 3:
            return parent

        current = parent

    return anchor


def extract_title(anchor: Tag, container: Tag) -> str:
    for selector in (
        ".film-name",
        ".title",
        "h1",
        "h2",
        "h3",
        "h4",
    ):
        element = container.select_one(selector)

        if isinstance(element, Tag):
            title = clean_text(element.get_text(" ", strip=True))

            if title:
                return title

    title_attribute = anchor.get("title")

    if isinstance(title_attribute, str):
        title = clean_text(title_attribute)

        if title:
            return title

    anchor_text = clean_text(anchor.get_text(" ", strip=True))

    if anchor_text:
        return anchor_text

    image = container.select_one("img")

    if isinstance(image, Tag):
        alt = image.get("alt")

        if isinstance(alt, str):
            title = re.sub(
                r"\s+(?:movie\s+)?poster\s*$",
                "",
                clean_text(alt),
                flags=re.IGNORECASE,
            )

            if title:
                return title

    return "Unknown"


def extract_poster(container: Tag, page_url: str) -> str | None:
    image = container.select_one("img")

    if not isinstance(image, Tag):
        return None

    for attribute in (
        "data-src",
        "data-original",
        "data-lazy-src",
        "src",
    ):
        value = image.get(attribute)

        if not isinstance(value, str):
            continue

        value = value.strip()

        if not value or value.startswith("data:image"):
            continue

        return normalize_url(value, page_url)

    srcset = image.get("srcset")

    if isinstance(srcset, str) and srcset.strip():
        first_item = srcset.split(",", maxsplit=1)[0].strip()
        first_url = first_item.split(" ", maxsplit=1)[0].strip()

        if first_url:
            return normalize_url(first_url, page_url)

    return None


def extract_quality(container: Tag) -> str | None:
    pattern = re.compile(
        r"\b("
        r"4K|UHD|FHD|HD|SD|CAM|"
        r"WEB[- ]?DL|WEBRIP|HDRIP|"
        r"BLURAY|BRRIP|DVDRIP"
        r")\b",
        flags=re.IGNORECASE,
    )

    for selector in (
        ".film-poster-quality",
        ".quality",
        ".badge",
        ".tick-quality",
    ):
        for element in container.select(selector):
            match = pattern.search(
                clean_text(element.get_text(" ", strip=True))
            )

            if match:
                return match.group(1).upper().replace(" ", "-")

    match = pattern.search(
        clean_text(container.get_text(" ", strip=True))
    )

    if match:
        return match.group(1).upper().replace(" ", "-")

    return None


def extract_metadata(
    container: Tag,
) -> tuple[int | None, int | None, str | None]:
    text = clean_text(container.get_text(" ", strip=True))

    year: int | None = None
    duration: int | None = None
    content_type: str | None = None

    year_match = re.search(
        r"\b(19\d{2}|20\d{2}|21\d{2})\b",
        text,
    )

    if year_match:
        year = int(year_match.group(1))

    duration_match = re.search(
        r"\b(\d{1,3})\s*(?:m|min|mins|minutes)\b",
        text,
        flags=re.IGNORECASE,
    )

    if duration_match:
        duration = int(duration_match.group(1))

    if re.search(r"\bTV\s*Show\b", text, re.IGNORECASE):
        content_type = "TV Show"
    elif re.search(r"\bMovie\b", text, re.IGNORECASE):
        content_type = "Movie"

    return year, duration, content_type


def parse_listing_page(
    html_text: str,
    page_url: str,
    page_number: int,
) -> list[dict[str, Any]]:
    """
    Website order அப்படியே preserve செய்யப்படும்.
    எந்த sort-மும் பயன்படுத்தப்படாது.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    movies: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    anchors = soup.select('a[href*="/movie/watch-"]')

    for anchor in anchors:
        if not isinstance(anchor, Tag):
            continue

        href = anchor.get("href")

        if not isinstance(href, str) or not href.strip():
            continue

        movie_url = normalize_url(href, page_url)

        if not is_movie_detail_url(movie_url):
            continue

        if movie_url in seen_urls:
            continue

        container = find_movie_container(anchor)
        year, duration, content_type = extract_metadata(container)

        movies.append(
            {
                "title": extract_title(anchor, container),
                "url": movie_url,
                "poster": extract_poster(container, page_url),
                "quality": extract_quality(container),
                "year": year,
                "duration_minutes": duration,
                "type": content_type,
                "page": page_number,
                "position": len(movies) + 1,
                "watch_urls": [],
            }
        )

        seen_urls.add(movie_url)

    return movies


def watch_url_matches_movie(
    candidate_url: str,
    movie_slug: str,
) -> bool:
    parsed = urlparse(candidate_url)

    if parsed.netloc.lower() != urlparse(BASE_URL).netloc.lower():
        return False

    expected_pattern = (
        rf"^/watch-movie/{re.escape(movie_slug)}\.\d+$"
    )

    return bool(re.fullmatch(expected_pattern, parsed.path))


def extract_server_title(element: Tag) -> str | None:
    """DOM element-இலிருந்து MyCloud / VidCloud title-ஐ கண்டறிகிறது."""
    allowed_titles = {
        "mycloud": "MyCloud",
        "vidcloud": "VidCloud",
    }

    candidates: list[str] = []

    for attribute in ("title", "data-title", "data-name", "aria-label"):
        value = element.get(attribute)
        if isinstance(value, str):
            candidates.append(value)

    candidates.append(element.get_text(" ", strip=True))

    parent = element.parent
    for _ in range(3):
        if not isinstance(parent, Tag):
            break
        candidates.append(parent.get_text(" ", strip=True))
        parent = parent.parent

    for candidate in candidates:
        normalized = clean_text(candidate).lower()
        for key, canonical_title in allowed_titles.items():
            if re.search(rf"\b{re.escape(key)}\b", normalized):
                return canonical_title

    return None


def extract_episode_id(watch_url: str) -> str | None:
    """.../movie-slug.14896 URL-இலிருந்து 14896 ID-ஐ எடுக்கிறது."""
    match = re.search(r"\.(\d+)$", urlparse(watch_url).path)
    return match.group(1) if match else None


def fetch_embed_url(
    session: requests.Session,
    episode_id: str,
    referer_url: str,
) -> str | None:
    """Episode source JSON endpoint-இலிருந்து iframe link-ஐ எடுக்கிறது."""
    source_url = f"{BASE_URL}/ajax/episode/sources/{episode_id}"

    try:
        response = session.get(
            source_url,
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": referer_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(
            f"      Source API error ({episode_id}): {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(payload, dict):
        return None

    link = payload.get("link")
    if not isinstance(link, str) or not link.strip():
        return None

    return html.unescape(link.strip()).replace("\\/", "/")


def extract_watch_urls(
    session: requests.Session,
    movie_url: str,
) -> list[dict[str, str]]:
    """
    MyCloud மற்றும் VidCloud server records மட்டும் return செய்கிறது.

    Output example:
    [
      {
        "title": "MyCloud",
        "url": "https://tamilyogi.cx/watch-movie/example.14896",
        "embed": "https://embedojo.net/video/..."
      }
    ]
    """
    watch_entry_url = build_watch_entry_url(movie_url)

    try:
        response = fetch_response(
            session=session,
            url=watch_entry_url,
            allow_redirects=True,
        )
    except ScraperError as exc:
        print(f"    Watch request error: {exc}", file=sys.stderr)
        return []

    redirected_url = normalize_url(response.url)
    movie_slug = urlparse(movie_url).path.rsplit("/", maxsplit=1)[-1]
    soup = BeautifulSoup(response.text, "html.parser")

    results: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()

    def add_candidate(
        title: str | None,
        raw_url: str | None = None,
        raw_episode_id: str | None = None,
    ) -> None:
        if title not in {"MyCloud", "VidCloud"}:
            return

        candidate_url: str | None = None

        if isinstance(raw_url, str) and raw_url.strip():
            cleaned_url = html.unescape(raw_url).replace("\\/", "/").strip()
            candidate_url = normalize_url(cleaned_url, redirected_url)

            if not watch_url_matches_movie(candidate_url, movie_slug):
                candidate_url = None

        if candidate_url is None and isinstance(raw_episode_id, str):
            episode_id = raw_episode_id.strip()
            if episode_id.isdigit():
                candidate_url = (
                    f"{BASE_URL}/watch-movie/{movie_slug}.{episode_id}"
                )

        if candidate_url is None:
            return

        if title in seen_titles or candidate_url in seen_urls:
            return

        episode_id = extract_episode_id(candidate_url)
        if episode_id is None:
            return

        embed_url = fetch_embed_url(
            session=session,
            episode_id=episode_id,
            referer_url=candidate_url,
        )

        if not embed_url:
            return

        results.append(
            {
                "title": title,
                "url": candidate_url,
                "embed": embed_url,
            }
        )
        seen_titles.add(title)
        seen_urls.add(candidate_url)

    selectors = (
        "a[href]",
        "[data-url]",
        "[data-link]",
        "[data-href]",
        "[data-src]",
        "[data-id]",
        "[data-server]",
        "[onclick]",
    )

    for element in soup.select(", ".join(selectors)):
        if not isinstance(element, Tag):
            continue

        title = extract_server_title(element)
        if title is None:
            continue

        raw_url: str | None = None
        for attribute in (
            "href",
            "data-url",
            "data-link",
            "data-href",
            "data-src",
        ):
            value = element.get(attribute)
            if isinstance(value, str) and value.strip():
                raw_url = value
                break

        raw_episode_id: str | None = None
        for attribute in ("data-id", "data-server"):
            value = element.get(attribute)
            if isinstance(value, str) and value.strip().isdigit():
                raw_episode_id = value.strip()
                break

        onclick = element.get("onclick")
        if isinstance(onclick, str):
            if raw_url is None:
                url_match = re.search(
                    rf"(/watch-movie/{re.escape(movie_slug)}\.\d+)",
                    onclick,
                    flags=re.IGNORECASE,
                )
                if url_match:
                    raw_url = url_match.group(1)

            if raw_episode_id is None:
                id_match = re.search(r"\b(\d{3,})\b", onclick)
                if id_match:
                    raw_episode_id = id_match.group(1)

        add_candidate(
            title=title,
            raw_url=raw_url,
            raw_episode_id=raw_episode_id,
        )

        if len(results) == 2:
            break

    results.sort(
        key=lambda item: {"MyCloud": 0, "VidCloud": 1}.get(
            item["title"],
            99,
        )
    )

    return results


def detect_last_page(
    html_text: str,
    current_url: str,
) -> int:
    soup = BeautifulSoup(html_text, "html.parser")
    page_numbers = {1}

    for anchor in soup.select("a[href]"):
        href = anchor.get("href")

        if not isinstance(href, str):
            continue

        url = normalize_url(href, current_url)
        parsed = urlparse(url)

        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue

        if parsed.path.rstrip("/") != "/movie":
            continue

        query = parse_qs(parsed.query)

        for value in query.get("page", []):
            if value.isdigit():
                page_numbers.add(int(value))

    return max(page_numbers)


def load_existing_movies(
    output_file: Path,
) -> list[dict[str, Any]]:
    if not output_file.exists():
        return []

    try:
        with output_file.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ScraperError(
            f"Existing JSON read error: {output_file}: {exc}"
        ) from exc

    if isinstance(payload, dict):
        records = payload.get("movies", [])
    elif isinstance(payload, list):
        records = payload
    else:
        raise ScraperError("JSON root invalid.")

    result: list[dict[str, Any]] = []

    for record in records:
        if not isinstance(record, dict):
            continue

        url = record.get("url")

        if not isinstance(url, str) or not url.strip():
            continue

        copied_record = dict(record)
        copied_record["url"] = normalize_url(url)

        result.append(copied_record)

    return result


def merge_preserving_order(
    scraped_movies: list[dict[str, Any]],
    existing_movies: list[dict[str, Any]],
    replace_mode: bool,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """
    Scrape செய்யப்பட்ட current website order முதலில் வரும்.

    replace_mode=False:
    Current scrape-ல் இல்லாத பழைய movies கடைசியில் வரும்.

    replace_mode=True:
    Current scrape data மட்டும் JSON-ல் இருக்கும்.
    """
    existing_by_url = {
        movie["url"]: movie
        for movie in existing_movies
        if isinstance(movie.get("url"), str)
    }

    result: list[dict[str, Any]] = []
    processed_urls: set[str] = set()

    new_count = 0
    updated_count = 0

    for scraped_movie in scraped_movies:
        movie_url = scraped_movie["url"]
        processed_urls.add(movie_url)

        old_movie = existing_by_url.get(movie_url)

        if old_movie is None:
            result.append(scraped_movie)
            new_count += 1
            continue

        merged_movie = dict(old_movie)
        changed = False

        for key, value in scraped_movie.items():
            if merged_movie.get(key) != value:
                merged_movie[key] = value
                changed = True

        if changed:
            updated_count += 1

        result.append(merged_movie)

    retained_count = 0

    if not replace_mode:
        for old_movie in existing_movies:
            movie_url = old_movie.get("url")

            if not isinstance(movie_url, str):
                continue

            if movie_url in processed_urls:
                continue

            result.append(old_movie)
            processed_urls.add(movie_url)
            retained_count += 1

    # Final global order update.
    for index, movie in enumerate(result, start=1):
        movie["order"] = index

    return result, new_count, updated_count, retained_count


def save_json_atomic(
    output_file: Path,
    movies: list[dict[str, Any]],
    scraped_pages: int,
    detected_last_page: int,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": MOVIES_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scraped_pages": scraped_pages,
        "detected_last_page": detected_last_page,
        "total_movies": len(movies),
        "movies": movies,
    }

    temporary_file = output_file.with_suffix(
        output_file.suffix + ".tmp"
    )

    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(
                payload,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")

        temporary_file.replace(output_file)

    except OSError as exc:
        raise ScraperError(f"JSON write error: {exc}") from exc

    finally:
        temporary_file.unlink(missing_ok=True)


def scrape(
    output_file: Path,
    max_pages: int,
    replace_mode: bool,
    skip_watch_urls: bool,
    proxy_url: str | None,
) -> None:
    session = create_session(proxy_url=proxy_url)
    existing_movies = load_existing_movies(output_file)

    first_page_url = build_listing_page_url(1)

    print(f"Fetching page 1: {first_page_url}")

    first_response = fetch_response(session, first_page_url)
    detected_last_page = detect_last_page(
        first_response.text,
        first_response.url,
    )

    pages_to_scrape = (
        detected_last_page
        if max_pages == 0
        else min(max_pages, detected_last_page)
    )

    print(f"Detected last page: {detected_last_page}")
    print(f"Pages to scrape: {pages_to_scrape}")
    print(f"Existing movies: {len(existing_movies)}")

    scraped_movies: list[dict[str, Any]] = []
    globally_seen_urls: set[str] = set()
    successful_pages = 0

    for page_number in range(1, pages_to_scrape + 1):
        page_url = build_listing_page_url(page_number)

        try:
            if page_number == 1:
                response = first_response
            else:
                print(f"\nFetching page {page_number}: {page_url}")
                response = fetch_response(session, page_url)

            page_movies = parse_listing_page(
                html_text=response.text,
                page_url=response.url,
                page_number=page_number,
            )

        except ScraperError as exc:
            print(
                f"Page {page_number} skipped: {exc}",
                file=sys.stderr,
            )
            continue

        if not page_movies:
            print(
                f"Page {page_number}: movies கிடைக்கவில்லை.",
                file=sys.stderr,
            )
            break

        page_added = 0

        for movie in page_movies:
            movie_url = movie["url"]

            if movie_url in globally_seen_urls:
                continue

            print(
                f"  [{len(scraped_movies) + 1}] "
                f"{movie['title']}"
            )

            if not skip_watch_urls:
                movie["watch_urls"] = extract_watch_urls(
                    session=session,
                    movie_url=movie_url,
                )

                print(
                    f"      Watch URLs: "
                    f"{len(movie['watch_urls'])}"
                )

                for watch_source in movie["watch_urls"]:
                    print(
                        f"      - {watch_source['title']}: "
                        f"{watch_source['url']}"
                    )
                    print(
                        f"        embed: {watch_source['embed']}"
                    )

                time.sleep(
                    random.uniform(
                        MOVIE_DELAY_MIN,
                        MOVIE_DELAY_MAX,
                    )
                )

            movie["order"] = len(scraped_movies) + 1

            scraped_movies.append(movie)
            globally_seen_urls.add(movie_url)
            page_added += 1

        successful_pages += 1

        print(
            f"Page {page_number}: "
            f"{len(page_movies)} cards, "
            f"{page_added} unique movies"
        )

        if page_number < pages_to_scrape:
            time.sleep(
                random.uniform(
                    PAGE_DELAY_MIN,
                    PAGE_DELAY_MAX,
                )
            )

    if not scraped_movies:
        raise ScraperError("Movie data எதுவும் கிடைக்கவில்லை.")

    (
        final_movies,
        new_count,
        updated_count,
        retained_count,
    ) = merge_preserving_order(
        scraped_movies=scraped_movies,
        existing_movies=existing_movies,
        replace_mode=replace_mode,
    )

    save_json_atomic(
        output_file=output_file,
        movies=final_movies,
        scraped_pages=successful_pages,
        detected_last_page=detected_last_page,
    )

    print("\nCompleted")
    print(f"Scraped movies: {len(scraped_movies)}")
    print(f"New movies: {new_count}")
    print(f"Updated movies: {updated_count}")
    print(f"Old retained movies: {retained_count}")
    print(f"Final movies: {len(final_movies)}")
    print(f"Saved: {output_file.resolve()}")

    if final_movies:
        print(f"First movie: {final_movies[0]['title']}")
        print(f"First URL: {final_movies[0]['url']}")
        print(
            "First watch URLs: "
            f"{final_movies[0].get('watch_urls', [])}"
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Movie metadata மற்றும் same-domain watch page URLs-ஐ "
            "website order-ல் JSON-க்கு update செய்கிறது."
        )
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output JSON file. Default: movies.json",
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help=(
            "Scrape செய்ய வேண்டிய pages. "
            "0 என்றால் எல்லா pagination pages."
        ),
    )

    parser.add_argument(
        "--replace",
        action="store_true",
        help="பழைய JSON records-ஐ retain செய்யாமல் replace செய்கிறது.",
    )

    parser.add_argument(
        "--skip-watch-urls",
        action="store_true",
        help="Watch URL extraction-ஐ skip செய்கிறது.",
    )

    parser.add_argument(
        "--proxy",
        default=os.getenv("SCRAPER_PROXY"),
        help=(
            "Optional HTTP/HTTPS proxy URL. "
            "SCRAPER_PROXY environment variable-லும் set செய்யலாம்."
        ),
    )

    args = parser.parse_args()

    if args.max_pages < 0:
        parser.error("--max-pages 0 அல்லது அதற்கு மேல் இருக்க வேண்டும்.")

    return args


def main() -> None:
    args = parse_arguments()

    try:
        scrape(
            output_file=args.output,
            max_pages=args.max_pages,
            replace_mode=args.replace,
            skip_watch_urls=args.skip_watch_urls,
            proxy_url=args.proxy,
        )

    except ScraperError as exc:
        raise SystemExit(f"Error: {exc}") from exc

    except KeyboardInterrupt:
        raise SystemExit("\nStopped.")


if __name__ == "__main__":
    main()