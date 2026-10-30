"""Draft identity: a digest that ties reviews and novelty approvals to the exact draft they judged."""
import hashlib
import json


def draft_digest(draft):
    return hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
