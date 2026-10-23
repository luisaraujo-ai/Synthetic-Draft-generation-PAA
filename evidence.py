"""Deterministic readiness rules shared by evaluation and packaging."""
import hashlib
import json


def draft_digest(draft):
    return hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def physically_ready(record):
    record = record or {}
    run = record.get('informed') or {}
    kit = record.get('kit') or {}
    return bool(record.get('outcome') == 'feasible' and kit.get('ok')
                and kit.get('audit_ok') and run.get('reward') == 1
                and not run.get('timed_out') and not record.get('blocking_issues'))
