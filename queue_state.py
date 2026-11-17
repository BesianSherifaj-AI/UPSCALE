"""Small, atomic queue snapshots. Media and engine reports stay in their files."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_queue(path: Path, entries: list[dict]) -> None:
    rows = []
    for entry in entries:
        result = entry.get('result')
        if result:
            result = {key: result[key] for key in (
                'output_path', 'report_path', 'width', 'height', 'fps', 'warnings'
            ) if key in result}
        rows.append(dict(path=str(entry['path']),
                         state=entry.get('state_before_probe', entry['state']),
                         pending=bool(entry['pending']), error=entry.get('error', ''),
                         result=result))
    atomic_write_json(path, {'version': 1, 'entries': rows})


def load_queue(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), list):
        raise ValueError('Unsupported queue file. The existing file has been kept.')
    rows, seen = [], set()
    for row in data['entries']:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str) or not row['path']:
            raise ValueError('Invalid queue entry. The existing file has been kept.')
        source = Path(row['path']).expanduser().resolve()
        key = os.path.normcase(str(source))
        if key in seen:
            continue
        seen.add(key)
        state, pending = row.get('state', 'Ready'), row.get('pending', True)
        error = row.get('error', '')
        result = row.get('result')
        if not isinstance(state, str) or not isinstance(pending, bool) or not isinstance(error, str):
            raise ValueError('Invalid queue state. The existing file has been kept.')
        if result is not None:
            if (not isinstance(result, dict)
                    or any(not isinstance(result.get(k), str) or not result[k]
                           for k in ('output_path', 'report_path'))
                    or any(not isinstance(result.get(k), (int, float))
                           or not math.isfinite(result[k]) or result[k] <= 0
                           for k in ('width', 'height', 'fps'))
                    or not isinstance(result.get('warnings', []), list)
                    or any(not isinstance(w, str) for w in result.get('warnings', []))):
                raise ValueError('Invalid saved result. The existing queue file has been kept.')
            if not Path(result['output_path']).is_file():
                result = None
                if state == 'Complete ✓':
                    state, pending = 'Failed', False
                    error = 'The previous output was moved or removed. Retry failed to create another output.'
        if state in ('Processing…', 'Queued'):
            state, pending, error = 'Interrupted', True, ''
        elif state == 'Reading video…':
            state, pending = 'Ready', True
        rows.append(dict(path=source, state=state, pending=pending, error=error, result=result))
    return rows
