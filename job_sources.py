"""
job_sources.py (v2) -- free job discovery + single-URL intake. No RapidAPI.
 
Two things this gives you:
  1. search_jobs(...)         -> gather + filter many candidate roles (for controlled discovery)
  2. fetch_job_from_url(url)  -> turn ONE posting URL into a job dict for the pipeline
 
Free public board APIs (no key, queried per company, auto-detected ATS):
  Greenhouse : https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  Lever      : https://api.lever.co/v0/postings/{slug}?mode=json
  Ashby      : https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
Optional aggregator: Adzuna (free key: ADZUNA_APP_ID + ADZUNA_APP_KEY).
 
SLUGS ARE BEST-GUESS. A wrong slug just 404s and is skipped. To fix a miss,
open the company's careers page and read the slug from the URL:
  boards.greenhouse.io/<slug>  |  jobs.lever.co/<slug>  |  jobs.ashbyhq.com/<slug>
"""
import os
import re
import html
import requests
 
# ---------------------------------------------------------------------------
# Company universe (curate freely). City tags are just for your reference.
# The fetcher tries Greenhouse, then Lever, then Ashby for each slug.
# ---------------------------------------------------------------------------
COMPANIES = [

# =========================
# FOUNDATION MODEL / AI
# =========================
"openai",
"anthropic",
"cohere",
"mistral",
"perplexity",
"scaleai",
"huggingface",
"togetherai",
"fireworksai",
"anyscale",
"databricks",
"writer",
"harvey",
"glean",
"sierra",
"cresta",
"langchain",
"pinecone",
"modal",
"baseten",
"replicate",
"runway",
"elevenlabs",
"cursor",
"replit",
"sourcegraph",
"poolside",
"magic",
"reflectionai",
"figure",
"wayve",
"hebbia",
"groq",
"cerebras",
"wandb",
"lumaai",
"notion",
"vercel",

# =========================
# CANADA AI / SOFTWARE
# =========================
"waabi",
"xanadu",
"deepgenomics",
"benchsci",
"wealthsimple",
"faire",
"1password",
"clio",
"applyboard",
"stackadapt",
"voiceflow",
"loopio",
"clearco",
"ecobee",
"pointclickcare",
"ritual",
"dialpad",
"coveo",
"ada",
"tenstorrent",

# =========================
# BIG TECH
# =========================
"google",
"deepmind",
"meta",
"microsoft",
"amazon",
"apple",
"nvidia",
"netflix",
"airbnb",
"spotify",
"uber",
"lyft",
"doordash",
"instacart",
"dropbox",
"figma",
"discord",
"coinbase",
"plaid",
"ramp",
"stripe",
"linkedin",
"salesforce",
"snowflake",
"mongodb",
"cloudflare",
"datadog",
"atlassian",
"shopify",

# =========================
# BANKS / FINTECH
# =========================
"jpmorganchase",
"goldmansachs",
"morganstanley",
"capitalone",
"americanexpress",
"visa",
"mastercard",
"paypal",
"robinhood",
"brex",
"chime",
"affirm",
"block",
"intuit",
"nubank",

# =========================
# DATA / ANALYTICS
# =========================
"palantir",
"snowplow",
"confluent",
"dbt",
"fivetran",
"airbyte",
"alteryx",
"tableau",
"looker",

]

 
# Where you'll consider roles. Add/remove freely.
DEFAULT_LOCATIONS = ["Toronto, Ontario, Canada", "San Francisco, CA", "New york", "Montreal","Calgary","Vancouver"]
 
_LOC_SYNONYMS = {
    "toronto": ["toronto", "ontario", "canada", "gta", "mississauga", "waterloo", "remote"],
    "san francisco": ["san francisco", "sf", "bay area", "palo alto", "mountain view",
                      "menlo park", "sunnyvale", "california", " ca", "remote", "united states", "usa","vancouver","calgary","montreal"],
}

_AI_TERMS = [

# AI Engineering
"ai engineer",
"ml engineer",
"machine learning engineer",
"llm engineer",
"applied ai engineer",
"agent engineer",
"ai systems engineer",
"research engineer",
"ai infrastructure",

# Data
"data scientist",
"applied scientist",
"research scientist",
"data analyst",
"analytics engineer",
"business intelligence",

# Core AI Terms
"llm",
"rag",
"agents",
"agentic",
"retrieval",
"inference",
"fine tuning",
"transformers",
"deep learning",
"nlp",
"pytorch",
"tensorflow",
"vllm",
"cuda",
"distributed systems",
"evaluation",
"observability",
"vector database",
"embeddings",
"prompt engineering",

]

 
_SENIOR = [" senior", "sr.", "sr ", " staff", " principal", " lead", " manager",
           " director", "head of", " vp", "vice president", " architect",
           " ii", " iii", " iv", " l4", " l5", " l6"]
_INTERN = ["intern", "co-op", "coop"]
 
_SAMPLE_JOB = {
    "title": "AI / ML Engineer (LLM Systems)", "company": "Example AI",
    "location": "Toronto, Ontario, Canada", "url": "https://example.com/careers/ai-engineer",
    "description": ("Build production LLM systems: multi-agent orchestration, RAG, fine-tuning "
                    "(LoRA/PEFT), reliable inference serving. Required: Python, PyTorch, agent "
                    "frameworks, cloud deployment. Nice: eval/observability infra, vLLM, vector DBs."),
}
 
 
# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()
 
 
def _loc_ok(loc: str, locations: list) -> bool:
    loc = (loc or "").lower()
    if not loc:
        return True
    allowed = []
    for L in locations:
        key = next((k for k in _LOC_SYNONYMS if k in L.lower()), None)
        allowed += _LOC_SYNONYMS.get(key, [w for w in re.split(r"[,\s]+", L.lower()) if len(w) > 2])
    return any(tok.strip() in loc for tok in allowed)
 
 
def _level_ok(title: str, level: str) -> bool:
    t = " " + (title or "").lower() + " "
    if level == "intern":
        return any(k in t for k in _INTERN)
    if level == "entry":
        return not any(s in t for s in _SENIOR)   # keep unmarked + junior, drop senior
    return True
 
 
def _title_ok(title: str, skills: str) -> bool:
    t = (title or "").lower()
    terms = _AI_TERMS + [w.lower() for w in skills.split() if len(w) > 2]
    return any(k in t for k in terms)
 
 
# ---- per-ATS fetchers ------------------------------------------------------
def fetch_greenhouse(slug: str) -> list:
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=8)
    if r.status_code != 200:
        return []
    return [{"title": j.get("title", ""), "company": slug,
             "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""), "description": _clean(j.get("content", ""))}
            for j in r.json().get("jobs", [])]
 
 
def fetch_lever(slug: str) -> list:
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=8)
    if r.status_code != 200 or not isinstance(r.json(), list):
        return []
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append({"title": j.get("text", ""), "company": slug,
                    "location": cats.get("location", ""), "url": j.get("hostedUrl", ""),
                    "description": _clean(j.get("descriptionPlain") or j.get("description", ""))})
    return out
 
 
def fetch_ashby(slug: str) -> list:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", timeout=8)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        if not j.get("isListed", True):
            continue
        out.append({"title": j.get("title", ""), "company": slug,
                    "location": j.get("location", ""), "url": j.get("jobUrl", ""),
                    "description": _clean(j.get("descriptionPlain") or j.get("descriptionHtml", ""))})
    return out
 
 
def fetch_company(slug: str) -> list:
    """Auto-detect ATS: first board that returns jobs wins."""
    for fn in (fetch_greenhouse, fetch_lever, fetch_ashby):
        try:
            jobs = fn(slug)
            if jobs:
                return jobs
        except requests.RequestException:
            continue
    return []
 
 
def fetch_adzuna(skills: str, where: str, max_results: int) -> list:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    country = "ca" if any(c in where.lower() for c in ("toronto", "ontario", "canada")) else "us"
    params = {"app_id": app_id, "app_key": app_key, "what": skills,
              "where": where.split(",")[0], "results_per_page": max_results,
              "content-type": "application/json"}
    r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params, timeout=20)
    if r.status_code != 200:
        return []
    return [{"title": j.get("title", ""), "company": (j.get("company") or {}).get("display_name", ""),
             "location": (j.get("location") or {}).get("display_name", ""),
             "url": j.get("redirect_url", ""), "description": _clean(j.get("description", ""))}
            for j in r.json().get("results", [])]
 
 
# ---------------------------------------------------------------------------
# 1) DISCOVERY: gather + filter many candidates (selection to 5 happens upstream)
# ---------------------------------------------------------------------------
def search_jobs(skills: str, locations=None, location=None,
                level: str = "entry", employment_type: str = "FULLTIME",
                max_candidates: int = 60) -> list:
    locations = locations or ([location] if location else DEFAULT_LOCATIONS)
    lvl = "intern" if employment_type.upper() == "INTERN" else level
    jobs = []
 
    for slug in COMPANIES:
        jobs += fetch_company(slug)
    for L in locations:
        try:
            jobs += fetch_adzuna(skills, L, 20)
        except requests.RequestException as e:
            print(f"[adzuna:{L}] {e}")
 
    seen, filtered = set(), []
    for j in jobs:
        key = (j["company"].lower(), j["title"].lower())
        if key in seen:
            continue
        if _title_ok(j["title"], skills) and _level_ok(j["title"], lvl) and _loc_ok(j["location"], locations):
            seen.add(key)
            filtered.append(j)
 
    print(f"[search_jobs] probed {len(COMPANIES)} companies -> {len(filtered)} candidate role(s) "
          f"(level='{lvl}', locations={locations})")
    return (filtered or [_SAMPLE_JOB])[:max_candidates]
 
 
# ---------------------------------------------------------------------------
# 2) SINGLE-URL INTAKE: posting URL -> job dict
# ---------------------------------------------------------------------------
def fetch_job_from_url(url: str):
    u = url.strip()
 
    # Greenhouse: .../{slug}/jobs/{id}  or  ?gh_jid={id}
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+).*?(?:/jobs/|gh_jid=)(\d+)", u)
    if "greenhouse" in u and m:
        slug, jid = m.group(1), m.group(2)
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}", timeout=15)
        if r.status_code == 200:
            j = r.json()
            return {"title": j.get("title", ""), "company": slug,
                    "location": (j.get("location") or {}).get("name", ""),
                    "url": j.get("absolute_url", u), "description": _clean(j.get("content", ""))}
 
    # Lever: jobs.lever.co/{slug}/{id}
    m = re.search(r"lever\.co/([^/?#]+)/([0-9a-f\-]{36})", u)
    if m:
        slug, jid = m.group(1), m.group(2)
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}/{jid}?mode=json", timeout=15)
        if r.status_code == 200:
            j = r.json()
            cats = j.get("categories") or {}
            return {"title": j.get("text", ""), "company": slug, "location": cats.get("location", ""),
                    "url": j.get("hostedUrl", u),
                    "description": _clean(j.get("descriptionPlain") or j.get("description", ""))}
 
    # Ashby: jobs.ashbyhq.com/{slug}/{id} -> fetch board, match by id in jobUrl
    m = re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-f\-]{36})", u)
    if m:
        slug, jid = m.group(1), m.group(2)
        for j in fetch_ashby(slug):
            if jid in (j.get("url") or ""):
                return j
 
    # Generic page: best-effort fetch + strip. If it fails, use apply_from_text.
    try:
        r = requests.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.text) > 500:
            title = re.search(r"<title>(.*?)</title>", r.text, re.I | re.S)
            body = _clean(r.text)
            if len(body) > 300:
                return {"title": _clean(title.group(1)) if title else "Role", "company": "",
                        "location": "", "url": u, "description": body[:6000]}
    except requests.RequestException:
        pass
 
    print(f"[fetch_job_from_url] couldn't auto-extract {u}. Paste the JD via apply_from_text() instead.")
    return None
 
 
if __name__ == "__main__":
    for j in search_jobs("AI ML engineer LLM")[:15]:
        print(f"- {j['title']}  @ {j['company']}  ({j['location']})")
 