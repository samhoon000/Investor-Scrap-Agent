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
    validate_founder_angel,
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
    text = f"{title} {body} {url}"
    
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
            co_parts = cofounder.lower().split()
            co_first = co_parts[0]
            co_last = co_parts[-1]
            co_slug = re.sub(r'[^a-z0-9]', '', cofounder.lower())
            
            # 1. If the URL contains the co-founder's slug, reject immediately (e.g. url contains 'patrickcollison')
            if co_slug in url:
                logger.info("[ENRICH] Rejected co-founder mismatch: %s vs co-founder %s", founder_name, cofounder)
                return 0, f"Rejected: Contains co-founder slug {cofounder}"
                
            # 2. If the URL contains the co-founder's first and last name separately
            if co_first in url and co_last in url:
                logger.info("[ENRICH] Rejected co-founder mismatch: %s vs co-founder %s", founder_name, cofounder)
                return 0, f"Rejected: Contains co-founder in URL"
                
            # 3. If the title starts with the co-founder's name or is dedicated to them
            if title.startswith(cofounder.lower()):
                logger.info("[ENRICH] Rejected co-founder mismatch: %s vs co-founder %s", founder_name, cofounder)
                return 0, f"Rejected: Title matches co-founder"

            # 4. If the co-founder has a different last name and their last name is in the URL/title, reject
            if co_last != last_name:
                if co_last in url or (co_last in title and name_lower not in title and name_lower not in url):
                    logger.info("[ENRICH] Rejected co-founder mismatch: %s vs co-founder %s", founder_name, cofounder)
                    return 0, f"Rejected: Contains co-founder last name"
                    
            # 5. If they share the same last name, check if co-founder's first name is present along with last name in URL
            if co_last == last_name and co_first in url and first_name not in url:
                logger.info("[ENRICH] Rejected co-founder mismatch: %s vs co-founder %s", founder_name, cofounder)
                return 0, f"Rejected: URL contains co-founder first name"

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


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url


def is_valid_personal_website(url: str, founder_name: str, company_domain: str) -> bool:
    if not url:
        return False
    url_lower = url.lower()
    parsed = urlparse(url_lower)
    domain = parsed.netloc.replace("www.", "")
    
    # 1. Never use / reject list (domains)
    never_use_domains = [
        "twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com",
        "youtube.com", "tiktok.com"
    ]
    if any(d in domain for d in never_use_domains):
        logger.info("[ENRICH] Rejected social URL: %s", url)
        return False
        
    if "github.com" in domain or "gitlab.com" in domain:
        logger.info("[ENRICH] Rejected social URL: %s", url)
        return False

    # 2. Reject directory / aggregator / news / encyclopedia domains
    directory_domains = [
        "clay.com", "apollo.io", "rocketreach.co", "sourcepulse.org", "nfx.com",
        "openvc.app", "angellist.com", "angel.co", "wellfound.com", "crunchbase.com",
        "investing.com", "forbes.com", "techcrunch.com", "wikipedia.org", "ycombinator.com"
    ]
    if any(d in domain for d in directory_domains):
        logger.info("[ENRICH] Rejected company page: %s", url)
        return False
        
    # 3. Reject company domains
    reject_company_domains = ["coinbase.com", "heap.io", "fivetran.com", "dropbox.com"]
    if company_domain:
        reject_company_domains.append(company_domain.lower())
        
    for d in reject_company_domains:
        if domain == d or domain.endswith("." + d):
            logger.info("[ENRICH] Rejected company page: %s", url)
            return False
            
    if "clay.com/dossier/" in url_lower or "fivetran.com/people/" in url_lower or "investing.com/news/" in url_lower:
        logger.info("[ENRICH] Rejected company page: %s", url)
        return False
        
    # 4. Reject organizational directory/news paths via segment-based matching
    path_and_query = parsed.path + "?" + parsed.query
    path_segments = re.split(r'[^a-z0-9]', path_and_query.lower())
    directory_keywords = {
        "people", "person", "profile", "profiles", "faculty", "staff",
        "directory", "dossier", "member", "members", "news", "article",
        "press", "wiki", "magazine", "mag", "author", "authors",
        "founder", "founders", "engineer", "engineers", "investor", "investors",
        "expert", "experts", "leader", "leaders", "fellow", "fellows", "alumni",
        "alumnus", "student", "students", "team", "about-us", "contact-us",
        "careers", "jobs", "hiring", "press-release", "newsroom"
    }
    if any(k in path_segments for k in directory_keywords):
        logger.info("[ENRICH] Rejected directory/article page: %s", url)
        return False

    # 5. Reject non-tech/unrelated professions
    non_tech_professions = ["writer", "author", "book", "novelist", "movie", "imdb", "film", "song", "music"]
    if any(p in domain for p in non_tech_professions):
        logger.info("[ENRICH] Rejected non-tech profession domain: %s", url)
        return False

    # 6. Reject academic/edu domains without a personal folder (tilde)
    if domain.endswith(".edu") or ".edu/" in url_lower:
        if "~" not in url_lower:
            logger.info("[ENRICH] Rejected academic/edu page: %s", url)
            return False

    return True



def is_valid_email(email: str) -> bool:
    if not email:
        return False
    email_clean = email.strip().lower()
    
    # Reject noreply emails
    if "users.noreply.github.com" in email_clean or "noreply" in email_clean:
        return False
        
    # Reject generic prefixes
    reject_prefixes = ["support@", "sales@", "careers@", "info@", "contact@", "jobs@", "admin@"]
    if any(email_clean.startswith(pref) for pref in reject_prefixes):
        return False
        
    return True


def extract_clean_emails(text: str) -> list[str]:
    raw_emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    valid_emails = []
    
    for email in raw_emails:
        email_clean = email.strip().lower()
        if is_valid_email(email_clean):
            valid_emails.append(email_clean)
            
    return valid_emails


def extract_email_from_text(text: str, personal_website: str = "") -> str:
    emails = extract_clean_emails(text)
    if not emails:
        return ""
        
    personal_providers = ["gmail.com", "outlook.com", "hotmail.com", "icloud.com", "yahoo.com", "protonmail.com", "proton.me", "me.com", "live.com"]
    
    def sort_key(email):
        domain = email.split("@")[-1]
        if personal_website:
            pw_domain = urlparse(personal_website).netloc.replace("www.", "").lower()
            if domain == pw_domain:
                return 0
        if domain in personal_providers:
            return 1
        return 2
        
    emails.sort(key=sort_key)
    return emails[0]


def extract_phone_from_text(text: str, founder_name: str) -> str:
    matches = re.findall(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}", text)
    if not matches:
        return ""
        
    reject_kws = ["support", "office", "sales", "fax", "tel", "customer", "service", "line", "headquarters", "hq", "call"]
    accept_kws = ["cell", "mobile", "personal", "phone", "contact", "direct"]
    
    parts = founder_name.lower().split()
    first_name = parts[0] if parts else ""
    
    for match in matches:
        match_clean = match.strip()
        idx = text.find(match)
        if idx == -1:
            continue
        start = max(0, idx - 100)
        end = min(len(text), idx + len(match) + 100)
        window = text[start:end].lower()
        
        if any(kw in window for kw in reject_kws):
            continue
            
        if first_name and first_name in window:
            return match_clean
        if any(kw in window for kw in accept_kws):
            return match_clean
            
    return ""


def enrich_founder_lightweight(
    founder_name: str,
    company_name: str,
    cofounders: list[str],
    yc_people_slug: str = "",
    yc_profile_html: str | None = None
) -> str:
    logger.info("[ENRICH] Lightweight founder enrichment")
    
    enrichment_texts = []
    
    if yc_profile_html:
        meta = extract_meta_description(yc_profile_html)
        yc_bio = meta.get("description") or ""
        if yc_bio:
            enrichment_texts.append(yc_bio)

    try:
        import time
        with DDGS() as ddgs:
            queries = [
                f'"{founder_name}" "{company_name}"',
                f'"{founder_name}" "portfolio"',
                f'"{founder_name}" "angel investor"',
            ]
            seen_urls = set()
            for i, q in enumerate(queries):
                if i > 0:
                    time.sleep(1.0)
                try:
                    results = list(ddgs.text(q, backend="lite", max_results=3))
                    for r in results:
                        url = r.get("href") or ""
                        url_lower = url.lower()
                        if url_lower in seen_urls:
                            continue
                        seen_urls.add(url_lower)
                        
                        score, reason = _calculate_confidence(r, founder_name, company_name, cofounders)
                        if score < 4:
                            logger.info("[ENRICH] Confidence too low")
                            continue
                            
                        enrichment_texts.append(f"{r.get('title')}: {r.get('body')}")
                except Exception as exc:
                    logger.warning("[ENRICH] DDG search failed for query '%s': %s", q, exc)

            time.sleep(1.0)
            try:
                results = list(ddgs.text(f'"{founder_name}" github', backend="lite", max_results=3))
                for r in results:
                    url = r.get("href") or ""
                    url_lower = url.lower()
                    if "github.com/" in url_lower and not any(x in url_lower for x in ["/features", "/pricing", "/about", "/trending"]):
                        score, reason = _calculate_confidence(r, founder_name, company_name, cofounders)
                        if score >= 4:
                            gh_html = fetch_html(url)
                            if gh_html:
                                gh_soup = BeautifulSoup(gh_html, "html.parser")
                                bio_el = gh_soup.find(attrs={"itemprop": "description"}) or gh_soup.find(class_="user-profile-bio")
                                gh_bio = bio_el.get_text(strip=True) if bio_el else ""
                                if gh_bio:
                                    enrichment_texts.append(f"GitHub bio: {gh_bio}")
                            break
            except Exception as exc:
                logger.warning("[ENRICH] DDG GitHub lightweight search failed: %s", exc)

            time.sleep(1.0)
            try:
                results = list(ddgs.text(f'"{founder_name}" crunchbase', backend="lite", max_results=3))
                for r in results:
                    url = r.get("href") or ""
                    if "crunchbase.com/person/" in url.lower():
                        score, reason = _calculate_confidence(r, founder_name, company_name, cofounders)
                        if score >= 4:
                            enrichment_texts.append(f"Crunchbase metadata: {r.get('body')}")
                            break
            except Exception as exc:
                logger.warning("[ENRICH] DDG Crunchbase lightweight search failed: %s", exc)
    except Exception as exc:
        logger.warning("[ENRICH] DDGS lightweight search initialization failed: %s", exc)

    return " ".join(enrichment_texts)


def enrich_founder_deep_contacts(
    founder_name: str,
    company_name: str,
    company_domain: str,
    cofounders: list[str],
    yc_people_slug: str = "",
    yc_profile_html: str | None = None
) -> dict[str, str]:
    personal_website = ""
    email = ""
    phone = ""
    linkedin_profile = ""
    
    if yc_profile_html:
        soup = BeautifulSoup(yc_profile_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            href = normalize_url(href)
            href_lower = href.lower()
            
            if "linkedin.com/in/" in href_lower:
                linkedin_profile = href
            elif "github.com/" in href_lower:
                pass
            elif is_valid_personal_website(href, founder_name, company_domain):
                if not personal_website:
                    personal_website = href
                    logger.info("[ENRICH] Website found: %s", href)

    try:
        import time
        with DDGS() as ddgs:
            # 1. Official website queries
            if not personal_website:
                queries = [
                    f'"{founder_name}" official website',
                    f'"{founder_name}" personal website',
                    f'"{founder_name}" blog',
                ]
                for i, q in enumerate(queries):
                    if personal_website:
                        break
                    if i > 0:
                        time.sleep(1.0)
                    try:
                        results = list(ddgs.text(q, backend="lite", max_results=3))
                        for r in results:
                            url = r.get("href") or ""
                            url = normalize_url(url)
                            
                            score, reason = _calculate_confidence(r, founder_name, company_name, cofounders)
                            if score < 4:
                                logger.info("[ENRICH] Confidence too low")
                                continue
                                
                            if is_valid_personal_website(url, founder_name, company_domain):
                                personal_website = url
                                logger.info("[ENRICH] Website found: %s", url)
                                break
                    except Exception as exc:
                        logger.warning("[ENRICH] DDG search failed for query '%s': %s", q, exc)

            # 2. Github search
            github_profile_url = ""
            if not personal_website or not email:
                time.sleep(1.0)
                try:
                    results = list(ddgs.text(f'"{founder_name}" github', backend="lite", max_results=3))
                    for r in results:
                        url = r.get("href") or ""
                        url = normalize_url(url)
                        url_lower = url.lower()
                        if "github.com/" in url_lower and not any(x in url_lower for x in ["/features", "/pricing", "/about", "/trending"]):
                            score, reason = _calculate_confidence(r, founder_name, company_name, cofounders)
                            if score >= 4:
                                github_profile_url = url
                                break
                except Exception as exc:
                    logger.warning("[ENRICH] DDG GitHub search failed: %s", exc)
            
            if github_profile_url:
                gh_html = fetch_html(github_profile_url)
                if gh_html:
                    gh_soup = BeautifulSoup(gh_html, "html.parser")
                    gh_blog = ""
                    for a in gh_soup.find_all("a", href=True):
                        if a.get("itemprop") == "url" or (a.get("rel") and "me" in a.get("rel")):
                            gh_blog = normalize_url(a["href"].strip())
                            break
                            
                    if gh_blog and is_valid_personal_website(gh_blog, founder_name, company_domain):
                        if not personal_website:
                            personal_website = gh_blog
                            logger.info("[ENRICH] Website found: %s", gh_blog)
                            
                    gh_email = ""
                    for a in gh_soup.find_all("a", href=True):
                        href = a["href"].strip()
                        if href.startswith("mailto:"):
                            gh_email = href.replace("mailto:", "").split("?")[0].strip()
                            break
                    if gh_email:
                        if is_valid_email(gh_email):
                            email = gh_email
                            logger.info("[ENRICH] Email found: %s", email)

            # 3. Crunchbase search
            if not personal_website:
                time.sleep(1.0)
                try:
                    results = list(ddgs.text(f'"{founder_name}" crunchbase', backend="lite", max_results=3))
                    for r in results:
                        url = r.get("href") or ""
                        url = normalize_url(url)
                        if "crunchbase.com/person/" in url.lower():
                            score, reason = _calculate_confidence(r, founder_name, company_name, cofounders)
                            if score >= 4:
                                body_text = r.get("body") or ""
                                found_links = re.findall(r'https?://[^\s\)]+', body_text)
                                cb_pw = ""
                                for fl in found_links:
                                    fl_clean = normalize_url(fl.rstrip(".,;"))
                                    if is_valid_personal_website(fl_clean, founder_name, company_domain):
                                        cb_pw = fl_clean
                                        break
                                if cb_pw:
                                    personal_website = cb_pw
                                    logger.info("[ENRICH] Website found: %s", cb_pw)
                                    break
                except Exception as exc:
                    logger.warning("[ENRICH] DDG Crunchbase search failed: %s", exc)

            if personal_website:
                pw_html = fetch_html(personal_website)
                if pw_html:
                    pw_soup = BeautifulSoup(pw_html, "html.parser")
                    pw_text = pw_soup.get_text(separator=" ", strip=True)
                    if not email:
                        email_candidate = extract_email_from_text(pw_text, personal_website)
                        if email_candidate and is_valid_email(email_candidate):
                            email = email_candidate
                            logger.info("[ENRICH] Email found: %s", email)
                    if not phone:
                        phone_candidate = extract_phone_from_text(pw_text, founder_name)
                        if phone_candidate:
                            phone = phone_candidate
                            logger.info("[ENRICH] Phone found: %s", phone)
                    sub_links = []
                    for a in pw_soup.find_all("a", href=True):
                        href = a["href"].strip()
                        link_text = a.get_text().lower()
                        if any(kw in link_text for kw in ["contact", "about", "me", "reach", "hire", "phone"]):
                            full_href = urljoin(personal_website, href)
                            if urlparse(full_href).netloc == urlparse(personal_website).netloc:
                                sub_links.append(full_href)
                    for sub_link in list(dict.fromkeys(sub_links))[:2]:
                        if email and phone:
                            break
                        sub_html = fetch_html(sub_link)
                        if sub_html:
                            sub_soup = BeautifulSoup(sub_html, "html.parser")
                            sub_text = sub_soup.get_text(separator=" ", strip=True)
                            if not email:
                                e_cand = extract_email_from_text(sub_text, personal_website)
                                if e_cand and is_valid_email(e_cand):
                                    email = e_cand
                                    logger.info("[ENRICH] Email found: %s", email)
                            if not phone:
                                p_cand = extract_phone_from_text(sub_text, founder_name)
                                if p_cand:
                                    phone = p_cand
                                    logger.info("[ENRICH] Phone found: %s", phone)

            # 4. General contact search query fallback
            if not email or not phone:
                time.sleep(1.0)
                try:
                    q = f'"{founder_name}" "{company_name}" email contact phone'
                    results = list(ddgs.text(q, backend="lite", max_results=3))
                    combined_text = " ".join(f"{r.get('title', '')} {r.get('body', '')}" for r in results)
                    if not email:
                        e_cand = extract_email_from_text(combined_text, personal_website)
                        if e_cand and is_valid_email(e_cand):
                            email = e_cand
                            logger.info("[ENRICH] Email found: %s", email)
                    if not phone:
                        p_cand = extract_phone_from_text(combined_text, founder_name)
                        if p_cand:
                            phone = p_cand
                            logger.info("[ENRICH] Phone found: %s", phone)
                except Exception as exc:
                    logger.warning("[ENRICH] DDG general contact search failed: %s", exc)

    except Exception as exc:
        logger.warning("[ENRICH] DDGS contact search initialization failed: %s", exc)

    return {
        "personal_website": personal_website,
        "email": email,
        "phone": phone,
        "linkedin_profile": linkedin_profile
    }

def get_company_founders(company_url: str) -> list[dict[str, Any]]:
    if company_url.startswith("/"):
        company_url = urljoin(YC_BASE, company_url)

    html = fetch_html(company_url)
    if not html:
        return []

    meta = extract_meta_description(html)
    description = meta.get("description") or ""
    slug = company_url.rstrip("/").split("/companies/")[-1].split("?")[0]
    company_name = _company_name_from_meta(slug, meta)

    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True)
    company_text = f"{company_name} {description} {page_text}"

    if not is_tech_startup(company_text):
        return []

    founder_names = parse_founders_from_company_description(description)
    linkedin_urls = extract_linkedin_profiles(html)
    company_site = meta.get("personal_website")

    people_extra: dict[str, str] = {}
    people_slug_map: dict[str, str] = {}
    people_html_map: dict[str, str] = {}
    
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
        norm_person = normalize_name(person_name)
        if is_valid_person_name(norm_person):
            people_extra[norm_person] = person_desc
            people_slug_map[norm_person] = people_slug
            people_html_map[norm_person] = person_html

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
        lightweight_text = enrich_founder_lightweight(
            norm,
            company_name,
            founder_names,
            people_slug_map.get(norm, ""),
            people_html_map.get(norm)
        )
        yc_bio = f"{norm} is a co-founder of {company_name}."
        if extra:
            yc_bio += f" {extra}"
        founder_profile_text = " ".join(filter(None, [yc_bio, lightweight_text]))
        validation_res = validate_founder_angel(norm, founder_profile_text)
        
        if not validation_res["valid"]:
            continue

        enrichment_result = enrich_founder_deep_contacts(
            norm, 
            company_name, 
            company_domain, 
            founder_names,
            people_slug_map.get(norm, ""),
            people_html_map.get(norm)
        )
        
        website = enrichment_result["personal_website"]
        email = enrichment_result["email"]
        phone = enrichment_result["phone"]
        yc_linkedin = match_linkedin_to_founder(norm, linkedin_urls)
        linkedin = yc_linkedin or enrichment_result["linkedin_profile"]

        if website:
            if website in assigned_websites:
                website = ""
            else:
                assigned_websites.add(website)

        founders.append(
            {
                "name": norm,
                "company_name": company_name,
                "company_description": description,
                "bio": yc_bio,
                "founder_profile_text": founder_profile_text,
                "website": website,
                "email": email,
                "phone": phone,
                "linkedin": linkedin,
                "company_url": company_url,
            }
        )

    return founders

def scrape_yc() -> Generator[dict[str, Any], None, None]:
    """
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
                "personal_website": founder.get("website") or "",
                "email": founder.get("email") or "",
                "phone": founder.get("phone") or "",
                "source_url": company_url,
            }


def scrape_source(source: str) -> Generator[dict[str, Any], None, None]:
    if source != "yc":
        logger.error("[YC] Unknown source '%s' — only 'yc' is enabled", source)
        return
    yield from scrape_yc()
