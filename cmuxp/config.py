import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repo root
HOME = Path(os.environ.get("CMUXP_HOME", str(Path.home() / ".cmuxp-sandbox")))
PROJECT = os.environ.get("CMUXP_PROJECT", "demo")
MODEL = os.environ.get("CMUXP_MODEL", "sonnet")
CMUX_BIN = os.environ.get("CMUXP_CMUX", "cmux")
SANDBOX = os.environ.get("CMUXP_SANDBOX", str(ROOT / "bin" / "sandbox-env.sh"))
PORT = int(os.environ.get("CMUXP_PORT", "8765"))


def project_dir(project: str = PROJECT) -> Path:
    return HOME / "projects" / project

# A git repo for the project (clone URL, owner/name, or local path). The coordinator
# gets the main clone; each thread gets its own worktree on branch cmuxp/<thread>.
REPO = os.environ.get("CMUXP_REPO", "")

# "tui" (sandboxed cmux-tui, default) or "app" (a cmux.app build, ideally a tagged one)
BACKEND = os.environ.get("CMUXP_BACKEND", "tui")
APP_SOCKET = os.environ.get("CMUXP_APP_SOCKET", "")   # e.g. /tmp/cmux-debug-<tag>.sock
APP_PATH = os.environ.get("CMUXP_APP_PATH", "")       # brought to front on dashboard clicks
