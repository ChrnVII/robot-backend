"""Static dependency guard; includes nested and relative Python imports."""

import ast
from importlib.util import resolve_name
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.no_database
ROOT = Path(__file__).resolve().parents[2]


def dependencies(source: str, package: str):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
            yield base
            yield from (f"{base}.{alias.name}" for alias in node.names)


def forbidden(module: str, target: str) -> bool:
    def under(name, prefix):
        return name == prefix or name.startswith(prefix + ".")
    if under(module, "app.contracts"):
        return target != "app" and not under(target, "app.contracts") and target.split(".")[0] not in sys.stdlib_module_names | {"__future__"}
    if under(module, "app.robot_core"):
        return any(under(target, f"app.{name}") for name in ("api", "services", "gateways", "db", "schemas", "security"))
    if any(under(module, f"app.{name}") for name in ("api", "services", "gateways")):
        return under(target, "app.robot_core")
    return False


def test_project_dependency_boundaries():
    violations = []
    for path in (ROOT / "app").rglob("*.py"):
        parts = path.relative_to(ROOT).with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        package = module if parts[-1] == "__init__" else module.rpartition(".")[0]
        for target in dependencies(path.read_text(encoding="utf-8-sig"), package):
            if forbidden(module, target):
                violations.append(f"{module} imports {target}")
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("source,package,module", [
    ("from app import robot_core", "app.services", "app.services.test"),
    ("from ...services import inspection", "app.robot_core.storage", "app.robot_core.storage.test"),
    ("def f():\n import app.db.database", "app.robot_core", "app.robot_core.test"),
    ("from app.common import config", "app.contracts", "app.contracts.test"),
    ("import pydantic", "app.contracts", "app.contracts.test"),
])
def test_guard_detects_forbidden_imports(source, package, module):
    assert any(forbidden(module, target) for target in dependencies(source, package))


def test_service_database_and_contract_standard_library_are_allowed():
    assert not forbidden("app.services.inspection", "app.db.database")
    assert not forbidden("app.contracts.wire", "dataclasses")
