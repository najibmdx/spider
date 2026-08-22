# Source

A published slice of Spider's backend: the intelligence layer.

## `spider_intelligence.py`

The four-module reasoning core that decides *what a token is*. It sits between
signal collection and the language model, and it is the part of Spider worth
reading — everything around it is I/O plumbing.

It has no network calls, no credentials, and no dependencies outside the standard
library. Every function is a pure transformation over a dict of already-collected
signals. That is a deliberate design property, not an accident of extraction: it
means the scoring is auditable, and it can be regression-tested against tokens
that have already dumped without touching an RPC.

| Module | Function | Question it answers |
|---|---|---|
| 1 | `classify_lifecycle` | What phase is this token in — and therefore which signals are meaningful? |
| 2 | `calculate_true_dump_risk` | How much supply can dump on me right now? |
| 3 | `calculate_player_intel` | Who is actually here, weighted by ability to move price? |
| 4 | `classify_token_state` | Synthesis — one of ten trader states, plus its invalidation condition. |

### Worked example

Real output from a live scan, reproduced by calling the module directly:

```python
data = {
    "age_minutes": 351.4,
    "pre_migration": False,
    "top_holders": [
        {"wallet": "FSPe4gBJkaGC", "pct_of_supply": 38.92},
        {"wallet": "2nDFbjRWkKbP", "pct_of_supply": 24.12},
        {"wallet": "5sYJiDYzLXUL", "pct_of_supply": 4.93},
    ],
    "bundler_clusters": [], "operators": [], "sniper_wallets": [],
}

lifecycle = classify_lifecycle(data)
risk      = calculate_true_dump_risk(data, lifecycle["mode"], known_hits=[])

# lifecycle["mode"]           → "SURVIVAL"
# risk["true_dump_risk_pct"]  → 63.04
# risk["dump_risk_severity"]  → "CRITICAL"
```

Two wallets clear the 5% threshold and sum to 63.04% of supply. The third, at
4.93%, does not count — and that gap is the whole design problem. Actors who
want to hold a controlling position without registering will split across
wallets and sit just under the line. Module 2's second component exists to
collapse those back into a single controller using bundler and operator
connection signals.

### What is not here

The wallet database, the collection layer, the RPC clients, the prompt
construction, and the HTTP server live in a private repository. This file is the
reasoning, not the system.
