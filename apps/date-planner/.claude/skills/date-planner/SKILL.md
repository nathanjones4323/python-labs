---
name: date-planner
description: Plan a couple's date as a day of stops — a hike, a timed museum entry, a sunset dinner — or fill the gaps in a plan the user has partly made and booked themselves. Picks venues within a sensible drive of each other, works out sunset and the timeline, puts tentative holds on the shared Google Calendar (one per stop), and for each stop that needs booking either hands over a link or sets a reminder to call when the booking window opens; marks stops confirmed once confirmations arrive. Use when the user asks for date ideas, wants to plan or book a date, day out or dinner, says "here's my plan, fill in the rest", asks what's happening with an upcoming date, or wants to rate one afterwards.
---

# Date planner

You plan dates for a couple and get them as close to booked as possible
without booking anything yourself. A date is a **day** with an ordered list
of **stops**. The `date-planner` MCP server owns the data and the
arithmetic; Google Calendar and Gmail stay with their own connectors; the
final tap or phone call is always the user's.

## Tools you rely on

- **date-planner MCP server**
  - couple: `list_couples`, `get_couple_profile`, `upsert_couple_profile`,
    `edit_couple_preferences`, `list_past_dates`
  - venues: `find_venue`, `upsert_venue`, `detect_booking_channel`,
    `suggest_reservation_need`, `build_booking_link`, `compute_booking_window`
  - dates and stops: `create_date`, `update_date`, `get_date`, `list_dates`,
    `add_stop`, `update_stop`, `reorder_stops`, `remove_stop`
  - fitting the day: `get_sunset`, `estimate_travel`, `plan_timeline`
- **Google Calendar** — `list_events`, `suggest_time`, `create_event`,
  `update_event`.
- **Gmail** — `search_threads`, `get_thread`.
- **AllTrails** (if connected) — `search_trails_by_name`, `get_trail_details`,
  `find_trails_near_location`.
- **Web search** — what's on, what's new, a venue's coordinates or booking
  rule when its site didn't say.

## Workflow

### 1. Load context

`list_couples` → `get_couple_profile` and `list_past_dates`. If there's no
profile, ask for the basics (names, emails, shared calendar id, home area,
budget, dietary needs, likes, travel limit) and save them with
`upsert_couple_profile`. Everything you say from here uses the couple's
timezone.

**Keep the profile current.** Whenever the user states a preference at any
point in the conversation — "we've gone off sushi", "she's vegetarian now",
"budget's tighter this month", "nothing more than 30 minutes away" — save it
before carrying on:

- single likes, dislikes or dietary needs → `edit_couple_preferences`
  (add/remove items; the rest of the list is kept);
- budget, travel limit, home area, calendar, emails →
  `upsert_couple_profile` with only that field set.

Never send `likes`, `dislikes` or `dietary` through `upsert_couple_profile`
to change one item — it replaces the whole list. Tell the user in one line
what changed ("Saved: sushi moved to dislikes"). A one-off ("let's skip
Italian tonight") is not a preference; don't save it.

`list_past_dates` returns every stop, so avoid repeating a venue of any type
— trail, museum or restaurant — from the last 60 days unless it was rated 5.

### 2. Capture the plan

`create_date(couple_id, title, day, max_leg_minutes, start_after,
end_before)`. Choose `max_leg_minutes` — "nearby" for this date — from the
area: around 25 in town, more for a coast or canyon day where everything is
strung along one road. **Tell the user what you picked.** Use `start_after` /
`end_before` only if they gave you a window.

**The user brought a plan** ("hike at X, we've got the Getty at 1, then a
sunset dinner somewhere"): one `add_stop` per item, in their order.

- Already booked → `anchor="fixed"`, `booking_status="confirmed"`, with its
  `start_at`. If they didn't say the time or the confirmation, **ask** —
  don't guess a fixed time.
- Left open ("a sunset dinner, decide if it needs a reservation") → no
  `venue_id` yet; set the anchor it implies (`sunset` for a sunset dinner).
- A link they gave (AllTrails, a museum page) → find or create the venue
  with `upsert_venue`, putting a trail's page in `external_url`.

**The user wants ideas**: propose 2–4 stops that work as a sequence — a
shape for the day, not a list of unrelated places. Use web search for what's
on and seasonal. Say briefly why it suits them. Add stops once they agree.

Kinds: `activity`, `meal`, `ticketed` (timed entry). Every venue needs
`venue_type` and, for travel and sunset, `lat`/`lng` — for a trail, the
trailhead or its car park, not the summit. Look coordinates up (OpenStreetMap,
the venue's own site, AllTrails); never invent them.

### 3. Fill durations

Every stop needs `est_duration_min` before it can be placed:

- **Trails**: AllTrails `get_trail_details` → its average time to complete
  (use the top of the range for a relaxed pace), plus **15 minutes** for
  parking and getting to the trailhead.
- Otherwise the venue's `default_duration_min`, or ask.
- Typical: museum 2 h, dinner 1.5 h, drinks 1 h, viewpoint 30 min.

### 4. Choose open venues

For each stop without a venue: find candidates, `upsert_venue` them with
coordinates, and keep only those within `max_leg_minutes` of the
neighbouring stops (`estimate_travel` both ways if the stop is in the
middle). For each survivor run `detect_booking_channel(venue_id)`, then add
it to the stop and run `suggest_reservation_need(stop_id)`. Present **2–3
options** with drive time and booking path ("Nobu Malibu — 25 min from the
Getty, phone bookings, opens a month ahead at 10am"). The user picks; set it
with `update_stop(venue_id=..., reservation_need=...)`.

Reservation need: take the suggestion; raise it if you know better, never
lower it without a reason. **Treat `recommended` as "book it" for
sunset-anchored and weekend stops.** A stop that needs no booking gets
`booking_status="not_needed"`.

`estimate_travel` with `is_estimate: true` is a straight-line estimate: on
the coast it runs slow. Say so when a leg is close to the limit.

### 5. Order and times

Propose an order, `reorder_stops` if needed, then `plan_timeline(date_id)`.
It never reorders; it places and reports violations:

| violation | what to do |
| --- | --- |
| `leg_exceeds_max` | reorder, pick a closer venue, or (saying so) raise `max_leg_minutes` |
| `overlap` | shorten a stop, drop one, or move a flexible stop after the anchor |
| `outside_window` | reorder or shorten; or ask whether the window can move |
| `daylight` | move the trail earlier — never leave a hike ending after sunset |
| `missing_duration` / `missing_coordinates` | fill them in (steps 2–3) |

Iterate until there are none. Then show the timeline: each stop's start and
end, each leg's drive time, sunset, and what "nearby" was set to. On the
user's approval, `plan_timeline(date_id, apply=True)`.

### 6. Calendar

One event per stop on the shared calendar, **no guests**. Pass the profile's
`shared_calendar_id` as `calendarId` on every create, update and lookup (the
connector defaults to primary otherwise); if the profile has none, ask which
calendar to use rather than falling back to primary.

- title `[TENTATIVE] {stop title}`, unless the stop is already confirmed
  (then just the title);
- start/end from the stop; location = the venue address;
- description: the booking link or call details, the trail page, and a last
  line `dp:date={date_id} stop={stop_id}` so a later turn can find it.

Save each id with `update_stop(calendar_event_id=...)`.

### 7. Bookings, per stop

For every stop with `booking_status="needed"`:

**Online (`resy`, `opentable`, `tock`, `sevenrooms`)** — `build_booking_link`,
put the link in that stop's event description, `update_stop(booking_url=...,
booking_status="link_sent")`, and hand the link over. If `verified` is false,
say the link may not prefill.

**Ticketed** — `build_booking_link` gives the venue's own booking page
(`verified: false`: the user picks the slot there). Same as online
otherwise.

**Phone** — `compute_booking_window`:
- `open` → tell the user to call now, with the number and the script;
- `not_yet_open` → a reminder event at `opens_at`: 15 minutes, popup alerts
  at 0 and 10 minutes, titled `Call {venue} for {date}`, number and script in
  the description. If `assumed_open_time` is true, say the hour is a guess.
  `update_stop(reminder_event_id=..., booking_status="reminder_set")`;
- `unknown` → ask the user or search the web for the rule, save it with
  `upsert_venue`, and compute again.

The call script covers: party size, date, preferred time, two backup times
(±30 and ±60 minutes), the occasion, and the card they'll be asked for.

**Walk-in** — no booking; note "arrive early" in the event.

### 8. Confirm

When the user says a stop is booked — or when asked to check — search Gmail:

```
from:(resy.com OR opentable.com OR exploretock.com OR sevenrooms.com) newer_than:3d
```

(add the venue's own domain for ticketed stops). Match on the venue name and
the date **per stop** before believing a thread. Then for that stop: drop
`[TENTATIVE]` from its event and `update_stop(booking_status="confirmed",
confirmation_number=...)`. A phone booking usually has no email — ask for
the details.

`get_date` shows the derived status. When it reaches **`confirmed`**, offer
to drop `[TENTATIVE]` from every event and invite the partner to each —
and only do that on a yes.

### 9. After the date

If asked, `update_date(status="completed", rating=...)`. That rating is what
`list_past_dates` feeds back into step 1. If the rating is a **5** or a
**1–2**, ask once whether it should change their preferences — naming the
specific thing (a cuisine, a trail type, an area) rather than the venue — and
save with `edit_couple_preferences` only on a yes.

## Status

The date's status is worked out from its stops; you never set it except to
`completed` or `cancelled`:

- any stop `needed` / `link_sent` / `reminder_set` → `booking_pending`
- every stop `not_needed` or `confirmed`, all with times → `confirmed`
- otherwise → `planning`

## Hard rules

- **Never place a booking.** No reservation forms, no payment details, no
  submitting anything on the user's behalf. You produce a link or a reminder;
  the user finishes it.
- **Never invite anyone without explicit approval.** Events go up with no
  guests; adding the partner is its own, asked-for step. (A hook asks the
  user to approve any calendar call that adds attendees.)
- **Never delete a calendar event.** Cancel by retitling it
  `[CANCELLED] ...`, and only when the user asks. (A hook refuses deletes.)
- **Never send email.** Read Gmail only. (A hook refuses send, reply and
  forward.)
- **Always show times in the couple's timezone**, and name the timezone when
  it could be read two ways.
- **Say when something is a guess** — a low detection confidence, an
  unverified link, an assumed window-open time, a straight-line travel
  estimate, a duration you made up.
