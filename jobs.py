
"""
AI-Engineering Job-Hunt Workflow (Agno 2.x) -- LaTeX edition
============================================================
Inputs you provide:
  - resume.tex                      your resume as LaTeX (structure preserved)
  - details/*.md  or  details/*.txt your "dossier": deep notes on your work
                                    (Nullset backend, AgentScribe, fine-tuning, QI, etc.)
Outputs per job (in job_hunt_output/<company_role>/):
  - tailored_resume.tex   (+ tailored_resume.pdf if a LaTeX compiler is present)
  - cover_letter.txt      (+ .pdf if a compiler is present)
  - outreach_email.txt    (3-4 lines, company-researched via Exa)
  - recruiter.json        (verified contact -- you send it yourself)
  - tailoring_report.json (what changed, claims you must be able to defend, real gaps)
 
Verified on this machine: agno==2.6.17, pdflatex/xelatex present, ExaTools imports.
 
Setup:
    pip install agno anthropic exa_py requests pydantic
    export ANTHROPIC_API_KEY=...
    export EXA_API_KEY=...        # company research for cover letter + outreach
    export RAPIDAPI_KEY=...       # JSearch job search (else a sample AI-eng job is used)
    export HUNTER_API_KEY=...     # recruiter email lookup (else public-page fallback)
    # LaTeX: have TeX Live (pdflatex) or `tectonic` on PATH for PDF output.
 
Run:
    python job_hunt_latex.py
"""
 
import os
import re
import json
import shutil
import pathlib
import subprocess
import requests
from typing import List, Optional
from job_sources import search_jobs
 
from pydantic import BaseModel, Field
 
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.deepseek import DeepSeek
from agno.workflow import Workflow, StepInput, StepOutput
 
try:
    from agno.tools.exa import ExaTools
    _HAVE_EXA = bool(os.environ.get("EXA_API_KEY"))
except Exception:
    ExaTools, _HAVE_EXA = None, False
 
# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
MODEL             = "deepseek-v4-pro"
SCORE_THRESHOLD   = 80
MAX_TAILOR_ROUNDS = 2
MAX_LATEX_FIX     = 2
OUT_DIR           = pathlib.Path("job_hunt_output")
OUT_DIR.mkdir(exist_ok=True)
 
# Canonical, true facts about the candidate (used for headers + as ground truth).
CANDIDATE_PROFILE = """
Name: Aryan Jain
Email: aryan.jain@mail.utoronto.ca | Phone: 647-897-0452
GitHub: github.com/jainary4 | LinkedIn: linkedin.com/in/AryanJain | Location: Toronto, ON
Nullset (founder): https://nullset.app/
AgentScribe (open-source, MIT): https://github.com/jainary4/AgentScribe
Status: Final-year Honours B.Sc. CS + Physics, University of Toronto (grad 2026).
"""
 
# Concept-level skill adjacency for THIS candidate. The tailoring agent uses this
# to reframe real experience in a JD's vocabulary WITHOUT inventing tool usage.
# Left of '<->' = actually used. Right = same concept, may appear in SKILLS only.
SKILL_ADJACENCY = """
- Serverless / cloud compute:  Modal, AWS EC2 (used)  <->  AWS Lambda, GCP Cloud Run, Azure Functions, SageMaker (same serverless/managed-compute concept)
- Multi-agent orchestration:   Agno, LangChain (used) <->  LangGraph, CrewAI, AutoGen, LlamaIndex (same agent-loop/tool-dispatch concept)
- LLM fine-tuning:             vLLM, Distilabel, TRL, LoRA/PEFT, Qwen-8B (used) <-> Axolotl, Unsloth, LLaMA-Factory, DeepSpeed/ZeRO (same PEFT/distributed-FT concept)
- Retrieval + caching:         RAG pipelines, Redis semantic cache (used) <-> Pinecone, Weaviate, pgvector, Qdrant (same vector-retrieval concept)
- Distributed / HPC:           MPI, OpenMP, SLURM, CUDA/CuPy (used) <-> Ray, distributed data/training (same parallelism concept)
- Eval / data infra:           AgentScribe (cross-framework capture, canonical schema, OpenAI/ShareGPT/Alpaca/DPO export), Nullset reliability+telemetry layer (used) <-> LangSmith/eval harnesses (same agent-eval concept)
- MLOps:                       Weights & Biases, TensorBoard, Docker (used)
- Scientific computing:        NumPy, SciPy, CuPy, C/C++ (used)
"""
 
 
# --------------------------------------------------------------------------
# TYPED OUTPUTS
# --------------------------------------------------------------------------
class ATSResult(BaseModel):

    score: int = Field(description="0-100 ATS likelihood score")

    hard_requirements_met: bool

    matched_keywords: List[str]

    missing_keywords: List[str]

    missing_critical_keywords: List[str]

    missing_secondary_keywords: List[str]

    weak_bullets: List[str]

    missing_quantification: List[str]

    title_alignment_issues: List[str]

    recruiter_concerns: List[str]

    rewrite_priorities: List[str]

    verdict: str

    rationale: str


 
 
class TailoringReport(BaseModel):
    changes_summary: List[str] = Field(description="what was rewritten/re-emphasized and why")
    claims_added: List[str] = Field(description="tools/skills now present that weren't before -- candidate MUST be able to defend each in an interview")
    honest_gaps: List[str] = Field(description="JD requirements still genuinely unmet")
    fabrication_flags: List[str] = Field(description="any bullet asserting false SPECIFIC experience; should be empty")
 
 
# --------------------------------------------------------------------------
# TOOLS
# --------------------------------------------------------------------------

 
 
def find_recruiter_email(company_domain: str, full_name: str = "") -> dict:
    """Verified recruiter contact via Hunter.io (permutation + SMTP verify).
    No key -> scrape the company's PUBLIC careers/contact page only (never LinkedIn).
    You send from your own inbox; this never auto-sends."""
    key = os.environ.get("HUNTER_API_KEY")
    if key:
        try:
            if full_name:
                first, _, last = full_name.partition(" ")
                r = requests.get("https://api.hunter.io/v2/email-finder",
                                 params={"domain": company_domain, "first_name": first,
                                         "last_name": last, "api_key": key}, timeout=20)
            else:
                r = requests.get("https://api.hunter.io/v2/domain-search",
                                 params={"domain": company_domain, "department": "hr", "api_key": key}, timeout=20)
            r.raise_for_status()
            return {"source": "hunter", "data": r.json().get("data", {})}
        except Exception as e:
            return {"source": "hunter", "error": str(e)}
    for path in ("careers", "contact", "about", ""):
        try:
            url = f"https://{company_domain}/{path}".rstrip("/")
            html = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).text
            emails = sorted(set(re.findall(r"[a-zA-Z0-9._%+-]+@" + re.escape(company_domain), html)))
            if emails:
                return {"source": f"public:{url}", "emails": emails}
        except Exception:
            continue
    return {"source": "none", "note": "Set HUNTER_API_KEY, or use the recruiter's published address on the careers page."}
 
 
def compile_latex(tex: str, workdir: pathlib.Path, stem: str = "doc") -> tuple[Optional[pathlib.Path], str]:
    """Compile LaTeX -> PDF. Tries tectonic, then xelatex, then pdflatex.
    Returns (pdf_path or None, error_tail). Degrades gracefully if none present."""
    workdir.mkdir(parents=True, exist_ok=True)
    tex_path = workdir / f"{stem}.tex"
    tex_path.write_text(tex)
 
    if shutil.which("tectonic"):
        cmds = [["tectonic", tex_path.name]]
    elif shutil.which("xelatex"):
        cmds = [["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name]] * 2
    elif shutil.which("pdflatex"):
        cmds = [["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name]] * 2
    else:
        return None, "no LaTeX compiler on PATH (install tectonic or TeX Live); .tex saved"
 
    last = ""
    for cmd in cmds:  # run twice so refs/spacing settle
        p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=120)
        last = (p.stdout or "") + (p.stderr or "")
    pdf = workdir / f"{stem}.pdf"
    if pdf.exists():
        return pdf, ""
    errs = [ln for ln in last.splitlines() if ln.startswith("!") or "Error" in ln]
    return None, "\n".join(errs[-12:]) or last[-1000:]
 
 
def _content(resp):
    return resp.content
 
 
def load_dossier(details_dir: str = "details") -> str:
    """Concatenate your .md/.txt work notes into one authoritative dossier.
    (For a very large dossier, swap this for Agno Knowledge + a vector store and
    let the agents retrieve; concatenation is fine up to ~tens of thousands of tokens.)"""
    d = pathlib.Path(details_dir)
    if not d.exists():
        return "(no dossier provided)"
    parts = []
    for f in sorted(d.glob("**/*")):
        if f.suffix.lower() in {".md", ".txt"}:
            parts.append(f"### FILE: {f.name}\n{f.read_text(errors='ignore')}")
    return "\n\n".join(parts) if parts else "(no dossier provided)"
 
 
# --------------------------------------------------------------------------
# AGENTS  --  the instructions are the product. Read these closely.
# --------------------------------------------------------------------------
ats_reviewer = Agent(
name="ATS Reviewer",
model=DeepSeek(id=MODEL),
output_schema=ATSResult,
instructions=[

"""

You are an enterprise ATS evaluator and senior technical recruiter.

You simulate how REAL ATS systems evaluate resumes:

* Greenhouse
* Lever
* Ashby
* Workday
* SmartRecruiters
* Taleo

You evaluate BOTH:

1. ATS keyword/semantic matching
2. Human recruiter quality

SCORING CRITERIA (0-100):

1. HARD REQUIREMENTS (MOST IMPORTANT)

* required tools/frameworks
* programming languages
* cloud platforms
* AI/ML infrastructure
* deployment experience
* production ML systems
* years/seniority alignment
* education requirements

2. SEMANTIC KEYWORD MATCHING
   Evaluate:

* exact keyword overlap
* semantic equivalents
* infrastructure terminology
* AI engineering terminology
* data engineering terminology
* ML deployment terminology

3. EXPERIENCE RELEVANCE
   Prioritize:

* production AI systems
* inference systems
* distributed systems
* agentic systems
* RAG pipelines
* fine-tuning
* evaluation infrastructure
* deployment pipelines
* observability
* scaling

4. RECRUITER SCREEN QUALITY
   Penalize:

* vague bullets
* generic SWE bullets
* lack of metrics
* lack of production context
* keyword stuffing
* weak action verbs

5. ATS PARSEABILITY
   Ensure:

* clean sections
* extractable skills
* standard formatting
* readable chronology

IMPORTANT:

* Do NOT reward keyword stuffing.
* Penalize skills that appear ONLY in skills sections but nowhere in experience.
* Penalize AI resumes that lack deployment or production evidence.
* Penalize resumes that feel research-only for engineering roles.

OUTPUT:

* realistic ATS score
* missing critical requirements
* recruiter objections
* rewrite priorities
* weak bullets
* missing quantification
* semantic keyword gaps

Be strict and realistic.
"""
],
)

 
resume_tailor = Agent(
    name="Resume Tailor (LaTeX)", model=DeepSeek(id=MODEL),
    instructions=[
        "You tailor a LaTeX resume to a specific job. You receive: the full .tex source, "
        "the job description, a candidate DOSSIER (authoritative truth about their work), "
        "a SKILL ADJACENCY map, and the current ATS gap list.",
        "",
        "OUTPUT: return ONLY the complete modified LaTeX document. No prose, no markdown "
        "fences, no commentary. It must compile with the SAME \\documentclass and packages.",
        "",
        "PRESERVE EXACTLY: the preamble, \\documentclass, \\usepackage lines, any custom "
        "command/environment definitions, section ordering, and the overall layout/formatting "
        "macros. You are editing the TEXTUAL CONTENT inside the body only (bullet wording, the "
        "skills/technologies lists, any summary line). Keep it the same length (if it was one "
        "page, keep it one page).",
        "",
        "HOW TO TAILOR (truthful, aggressive repositioning):",
        "1. Rewrite experience bullets to mirror the JD's vocabulary WHERE THAT ACCURATELY "
        "   describes work already in the resume or dossier. Pull concrete, true specifics from "
        "   the dossier (real architectures, numbers, tools) to make bullets land harder.",
        "2. Reframe by shared CONCEPT using the adjacency map: e.g. if the JD wants 'distributed "
        "   training' and the candidate did MPI/OpenMP/CUDA + Distilabel/vLLM work, name the "
        "   underlying competency in the JD's terms -- the work is real, only the framing changes.",
        "3. SKILLS / TECHNOLOGIES section: you MAY list a tool the JD requires that is genuinely "
        "   adjacent to one the candidate has used (per the adjacency map) -- e.g. JD wants GCP, "
        "   candidate used Modal/AWS. Put it in the skills list only. This is the standard place "
        "   for transferable tools.",
        "",
        "HARD RULES (never break, even if asked):",
        "- Never invent or alter employers, titles, dates, degrees, or quantitative metrics.",
        "- Never write a tool the candidate hasn't used INTO AN EXPERIENCE BULLET as something "
        "  they built/deployed with. The named tool inside a project narrative stays truthful; "
        "  adjacent tools live in the SKILLS list, not in fabricated project history.",
        "- If a required item is genuinely absent and has no honest adjacency, leave it out -- "
        "  do not bluff it into the experience section.",
        "Bias toward strengthening what is TRUE. This candidate's real stack (Modal, vLLM, "
        "Distilabel, multi-agent orchestration, RAG+Redis, reliability/eval infra, HPC, quantum "
        "simulation) is strong for AI-engineering roles; surface it, don't counterfeit a new one.",
    ],
)
 
latex_fixer = Agent(
    name="LaTeX Fixer", model=DeepSeek(id=MODEL),
    instructions=[
        "You are given LaTeX that failed to compile, plus the compiler error tail.",
        "Return ONLY corrected LaTeX (no prose, no fences). Fix the compile error with the "
        "smallest change possible. Do NOT alter the resume's content, wording, or meaning -- "
        "only repair the syntax/markup that broke compilation.",
    ],
)
 
resume_auditor = Agent(
    name="Resume Auditor", model=DeepSeek(id=MODEL), output_schema=TailoringReport,
    instructions=[
        "Compare the ORIGINAL resume LaTeX with the TAILORED LaTeX.",
        "Report: what changed and why (changes_summary); every tool/skill that is now present "
        "but wasn't before, which the candidate must be able to defend (claims_added); JD "
        "requirements still genuinely unmet (honest_gaps); and any bullet that now asserts FALSE "
        "specific experience (fabrication_flags -- this should be empty).",
        "Be strict. Treat a tool named inside an experience bullet as a claim of hands-on use.",
    ],
)
 
company_researcher = Agent(
    name="Company Researcher", model=DeepSeek(id=MODEL),
    tools=[ExaTools()] if _HAVE_EXA else [],
    instructions=[
        "Research the company for a job applicant." + ("" if _HAVE_EXA else
        " (No web tool available -- infer cautiously from the job description only.)"),
        ("Use Exa to find: what they actually build, their stack/approach, any recent launch or "
         "announcement, and the concrete engineering problem this role exists to solve. "
         if _HAVE_EXA else ""),
        "Output 4-6 tight bullets: what they build, a recent signal, the core engineering "
        "problem, and the one place a strong AI engineer adds the most value. No fluff, facts only.",
    ],
)
 
cover_letter_writer = Agent(
    name="Cover Letter Writer", model=DeepSeek(id=MODEL),
    instructions=[
        "Write a cover letter in the candidate's voice: direct, technical, concrete, first-person.",
        "BANNED: 'I am writing to express', 'I hope this finds you well', 'passionate', 'synergy', "
        "'leverage', 'I believe my skills', and every generic recruiter cliche. Plain confident sentences.",
        "Inputs: candidate profile + DOSSIER (truth about their work) + resume + JD + company research.",
        "STRUCTURE (<320 words):",
        "- Contact header from the candidate profile.",
        "- One opening line: name the exact role and the ONE specific reason this candidate fits "
        "  THIS company (use the research -- reference their actual problem, not generic praise).",
        "- 1-2 body paragraphs: 2-3 CONCRETE, TRUE proof points from the dossier mapped to the "
        "  JD's top needs (e.g. the Nullset generate->verify->classify->repair reliability layer, "
        "  AgentScribe's cross-framework capture + canonical schema, the vLLM/Distilabel fine-tune "
        "  of Qwen-8B, the QI simulation O(d^6M)->O(M d^3) optimization). Quantify where the resume does.",
        "- One closing line: a specific contribution he'd make to THEIR problem + a low-friction CTA.",
        "Only facts present in the resume/dossier. Output only the letter.",
    ],
)
 
outreach_writer = Agent(
    name="Recruiter Outreach Writer", model=DeepSeek(id=MODEL),
    instructions=[
        "Write a cold email to a recruiter/hiring manager. You receive company research, the JD, "
        "the candidate dossier, and the contact info.",
        "FORMAT: first line 'Subject: ...' (specific, not generic). Then a 3-4 LINE body, no more.",
        "The body must: (1) in one line, connect a specific piece of the candidate's REAL work to "
        "the company's actual problem (from the research); (2) state in 1-2 lines what he can "
        "deliver that's valuable to them, concretely (not 'I'm a great fit'); (3) one low-friction "
        "ask (15-min chat or a pointer to the right person).",
        "Voice: direct, technical, zero filler, zero hype. Only true facts. Output only the email.",
    ],
)
 
 
# --------------------------------------------------------------------------
# WORKFLOW STEPS  (state travels as JSON in step content; LaTeX round-trips fine)
# --------------------------------------------------------------------------
def step_ats_gate(step_input: StepInput) -> StepOutput:
    s = json.loads(step_input.input)
    jd, dossier = s["job"]["description"], s["dossier"]
    original_tex = s["resume_tex"]
    tex = original_tex
 
    res: ATSResult = _content(ats_reviewer.run(f"JOB:\n{jd}\n\nRESUME (LaTeX):\n{tex}"))
    rounds = 0
    while res.score < SCORE_THRESHOLD and rounds < MAX_TAILOR_ROUNDS:
        tex = _content(resume_tailor.run(
            f"JOB DESCRIPTION:\n{jd}\n\nCANDIDATE PROFILE:\n{CANDIDATE_PROFILE}\n\n"
            f"SKILL ADJACENCY:\n{SKILL_ADJACENCY}\n\nDOSSIER:\n{dossier}\n\n"
            f"ATS GAPS TO CLOSE (only if truthful): {res.missing_keywords}\n\n"
            f"CURRENT RESUME (LaTeX -- return the full modified .tex):\n{tex}"))
        tex = re.sub(r"^```(latex|tex)?|```$", "", str(tex).strip(), flags=re.MULTILINE).strip()
        res = _content(ats_reviewer.run(f"JOB:\n{jd}\n\nRESUME (LaTeX):\n{tex}"))
        rounds += 1
 
    report: TailoringReport = _content(resume_auditor.run(
        f"ORIGINAL:\n{original_tex}\n\nTAILORED:\n{tex}")) if tex != original_tex else \
        TailoringReport(changes_summary=["no change needed; already above threshold"],
                        claims_added=[], honest_gaps=res.missing_keywords, fabrication_flags=[])
 
    s.update(resume_tex=tex, score=res.score, passed=res.score >= SCORE_THRESHOLD,
             verdict=res.verdict, report=report.model_dump())
    print(f"  ATS {s['job']['title']}: {res.score} "
          f"({'PASS' if s['passed'] else 'below threshold'}), {rounds} tailor round(s)")
    if report.fabrication_flags:
        print(f"  !! auditor flagged possible fabrication: {report.fabrication_flags}")
    return StepOutput(content=json.dumps(s))
 
 
def step_compile_resume(step_input: StepInput) -> StepOutput:
    s = json.loads(step_input.previous_step_content)
    if not s.get("passed"):
        return StepOutput(content=json.dumps(s))
    job = s["job"]
    folder = OUT_DIR / re.sub(r"[^A-Za-z0-9]+", "_", f"{job['company']}_{job['title']}").strip("_")[:60]
    tex = s["resume_tex"]
 
    pdf, err = compile_latex(tex, folder, stem="tailored_resume")
    fixes = 0
    while pdf is None and err and "no LaTeX compiler" not in err and fixes < MAX_LATEX_FIX:
        tex = re.sub(r"^```(latex|tex)?|```$", "", str(_content(
            latex_fixer.run(f"COMPILER ERROR:\n{err}\n\nLATEX:\n{tex}"))).strip(),
            flags=re.MULTILINE).strip()
        pdf, err = compile_latex(tex, folder, stem="tailored_resume")
        fixes += 1
 
    s.update(resume_tex=tex, folder=str(folder), resume_pdf=str(pdf) if pdf else None)
    print(f"  resume.tex written -> {folder}" + (f" (PDF ok, {fixes} fix round(s))" if pdf
          else f" (PDF skipped: {err[:60]})"))
    return StepOutput(content=json.dumps(s))
 
 
def step_generate_assets(step_input: StepInput) -> StepOutput:
    s = json.loads(step_input.previous_step_content)
    if not s.get("passed"):
        folder = OUT_DIR / "_below_threshold"
        folder.mkdir(exist_ok=True)
        (folder / f"{re.sub(r'[^A-Za-z0-9]+','_',s['job']['company'])}.json").write_text(
            json.dumps({"job": s["job"], "score": s["score"], "report": s["report"]}, indent=2))
        print(f"  skipped assets (score {s['score']} < {SCORE_THRESHOLD}); gaps logged")
        return StepOutput(content=json.dumps(s))
 
    job, dossier, folder = s["job"], s["dossier"], pathlib.Path(s["folder"])
    domain = re.sub(r"^https?://(www\.)?", "", job["url"]).split("/")[0] or "example.com"
 
    research = str(_content(company_researcher.run(
        f"Company: {job['company']}\nRole: {job['title']}\nJob description:\n{job['description']}")))
 
    cover = str(_content(cover_letter_writer.run(
        f"CANDIDATE PROFILE:\n{CANDIDATE_PROFILE}\n\nDOSSIER:\n{dossier}\n\n"
        f"RESUME (LaTeX):\n{s['resume_tex']}\n\nJOB at {job['company']} - {job['title']}:\n"
        f"{job['description']}\n\nCOMPANY RESEARCH:\n{research}")))
 
    contact = find_recruiter_email(domain)
 
    outreach = str(_content(outreach_writer.run(
        f"COMPANY: {job['company']}\nROLE: {job['title']}\nJD:\n{job['description']}\n\n"
        f"COMPANY RESEARCH:\n{research}\n\nCANDIDATE DOSSIER:\n{dossier}\n\n"
        f"CONTACT INFO: {json.dumps(contact)}")))
 
    (folder / "cover_letter.txt").write_text(cover)
    (folder / "outreach_email.txt").write_text(outreach)
    (folder / "recruiter.json").write_text(json.dumps(contact, indent=2))
    (folder / "tailoring_report.json").write_text(json.dumps(s["report"], indent=2))
 
    # optional: cover letter -> PDF via a minimal article wrapper
    esc = (cover.replace("\\", r"\textbackslash{}").replace("&", r"\&").replace("%", r"\%")
                .replace("$", r"\$").replace("#", r"\#").replace("_", r"\_"))
    wrap = ("\\documentclass[11pt]{article}\\usepackage[margin=1in]{geometry}"
            "\\usepackage{parskip}\\begin{document}" +
            esc.replace("\n\n", "\\par ").replace("\n", "\\\\ ") + "\\end{document}")
    cpdf, _ = compile_latex(wrap, folder, stem="cover_letter")
 
    print(f"  wrote cover_letter.txt{'/.pdf' if cpdf else ''}, outreach_email.txt, "
          f"recruiter.json, tailoring_report.json -> {folder}")
    return StepOutput(content=json.dumps({**s, "done": True}))
 
 
job_workflow = Workflow(
    name="AI-Eng Job Application Pipeline",
    steps=[step_ats_gate, step_compile_resume, step_generate_assets],
)
 
 
# --------------------------------------------------------------------------
# DRIVER
# --------------------------------------------------------------------------
def run(resume_tex_path: str = "resume.tex", details_dir: str = "details",
        skills: str = "AI engineer LLM agents machine learning",
        location: str = "Toronto, Ontario, Canada", employment_type: str = "FULLTIME"):
    resume_p = pathlib.Path(resume_tex_path)
    resume_tex = resume_p.read_text() if resume_p.exists() else \
        "% paste your resume LaTeX here\n\\documentclass{article}\\begin{document}RESUME\\end{document}"
    dossier = load_dossier(details_dir)
 
    jobs = search_jobs(skills=skills, location=location, employment_type=employment_type)
    print(f"Found {len(jobs)} role(s) near {location}\n")
    for job in jobs:
        print(f"- {job['title']} @ {job['company']}")
        job_workflow.run(input=json.dumps(
            {"resume_tex": resume_tex, "dossier": dossier, "job": job}))
        print()
 
 
if __name__ == "__main__":
    run(
        resume_tex_path="resume.tex",   # your LaTeX resume
        details_dir="details",          # folder of .md/.txt work notes (your dossier)
        skills="AI engineer LLM agents machine learning",
        location="Toronto, Ontario, Canada",
        employment_type="FULLTIME",     # "INTERN" for internships
    )