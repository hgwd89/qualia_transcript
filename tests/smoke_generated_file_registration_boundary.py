import ast
from pathlib import Path


EXCLUDED_TOP_LEVEL = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "tests",
    "migrations",
}
ALLOWED_CONSTRUCTOR_PATHS = {
    Path("services/file_manager.py"),
}


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def is_generated_file_constructor(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "GeneratedFile"
    if isinstance(func, ast.Attribute):
        return func.attr == "GeneratedFile"
    return False


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    scanned = 0

    for path in sorted(repo_root.rglob("*.py")):
        relative = path.relative_to(repo_root)
        if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        if relative in ALLOWED_CONSTRUCTOR_PATHS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            violations.append(f"{relative}: parse failed: {type(exc).__name__}: {exc}")
            continue
        scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and is_generated_file_constructor(node):
                violations.append(f"{relative}:{getattr(node, 'lineno', '?')}: direct GeneratedFile construction")

    failures = 0
    failures += check(
        "runtime Python sources are parseable for generated-file boundary audit",
        not any("parse failed" in item for item in violations),
        "; ".join(item for item in violations if "parse failed" in item),
    )
    direct = [item for item in violations if "direct GeneratedFile construction" in item]
    failures += check(
        "GeneratedFile construction is centralized in services/file_manager.py",
        not direct,
        "; ".join(direct),
    )
    failures += check(
        "generated-file boundary audit scanned runtime Python sources",
        scanned > 0,
        f"scanned={scanned}",
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
