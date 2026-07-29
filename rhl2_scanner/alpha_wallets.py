"""Curated high-signal RH-Chain wallets — the Robinhood team + major-protocol
devs. These are NOT harvested guesses; they're hand-verified operators whose
touch on a token is a near-guaranteed mover ($WALLET, $PLTS precedent).

Seeded at startup into the smart-money set (so their buys drive the cluster /
core-alpha signals) AND labelled for the tracked-wallet feed, so every time one
interacts with a token you get a named ping. Persist across restarts.

Addresses are lowercased on load. Extend via RHL2_ALPHA_WALLETS
("0xaddr=label,0xaddr=label") — env entries merge on top of this list.
"""

from __future__ import annotations

# address (lowercased) -> short label shown on the alert
ALPHA_WALLETS: dict[str, str] = {
    "0xe4a0015b4c12f84bf9b8b9db56b7ef0bc539d88f": "RH faucet-funded · $156M deployer",
    "0xbe498aad9c6fd0e4cd6d1e3fbb395026c5d28215": "USDG deployer · $1.71B vol",
    "0xcdfc08a1c1fbafb355645e5ddc32122e5716ca90": "CashCat creator",
    "0x5522427804f077f6649ed3a66b38e06eedc6c35f": "Algebra/Sheriff DEX builder",
    "0xfeed46c11f57b7126a773eec6ae9ca7ae1c03c9a": "Morpho deployer (0xfeed)",
    "0xda4bcee76b29efec9697fcf663601c2042043968": "Pons core dev · #1 launchpad",
    "0x0c628d6535b781f6c703ad319af8b1bcec63fbb4": "Launchpad proxy-admin (upgrades live)",
    "0xa9edf3dd77c28d9891a470b4e6f0d37724286501": "Serial builder · 6 protocols",
    "0x89562eb8979db1e85a01e85120bfd6a7c47a39cb": "USDG + stock-treasury operator",
    "0xe6253663eb61933b20b0b3886ce5226594d2cd9d": "Router infra · 61,302 users",
    "0x937933e11ad6307ae0d8b8115986e91734be2d5c": "Vault + LP-locker operator",
    "0xb668382cf44038a3e8140e789060f6a809787cda": "NFT-AMM sole operator",
    "0x6cbea9d73c6642aba5be2421b3671a4709606059": "Base DEX primitives · V2 router",
    "0x218d773fee2a3c6c88e1e50a663f21c3dc55f2d7": "Token-factory operator · 3,128 devs",
    "0xbd4105e98eb481e3cb40b0dc35f8f2241f460bba": "Router deployer · 4,180 users",
}


def load_alpha_wallets(env_value: str = "") -> dict[str, str]:
    """Return the curated map merged with any RHL2_ALPHA_WALLETS env entries
    ("0xaddr=label,0xaddr=label"). All addresses lowercased."""
    out = {a.lower(): lbl for a, lbl in ALPHA_WALLETS.items()}
    for pair in (env_value or "").split(","):
        pair = pair.strip()
        if "=" in pair:
            addr, _, lbl = pair.partition("=")
            addr = addr.strip().lower()
            if addr.startswith("0x") and len(addr) == 42:
                out[addr] = lbl.strip() or "alpha wallet"
    return out
