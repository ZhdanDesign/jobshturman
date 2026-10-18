"""HeadHunter.

hh_api  - official api.hh.ru with an OAuth/app token. Since spring 2026 anonymous
          GET /vacancies answers 403, so a token is mandatory.
hh_web  - public hh.ru search pages; reads the JSON state embedded in the page
          (template#HH-Lux-InitialState). No account. Same identity (vacancy_id)
          as the API, so both transports produce the same item_key hh:<id>.
"""

from __future__ import annotations

import html as htmllib
import json
import re

from ..errors import SourceError
from ..models import Salary, SourceSpec, VacancyData
from ..util import html_to_text, parse_dt
from .base import Page, SourceAdapter, make_vacancy

_STATE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)
_PERIODS = {"MONTH": "month", "HOUR": "hour", "DAY": "day", "SHIFT": "shift", "FLY_IN_FLY_OUT": "fifo"}


SALARY_PARAMS = ("salary", "only_with_salary")


def _search_params(spec: SourceSpec) -> dict:
    text = str(spec.config.get("text") or "").strip()
    if not text:
        raise SourceError("not_configured", "hh search needs config.text")
    params: dict = {"text": text, "order_by": "publication_time"}
    if spec.config.get("area"):
        params["area"] = spec.config["area"]
    params.update(spec.config.get("params") or {})
    # Salary is checked after collection: hh would hide every vacancy without a stated salary.
    for key in SALARY_PARAMS:
        params.pop(key, None)
    return params


def _snippet_text(title, company, location, salary: Salary | None) -> str:
    parts = [title, company, location]
    if salary and (salary.min or salary.max):
        parts.append(f"{salary.min or ''}-{salary.max or ''} {salary.currency or ''}".strip())
    return "\n".join(p for p in parts if p)


class HHWebAdapter(SourceAdapter):
    platform = "hh"
    transport = "hh_web"
    version = "hh_web/0.1.0"
    limits = [
        "public search pages, no account: no applications, no chat",
        "search results carry no description; fetch_full reads the vacancy page",
        "hh.ru shows at most 2000 results per query",
        "hh.ru may answer with a captcha; then the source stops with captcha_required",
    ]

    def check_config(self, spec: SourceSpec) -> None:
        super().check_config(spec)
        _search_params(spec)

    def _state(self, url: str, params: dict | None = None) -> dict:
        resp = self.http.get(url, params=params, ok_statuses=(200, 404))
        if resp.status_code == 404:
            raise SourceError("unavailable", f"{url} returned 404")
        if "/account/captcha" in str(resp.url) or "/captcha" in resp.url.path:
            raise SourceError("captcha_required", "hh.ru asks for a captcha; a person must open hh.ru in a browser", can_retry=True, retry_after=3600)
        m = _STATE.search(resp.text)
        if not m:
            raise SourceError("changed_page", "hh.ru page has no embedded state")
        try:
            return json.loads(htmllib.unescape(m.group(1)))
        except json.JSONDecodeError as exc:
            raise SourceError("changed_page", f"hh.ru state is not JSON: {exc}") from exc

    def read_page(self, spec: SourceSpec, token: str | None, since=None) -> Page:
        params = _search_params(spec) | {"page": int(token or 0), "items_on_page": 50}
        state = self._state("https://hh.ru/search/vacancy", params)
        result = state.get("vacancySearchResult")
        if not isinstance(result, dict) or "vacancies" not in result:
            raise SourceError("changed_page", "vacancySearchResult missing")
        items = [self._from_search(v) for v in result["vacancies"]]
        nxt = (result.get("paging") or {}).get("next") or {}
        next_token = str(nxt["page"]) if nxt and not nxt.get("disabled") and items else None
        return Page(items=items, next_token=next_token, source_meta={"total_results": result.get("totalResults")})

    def _from_search(self, v: dict) -> VacancyData:
        vid = str(v["vacancyId"])
        company = (v.get("company") or {}).get("visibleName") or (v.get("company") or {}).get("name")
        location = (v.get("area") or {}).get("name")
        salary = _salary(v.get("compensation"))
        formats = sorted({f for wf in v.get("workFormats") or [] for f in wf.get("workFormatsElement", [])})
        return make_vacancy(
            platform="hh",
            item_key=f"hh:{vid}",
            external_id=vid,
            url=f"https://hh.ru/vacancy/{vid}",
            title=v.get("name"),
            company=company,
            location=location,
            salary=salary,
            employment=v.get("employmentForm"),
            work_format=formats,
            published_at=parse_dt((v.get("publicationTime") or {}).get("$")),
            text=_snippet_text(v.get("name"), company, location, salary),
            completeness="snippet",
            is_vacancy="likely",
            raw={"promoted": bool(v.get("@isAdv")), "employer_id": (v.get("company") or {}).get("id"), "area_id": (v.get("area") or {}).get("@id"),
                 "work_experience": v.get("workExperience"), "closed_for_applicants": v.get("closedForApplicants")},
        )

    def fetch_full(self, spec: SourceSpec, external_id: str) -> VacancyData:
        state = self._state(f"https://hh.ru/vacancy/{int(external_id)}")
        try:
            v = state["vacancyView"]["vacancyFull"]["vacancy"]
        except (KeyError, TypeError) as exc:
            raise SourceError("changed_page", "vacancyView.vacancyFull.vacancy missing", stage="fetch_full") from exc
        company = (v.get("company") or {}).get("visibleName") or (v.get("company") or {}).get("name")
        location = (v.get("area") or {}).get("name")
        salary = _salary(v.get("compensation"))
        status = v.get("status") or {}
        description = html_to_text(v.get("description"))
        skills = [s for s in v.get("keySkills") or [] if s]
        text = description + (("\n\nКлючевые навыки: " + ", ".join(skills)) if skills else "")
        return make_vacancy(
            platform="hh",
            item_key=f"hh:{external_id}",
            external_id=str(external_id),
            url=f"https://hh.ru/vacancy/{external_id}",
            title=v.get("name"),
            company=company,
            location=location,
            salary=salary,
            employment=v.get("employmentForm"),
            work_format=sorted(v.get("workFormats") or []),
            published_at=parse_dt(v.get("publicationTimeIso")),
            edited_at=parse_dt(v.get("lastChangeTimeIso")),
            text=text,
            completeness="full",
            is_vacancy="likely",
            raw={"employer_id": (v.get("company") or {}).get("id"), "archived": status.get("archived"),
                 "closed_for_applicants": v.get("closedForApplicants"), "work_experience": v.get("workExperience"),
                 "valid_to": v.get("validToTimeIso"), "key_skills": skills},
        )


class HHApiAdapter(SourceAdapter):
    platform = "hh"
    transport = "hh_api"
    version = "hh_api/0.1.0"
    limits = ["needs an hh.ru access token (application or candidate OAuth)", "API returns at most 2000 results per query"]

    def _headers(self) -> dict:
        creds = self.credentials.hh if self.credentials else None
        if creds is None:
            raise SourceError("not_configured", "hh_api needs credentials.hh.access_token")
        return {
            "Authorization": f"Bearer {creds.access_token.get_secret_value()}",
            "HH-User-Agent": "JobShturman-collector/0.1",
        }

    def check_config(self, spec: SourceSpec) -> None:
        super().check_config(spec)
        _search_params(spec)
        self._headers()

    def account_authorized(self) -> bool | None:
        return True

    def _get(self, url: str, params: dict | None = None) -> dict:
        resp = self.http.get(url, params=params, headers=self._headers(), ok_statuses=(200, 403, 401, 404, 400))
        if resp.status_code in (401, 403):
            raise SourceError("not_authenticated", f"api.hh.ru {resp.status_code}: token missing, expired or without rights")
        if resp.status_code == 404:
            raise SourceError("unavailable", f"{url} not found")
        if resp.status_code == 400:
            raise SourceError("invalid_schema", f"api.hh.ru 400: {resp.text[:200]}", can_retry=False)
        return resp.json()

    def read_page(self, spec: SourceSpec, token: str | None, since=None) -> Page:
        params = _search_params(spec) | {"page": int(token or 0), "per_page": 100}
        data = self._get("https://api.hh.ru/vacancies", params)
        items = []
        for v in data.get("items", []):
            vid = str(v["id"])
            sal = v.get("salary") or {}
            salary = Salary(min=sal.get("from"), max=sal.get("to"), currency=sal.get("currency"), gross=sal.get("gross")) if sal else None
            snippet = v.get("snippet") or {}
            text = "\n".join(html_to_text(x) for x in (snippet.get("responsibility"), snippet.get("requirement")) if x)
            company = (v.get("employer") or {}).get("name")
            location = (v.get("area") or {}).get("name")
            items.append(make_vacancy(
                platform="hh", item_key=f"hh:{vid}", external_id=vid, url=v.get("alternate_url"),
                title=v.get("name"), company=company, location=location, salary=salary,
                employment=(v.get("employment") or {}).get("id"),
                published_at=parse_dt(v.get("published_at")),
                text=text or _snippet_text(v.get("name"), company, location, salary),
                completeness="snippet", is_vacancy="likely",
                raw={"employer_id": (v.get("employer") or {}).get("id"), "archived": v.get("archived")},
            ))
        page, pages = data.get("page", 0), data.get("pages", 0)
        return Page(items=items, next_token=str(page + 1) if page + 1 < pages else None,
                    source_meta={"total_results": data.get("found")})

    def fetch_full(self, spec: SourceSpec, external_id: str) -> VacancyData:
        v = self._get(f"https://api.hh.ru/vacancies/{int(external_id)}")
        sal = v.get("salary") or {}
        salary = Salary(min=sal.get("from"), max=sal.get("to"), currency=sal.get("currency"), gross=sal.get("gross")) if sal else None
        skills = [s.get("name") for s in v.get("key_skills") or [] if s.get("name")]
        text = html_to_text(v.get("description")) + (("\n\nКлючевые навыки: " + ", ".join(skills)) if skills else "")
        return make_vacancy(
            platform="hh", item_key=f"hh:{external_id}", external_id=str(external_id), url=v.get("alternate_url"),
            title=v.get("name"), company=(v.get("employer") or {}).get("name"), location=(v.get("area") or {}).get("name"),
            salary=salary, employment=(v.get("employment") or {}).get("id"),
            published_at=parse_dt(v.get("published_at")), text=text, completeness="full", is_vacancy="likely",
            raw={"employer_id": (v.get("employer") or {}).get("id"), "archived": v.get("archived"), "key_skills": skills},
        )


def _salary(comp: dict | None) -> Salary | None:
    if not comp or not (comp.get("from") or comp.get("to")):
        return None
    return Salary(min=comp.get("from"), max=comp.get("to"), currency=comp.get("currencyCode"),
                  gross=comp.get("gross"), period=_PERIODS.get(comp.get("mode") or "", comp.get("mode")))
