"""YC-only scraping: companies → founders → validation (no /partners discovery)."""

from __future__ import annotations

import logging
import re
from typing import Any, Generator
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    MAX_COMPANIES_TO_CHECK,
    REQUEST_TIMEOUT,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF,
    USER_AGENT,
    YC_COMPANY_SLUGS,
    YC_INVALID_COMPANY_SLUGS,
)
from parser import (
    build_founder_profile_text,
    extract_linkedin_profiles,
    extract_meta_description,
    is_valid_person_name,
    normalize_name,
    parse_founders_from_company_description,
    match_linkedin_to_founder,
    is_tech_startup,
)
import base64
from urllib.parse import urlparse, parse_qs
from lxml.html import document_fromstring
from lxml.etree import _Element
import duckduckgo_search
from duckduckgo_search import DDGS
from duckduckgo_search.utils import _normalize, _normalize_url

def patched_text_bing(self, keywords, region=None, timelimit=None, max_results=None):
    payload = {
        "q": keywords,
    }
    cache = set()
    results = []
    
    for page in range(5):
        resp = self._get_url("GET", "https://www.bing.com/search", params=payload)
        resp_content = resp.text
        
        tree = document_fromstring(resp_content, self.parser)
        elements = tree.xpath("//li[contains(@class, 'b_algo')]")
        if not elements:
            break
            
        for e in elements:
            if isinstance(e, _Element):
                hrefxpath = e.xpath("./h2/a/@href | ./div[contains(@class, 'header')]/a/@href")
                href = str(hrefxpath[0]) if hrefxpath else None
                if href and href.startswith("https://www.bing.com/ck/a?"):
                    u_param = parse_qs(urlparse(href).query).get("u", [""])[0]
                    if u_param:
                        if u_param.startswith("a1"):
                            u_param = u_param[2:]
                        u_param += "=" * ((-len(u_param)) % 4)
                        try:
                            href = base64.urlsafe_b64decode(u_param.encode()).decode("utf-8", errors="ignore")
                        except Exception:
                            pass
                if href and href not in cache:
                    cache.add(href)
                    titlexpath = e.xpath("./h2/a//text() | ./div[contains(@class, 'header')]/a/h2//text()")
                    title = "".join(str(x) for x in titlexpath) if titlexpath else ""
                    bodyxpath = e.xpath(".//p//text()")
                    body = "".join(str(x) for x in bodyxpath) if bodyxpath else ""
                    results.append(
                        {
                            "title": _normalize(title),
                            "href": _normalize_url(href),
                            "body": _normalize(body).replace("\xa0", " "),
                        }
                    )
                    if max_results and len(results) >= max_results:
                        return results
        if not max_results:
            return results
        payload["first"] = f"{((page + 1) * 10) + 1}"
        payload["FORM"] = f"PERE{page if page > 0 else ''}"
        
    return results

duckduckgo_search.DDGS._text_bing = patched_text_bing

logger = logging.getLogger(__name__)

YC_BASE = "https://www.ycombinator.com"
COMPANIES_LIST_URL = f"{YC_BASE}/companies"


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=RETRY_ATTEMPTS,
        connect=RETRY_ATTEMPTS,
        read=RETRY_ATTEMPTS,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


SESSION = _build_session()


def fetch_html(url: str) -> str | None:
    try:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        logger.warning("fetch failed %s: %s", url, exc)
        return None


def scrape_page(url: str) -> str:
    html = fetch_html(url)
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def get_yc_company(company_slug: str) -> str:
    return scrape_page(f"{YC_BASE}/companies/{company_slug}")


def _is_valid_company_path(path: str) -> bool:
    if not path.startswith("/companies/"):
        return False
    slug = path[len("/companies/") :].strip("/").split("/")[0].lower()
    if not slug or slug in YC_INVALID_COMPANY_SLUGS:
        return False
    if not re.match(r"^[a-z0-9][a-z0-9\-]*$", slug):
        return False
    return True


def get_yc_company_urls() -> list[str]:
    """
    Scrape YC companies directory and return deduplicated company paths.
    Falls back to config slugs when the listing page is client-rendered.
    """
    paths: list[str] = []
    seen: set[str] = set()

    def add_path(path: str) -> None:
        path = path.split("?")[0].rstrip("/")
        if not _is_valid_company_path(path):
            return
        if path not in seen:
            seen.add(path)
            paths.append(path)

    html = fetch_html(COMPANIES_LIST_URL)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if href.startswith("http"):
                parsed = urlparse(href)
                if "ycombinator.com" not in parsed.netloc:
                    continue
                href = parsed.path
            if href.startswith("/companies/"):
                add_path(href)
        for match in re.finditer(r'["\']/(companies/[a-z0-9\-]+)["\']', html, re.I):
            add_path(f"/{match.group(1)}")

    for slug in YC_COMPANY_SLUGS:
        add_path(f"/companies/{slug.lower().strip()}")

    # Validate response status of company URLs
    valid_paths: list[str] = []
    for path in paths:
        if len(valid_paths) >= MAX_COMPANIES_TO_CHECK:
            break
        company_url = urljoin(YC_BASE, path)
        slug = path[len("/companies/") :].strip("/").split("/")[0]
        
        try:
            response = SESSION.head(company_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if response.status_code == 200:
                valid_paths.append(path)
            else:
                logger.info("[YC] Invalid company URL skipped | %s", slug)
        except requests.RequestException:
            logger.info("[YC] Invalid company URL skipped | %s", slug)

    return valid_paths


def _company_name_from_meta(slug: str, meta: dict[str, str | None]) -> str:
    title = meta.get("title") or ""
    if title:
        name = re.sub(r"\s*[\|\-–—:].*$", "", title).strip()
        name = re.sub(
            r":\s*.*$",
            "",
            name.replace(" | Y Combinator", ""),
        ).strip()
        if name and name.lower() not in ("y combinator", "company"):
            return name
    return slug.replace("-", " ").title()


def _calculate_confidence(result: dict[str, Any], founder_name: str, company_name: str, cofounders: list[str]) -> tuple[int, str]:
    score = 0
    reasons = []
    url = (result.get("href") or "").lower()
    title = (result.get("title") or "").lower()
    body = (result.get("body") or "").lower()
    text = f"{title} {body}"
    
    name_lower = founder_name.lower()
    name_parts = name_lower.split()
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[-1] if len(name_parts) > 1 else ""
    comp_lower = company_name.lower()

    # Reject if missing full name (Identity: Full name match required)
    if name_lower not in text:
        return 0, "Rejected: Missing full name"

    # Co-founder rejection
    for cofounder in cofounders:
        if cofounder.lower() != name_lower and len(cofounder.split()) > 1:
            co_last = cofounder.lower().split()[-1]
            if cofounder.lower() in text or (co_last and f" {co_last}" in text and co_last != last_name):
                return 0, f"Rejected: Contains co-founder {cofounder}"

    score += 3
    reasons.append("+3 full name")

    if last_name and (last_name in title or last_name in body or last_name in url):
        score += 2
        reasons.append("+2 last name")

    if comp_lower and comp_lower in text:
        score += 1
        reasons.append("+1 company")

    name_slug = re.sub(r'[^a-z0-9]', '', name_lower)
    if name_slug in url or (last_name and last_name in url):
        score += 1
        reasons.append("+1 URL match")

    if name_lower in title or comp_lower in title:
        score += 1
        reasons.append("+1 title match")

    if any(kw in text for kw in ["about", "bio", "portfolio", "personal", "blog", "founder", "angel", "investor", "github"]):
        score += 1
        reasons.append("+1 bio/keywords")
        
    return score, ", ".join(reasons)


def enrich_founder_profile(founder_name: str, company_name: str, company_domain: str, cofounders: list[str]) -> dict[str, str]:
    logger.info("[ENRICH] Starting enrichment for %s (Company: %s)", founder_name, company_name)
    enrichment_texts = []
    personal_website = ""
    linkedin_profile = ""
    
    queries = [
        f'"{founder_name}" {company_name} personal website OR blog',
        f'"{founder_name}" {company_name} github',
        f'"{founder_name}" {company_name} crunchbase',
        f'"{founder_name}" {company_name} angel investor',
    ]
    
    results = []
    try:
        with DDGS() as ddgs:
            for q in queries:
                try:
                    r = list(ddgs.text(q, backend="lite", max_results=3))
                    results.extend(r)
                except Exception as exc:
                    logger.warning("[ENRICH] DDG search failed for query '%s': %s", q, exc)
    except Exception as exc:
        logger.warning("[ENRICH] DDGS initialization failed: %s", exc)

    disallowed_domains = [
        "linkedin.com", "crunchbase.com", "openvc.app", "angellist.com",
        "angel.co", "wellfound.com", "twitter.com", "x.com", "facebook.com",
        "instagram.com", "github.com", "youtube.com", "medium.com", "substack.com"
    ]
    if company_domain:
        disallowed_domains.append(company_domain)
    
    seen_urls = set()
    fetched_count = 0
    
    for r in results:
        url = r.get("href") or ""
        body = r.get("body") or ""
        title = r.get("title") or ""
        url_lower = url.lower()
        
        if url_lower in seen_urls:
            continue
        seen_urls.add(url_lower)
        
        score, reason = _calculate_confidence(r, founder_name, company_name, cofounders)
        if score < 4:
            logger.debug("[ENRICH] Rejected %s | Score: %d | Reason: %s", url, score, reason)
            continue
            
        logger.info("[ENRICH] Accepted %s | Score: %d | Reasons: %s", url, score, reason)
        
        item_text = f"{title}: {body}" if title else body
        if item_text:
            enrichment_texts.append(item_text)
            
        is_disallowed = any(domain in url_lower for domain in disallowed_domains)
        if not is_disallowed and not personal_website:
            personal_website = url
            
        if "linkedin.com/in/" in url_lower and not linkedin_profile:
            linkedin_profile = url
            
        # Extract GitHub blog website
        if "github.com" in url_lower and not personal_website:
            try:
                gh_html = fetch_html(url)
                if gh_html:
                    gh_soup = BeautifulSoup(gh_html, "html.parser")
                    for a in gh_soup.find_all("a", rel=lambda x: x and "nofollow" in x and "me" in x):
                        gh_url = a.get("href", "")
                        if gh_url and not any(d in gh_url.lower() for d in disallowed_domains):
                            personal_website = gh_url
                            logger.info("[ENRICH] Found personal website via GitHub: %s", gh_url)
                            break
            except Exception as e:
                logger.warning("[ENRICH] Failed parsing GitHub page: %s", e)

        if fetched_count < 2:
            is_personal_or_bio = False
            if "wikipedia.org" in url_lower or "ycombinator.com" in url_lower:
                is_personal_or_bio = True
            elif not is_disallowed:
                name_slug = re.sub(r'[^a-z0-9]', '', founder_name.lower())
                clean_url = re.sub(r'[^a-z0-9]', '', url_lower)
                if name_slug in clean_url or any(x in url_lower for x in ["about", "bio", "portfolio", "personal", "blog", "homepage"]):
                    is_personal_or_bio = True
                    
            if is_personal_or_bio:
                logger.debug("[ENRICH] Fetching enrichment page: %s", url)
                page_html = fetch_html(url)
                if page_html:
                    try:
                        soup = BeautifulSoup(page_html, "html.parser")
                        for script in soup(["script", "style", "nav", "footer", "header"]):
                            script.decompose()
                        page_text = soup.get_text(separator=" ", strip=True)
                        page_text = re.sub(r'\s+', ' ', page_text).strip()
                        if len(page_text) > 1500:
                            page_text = page_text[:1500]
                        enrichment_texts.append(page_text)
                        fetched_count += 1
                    except Exception as e:
                        logger.warning("[ENRICH] Failed to parse page %s: %s", url, e)

    return {
        "text": " ".join(enrichment_texts),
        "personal_website": personal_website,
        "linkedin_profile": linkedin_profile
    }



def get_company_founders(company_url: str) -> list[dict[str, Any]]:
    """
    Open a YC company page and extract founder-level structured data.
    """
    if company_url.startswith("/"):
        company_url = urljoin(YC_BASE, company_url)

    html = fetch_html(company_url)
    if not html:
        return []

    meta = extract_meta_description(html)
    description = meta.get("description") or ""
    slug = company_url.rstrip("/").split("/companies/")[-1].split("?")[0]
    company_name = _company_name_from_meta(slug, meta)

    # Tech startup check before founder extraction
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True)
    company_text = f"{company_name} {description} {page_text}"

    if not is_tech_startup(company_text):
        logger.info("[YC] SKIPPED → Non-tech startup | %s", company_name)
        return []

    founder_names = parse_founders_from_company_description(description)
    linkedin_urls = extract_linkedin_profiles(html)
    company_site = meta.get("personal_website")

    people_extra: dict[str, str] = {}
    for people_slug in dict.fromkeys(re.findall(r"/people/([a-z0-9\-]+)", html)):
        person_html = fetch_html(f"{YC_BASE}/people/{people_slug}")
        if not person_html:
            continue
        person_meta = extract_meta_description(person_html)
        person_desc = person_meta.get("description") or ""
        person_title = person_meta.get("title") or ""
        person_name = (
            person_title.split(":")[0].strip()
            if ":" in person_title
            else people_slug.replace("-", " ").title()
        )
        if is_valid_person_name(person_name):
            people_extra[person_name] = person_desc

    founders: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    assigned_websites: set[str] = set()
    
    company_domain = urlparse(company_site).netloc.lower().replace("www.", "") if company_site else ""

    for name in founder_names:
        norm = normalize_name(name)
        key = norm.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        extra = people_extra.get(norm, "")
        
        # Enrich the founder profile
        enrichment_result = enrich_founder_profile(
            norm, 
            company_name, 
            company_domain, 
            founder_names
        )
        enrichment_text = enrichment_result["text"]
        
        yc_linkedin = match_linkedin_to_founder(norm, linkedin_urls)
        linkedin = yc_linkedin or enrichment_result["linkedin_profile"]
        website = enrichment_result["personal_website"]

        # Deduplication check
        if website:
            if website in assigned_websites:
                logger.info("[ENRICH] Rejecting website %s for %s - already assigned to another founder", website, norm)
                website = ""
            else:
                assigned_websites.add(website)

        founder_title = f"Co-founder of {company_name}"
        founder_profile_text = " ".join(
            filter(None, [founder_title, extra, enrichment_text])
        )
        
        bio = build_founder_profile_text(norm, company_name, description, extra)

        founders.append(
            {
                "name": norm,
                "company_name": company_name,
                "company_description": description,
                "bio": bio,
                "founder_profile_text": founder_profile_text,
                "website": website,
                "linkedin": linkedin,
                "company_url": company_url,
            }
        )

    return founders


def scrape_yc() -> Generator[dict[str, Any], None, None]:
    """
    Company-first discovery:
    companies → founders → raw records for parser validation.
    """
    company_paths = get_yc_company_urls()
    total = len(company_paths)
    logger.info("[YC] Found %d companies", total)

    for index, path in enumerate(company_paths, start=1):
        company_url = urljoin(YC_BASE, path)
        logger.info("[YC] Checking company %d/%d", index, total)

        try:
            founders = get_company_founders(company_url)
        except Exception as exc:
            logger.warning("[YC] Failed founders for %s: %s", company_url, exc)
            continue

        if not founders:
            logger.debug("[YC] No founders extracted from %s", company_url)
            continue

        for founder in founders:
            yield {
                "source": "yc",
                "full_name": founder["name"],
                "company_name": founder["company_name"],
                "company_description": founder.get("company_description", ""),
                "title": founder["company_name"],
                "description": founder.get("company_description", ""),
                "bio": founder["bio"],
                "founder_profile_text": founder.get("founder_profile_text"),
                "linkedin_profile": founder.get("linkedin"),
                "personal_website": founder.get("website") or company_url,
                "source_url": company_url,
            }


def scrape_source(source: str) -> Generator[dict[str, Any], None, None]:
    if source != "yc":
        logger.error("[YC] Unknown source '%s' — only 'yc' is enabled", source)
        return
    yield from scrape_yc()
