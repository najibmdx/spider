"""
Spider — Intelligence Layer
The four-module reasoning core that turns raw on-chain signals into a token state.

Extracted from Spider's production backend for reference. This is the layer that
decides *what a token is*, and it is deliberately free of I/O: no RPC calls, no
API keys, no network. Every function is a pure transformation over a dict of
already-collected signals, which is what makes the scoring auditable and
regression-testable against tokens that have already dumped.

Pipeline:
    collect()                 → raw on-chain + market signals   (not in this file)
    classify_lifecycle()      → Module 1: what phase is this token in
    calculate_true_dump_risk()→ Module 2: how much supply can dump right now
    calculate_player_intel()  → Module 3: who is actually here, weighted by clout
    classify_token_state()    → Module 4: synthesis into one trader state

Design constraint: the language model downstream receives the output of Module 4
as a short briefing and writes prose only. It never sees this arithmetic and
never produces a number.

Part of Spider — https://github.com/najibmdx/spider
"""

# ── Lifecycle Classifier — Module 1 of Spider Brain ──────────────────────────
#
# Determines BONDING_CURVE / LAUNCH / SURVIVAL mode.
# Every downstream signal is interpreted differently per mode.
#
# BONDING_CURVE: pre-migration — MC velocity is the only real signal
# LAUNCH:        under 60 min — dump risk is primary, launch signals matter
# SURVIVAL:      60 min+ — launch history is noise, floor + who's still in matters
#
# pre_migration always overrides age. Boundary 60 min is inclusive → SURVIVAL.

LIFECYCLE_CONFIG = {
    "launch_threshold_mins": 60,
}

# Signals active per mode — downstream modules respect this filter
LIFECYCLE_SIGNALS = {
    "BONDING_CURVE": {
        "active":  ["dump_risk", "mc_velocity", "holder_count", "kol_presence"],
        "ignored": ["vol_liq", "floor", "entry_mc", "farm_history", "snipers"],
    },
    "LAUNCH": {
        "active":  ["dump_risk", "bundles", "snipers", "supply_structure",
                    "kol_presence", "vol_liq", "buy_pressure", "holder_count", "entry_mc"],
        "ignored": ["floor", "social", "long_term_vol_trend"],
    },
    "SURVIVAL": {
        "active":  ["dump_risk", "kol_presence", "known_wallets", "vol_liq",
                    "holder_count", "floor", "entry_mc"],
        "ignored": ["bundles", "snipers", "farm_history", "supply_structure_at_launch"],
    },
}

def classify_lifecycle(data: dict) -> dict:
    """
    Classify token lifecycle mode from age and migration status.
    Pure classification — no RPC calls, no external dependencies.
    Called at the top of ai_analyze() before any signal processing.
    """
    age           = data.get("age_minutes") or 0
    pre_migration = data.get("pre_migration", False)
    threshold     = LIFECYCLE_CONFIG["launch_threshold_mins"]

    if pre_migration:
        mode   = "BONDING_CURVE"
        reason = "Token still on bonding curve — no LP pool exists"
    elif age < threshold:
        mode   = "LAUNCH"
        reason = f"Token is {age:.0f} minutes old — launch phase signals active"
    else:
        mode   = "SURVIVAL"
        reason = f"Token is {age:.0f} minutes old — survival phase, launch history ignored"

    return {
        "mode":           mode,
        "age_minutes":    age,
        "active_signals": LIFECYCLE_SIGNALS[mode]["active"],
        "ignored_signals": LIFECYCLE_SIGNALS[mode]["ignored"],
        "mode_reason":    reason,
    }


# ── True Dump Risk — Module 2 of Spider Brain ────────────────────────────────
#
# Core reframe: assume ALL supply is hostile. Every wallet above 5% = dump risk.
# No trust categories. No exemptions. KOL, smart money, unknown — all the same rule.
# Then unmask hidden concentration: connected wallets disguising one actor.
#
# The 5% threshold is Jeeps' hard rule:
#   Above 5% per wallet = one decision moves price significantly.
#   Multi-walling (staying just under 5%) is the real poison — this finds it.
#
# Lifecycle filter:
#   SURVIVAL mode — exited bundlers/snipers = ZERO contribution (ancient history).
#   LAUNCH / BONDING_CURVE — all current holders counted.

DUMP_RISK_CONFIG = {
    "per_wallet_threshold_pct": 5.0,
    "critical_threshold_pct":  30.0,
    "high_threshold_pct":      20.0,
    "medium_threshold_pct":    10.0,
    "low_threshold_pct":        5.0,
}

def calculate_true_dump_risk(data: dict, lifecycle_mode: str, known_hits: list) -> dict:
    """
    Calculate true dump risk from raw supply concentration.
    No trust categories. No exemptions. Every wallet above 5% is a risk.
    Hidden clusters (bundlers/operators staying just under threshold) are unmasked.
    Returns dict consumed by ai_analyze() for intel report and return payload.
    """
    top_holders = data.get("top_holders", [])
    bundlers    = data.get("bundler_clusters", [])
    snipers     = data.get("sniper_wallets", [])
    operators   = data.get("operators", [])
    threshold   = DUMP_RISK_CONFIG["per_wallet_threshold_pct"]
    survival_mode = (lifecycle_mode == "SURVIVAL")

    # ── Component A: Per-wallet threshold scan ────────────────────────────────
    # Every wallet >= 5% flagged. Known label is context only — not an exemption.
    known_short_map = {k.get("wallet_short", ""): k.get("label", "Unknown") for k in known_hits}

    dangerous_wallets = []
    for h in top_holders:
        pct = h.get("pct_of_supply", 0)
        if pct >= threshold:
            w_short  = h.get("wallet", "")
            known_as = known_short_map.get(w_short, None)
            dangerous_wallets.append({
                "wallet_short":    w_short,
                "pct":             round(pct, 2),
                "known_as":        known_as,
                "above_threshold": True,
            })

    dangerous_supply_pct = round(sum(w["pct"] for w in dangerous_wallets), 2)

    # ── Component B: Hidden concentration unmasker ────────────────────────────
    # Collapse connected wallets into true controller.
    # These actors stay UNDER 5% individually — that's the point of multi-walling.
    # Bundler clusters and operator networks are the two connection signals.
    hidden_clusters = []

    # Bundler clusters — same-slot buyers = same controller
    for b in bundlers:
        if survival_mode and b.get("still_holding", 0) == 0:
            continue  # exited bundlers in survival mode = irrelevant
        combined_pct = round(b.get("total_hold_pct", 0), 2)
        wallet_count = b.get("wallet_count", 1)
        if combined_pct >= threshold:
            hidden_clusters.append({
                "cluster_type":   "bundler",
                "wallet_count":   wallet_count,
                "combined_pct":   combined_pct,
                "individual_max": round(combined_pct / max(wallet_count, 1), 2),
                "still_holding":  b.get("still_holding", 0) > 0,
            })

    # Operator networks — same funder = same controller
    for o in operators:
        if not o.get("is_multi"):
            continue
        if survival_mode and not o.get("still_holding"):
            continue
        combined_pct = round(o.get("hold_pct", 0), 2)
        wallet_count = o.get("wallet_count", 1)
        if combined_pct >= threshold:
            hidden_clusters.append({
                "cluster_type":   "operator",
                "wallet_count":   wallet_count,
                "combined_pct":   combined_pct,
                "individual_max": round(combined_pct / max(wallet_count, 1), 2),
                "still_holding":  bool(o.get("still_holding")),
            })

    # Sniper cluster — same 0-30s window, combined >= threshold
    sniper_active = [s for s in snipers if s.get("still_holding")]
    sniper_pool   = sniper_active if survival_mode else snipers
    if len(sniper_pool) > 1:
        sniper_combined = round(sum(s.get("hold_pct", 0) for s in sniper_pool), 2)
        if sniper_combined >= threshold:
            hidden_clusters.append({
                "cluster_type":   "sniper",
                "wallet_count":   len(sniper_pool),
                "combined_pct":   sniper_combined,
                "individual_max": round(sniper_combined / max(len(sniper_pool), 1), 2),
                "still_holding":  len(sniper_active) > 0,
            })

    hidden_supply_pct = round(sum(c["combined_pct"] for c in hidden_clusters), 2)

    # ── Component C: True dump risk number ───────────────────────────────────
    # Clusters stay UNDER 5% individually — minimal overlap with dangerous_wallets.
    # Safe to sum both axes for the true picture.
    true_dump_risk_pct = min(round(dangerous_supply_pct + hidden_supply_pct, 2), 100.0)

    cfg = DUMP_RISK_CONFIG
    if true_dump_risk_pct >= cfg["critical_threshold_pct"]:
        dump_risk_severity = "CRITICAL"; dump_risk_color = "#E24B4A"
    elif true_dump_risk_pct >= cfg["high_threshold_pct"]:
        dump_risk_severity = "HIGH";     dump_risk_color = "#EF9F27"
    elif true_dump_risk_pct >= cfg["medium_threshold_pct"]:
        dump_risk_severity = "MEDIUM";   dump_risk_color = "#BA7517"
    elif true_dump_risk_pct >= cfg["low_threshold_pct"]:
        dump_risk_severity = "LOW";      dump_risk_color = "#378ADD"
    else:
        dump_risk_severity = "SAFE";     dump_risk_color = "#1D9E75"

    print(f"  [TrueDumpRisk] visible={dangerous_supply_pct}% hidden={hidden_supply_pct}% "
          f"total={true_dump_risk_pct}% → {dump_risk_severity}")

    return {
        "true_dump_risk_pct":     true_dump_risk_pct,
        "dump_risk_severity":     dump_risk_severity,
        "dump_risk_color":        dump_risk_color,
        "dangerous_wallets":      dangerous_wallets,
        "dangerous_wallet_count": len(dangerous_wallets),
        "dangerous_supply_pct":   dangerous_supply_pct,
        "hidden_clusters":        hidden_clusters,
        "hidden_cluster_count":   len(hidden_clusters),
        "hidden_supply_pct":      hidden_supply_pct,
        "dump_risk_breakdown": {
            "visible_pct": dangerous_supply_pct,
            "hidden_pct":  hidden_supply_pct,
        },
    }


# ── Player Intel — Module 3 of Spider Brain ──────────────────────────────────
#
# Weights players by actual market-moving power in the current memecoin meta.
# Not just "who is present" — who can move price and what is their incentive.
#
# Real hierarchy from Jeeps' trading knowledge:
#   TIER 1: KOLscan wallets + Livestreamers — create waves, move markets
#   TIER 2: Smart money + Known whales — conviction signals, size-dependent
#   TIER 3: Unknown, bots — context only, no market-moving weight
#
# Smart money is meaningful ONLY in multiples (>= 2) or large size (>= 3%).
# KOLs outweigh smart money. Livestreamers outweigh everything.

PLAYER_INTEL_CONFIG = {
    "sm_meaningful_count":  2,     # >= 2 smart money wallets = meaningful
    "sm_meaningful_pct":    3.0,   # >= 3% single smart money = meaningful
    "whale_meaningful_pct": 3.0,   # >= 3% known whale = significant
    "tier1_strong_count":   2,     # >= 2 tier1 players = STRONG signal
    "player_weights": {
        "livestreamer":    10,     # highest — real-time audience, instant price impact
        "kolscan":          9,     # above kol — verified on-chain social influence
        "kol":              8,     # audience capture confirmed
        "smart_money":      5,     # when meaningful (count or size)
        "smart_money_weak": 2,     # single small position
        "whale":            4,     # when significant (>= 3%)
        "whale_dust":       1,     # present but small
    }
}

def calculate_player_intel(known_hits: list, kw_sm_count: int, kw_kol_count: int, lifecycle_mode: str) -> dict:
    """
    Classify players by market-moving tier and compute collective signal.
    No RPC calls — pure classification from known_hits already built in ai_analyze().
    Called after Module 2 (true dump risk), before Step 3 coordinated threat.
    """
    cfg     = PLAYER_INTEL_CONFIG
    weights = cfg["player_weights"]

    # ── Component A: Per-player tier assignment ───────────────────────────────
    tiered_players = []
    for k in known_hits:
        cat  = k.get("category", "unknown")
        pct  = k.get("pct", 0)
        notes = k.get("notes", "")

        if cat == "livestreamer":
            tier   = 1
            weight = weights["livestreamer"]
            label  = "LIVESTREAMER"

        elif cat == "kolscan":
            tier   = 1
            weight = weights["kolscan"]
            label  = "KOLSCAN"

        elif cat == "kol":
            tier   = 1
            weight = weights["kol"]
            label  = "KOL"

        elif cat == "smart_money":
            # Meaningful if count >= 2 OR single position >= 3%
            is_meaningful = (kw_sm_count >= cfg["sm_meaningful_count"]) or (pct >= cfg["sm_meaningful_pct"])
            tier   = 2
            weight = weights["smart_money"] if is_meaningful else weights["smart_money_weak"]
            label  = "SMART MONEY — MEANINGFUL" if is_meaningful else "SMART MONEY — WEAK SIGNAL"

        elif cat == "whale":
            is_significant = pct >= cfg["whale_meaningful_pct"]
            tier   = 2
            weight = weights["whale"] if is_significant else weights["whale_dust"]
            label  = "KNOWN WHALE — SIGNIFICANT" if is_significant else "KNOWN WHALE — DUST"

        elif cat in ("insider", "bundler"):
            tier   = 2
            weight = 3
            label  = cat.upper()

        else:
            tier   = 3
            weight = 0
            label  = "CONTEXT ONLY"

        tiered_players.append({
            "wallet_short": k.get("wallet_short", ""),
            "category":     cat,
            "tier":         tier,
            "weight":       weight,
            "label":        label,
            "pct":          pct,
            "notes":        notes,
        })

    tier1_players = [p for p in tiered_players if p["tier"] == 1]
    tier2_players = [p for p in tiered_players if p["tier"] == 2]
    tier1_count   = len(tier1_players)
    tier2_count   = len(tier2_players)
    tier1_weight  = sum(p["weight"] for p in tier1_players)
    tier2_weight  = sum(p["weight"] for p in tier2_players)

    # ── Component B: Collective player signal ─────────────────────────────────
    # Tier 1 signal — market movers present?
    if tier1_count >= cfg["tier1_strong_count"]:
        tier1_signal = "STRONG"
    elif tier1_count == 1:
        tier1_signal = "PRESENT"
    else:
        tier1_signal = "ABSENT"

    # Smart money signal — size and count dependent
    sm_players = [p for p in tiered_players if p["category"] == "smart_money"]
    any_sm_meaningful = any(p["weight"] >= weights["smart_money"] for p in sm_players)
    if kw_sm_count >= cfg["sm_meaningful_count"]:
        sm_signal = "STRONG"
    elif kw_sm_count == 1 and any_sm_meaningful:
        sm_signal = "PRESENT"
    elif kw_sm_count == 1:
        sm_signal = "WEAK"
    else:
        sm_signal = "ABSENT"

    # Combined player signal — what Module 4 (state engine) uses
    if tier1_signal in ("STRONG", "PRESENT") and sm_signal in ("STRONG", "PRESENT"):
        player_signal = "STRONG"
    elif tier1_signal in ("STRONG", "PRESENT"):
        player_signal = "KOL_ONLY"
    elif sm_signal in ("STRONG", "PRESENT"):
        player_signal = "SM_ONLY"
    else:
        player_signal = "NONE"

    # Human-readable summary for Mistral intel report
    parts = []
    ls_count  = sum(1 for p in tier1_players if p["category"] == "livestreamer")
    kol_count = sum(1 for p in tier1_players if p["category"] in ("kol", "kolscan"))
    sm_count  = kw_sm_count
    wh_count  = sum(1 for p in tier2_players if p["category"] == "whale")

    if ls_count:    parts.append(f"{ls_count} livestreamer(s) — highest market-moving weight")
    if kol_count:   parts.append(f"{kol_count} KOL wallet(s) — audience capture confirmed")
    if sm_signal == "STRONG":   parts.append(f"{sm_count} smart money wallets — meaningful conviction signal")
    elif sm_signal == "PRESENT": parts.append(f"{sm_count} smart money wallet — significant position")
    elif sm_signal == "WEAK":    parts.append(f"{sm_count} smart money wallet — small position, weak signal")
    if wh_count:    parts.append(f"{wh_count} known whale(s) present")

    player_summary = "; ".join(parts) if parts else "No recognized market-moving wallets detected"

    print(f"  [PlayerIntel] tier1={tier1_count}({tier1_weight}pts) tier2={tier2_count}({tier2_weight}pts) signal={player_signal}")

    return {
        "tiered_players":  tiered_players,
        "tier1_count":     tier1_count,
        "tier2_count":     tier2_count,
        "tier1_weight":    tier1_weight,
        "tier2_weight":    tier2_weight,
        "tier1_signal":    tier1_signal,
        "sm_signal":       sm_signal,
        "player_signal":   player_signal,
        "player_summary":  player_summary,
    }


# ── Token State Engine — Module 4 of Spider Brain ────────────────────────────
#
# Synthesizes Modules 1+2+3 into a single trader-ready state.
# Python determines state and invalidation. Mistral only translates to trader language.
#
# Priority order matters — first match wins.
# State reflects what game is being played RIGHT NOW, not what happened at launch.

TOKEN_STATE_CONFIG = {
    "active_threat_bun_min_pct": 2.0,  # bundler hold% threshold to fire ACTIVE_THREAT (filters dust)
    "active_threat_op_min_pct":  5.0,  # operator hold% threshold
    "trap_set_vol_liq_min":      5.0,   # lowered from 8x — fresh launches with pool + 5x+ vol/liq = visible pump
    "farm_dead_vol_liq_max":    8.0,
    "smart_money_demand_min":   40,
    "clean_accum_demand_min":   30,
    "clean_dead_demand_max":    30,
    "distribution_demand_min":  50,
}

def classify_token_state(
    lifecycle_mode, dump_risk_severity, true_dump_risk_pct,
    bundler_hold_pct, op_hold_pct, is_farming_dump,
    vol_liq_ratio, demand_score, player_signal,
    bundle_status, dangerous_wallets, max_holder_wallet,
    dump_risk_breakdown, hidden_clusters, player_summary,
    mc, age, vol_liq_ratio_raw, vol_liq_class, bp_class,
    buys_1h, sells_1h, sniper_hold_pct, liquidity_usd=0.0
) -> dict:
    """
    Classify token into one of 11 trader states.
    Returns state name, invalidation string, and state_context for Mistral.
    No RPC calls. Pure synthesis from Modules 1-3 + existing ai_analyze variables.
    """
    cfg = TOKEN_STATE_CONFIG
    vl  = vol_liq_ratio if vol_liq_ratio is not None else 0.0

    # ── Priority classification — first match wins ────────────────────────────
    # NOTE: BONDING_CURVE does NOT suppress ACTIVE_THREAT or TRAP_SET.
    # A farm dump + hot demand on the bonding curve is still TRAP_SET.
    # A coordinated hold on the bonding curve is still ACTIVE_THREAT.
    # BONDING_CURVE only fires when no more specific dangerous state is present.

    # 1. ACTIVE_THREAT — bundlers/operators actively holding (not a farming exit pattern)
    # Severity gate removed: bundler_hold_pct >= 2% is itself the threat signal.
    # dump_risk_severity can be LOW when bundlers hold <5% individually — the gate was wrong.
    # is_farming_dump exclusion: farming tokens route to TRAP_SET/FARM_DEAD instead.
    if (
        not is_farming_dump and
        (bundler_hold_pct >= cfg["active_threat_bun_min_pct"] or op_hold_pct > cfg["active_threat_op_min_pct"])
    ):
        state = "ACTIVE_THREAT"

    # 2. DISTRIBUTION — real demand masking real risk (fires even pre-migration)
    elif dump_risk_severity in ("MEDIUM", "HIGH", "CRITICAL") and demand_score >= cfg["distribution_demand_min"]:
        state = "DISTRIBUTION"

    # 3. LOADED_GUN — single whale above 5%, no bundlers, demand weak or absent
    elif dump_risk_severity in ("CRITICAL", "HIGH") and len(dangerous_wallets) >= 1 and bundler_hold_pct == 0:
        state = "LOADED_GUN"

    # 4. TRAP_SET — farm cleared, pump visible, bait is set (fires even pre-migration)
    elif is_farming_dump and vl > cfg["trap_set_vol_liq_min"] and dump_risk_severity not in ("CRITICAL",):
        state = "TRAP_SET"

    # 5. FARM_DEAD — farm ran, nobody bought
    elif is_farming_dump and vl <= cfg["farm_dead_vol_liq_max"]:
        state = "FARM_DEAD"

    # 6. BONDING_CURVE — pre-migration, no dangerous state detected
    # Only fires when the token is genuinely clean on the curve
    elif lifecycle_mode == "BONDING_CURVE":
        state = "BONDING_CURVE"

    # 7. SMART_MONEY_ENTRY — conviction capital entered post-farm or clean launch
    elif (
        player_signal in ("STRONG", "SM_ONLY") and
        dump_risk_severity in ("SAFE", "LOW") and
        demand_score >= cfg["smart_money_demand_min"]
    ):
        state = "SMART_MONEY_ENTRY"

    # 8. CLEAN_ACCUMULATION — clean setup, KOLs in, demand real, no landmines
    # Demand gate is waived for no-pool tokens: demand is unmeasurable without a pool.
    elif (
        dump_risk_severity in ("SAFE", "LOW") and
        player_signal in ("STRONG", "KOL_ONLY") and
        (demand_score >= cfg["clean_accum_demand_min"] or liquidity_usd <= 0)
    ):
        state = "CLEAN_ACCUMULATION"

    # 9. CLEAN_DEAD — safe token, nobody buying
    elif dump_risk_severity in ("SAFE", "LOW") and demand_score < cfg["clean_dead_demand_max"]:
        state = "CLEAN_DEAD"

    # 10. CONTESTED — real supply overhang (MEDIUM), demand below DISTRIBUTION gate
    # MEDIUM = enough hostile supply to matter but not enough for ACTIVE_THREAT/LOADED_GUN.
    # Trader needs to know: two forces fighting — not a broken scan.
    elif dump_risk_severity == "MEDIUM":
        state = "CONTESTED"

    # 11. UNKNOWN — genuinely unresolvable (SAFE/LOW risk, no player, mid demand)
    else:
        state = "UNKNOWN"

    # ── Invalidation conditions — state-specific ─────────────────────────────
    whale_label = dangerous_wallets[0]["wallet_short"] if dangerous_wallets else "unknown"

    invalidations = {
        "BONDING_CURVE":      "MC velocity stalls for 2+ minutes, or curve fails to migrate",
        "ACTIVE_THREAT":      f"Bundlers/operators exit fully ({bundler_hold_pct:.1f}% bundlers + {op_hold_pct:.1f}% operators drops to 0) — re-scan immediately",
        "LOADED_GUN":         f"Wallet {whale_label}... sells any position — watch top holder list",
        "DISTRIBUTION":       f"Vol/liq collapses below 5x (currently {vl:.1f}x) OR risk score drops as distribution completes",
        "TRAP_SET":           f"Controllers decide when this ends — not you. Take profits into pumps. Exit signal: vol/liq drops below 5x (currently {vl:.1f}x) OR any known wallet starts selling — whichever comes first",
        "FARM_DEAD":          f"Vol/liq spikes above 8x (currently {vl:.1f}x) — re-evaluate immediately if it does",
        "SMART_MONEY_ENTRY":  f"Smart money wallet exits OR vol/liq drops below 8x (currently {vl:.1f}x) OR buy pressure flips below 0.8x",
        "CLEAN_ACCUMULATION": f"Buy pressure drops below 0.8x OR vol/liq falls below 5x (currently {vl:.1f}x)",
        "CLEAN_DEAD":         "No active thesis — token has no momentum",
        "CONTESTED":          f"Supply overhang resolves OR demand collapses — re-scan if dump risk drops to LOW or vol/liq spikes above 8x (currently {vl:.1f}x)",
        "UNKNOWN":            "Run deep scan — signals are contradictory or incomplete",
    }
    invalidation = invalidations[state]

    # ── State context block for Mistral ──────────────────────────────────────
    # Tight, specific, state-aware. Mistral translates — does not reason from scratch.
    mc_vel = 0
    vl_display = f"{vl:.1f}x" if vl > 0 else "N/A (no pool)"

    # Farm dump line — always explicit so Mistral never misses it
    farm_line = (
        f"FARM DUMP:   {len(hidden_clusters)} cluster(s) ran coordinated dump at launch — "
        f"bait pump may be in progress"
        if is_farming_dump else
        "FARM DUMP:   None detected"
    )

    # Stale bonding curve warning — token sat on curve beyond normal migration window
    # Normal pump.fun migration: under 60 minutes. 60+ min = migration risk rising.
    stale_curve_line = ""
    if lifecycle_mode == "BONDING_CURVE" and age > 60:
        stale_curve_line = (
            f"WARNING:     Token has been on bonding curve for {age:.0f} minutes — "
            f"normal migration window is under 60min. Migration risk increasing."
        )

    state_context = (
        f"TOKEN STATE: {state}\n"
        f"LIFECYCLE:   {lifecycle_mode} ({age:.0f} min old, ${mc:,.0f} MC)\n"
        f"DUMP RISK:   {true_dump_risk_pct:.1f}% unverified supply ({dump_risk_severity})\n"
        f"             Visible: {dump_risk_breakdown['visible_pct']:.1f}% — {len(dangerous_wallets)} wallet(s) above 5% threshold\n"
        f"             Hidden:  {dump_risk_breakdown['hidden_pct']:.1f}% — {len(hidden_clusters)} cluster(s) unmasked\n"
        f"{farm_line}\n"
        + (f"{stale_curve_line}\n" if stale_curve_line else "")
        + f"PLAYERS:     {player_summary}\n"
        f"DEMAND:      {vol_liq_class} ({vl_display} vol/liq) — {bp_class} ({buys_1h}B/{sells_1h}S last hour)\n"
        f"INVALIDATION: {invalidation}"
    )

    print(f"  [TokenState] {state} | invalidation: {invalidation[:60]}...")

    return {
        "token_state":  state,
        "invalidation": invalidation,
        "state_context": state_context,
    }

