# skills_server.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

from pathlib import Path
import platform
import sys
from fastmcp import FastMCP

# DISABLE HERE #######################################
DISABLED = set()
# When using llama.cpp (no native skills): leave empty
# Claude/DSH/Others (they have their own tools)
DISABLED = {
    # "list_skills",
    # "skills_read"
}
# ####################################################


try:
    from fastmcp.server.providers.skills import SkillsDirectoryProvider

    HAS_PROVIDER = True
except ImportError:
    HAS_PROVIDER = False

mcp_skills = FastMCP("CtrlAsistent Skills")
_SKILLS_DIR = Path(__file__).parent / "skills"


def _env_summary() -> str:
    return (
        f"Environment: {platform.system()} {platform.release()} "
        f"({platform.machine()}), Python: {sys.executable}, "
        f"CWD: {Path.cwd()}"
    )


def _env_header(skill_dir: Path) -> str:
    return f"""## ENVIRONMENT
- OS: {platform.system()} {platform.release()} ({platform.machine()})
- Python interpreter (use THIS for skill scripts): {sys.executable}
- Server working directory: {Path.cwd()}
- Skill directory (absolute): {skill_dir}
- SKILL.md location: {skill_dir / "SKILL.md"}
- Scripts directory (if it exists): {skill_dir / "scripts"}

## PATH RESOLUTION - READ CAREFULLY
Relative paths in this SKILL.md are relative to the SKILL DIRECTORY, not to the CWD.

- If SKILL.md says: `python scripts/pdf_create.py spec.json -o out.pdf`
  → run:
    {sys.executable} {skill_dir}/scripts/pdf_create.py spec.json -o out.pdf
- If SKILL.md refers to `templates/x.html`
  → resolve as: {skill_dir}/templates/x.html
- If SKILL.md refers to `spec.json` as an input you should create
  → write it to the CWD (e.g. {Path.cwd()}/spec.json), NOT inside the skill directory.

DO NOT run `python` bare - always use the interpreter above so dependencies resolve.

---

"""


def _parse_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _iter_skills():
    if not _SKILLS_DIR.is_dir():
        return
    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            yield skill_dir, skill_md, _parse_frontmatter(skill_md)


if "list_skills" not in DISABLED:

    @mcp_skills.tool()
    def list_skills() -> str:
        """
        Lists all available skills. Skills are NOT tools!

        What are Skills?
        - Documented procedures, scripts, and patterns
        - Step-by-step instructions for specific tasks

        What are Tools?
        - Direct functions you can call (like write_file)
        - Capable of performing the core action immediately

        Key Difference:
        Tools execute actions; Skills guide you through complex workflows.

        Usage Guidelines:
        1. Use tools (function calls) for operations
        2. Use skills (read_skill) for complex or specialized tasks
        3. Don't attempt complex tasks from scratch - check skills first!
        """
        rows = [_env_summary(), "", "Available skills:", ""]
        for skill_dir, skill_md, fm in _iter_skills():
            name = fm.get("name", skill_dir.name)
            desc = fm.get("description", "(no description)")
            rows.append(f"- {name}: {desc}")
            rows.append(f"  Path: {skill_dir}")
        if len(rows) == 4:
            return "No skills available."
        rows += ["", "For full instructions and exact paths call: read_skill(name)"]
        return "\n".join(rows)


if "read_skill" not in DISABLED:

    @mcp_skills.tool()
    def read_skill(name: str) -> str:
        """
        Returns the full SKILL.md content for a given skill.
        The output starts with an ENVIRONMENT header that tells you the OS,
        Python interpreter, and absolute skill directory - use these to
        resolve any relative paths that SKILL.md mentions.
        """
        for skill_dir, skill_md, fm in _iter_skills():
            skill_name = fm.get("name", skill_dir.name)
            if skill_name == name:
                body = skill_md.read_text(encoding="utf-8")
                return _env_header(skill_dir) + body
        available = [fm.get("name", d.name) for d, _, fm in _iter_skills()]
        return (
            f"Skill '{name}' not found. Available: {', '.join(available) or '(none)'}"
        )


# Optional - keep native provider for clients that support it
if HAS_PROVIDER and _SKILLS_DIR.is_dir():
    try:
        mcp_skills.add_provider(SkillsDirectoryProvider(roots=_SKILLS_DIR))
    except Exception as e:
        print(f"⚠️  SkillsDirectoryProvider could not be loaded: {e}")


if __name__ == "__main__":
    mcp_skills.run(transport="stdio")
