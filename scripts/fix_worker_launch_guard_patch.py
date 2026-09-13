from pathlib import Path

path = Path("services/worker_launch_guard.py")
text = path.read_text(encoding="utf-8")
marker = "        updated = query.update("
start = text.index(marker)
end = text.index("        db.session.commit()", start)
chunk = text[start:end]
extra = "        )\n"
if not chunk.endswith(extra):
    raise RuntimeError("expected generated extra closing parenthesis was not found")
chunk = chunk[:-len(extra)]
path.write_text(text[:start] + chunk + text[end:], encoding="utf-8")
print("worker launch guard generated shape repaired")
