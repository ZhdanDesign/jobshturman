"""LinkedIn public guest job search (no login).

Uses the same HTML fragments linkedin.com serves to logged-out visitors:
  /jobs-guest/jobs/api/seeMoreJobPostings/search  - cards, 10 per page
  /jobs-guest/jobs/api/jobPosting/<id>             - full description
LinkedIn has no public job-search API. This route is anonymous and fragile:
it may throttle (HTTP 429/999) and its terms restrict automated access, so keep
the page budget small and treat the source as supplementary (ТЗ: LinkedIn is
"подключается дополнительно").
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import ClassVar

from bs4 import BeautifulSoup

from ..errors import SourceError
from ..models import SourceSpec, VacancyData
from ..util import html_to_text, parse_dt, utcnow
from .base import Page, SourceAdapter, make_vacancy

BASE = "https://www.linkedin.com/jobs-guest/jobs/api"


def _has_markup(text: str) -> bool:
    """Past the last page LinkedIn answers 200 with only a doctype and an empty comment."""
    rest = re.sub(r"<!DOCTYPE[^>]*>|<!--.*?-->", "", text, flags=re.DOTALL | re.IGNORECASE)
    return bool(rest.strip())


def _clean(el) -> str | None:
    return el.get_text(" ", strip=True) if el else None


class LinkedInGuestAdapter(SourceAdapter):
    platform = "linkedin"
    transport = "linkedin_guest"
    version = "linkedin_guest/0.1.0"
    date_ordered = False
    limits: ClassVar[list[str]] = [
        "anonymous guest pages: no applications, no messages, no profile data",
        "publication dates have day precision; results are not date-ordered, the time window is a platform filter",
        "linkedin throttles anonymous traffic (429/999); keep page_budget small",
        "linkedin terms restrict automated access; supplementary source only",
    ]

    def check_config(self, spec: SourceSpec) -> None:
        super().check_config(spec)
        if not str(spec.config.get("keywords") or "").strip():
            raise SourceError("not_configured", "linkedin search needs config.keywords")

    def _get(self, url: str, params: dict | None = None):
        resp = self.http.get(url, params=params, ok_statuses=(200, 400, 404, 403, 999))
        if resp.status_code in (403, 999):
            raise SourceError("rate_limited", f"linkedin blocked anonymous request (HTTP {resp.status_code})", can_retry=True, retry_after=3600)
        return resp

    def read_page(self, spec: SourceSpec, token: str | None, since: datetime | None = None) -> Page:
        self.check_config(spec)
        start = int(token or 0)
        params = {"keywords": spec.config["keywords"], "start": start}
        if spec.config.get("location"):
            params["location"] = spec.config["location"]
        if since is not None:
            # Guest search ignores sortBy=DD, but honours a "posted within N seconds" filter.
            params["f_TPR"] = f"r{max(3600, math.ceil((utcnow() - since).total_seconds()))}"
        params.update(spec.config.get("params") or {})
        resp = self._get(f"{BASE}/seeMoreJobPostings/search", params)
        if resp.status_code in (400, 404):
            return Page(items=[], next_token=None)  # past the last result page
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        for card in soup.select("div.base-search-card[data-entity-urn]"):
            m = re.search(r"jobPosting:(\d+)", str(card["data-entity-urn"]))
            if not m:
                continue
            job_id = m.group(1)
            title = _clean(card.select_one(".base-search-card__title"))
            company = _clean(card.select_one(".base-search-card__subtitle"))
            location = _clean(card.select_one(".job-search-card__location"))
            time_el = card.select_one("time[datetime]")
            items.append(make_vacancy(
                platform="linkedin", item_key=f"linkedin:{job_id}", external_id=job_id,
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                title=title, company=company, location=location,
                published_at=parse_dt(str(time_el["datetime"])) if time_el else None,
                text="\n".join(x for x in (title, company, location) if x),
                completeness="snippet", is_vacancy="likely",
                raw={"salary_text": _clean(card.select_one(".job-search-card__salary-info"))},
            ))
        if not items and _has_markup(resp.text):
            raise SourceError("changed_page", "linkedin search fragment has no job cards")
        next_token = str(start + len(items)) if items and start + len(items) < 1000 else None
        return Page(items=items, next_token=next_token)

    def fetch_full(self, spec: SourceSpec, external_id: str) -> VacancyData:
        resp = self._get(f"{BASE}/jobPosting/{int(external_id)}")
        if resp.status_code != 200:
            raise SourceError("unavailable", f"linkedin job {external_id}: HTTP {resp.status_code}", stage="fetch_full")
        soup = BeautifulSoup(resp.text, "html.parser")
        desc = soup.select_one(".description__text--rich .show-more-less-html__markup") or soup.select_one(".description__text--rich")
        if desc is None:
            raise SourceError("changed_page", "linkedin job page has no description", stage="fetch_full")
        criteria = {}
        for li in soup.select(".description__job-criteria-item"):
            key = _clean(li.select_one(".description__job-criteria-subheader"))
            val = _clean(li.select_one(".description__job-criteria-text"))
            if key and val:
                criteria[key] = val
        title = _clean(soup.select_one(".top-card-layout__title"))
        company = _clean(soup.select_one(".topcard__org-name-link")) or _clean(soup.select_one(".topcard__flavor"))
        location = _clean(soup.select_one(".topcard__flavor--bullet"))
        text = html_to_text(str(desc))
        return make_vacancy(
            platform="linkedin", item_key=f"linkedin:{external_id}", external_id=str(external_id),
            url=f"https://www.linkedin.com/jobs/view/{external_id}/",
            title=title, company=company, location=location,
            employment=criteria.get("Employment type"),
            text=text, completeness="full", is_vacancy="likely",
            raw={"criteria": criteria},
        )
