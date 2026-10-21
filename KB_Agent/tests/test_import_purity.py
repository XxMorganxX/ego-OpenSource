import ast
import subprocess
import sys
from pathlib import Path


def test_agent_never_imports_psycopg():
    """The agent must run on a Mac with no database driver semantics: block psycopg and
    import every agent module in a fresh interpreter."""
    code = (
        "import sys; sys.modules['psycopg'] = None; sys.modules['psycopg_pool'] = None\n"
        "import kb_agent.config, kb_agent.signing, kb_agent.state, kb_agent.scanner, "
        "kb_agent.client, kb_agent.pusher, kb_agent.jobs, kb_agent.loop, kb_agent.cli\n"
        "print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_the_only_kb_core_import_is_the_stamp_module():
    """The agent ships to tenants' Macs: everything else it needs it implements itself."""
    src = Path(__file__).resolve().parents[1] / "src" / "kb_agent"
    found = set()
    for path in src.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            found |= {n for n in names if n == "kb_core" or n.startswith("kb_core.")}
    assert found == {"kb_core.adapters.obsidian.stamp"}
