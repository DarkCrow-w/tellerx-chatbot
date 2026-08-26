"""Executable dependency rules for the backend package boundaries.

Architecture documentation is easy to ignore during a hurried change. These
tests make the intended dependency direction part of the normal test suite.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
APP_ROOT = PROJECT_ROOT / "app"

ALLOWED_APP_DEPENDENCIES = {
    "contracts": {"contracts"},
    "knowledge": {"knowledge"},
    "db": {"core", "db"},
    "integrations": {"core", "integrations", "knowledge"},
    "repositories": {"core", "db", "repositories"},
    "services": {
        "contracts",
        "core",
        "db",
        "integrations",
        "knowledge",
        "repositories",
        "services",
    },
    "application": {
        "application",
        "contracts",
        "core",
        "db",
        "knowledge",
        "repositories",
        "services",
    },
    "api": {"api", "application", "contracts", "core", "db"},
}


def _app_import_layers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    layers: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        for name in names:
            if name.startswith("app."):
                layers.add(name.split(".", 2)[1])
    return layers


def test_app_root_contains_only_the_asgi_entrypoint() -> None:
    """Feature modules must live in a named package, not the app root."""

    root_modules = {path.name for path in APP_ROOT.glob("*.py")}
    assert root_modules == {"__init__.py", "main.py"}


def test_layer_dependencies_point_in_the_documented_direction() -> None:
    violations: list[str] = []
    for layer, allowed in ALLOWED_APP_DEPENDENCIES.items():
        for path in (APP_ROOT / layer).rglob("*.py"):
            # The composition root is the one intentional exception: its job
            # is to construct concrete adapters and inject them into services.
            if path == APP_ROOT / "core" / "container.py":
                continue
            imported = _app_import_layers(path)
            forbidden = imported - allowed
            if forbidden:
                violations.append(
                    f"{path.relative_to(APP_ROOT)} imports forbidden layers: {sorted(forbidden)}"
                )
    assert not violations, "\n".join(violations)


def test_entrypoint_packages_are_not_imported_by_runtime_layers() -> None:
    """Commands and jobs are outermost adapters and must remain dependency leaves."""

    runtime_layers = (
        "api",
        "application",
        "contracts",
        "core",
        "db",
        "integrations",
        "knowledge",
        "repositories",
        "services",
    )
    violations = []
    for layer in runtime_layers:
        for path in (APP_ROOT / layer).rglob("*.py"):
            imported = _app_import_layers(path)
            forbidden = imported & {"commands", "jobs"}
            if forbidden:
                violations.append(
                    f"{path.relative_to(APP_ROOT)} imports entrypoints: {sorted(forbidden)}"
                )
    assert not violations, "\n".join(violations)


def test_controllers_do_not_contain_database_queries_or_business_services() -> None:
    """Controller 只能调用应用用例，不能直接访问 ORM 模型或底层业务服务。"""

    violations: list[str] = []
    for path in (APP_ROOT / "api" / "routes").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if module == "app.db.models" or (module and module.startswith("app.services")):
                violations.append(f"{path.name} imports {module}")
            if module == "sqlalchemy":
                imported = {alias.name for alias in node.names}
                forbidden = imported & {"select", "func", "text", "delete", "update"}
                if forbidden:
                    violations.append(f"{path.name} imports SQL builders: {sorted(forbidden)}")
    assert not violations, "\n".join(violations)


def test_production_package_does_not_import_quality_code() -> None:
    """Production code must never depend on evaluation or test packages."""

    violations: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            forbidden = [
                name
                for name in names
                if name in {"evaluation", "tests"} or name.startswith(("evaluation.", "tests."))
            ]
            if forbidden:
                violations.append(
                    f"{path.relative_to(APP_ROOT)} imports quality code: {sorted(forbidden)}"
                )
    assert not violations, "\n".join(violations)


def test_production_distribution_excludes_quality_tooling() -> None:
    """The production wheel and console scripts must contain only deployable code."""

    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["setuptools"]["packages"]["find"]["include"] == ["app*"]
    assert all(
        not target.startswith("evaluation.") for target in config["project"]["scripts"].values()
    )
    assert not any(
        dependency.casefold().startswith("reportlab")
        for dependency in config["project"]["dependencies"]
    )
