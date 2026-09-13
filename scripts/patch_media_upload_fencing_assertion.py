from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parents[1] / "tests" / "smoke_media_upload_integrity.py"
text = path.read_text(encoding="utf-8")
old = '''            failures += check(\n                "local fallback inherits durable lease checks and stale errors are fenced",\n                transcription_source.count("lease_check=lease_check") >= 2\n                and transcription_source.count("If this exception reflects durable lease loss") == 2,\n            )\n'''
new = '''            failures += check(\n                "local fallback inherits durable callbacks and stale error commits are result-write fenced",\n                transcription_source.count("lease_check=lease_check") >= 2\n                and transcription_source.count("result_write_guard=result_write_guard") >= 2\n                and transcription_source.count("# Error status is canonical too. Reserve the write") == 2\n                and transcription_source.count(\n                    "if result_write_guard is not None:\\n            result_write_guard()\\n        elif lease_check is not None:\\n            lease_check()\\n        tr.status = \\\"error\\\""\n                ) == 2,\n            )\n'''
count = text.count(old)
if count != 1:
    raise RuntimeError(f"expected one legacy source assertion, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
