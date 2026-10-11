"""Fitting a day's stops together: sunset, travel between them, and times.

`sunset` and `timeline` are pure arithmetic; `travel` is the one module that
may reach a routing service, and only when `TRAVEL_PROVIDER` asks it to.
"""
