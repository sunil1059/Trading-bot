"""Placeholder for the upstox adapter.

Not implemented yet, on purpose: its API has not been verified against official docs in
this project. It exists so the broker registry shows the slot; using it raises.
"""

from backend.brokers.base import BrokerBase


class UpstoxBroker(BrokerBase):
    name = "upstox"

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise NotImplementedError("upstox adapter is not implemented; use zerodha")
