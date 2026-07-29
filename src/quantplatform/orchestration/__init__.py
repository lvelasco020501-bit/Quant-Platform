"""Composition root and the runtime pipeline that wires the domains together.

This package owns the mandatory flow from market data through features, signals, order
intents, risk decisions, execution, portfolio accounting, reconciliation and health
evaluation. It is the only package permitted to depend on all the others.
"""
