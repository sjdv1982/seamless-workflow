"""Pure attachment policy. Checksums here are comparison-only strings."""
ABSENT = 'ABSENT'
INVALID = 'INVALID'


def decide_initial(spec, disk, node, *, no_value=False, node_is_null=False):
    """Choose an initial action; ``node is None`` means incomplete, not null."""
    if node is None:
        if 'r' not in spec.mode: return 'nothing'
        if disk == INVALID: return 'error'
        if disk == ABSENT and spec.authority == 'file-strict': return 'sense-null-error'
        return 'sense-null' if no_value or disk == ABSENT else 'sense'
    if disk == ABSENT or no_value:
        if disk == ABSENT and spec.authority == 'file-strict': return 'error'
        if node_is_null: return 'nothing'
        if spec.mode == 'w': return 'write'
        return 'write' if 'w' in spec.mode else 'nothing'
    if spec.mode == 'w':
        return 'write' if node is not None and node != disk else 'nothing'
    if spec.authority == 'cell' and node is not None:
        return 'write' if 'w' in spec.mode and node != disk else 'nothing'
    if disk == INVALID: return 'error'
    return 'sense' if disk != node else 'nothing'


def classify_observation(ws, processed_ws, checksum, disk, in_flight=None, *, fingerprint=None, disk_fingerprint=None):
    if ws <= processed_ws: return 'stale'
    if checksum == disk and (checksum not in {ABSENT, INVALID} or fingerprint == disk_fingerprint): return 'unchanged'
    if checksum == INVALID: return 'rejected'
    if checksum == ABSENT: return 'absent'
    if checksum == in_flight: return 'echo'
    return 'foreign'


def reassert(mode, state, classification):
    return mode == 'w' and state == 'complete' and classification in {'foreign', 'rejected', 'absent'}


def detector(timestamps, now, *, window=20., threshold=3):
    recent = tuple(t for t in timestamps if now - t <= window) + (now,)
    return recent, len(recent) >= threshold
