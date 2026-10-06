"""Guard: no production module saves a roll as a snapshot.

Every roll write goes through `roll_manifest.mutate_roll_manifest` (or
`repo.insert_roll` for a new roll), which reloads the roll inside a
`BEGIN IMMEDIATE` transaction and applies only the writer's own change.
`write_roll_manifest` and `repo.save_roll` instead make the database equal a
copy loaded earlier, so with a stale copy they delete or revert other
writers' work. They survive only to build test fixtures.

This test parses the source (it imports nothing it scans) and fails, naming
file and line, if a production module calls either function or imports
`write_roll_manifest`. See docs/TRANSACTIONAL_WRITES_PLAN.md §3.5.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

SNAPSHOT_SAVES = frozenset({"write_roll_manifest", "save_roll"})
FORBIDDEN_IMPORTS = frozenset({"write_roll_manifest"})

# Where the functions are defined (and, in roll_manifest.py, where
# `write_roll_manifest` calls `repo.save_roll`).
ALLOWED = frozenset({"roll_manifest.py", "library/repo.py"})


def _production_modules() -> list[Path]:
    modules = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        name = path.name
        if "__pycache__" in path.parts:
            continue
        if name == "conftest.py" or name.endswith(("_test.py", "_support.py")):
            continue
        modules.append(path)
    return modules


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _violations(path: Path) -> list[str]:
    rel = path.relative_to(PACKAGE_DIR).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in SNAPSHOT_SAVES:
                found.append(f"{rel}:{node.lineno}: calls {name}()")
        elif isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] in FORBIDDEN_IMPORTS:
                    found.append(f"{rel}:{node.lineno}: imports {alias.name}")
    return found


def test_no_production_module_saves_a_roll_as_a_snapshot() -> None:
    modules = _production_modules()
    scanned = {p.relative_to(PACKAGE_DIR).as_posix() for p in modules}
    # The scan must really reach the package, including `library/`, and the
    # allowed modules must still exist: a moved definition would otherwise
    # leave a stale exemption.
    assert "cli.py" in scanned
    assert any(name.startswith("library/") for name in scanned)
    assert ALLOWED <= scanned

    violations = [
        violation
        for path in modules
        if path.relative_to(PACKAGE_DIR).as_posix() not in ALLOWED
        for violation in _violations(path)
    ]
    assert not violations, (
        "Production code must change a roll with "
        "roll_manifest.mutate_roll_manifest (or repo.insert_roll), not a "
        "snapshot save (docs/TRANSACTIONAL_WRITES_PLAN.md §3.5):\n  "
        + "\n  ".join(violations)
    )
