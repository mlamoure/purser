"""Manually tracked brokerage holdings (issue #390, DESIGN §8, decisions 88/89).

- ``service``: positions and their events — create, record, reverse, close —
  and switching an account's holdings source.
- ``projection``: the manual ``holdings_snapshots`` rows, derived from the
  events and prices (``purser.core.holdings`` computes them).
- ``prices``: manual closes, the daily provider fetch, staleness.
- ``providers``: the price provider protocol and Tiingo.

Every write locks the account rows it projects (``FOR NO KEY UPDATE``, in id
order, before the balance watermark), writes its audit row, and re-projects
the affected accounts inside the same transaction.
"""
