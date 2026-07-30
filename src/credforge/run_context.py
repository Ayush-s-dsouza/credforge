"""Per-invocation run identity.

A run_id is generated once per CLI invocation (run/batch/resolve/report)
and threaded through every log line and registry entry, so a single
`grep run_id logs.jsonl` reconstructs exactly what one invocation did.
"""

import uuid
from datetime import datetime, timezone


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{timestamp}_{uuid.uuid4().hex[:8]}"
