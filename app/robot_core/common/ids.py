import secrets
import time


def new_id(prefix: str) -> str:
    """Return a lexicographically sortable, locally generated identifier."""
    milliseconds = int(time.time() * 1000)
    return f"{prefix}_{milliseconds:013d}_{secrets.token_hex(8)}"

