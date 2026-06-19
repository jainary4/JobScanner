"""
apply.py -- the entrypoint. Two ways to use it:
 
  # A) You found a posting -> get tailored resume + cover letter + outreach email
  python apply.py --url "https://jobs.lever.co/somecompany/abc-123"
 
  # B) Fetch failed / you only have the text -> paste the JD
  python apply.py --text jd.txt --company "Cohere" --title "ML Engineer" --url "https://..."
 
  # C) Let the system find roles, in a CONTROLLED way (entry-level, Toronto + SF)
  python apply.py --discover --top 5
 
Controlled discovery means: gather many candidates from the free boards, run a
CHEAP pre-screen (Haiku) that estimates ATS fit AND whether your resume can be
HONESTLY tailored to pass the threshold, then run the full (expensive) tailoring
+ cover-letter + outreach pipeline on ONLY the best 5. Nothing is auto-sent.
 
Reuses the pipeline you already have in jobs.py:
  job_workflow, load_dossier, CANDIDATE_PROFILE, SCORE_THRESHOLD
(If your file isn't named jobs.py, change the import below.)
"""
import os
import json
import pathlib
import argparse
from typing import List
 
from pydantic import BaseModel, Field
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.deepseek import DeepSeek
 
from jobs import job_workflow, load_dossier, CANDIDATE_PROFILE, SCORE_THRESHOLD  # noqa
from job_sources import search_jobs, fetch_job_from_url
 
PRESCREEN_MODEL = "deepseek-v4-pro"   # cheap gate
MAX_PRESCREEN   = 70              # cap candidates we pay to score
RESUME_PATH     = "resume.tex"
DETAILS_DIR     = "details"
 
 
class PreScore(BaseModel):
    score: int = Field(description="current ATS fit of the resume vs this JD, 0-100")
    tailorable: bool = Field(description="true ONLY if tailoring (reframing real "
                                         "experience, no fabrication) from resume and dossier could plausibly push it past "
                                         "the threshold; false if gaps are fundamental (missing "
                                         "degree, wrong domain, required years he lacks)")
    reason: str = Field(description="one line")
 
 
prescreen = Agent(
    name="Pre-Screener", model=DeepSeek(id=PRESCREEN_MODEL), output_schema=PreScore,
    instructions=[
        "You triage whether a candidate's resume can be tailored to fit THIS job.",
        "You get: the JOB, the current one-page RESUME (a short SUBSET of the candidate's experience), "
        "and a DOSSIER holding their FULL real experience (deep project detail, research, skills).",
        "Judge fit against EVERYTHING in the resume AND the dossier — tailoring can pull any real "
        "detail from the dossier onto the resume, so don't penalize a gap that the dossier already fills.",
        "Estimate the ATS fit ACHIEVABLE after honest tailoring (0-100): reframe real experience and "
        "list transferable tools where the underlying concept is genuinely the same, WITHOUT claiming "
        "hands-on use of tools the candidate has never touched.",
        "Set tailorable=false only when the gap is structural (a required degree, years, or domain the "
        "candidate genuinely lacks even after the dossier). Be decisive and brief.",
    ],
)

 
def _load_inputs():
    resume = pathlib.Path(RESUME_PATH).read_text() if pathlib.Path(RESUME_PATH).exists() else \
        "% missing resume.tex\n\\documentclass{article}\\begin{document}RESUME\\end{document}"
    return resume, load_dossier(DETAILS_DIR)
 
 
def _run_pipeline_on(job: dict):
    resume, dossier = _load_inputs()
    job_workflow.run(input=json.dumps({"resume_tex": resume, "dossier": dossier, "job": job}))
 
 
# --- A/B: one posting -------------------------------------------------------
def apply_from_url(url: str):
    job = fetch_job_from_url(url)
    if not job:
        return
    print(f"\nApplying to: {job['title']} @ {job.get('company') or '(unknown)'}  [{url}]")
    _run_pipeline_on(job)
 
 
def apply_from_text(text_path: str, company: str, title: str, url: str = ""):
    job = {"title": title, "company": company, "location": "",
           "url": url, "description": pathlib.Path(text_path).read_text()}
    print(f"\nApplying to: {title} @ {company}")
    _run_pipeline_on(job)


def _interleave(cands):
    """Round-robin candidates by company so the pre-screen budget covers many
    companies instead of being eaten by the first one or two."""
    from collections import OrderedDict
    by_co = OrderedDict()
    for j in cands:
        by_co.setdefault(j["company"], []).append(j)
    pools, out = [list(v) for v in by_co.values()], []
    while pools:
        for pool in list(pools):
            out.append(pool.pop(0))
            if not pool:
                pools.remove(pool)
    return out
 
# --- C: controlled discovery ------------------------------------------------
def discover_and_apply(top_n: int = 5, skills: str = "AI ML engineer LLM agents",
                       employment_type: str = "FULLTIME", pre_floor: int = 55):
    resume, dossier = _load_inputs()                         # <-- keep the dossier!
    all_cands = search_jobs(skills=skills, employment_type=employment_type,
                            level="entry", max_candidates=1000)
    candidates = _interleave(all_cands)[:MAX_PRESCREEN]      # spread across companies

    scored = []
    for j in candidates:
        try:
            ps: PreScore = prescreen.run(
                f"JOB: {j['title']} @ {j['company']}\n{j['description'][:4000]}\n\n"
                f"RESUME (LaTeX, short subset):\n{resume}\n\n"
                f"CANDIDATE DOSSIER (full real experience to tailor from):\n{dossier}").content
        except Exception as e:
            print(f"  [prescreen failed: {j['title']}] {e}")
            continue
        scored.append((j, ps))
        print(f"  {ps.score:>3}  tailorable={str(ps.tailorable):5}  {j['title']} @ {j['company']}")

    shortlist = sorted([(j, ps) for j, ps in scored if ps.tailorable and ps.score >= pre_floor],
                       key=lambda x: x[1].score, reverse=True)[:top_n]

    print(f"\n=== Shortlist ({len(shortlist)} of {len(scored)} scored, floor={pre_floor}, "
          f"threshold={SCORE_THRESHOLD}) ===")
    for j, ps in shortlist:
        print(f"  {ps.score}  {j['title']} @ {j['company']} — {ps.reason}")
    if not shortlist:
        print("  Nothing cleared the gate. Loosen pre_floor, widen COMPANIES, or check filters.")
        return

    print("\nRunning full pipeline on the shortlist only:\n")
    for j, _ in shortlist:
        print(f"--- {j['title']} @ {j['company']} ---")
        _run_pipeline_on(j)
 
 
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", help="apply to a single posting URL")
    p.add_argument("--text", help="path to a .txt JD (with --company/--title)")
    p.add_argument("--company", default="")
    p.add_argument("--title", default="")
    p.add_argument("--discover", action="store_true", help="controlled discovery")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--intern", action="store_true", help="search internships instead of full-time")
    a = p.parse_args()
 
    et = "INTERN" if a.intern else "FULLTIME"
    if a.url and not a.text:
        apply_from_url(a.url)
    elif a.text:
        apply_from_text(a.text, a.company, a.title, a.url or "")
    elif a.discover:
        discover_and_apply(top_n=a.top, employment_type=et)
    else:
        p.print_help()