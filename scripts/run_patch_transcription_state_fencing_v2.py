from __future__ import annotations

from pathlib import Path
import runpy


root = Path(__file__).resolve().parents[1]
path = root / "services" / "transcription.py"
text = path.read_text(encoding="utf-8")
shared = '''    tr.status = "running"\n    tr.started_at = datetime.now(timezone.utc)\n    tr.error_message = None\n    db.session.commit()\n\n    media_snapshot = None\n    try:\n'''
positions = []
start = 0
while True:
    pos = text.find(shared, start)
    if pos < 0:
        break
    positions.append(pos)
    start = pos + 1
if len(positions) != 2:
    raise RuntimeError(f"expected two running-transition blocks, found {len(positions)}")
second = positions[1]
local_variant = shared.replace(
    "    media_snapshot = None\n",
    "    # Local provider transition marker: the lease/result-write fence is above.\n    media_snapshot = None\n",
    1,
)
text = text[:second] + local_variant + text[second + len(shared):]
path.write_text(text, encoding="utf-8")

runpy.run_path(str(root / "scripts" / "patch_transcription_state_fencing.py"), run_name="__main__")
