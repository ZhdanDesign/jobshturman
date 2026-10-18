"""The product is JobShturman: no old visible names or addresses in what we ship (see docs/RENAME-PLAN.md)."""
import re
import subprocess
from pathlib import Path

from hrshturman_core.brand import ALIASES, PRODUCT_NAME

from hrs_cli import web

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = [".claude-plugin", "plugin", "hrs", "core", "local-app", "collector", "key-server", "docs", "README.md",
           "LICENSE", "install.sh", "install.ps1"]
OLD = re.compile(r"HR[ -]Штурман|HR[ -]Shturman|tools\.owai\.ru/hrshturman|ZhdanDesign/hrshturman")
# Places that must keep the old names: the alias list for the agent and the old address hrs migrates from.
ALLOWED = {"core/hrshturman_core/brand.py", "plugin/skills/hrshturman/SKILL.md", "hrs/hrs_cli/license.py",
           "docs/RENAME-PLAN.md", "plugin/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
           "hrs/tests/test_brand.py", "hrs/tests/test_license.py"}


def test_no_old_names_in_shipped_files():
    tracked = subprocess.run(["git", "ls-files", "-z", *SHIPPED], cwd=ROOT, capture_output=True, check=True,
                             stdin=subprocess.DEVNULL).stdout.decode().split("\0")
    found = []
    for rel in filter(None, tracked):
        if rel not in ALLOWED and (ROOT / rel).is_file():
            text = (ROOT / rel).read_bytes().decode("utf-8", "ignore")
            found += [f"{rel}: {m}" for m in sorted(set(OLD.findall(text)))]
    assert found == []


def test_the_page_and_the_skill_use_the_name():
    page = web.page()
    assert f"<title>{PRODUCT_NAME}</title>" in page and "{{PRODUCT}}" not in page
    skill = (ROOT / "plugin/skills/hrshturman/SKILL.md").read_text(encoding="utf-8").split("\n")[2]
    assert all(alias in skill for alias in ALIASES)
