"""Cost ledger: one record per model call, with tokens and dollars, and per-purpose totals."""
import uuid

PRICES = {'claude-opus-5-5': (4.0, 20.0, 0.20), 'claude-sonnet-5-5': (2.0, 10.0, 0.20),
          'claude-haiku-5-5': (0.10, 0.50, 0.01)}  # $/M input, output, cache read; a 1 h cache write is 2x input
LEDGER = []


def log_call(model, purpose, inp, out, cache_read=0, cache_write=0, usd=None):
    if usd is None and model in PRICES:
        pin, pout, pread = PRICES[model]
        usd = (inp * pin + out * pout + cache_read * pread + cache_write * 2 * pin) / 1e6
    LEDGER.append({'call_id': uuid.uuid4().hex, 'model': model, 'purpose': purpose, 'input': inp, 'output': out, 'cache_read': cache_read,
                   'cache_write': cache_write, 'usd': usd})


def cost_summary(records=None):
    records = LEDGER if records is None else records
    by = {}
    for r in records:
        k = f"{r['purpose'] or '-'} ({r['model']})"
        b = by.setdefault(k, {'calls': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'usd': 0.0})
        b['calls'] += 1
        for f in ('input', 'output', 'cache_read'):
            b[f] += r[f]
        b['usd'] += r['usd'] or 0.0
    return {'total_usd': round(sum(b['usd'] for b in by.values()), 4),
            'by_purpose': {k: {**v, 'usd': round(v['usd'], 4)} for k, v in sorted(by.items())},
            'unpriced_calls': sum(r['usd'] is None for r in records)}
