"""Position reconciliation: the broker is the source of truth."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.models.domain import BrokerPosition


@dataclass
class ReconcileReport:
    matched: dict[str, int] = field(default_factory=dict)
    adjusted: dict[str, tuple[int, int]] = field(default_factory=dict)  # symbol -> (local, broker)
    unknown_at_broker: dict[str, int] = field(default_factory=dict)  # broker has it, we don't
    shorts: dict[str, int] = field(default_factory=dict)  # must never happen

    @property
    def critical(self) -> bool:
        return bool(self.shorts or self.unknown_at_broker)

    @property
    def clean(self) -> bool:
        return not (self.adjusted or self.unknown_at_broker or self.shorts)


def reconcile(local: dict[str, int], broker: list[BrokerPosition], exchanges: tuple[str, ...] = ("NFO",),
              product: str = "MIS") -> ReconcileReport:
    """Compare local open quantities (symbol -> qty) with broker net positions."""
    rep = ReconcileReport()
    seen = set()
    for p in broker:
        if p.exchange not in exchanges or p.product != product:
            continue
        seen.add(p.tradingsymbol)
        if p.quantity < 0:
            rep.shorts[p.tradingsymbol] = p.quantity
            continue
        mine = local.get(p.tradingsymbol)
        if mine is None:
            if p.quantity != 0:
                rep.unknown_at_broker[p.tradingsymbol] = p.quantity
        elif mine == p.quantity:
            rep.matched[p.tradingsymbol] = mine
        else:
            rep.adjusted[p.tradingsymbol] = (mine, p.quantity)
    for sym, qty in local.items():
        if sym not in seen and qty != 0:
            rep.adjusted[sym] = (qty, 0)
    return rep
