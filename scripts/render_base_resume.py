"""
Renders profile/candidate_profile.json into profile/base_resume.tex using
resume/templates/base_resume_template.tex - the generic LaTeX template
tailor_cv.py later patches (summary + experience sections) per job. This is
what makes the repo work without anyone hand-writing LaTeX: run
setup_profile.py once (which calls this automatically) and you have a
compilable base resume.

Usage:
    python3 scripts/render_base_resume.py
Produces:
    profile/base_resume.tex (gitignored - personal, regenerate any time
    candidate_profile.json changes)
"""
import json
import subprocess
import sys
from pathlib import Path

from tailor_cv import _latex_escape, _render_experience_section

ROOT = Path(__file__).parent.parent
PROFILE_PATH = ROOT / "profile" / "candidate_profile.json"
TEMPLATE_PATH = ROOT / "resume" / "templates" / "base_resume_template.tex"
OUT_PATH = ROOT / "profile" / "base_resume.tex"
COMPILE_SCRIPT = ROOT / "resume" / "compile.sh"

def _contact_line(profile: dict) -> str:
    p = profile["profile"]
    parts = []
    if p.get("phone"):
        parts.append(rf"\href{{tel:{p['phone']}}}{{\raisebox{{-0.05\height}}\faWhatsapp\ {_latex_escape(p['phone'])}}}")
    if p.get("email"):
        parts.append(rf"\href{{mailto:{p['email']}}}{{\raisebox{{-0.15\height}}\faEnvelope\ {_latex_escape(p['email'])}}}")
    if p.get("linkedin"):
        parts.append(rf"\href{{{p['linkedin']}}}{{\raisebox{{-0.15\height}}\faLinkedin\ LinkedIn}}")
    if p.get("github"):
        parts.append(rf"\href{{{p['github']}}}{{\raisebox{{-0.15\height}}\faGithub\ GitHub}}")
    if p.get("location"):
        suffix = r" \textit{(Open to remote)}" if p.get("open_to_remote") else ""
        parts.append(rf"\raisebox{{-0.15\height}}\faMapMarker\ {_latex_escape(p['location'])}{suffix}")
    return "\n  ~ | ~\n  ".join(parts)


def _skills_items(skills: dict) -> str:
    lines = []
    for category, items in skills.items():
        label = category.replace("_", " ").title()
        lines.append(rf"  \item \textbf{{{_latex_escape(label)}:}} {_latex_escape(', '.join(items))}")
    return "\n".join(lines)


def _education_section(education: list[dict]) -> str:
    blocks = []
    for entry in education:
        date_range = f"{entry.get('start_date') or ''} -- {entry.get('end_date') or ''}".strip(" -")
        header = f"\\headingBf{{{_latex_escape(entry.get('degree') or '')}}}{{{_latex_escape(entry.get('institution') or '')} -- {date_range}}}"
        items = f"  \\item {_latex_escape(entry['details'])}" if entry.get("details") else ""
        block = f"{header}\n\\begin{{resume_list}}\n{items}\n\\end{{resume_list}}" if items else header
        blocks.append(block)
    return "\n\n\\vspace{4pt}\n".join(blocks)


def render(profile: dict | None = None) -> Path:
    if profile is None:
        profile = json.loads(PROFILE_PATH.read_text())

    p = profile["profile"]
    template = TEMPLATE_PATH.read_text()

    rendered = template
    rendered = rendered.replace("{{FULL_NAME}}", _latex_escape(f"{p['first_name']} {p['last_name']}".upper()))
    rendered = rendered.replace("{{HEADLINE}}", _latex_escape(p.get("headline") or ""))
    rendered = rendered.replace("{{CONTACT_LINE}}", _contact_line(profile))
    rendered = rendered.replace("{{SUMMARY}}", _latex_escape(profile.get("summary_template") or ""))
    rendered = rendered.replace("{{SKILLS_ITEMS}}", _skills_items(profile.get("skills") or {}))
    rendered = rendered.replace("{{EXPERIENCE_SECTION}}", _render_experience_section(profile.get("experience") or []))
    rendered = rendered.replace("{{EDUCATION_SECTION}}", _education_section(profile.get("education") or []))

    OUT_PATH.write_text(rendered)
    return OUT_PATH


def main():
    out_path = render()
    print(f"Wrote {out_path}")

    try:
        subprocess.run([str(COMPILE_SCRIPT), str(out_path)], check=True)
    except FileNotFoundError:
        print("compile.sh not executable or pdflatex not found - install TeX Live/TinyTeX to compile a PDF.", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        print(f"Compile failed ({exc}) - check the .log file in profile/", file=sys.stderr)


if __name__ == "__main__":
    main()
