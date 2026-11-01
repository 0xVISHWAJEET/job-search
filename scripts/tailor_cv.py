"""
Tailors the resume's professional summary and experience bullets to a
specific job description - reordering/selecting/lightly rewording bullets
that already exist in candidate_profile.json, never inventing new claims,
companies, or metrics. Reuses the base resume's own LaTeX macros/styling
(profile/base_resume.tex, rendered by render_base_resume.py from your
candidate_profile.json - see setup_profile.py), so the output looks
identical to the base resume, just re-emphasized.

Usage:
    python3 tailor_cv.py "Acme Corp" "Senior Blockchain Engineer" path/to/jd.txt
Produces:
    ../jobs/outputs/tailored_Acme_Corp_Senior_Blockchain_Engineer.tex
    ../jobs/outputs/tailored_Acme_Corp_Senior_Blockchain_Engineer.pdf
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from gemini_client import GeminiError, generate

ROOT = Path(__file__).parent.parent
PROFILE_PATH = ROOT / "profile" / "candidate_profile.json"
BASE_TEX_PATH = ROOT / "profile" / "base_resume.tex"
OUTPUT_DIR = ROOT / "jobs" / "outputs"
COMPILE_SCRIPT = ROOT / "resume" / "compile.sh"


def _slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def _ask_gemini_to_tailor(job_description: str, profile: dict) -> dict:
    prompt = f"""You are tailoring a resume for a specific job posting. You may ONLY
reorder, select a subset of, or lightly reword the bullets already given below -
never invent a new achievement, metric, company, or skill that isn't already present.

Candidate's full experience inventory (source of truth - every bullet you can use):
{json.dumps(profile["experience"], indent=2)}

Candidate's existing summary (rewrite the emphasis/framing only, same facts):
{profile["summary_template"]}

Job description:
{job_description}

Return ONLY a JSON object (no markdown fences, no other text) with exactly these keys:
- "summary": a rewritten 2-4 sentence professional summary, same facts as the
  original, re-emphasized toward this JD
- "experience": an array matching the input experience array's order and
  "company"/"position"/"start_date"/"end_date"/"location" fields exactly,
  but each "highlights" array trimmed/reordered/lightly reworded to lead
  with what's most relevant to this JD (keep at least 2 highlights per
  role, keep all 4-5 for the most relevant 1-2 roles)
"""

    raw = generate(prompt).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw.strip())


def _latex_escape(text: str) -> str:
    replacements = {
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}",
    }
    for char, escaped in replacements.items():
        text = text.replace(char, escaped)
    return text


def _render_experience_section(experience: list[dict]) -> str:
    blocks = []
    for entry in experience:
        date_range = f"{entry['start_date']} -- {entry['end_date']}"
        header = f"\\headingBf{{{_latex_escape(entry['position'])}}}{{{_latex_escape(entry['company'])} -- {date_range}}}"
        location = f"\\headingIt{{{_latex_escape(entry.get('location') or '')}}}{{}}"
        items = "\n".join(f"  \\item {_latex_escape(h)}" for h in entry["highlights"])
        blocks.append(f"{header}\n{location}\n\\begin{{resume_list}}\n{items}\n\\end{{resume_list}}")

    return "\n\n\\vspace{4pt}\n".join(blocks)


def tailor(company: str, role: str, job_description: str) -> Path:
    profile = json.loads(PROFILE_PATH.read_text())
    if not BASE_TEX_PATH.exists():
        raise FileNotFoundError(
            f"{BASE_TEX_PATH} doesn't exist yet - run "
            "`python3 scripts/setup_profile.py path/to/your_resume.pdf` first "
            "(it generates both candidate_profile.json and base_resume.tex)."
        )
    base_tex = BASE_TEX_PATH.read_text()

    tailored = _ask_gemini_to_tailor(job_description, profile)

    # Swap the summary paragraph (between the "Professional Summary" and
    # "Skills" section markers) and the whole Experience section, leaving
    # everything else (skills, education, honors, certifications,
    # interests) exactly as in the base resume.
    summary_pattern = re.compile(
        r"(%% ── Professional Summary.*?\n)(.*?)(\n\n%% ── Skills)", re.DOTALL
    )
    new_tex = summary_pattern.sub(
        lambda m: f"{m.group(1)}\n  \\small\n  {_latex_escape(tailored['summary'])}\n{m.group(3)}",
        base_tex,
    )

    experience_pattern = re.compile(
        r"(%% ── Experience.*?\\section\{Experience\}\n\n)(.*?)(\n\n%% ── Education)", re.DOTALL
    )
    new_tex = experience_pattern.sub(
        lambda m: f"{m.group(1)}{_render_experience_section(tailored['experience'])}{m.group(3)}",
        new_tex,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_name = f"tailored_{_slugify(company)}_{_slugify(role)}"
    tex_path = OUTPUT_DIR / f"{out_name}.tex"
    tex_path.write_text(new_tex)

    subprocess.run([str(COMPILE_SCRIPT), str(tex_path)], check=True)

    return OUTPUT_DIR / f"{out_name}.pdf"


def main():
    if len(sys.argv) != 4:
        print(f"Usage: python3 {sys.argv[0]} <company> <role> <jd_file_path>", file=sys.stderr)
        sys.exit(1)

    company, role, jd_path = sys.argv[1], sys.argv[2], sys.argv[3]
    job_description = Path(jd_path).read_text()

    try:
        pdf_path = tailor(company, role, job_description)
    except GeminiError as exc:
        print(f"Tailoring failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Tailored resume: {pdf_path}")


if __name__ == "__main__":
    main()
