# Where to run the bot

## Paper trading (now): your own computer, free
Paper mode places no broker orders, so SEBI's static-IP rule does not apply.
Kite market data and WebSocket work from any IP. Run it on your PC during market hours.

## LIVE (later, optional): Oracle Cloud "Always Free" VM
Per Oracle's [Always Free resources page](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm)
(checked 2026-11-27):
- Ampere A1: up to 2 OCPUs and 12 GB RAM free, 10 TB/month outbound data.
- Instances are created in your home region (pick Mumbai or Hyderabad for low latency).
- **Idle instances may be reclaimed**: 95th-percentile CPU, network and (A1) memory all under 20% over 7 days.
  A trading bot is often that quiet, so this is a real risk.

Caveats to resolve before LIVE (not verified yet):
- Whether a *reserved* (permanent) public IP is free on Always Free. Zerodha allows only one
  static-IP change per calendar week, so the IP must not change.
- Whether upgrading to a Pay-As-You-Go account (still free within limits) avoids idle reclamation.

Fallback if Oracle is unsuitable: a small paid VPS in an Indian region with a fixed IP.
