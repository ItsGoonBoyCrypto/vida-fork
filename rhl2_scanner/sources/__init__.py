"""Data-source clients: DexScreener, chain RPC/explorer, safety tooling.

Every client conforms to an interface in ``base.py`` so the scanner is
provider-agnostic and RH-L2-specific endpoints can be swapped in without
touching the pipeline.
"""
