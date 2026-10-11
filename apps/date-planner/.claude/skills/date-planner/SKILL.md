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
  - showing it: `render_date_preview`
- **Google Calendar** — `list_events`, `suggest_time`, `create_event`,
  `update_event`.
- **Gmail** — `search_threads`, `get_thread`.
- **AllTrails** (if connected) — `search_trails_by_name`, `get_trail_details`,
  `find_trails_near_location`.
- **Web search** — what's on, what's new, a venue's coordinates or booking
  rule when its site didn't say.
- **Artifact** — publishes the preview page so it opens as a card in the
  chat.

## Before the first call

The MCP tools may be deferred. If they are, load every one you'll need in
**one** `ToolSearch` call before calling any of them, so you work from their
real schemas instead of guessing:

```
select:mcp__date-planner__list_couples,mcp__date-planner__get_couple_profile,mcp__date-planner__list_past_dates,mcp__date-planner__edit_couple_preferences,mcp__date-planner__upsert_couple_profile,mcp__date-planner__find_venue,mcp__date-planner__upsert_venue,mcp__date-planner__detect_booking_channel,mcp__date-planner__suggest_reservation_need,mcp__date-planner__build_booking_link,mcp__date-planner__compute_booking_window,mcp__date-planner__create_date,mcp__date-planner__update_date,mcp__date-planner__get_date,mcp__date-planner__list_dates,mcp__date-planner__add_stop,mcp__date-planner__update_stop,mcp__date-planner__reorder_stops,mcp__date-planner__remove_stop,mcp__date-planner__get_sunset,mcp__date-planner__estimate_travel,mcp__date-planner__plan_timeline,mcp__date-planner__render_date_preview,mcp__claude_ai_Google_Calendar__list_events,mcp__claude_ai_Google_Calendar__create_event,mcp__claude_ai_Google_Calendar__update_event,WebSearch
```

### Argument shapes

| tool | arguments |
| --- | --- |
| `upsert_venue` | `venue={...}` — a new venue needs `name`, `city` and `venue_type`; put `lat`/`lng`, `address`, `phone`, `website` here |
| `add_stop` | `date_id`, `stop={title, kind, venue_id?, start_at?, est_duration_min?, anchor?, reservation_need?, booking_status?}` |
| `update_stop` | `stop_id`, `patch={...}` — only the fields to change |
| `update_date` | `date_id`, `patch={...}` |
| `upsert_couple_profile` | `profile={...}` |

- Coordinates, address and website belong to the **venue**, never the stop.
  Create the venue first, then add the stop with its `venue_id`.
- Numbers are numbers: `lat: 34.48`, not `"34.48"`.
- `venue_type`: `restaurant`, `trail`, `museum`, `bar`, `park`, `viewpoint`,
  `other` (a swimming hole or beach is `park` or `other`).
- `kind`: `activity`, `meal`, `ticketed`.
- `anchor`: `none` (plan_timeline places it), `fixed` (keeps `start_at`),
  `sunset`. There is no `flexible`; that's `none`.
- `reservation_need`: `required`, `recommended`, `none`, `unknown`.
- `booking_status`: `not_needed`, `needed`, `link_sent`, `reminder_set`,
  `confirmed`, `cancelled`.

## How to work

The person may be on a phone. **Get as far as you can in one turn** and keep
round trips for real decisions:

- **Act, then show.** When the request is specific enough, pick the
  best-fitting venue yourself, apply the timeline, and put up the
  `[TENTATIVE]` holds. Then show the plan with the alternatives in one line
  ("or Suzanne's / Olivella — say the word and I'll swap"). Holds have no
  guests and can be retitled, so they're safe to place unasked.
- **Assume and say so** rather than asking: "next Thursday = Oct 15",
  "dinner at 6:45 to give you time to change". Ask only when you're blocked:
  a booked stop's time or confirmation you can't infer, no calendar on the
  profile, or two readings of the request that lead to different days.
- **Ask everything at once.** If you must ask, put every question in one
  message.
- **Short replies.** A timeline table, the one next action (call this
  number, tap this link), and at most three one-line caveats. No long
  "couldn't confirm" lists.

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
on and seasonal. Say briefly why it suits them. This is the one place to
stop and wait: add the stops once they agree.

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

For each stop without a venue: find candidates and `upsert_venue` them with
coordinates **and their website and phone** (web search for them if you
don't have them). Keep only those within `max_leg_minutes` of the
neighbouring stops (`estimate_travel` both ways if the stop is in the
middle). `detect_booking_channel` reads the venue's website, so only run it
once the website is saved; if it fails, say the booking method is unknown
and fall back to the phone number rather than retrying.

Pick the best fit for the couple and add it to the stop, then run
`suggest_reservation_need(stop_id)`. Mention one or two runners-up in a
single line with their drive time and booking path ("or Nobu Malibu — 25
min, phone bookings, opens a month ahead at 10am"). Only stop and ask for a
pick when the options are genuinely different kinds of evening and nothing
in the request or profile decides between them.

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

`plan_timeline` starts a flexible stop as soon as the previous leg ends. A
meal after a swim, a hike or anything else that needs a change of clothes
wants **20–30 minutes** more: give it a `fixed` `start_at` with that cushion
instead.

Iterate until there are none, then `plan_timeline(date_id, apply=True)` and
go straight on to the calendar. Show the timeline in the same reply: each
stop's start and end, each leg's drive time, sunset, and what "nearby" was
set to. Changes after that are a quick update, not a reason to wait first.

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

### 8. Show the day

Once the holds are up and the bookings handed over, show the plan as a page:
a map with the stops numbered in order and the route between them, then
each stop's time, the drive before it, where sunset falls, and its booking
state. It's what the user looks at on the phone; the reply stays short.

1. `render_date_preview(date_id)` writes the page and its map images.
2. `Read` `page_path` and every file in `files` before publishing them.
3. Publish with the **Artifact** tool:
   - `preview_url` empty → `file_path=page_path`, `files` as returned,
     `icon="map"`, a one-line `description` ("Malibu day, Oct 24: hike,
     Getty Villa, sunset dinner"). Save the link it returns with
     `update_date(preview_url=...)`.
   - `preview_url` set → `Artifact` `action="read"` on it, then publish with
     `url=preview_url`, `file_path` and `files` (no `icon`). Same link, new
     content.
4. In the reply, put the link first, then the timeline and the one next
   action as usual.

Render and republish after anything that changes what the page shows: a
stop added, removed, swapped or retimed, a booking confirmed, the date
moved. If `missing_coordinates` lists a stop, it's off the map: save the
venue's lat/lng if you can. `basemap: false` means the map tiles couldn't be
fetched; the pins still show, on a plain grid — say so in a few words.

### 9. Confirm

When the user says a stop is booked — or when asked to check — search Gmail:

```
from:(resy.com OR opentable.com OR exploretock.com OR sevenrooms.com) newer_than:3d
```

(add the venue's own domain for ticketed stops). Match on the venue name and
the date **per stop** before believing a thread. Then for that stop: drop
`[TENTATIVE]` from its event and `update_stop(booking_status="confirmed",
confirmation_number=...)`. A phone booking usually has no email — ask for
the details.

Republish the preview (step 8) so the stop shows as booked.

`get_date` shows the derived status. When it reaches **`confirmed`**, offer
to drop `[TENTATIVE]` from every event and invite the partner to each —
and only do that on a yes.

### Moving the date to another day

`update_date(day=...)` moves every stop to the same times on the new day,
and puts any stop that was booked (or had a link or reminder out) back to
`needed`. Then:

- check the new day with `plan_timeline(date_id)`: sunset moves, and a
  `daylight` violation can appear;
- move each stop's calendar event to its new time and put `[TENTATIVE]`
  back on any that lose their booking. A call reminder for the old day is
  moved to the new day's `compute_booking_window`, not deleted;
- tell the user which bookings to cancel, by confirmation number. Never
  cancel one yourself;
- go through step 7 again for every stop that's `needed`;
- republish the preview (step 8).

### 10. After the date

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
  estimate, a duration you made up. A few words beside the number is
  enough ("~17 min, straight-line").
