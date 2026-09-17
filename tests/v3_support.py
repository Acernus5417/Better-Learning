"""Synthetic worker receipts for mechanical tests; not actual vision evidence."""
from _common import write_json


def sidecar(course, attempt, uid, output, text, status=None):
    status = status or ('vision_unavailable' if '[不具备读图能力]' in text else 'unreadable' if '[待核实]' in text else 'complete')
    tiles = [t['id'] for t in attempt.get('visual_assets', {}).get(uid, {}).get('tiles', [])]
    write_json(output.with_suffix('.result.json'), {'schema_version': 1, 'status': status, 'confidence': 'normal', 'reason': '', 'tiles_read': tiles})
