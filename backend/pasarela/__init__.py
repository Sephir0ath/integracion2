"""Simulated external payment gateway (the fragile dependency).

It is *not* a domain service: it represents a third party provider that the
ticket system cannot control. It answers the two endpoints the `pagos`
service needs (`POST /pagos` and `POST /pagos/{pago_id}/reembolso`), keeps
its own ledger in memory (a simulator has no database) and exposes a control
plane (`/admin/fault`) used by the demo scripts to inject faults.
"""
