"""Date planner: the data and arithmetic behind planning a couple's date.

This package is an MCP server (`date_planner.server`). A date is a day with
an ordered list of stops. The server owns venue records, works out how a
venue takes reservations, builds booking links, computes when a booking
window opens, times the legs between stops, places them around sunset and
fixed bookings, and tracks each date and stop in Postgres.

It deliberately does *not* touch Google APIs and does *not* place bookings.
Calendar and Gmail stay with the official connectors; the final booking step
stays with a person. See `.claude/skills/date-planner/SKILL.md` for the
orchestration that ties the two together.
"""
