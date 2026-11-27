"""Placeholder for the angel_one adapter.

Not implemented yet, on purpose: its API has not been verified against official docs in
this project. It exists so the broker registry shows the slot; using it raises.
"""

from backend.brokers.base import BrokerBase


class AngelOneBroker(BrokerBase):
    name = "angel_one"

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise NotImplementedError("angel_one adapter is not implemented; use zerodha")
