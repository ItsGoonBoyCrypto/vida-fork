"""Contract-level rug / manipulation detection.

Bundle-% and honeypot sim catch a lot, but the token *contract itself* often
declares its intent: a mint function (infinite supply), a blacklist (they can
freeze your sell), owner-settable fees/limits (they can tax you to zero), a
pause switch. This module scans a contract's function names for those hooks, and
flags wash-trading (volume with no holder growth) from the live snapshot.

Pure + dependency-free: the caller fetches the ABI/method list (Blockscout) and
passes names in; `audit_functions` classifies them, `wash_trade` reads the
snapshot. Findings feed both the /audit command and an alert-time safety gate.
"""

from __future__ import annotations

# (substring, severity, human label) — matched against lowercased fn names.
_PATTERNS = [
    ("mint", "critical", "mint (can inflate supply)"),
    ("blacklist", "critical", "blacklist (can block your sell)"),
    ("blocklist", "critical", "blocklist (can block your sell)"),
    ("denylist", "critical", "denylist (can block your sell)"),
    ("setban", "critical", "ban (can block your sell)"),
    ("pause", "critical", "pause/freeze trading"),
    ("freeze", "critical", "freeze accounts"),
    ("settax", "warning", "owner-settable tax"),
    ("setfee", "warning", "owner-settable fee"),
    ("updatefee", "warning", "owner-settable fee"),
    ("setmaxtx", "warning", "owner-settable max-tx limit"),
    ("setmaxwallet", "warning", "owner-settable max-wallet"),
    ("setlimit", "warning", "owner-settable limits"),
    ("excludefromfee", "info", "fee-exclusion list"),
    ("setrouter", "warning", "owner-settable router"),
]

# A renounce function present is a good sign IF ownership is actually renounced —
# but its mere presence doesn't prove it. We surface it as context, not a pass.


def audit_functions(names) -> dict:
    """Classify a contract's function names → {critical:[], warning:[], info:[],
    has_renounce: bool}. Deduped, human-readable labels."""
    lowered = [str(n).lower() for n in (names or [])]
    found: dict = {"critical": [], "warning": [], "info": []}
    seen = set()
    for fn in lowered:
        for needle, sev, label in _PATTERNS:
            if needle in fn and label not in seen:
                found[sev].append(label)
                seen.add(label)
    found["has_renounce"] = any("renounce" in fn for fn in lowered)
    return found


def wash_trade(snap) -> tuple[bool, str]:
    """Heuristic: heavy volume with no holder growth = likely wash/bot trading.

    Real demand adds holders; wash trading is the same bots ping-ponging, so
    turnover (1h volume vs market cap) is high while holder growth is ~flat.
    """
    vol = snap.volume_1h
    mcap = snap.market_cap_usd
    hg = snap.holder_growth_1h
    if not vol or not mcap or hg is None:
        return False, ""
    turnover = vol / mcap
    if turnover >= 1.0 and hg <= 2:
        return True, f"{turnover:.1f}x mcap traded in 1h but only +{hg} holders — wash-like"
    return False, ""


def risk_summary(audit: dict, wash: tuple) -> tuple[str, list]:
    """Overall verdict + flat list of findings for display/gating."""
    findings = []
    for sev in ("critical", "warning", "info"):
        for label in audit.get(sev, []):
            findings.append((sev, label))
    if wash and wash[0]:
        findings.append(("warning", wash[1]))
    if any(s == "critical" for s, _ in findings):
        verdict = "🔴 HIGH RISK"
    elif any(s == "warning" for s, _ in findings):
        verdict = "🟡 CAUTION"
    else:
        verdict = "🟢 no contract red flags found"
    return verdict, findings
