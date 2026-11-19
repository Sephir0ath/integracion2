"""Payment service of the ticketera domain.

The service owns the payment ledger (`pagos` database), is the only
component that talks to the external payment gateway (the simulated
`pasarela`) and is the place where the saga step `COBRAR` and its
compensation `REEMBOLSAR` are implemented.

Every call to the gateway goes through `service.pasarela`, which applies the
timeout, the Bulkhead, the Circuit Breaker and the retry with exponential
backoff.
"""
