"""Date planner: the data and arithmetic behind planning a couple's date.

This package is an MCP server (`date_planner.server`). It owns restaurant
records, works out how a restaurant takes reservations, builds prefilled
booking links, computes when a booking window opens, and tracks the state of
each planned date in Postgres.

It deliberately does *not* touch Google APIs and does *not* place bookings.
Calendar and Gmail stay with the official connectors; the final booking step
stays with a person. See `skills/date-planner/SKILL.md` for the orchestration
that ties the two together.
"""
