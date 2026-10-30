"""Cell sampling: a limitation from the mined catalogue x a subdomain, weighted by confidence and by how few accepted
tasks the cell already holds (ledger). Inside the cell the circuit family rotates, the descriptors (phenomenon, design
freedom, reasoning) go to under-represented values, and the secondary axes are stratified per run.
"""
import random
from collections import Counter

CONF_WEIGHT = {'high': 3.0, 'medium': 2.0, 'low': 1.0}


def abstract(lim):
    """The limitation as the generator sees it: the mechanism, never the tasks it was mined from."""
    return {k: lim[k] for k in ('id', 'name', 'mechanism', 'how_to_elicit', 'how_to_grade', 'confidence')}


def stratified(axes, keys, n, rng):
    pools, out = {k: [] for k in keys}, []
    for _ in range(n):
        p = {}
        for k in keys:  # refill each axis pool when empty: every value is used before any repeats
            if not pools[k]:
                pools[k] = rng.sample(axes[k], len(axes[k]))
            p[k] = pools[k].pop()
        out.append(p)
    return out


class Sampler:
    """Cell sampling against the ledger of accepted tasks (plus this run's picks)."""

    def __init__(self, domain, n, seed=0):
        self.domain, self.rng = domain, random.Random(seed)
        self.lims = domain.limitations()['limitations']
        if not self.lims:
            raise SystemExit(f'{domain.limitations_file} has no limitations: re-run mine.py')
        self.vocab = domain.vocab()
        self.profiles = stratified(domain.axes(), domain['secondary_axes'], n, self.rng)
        self.fam_pools = {}
        self.cell, self.single, self.pair = Counter(), Counter(), Counter()
        for e in domain.ledger():
            if e['status'] == 'accepted':
                self.count(e['limitation'], e['subdomain'], e['descriptors'])

    def count(self, lim_id, subdomain, descriptors):
        self.cell[(lim_id, subdomain)] += 1
        for dim, val in descriptors.items():
            self.single[(subdomain, dim, val)] += 1
            self.pair[(lim_id, subdomain, dim, val)] += 1

    def pick(self, i, subdomain=None):
        sub = subdomain or self.rng.choice(self.domain.subdomains())
        w = [CONF_WEIGHT[l['confidence']] / (1 + self.cell[(l['id'], sub)]) for l in self.lims]
        lim = self.rng.choices(self.lims, weights=w)[0]
        pool = self.fam_pools.setdefault(sub, [])
        if not pool:
            fams = self.domain.families(sub)
            pool.extend(self.rng.sample(fams, len(fams)))
        desc = {}
        for dim, vals in self.vocab.items():
            w = [1 / (1 + self.single[(sub, dim, v)] + 2 * self.pair[(lim['id'], sub, dim, v)]) for v in vals]
            desc[dim] = self.rng.choices(vals, weights=w)[0]
        return sub, lim, pool.pop(), desc, self.profiles[i]
