# garmin-mcp

A local MCP server for one person's Garmin Connect account. An agent can
read training data (activities, sleep, HRV, readiness and more), build
strength and running workouts, put them on the Garmin calendar, and send
them to a watch such as a Venu 4.

It's an execution layer, not a coach. The agent and the user decide what
the training should be; this server checks it, previews it, writes it once
the user approves, and reads Garmin back to confirm it landed.

It talks to Garmin through
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
an **unofficial** client for Garmin Connect's internal APIs. Garmin can
change those APIs without notice. Expect occasional breakage and
re-authentication. This is not Garmin's developer API.

## Quick start

```sh
cd apps/garmin-mcp
cp .env.example .env                                  # writes stay preview-only until you change GARMIN_DRY_RUN
uv sync --package garmin-mcp
uv run --package garmin-mcp garmin-mcp-login          # email, password, MFA code if you use 2FA
```

Then start Claude Code in `apps/garmin-mcp`. `.mcp.json` registers the
server as `garmin`, and `.claude/settings.json` lets the read tools run
without asking. Ask something like "how did I sleep this week?" or "make
me a 5x5 squat workout for Thursday".

When the previews look right, set `GARMIN_DRY_RUN=false` in `.env` and
restart the server (`/mcp`, reconnect `garmin`) so approved writes can go
through.

For Claude Desktop, or another client, register the command
`uv run --package garmin-mcp garmin-mcp`, with the working directory set to
this folder so it finds `.env`.

## Authentication

Signing in can need an MFA code typed by a person, and a stdio server has
no way to ask for one. So sign-in happens in a separate command,
`garmin-mcp-login`, and the server only ever restores the session tokens
that command saved.

- The password is typed into a `getpass` prompt and is never written
  anywhere.
- The tokens go to `$GARMIN_TOKENSTORE/garmin_tokens.json`. The default is
  `~/.config/garmin-mcp/tokens`, outside the repo. The library writes the
  file 0600 inside a 0700 directory. The server re-tightens both if they
  were loosened, and refuses a token store that is a symlink.
- The library refreshes tokens before they expire and writes the new ones
  back, so a session survives restarts until Garmin revokes it.
- When Garmin stops accepting the session, every tool reports an
  `authentication` error. To fix it, run `garmin-mcp-login` again and
  restart the server. The login command always does a fresh credential
  sign-in and replaces the old tokens.

Tokens made by older versions of the library (garth's `oauth1_token.json`
and `oauth2_token.json`) aren't supported. Sign in again.

## Configuration

All settings are environment variables (or `.env` / `.env.local` in this
folder). See `.env.example`.

| Variable | Default | |
| --- | --- | --- |
| `GARMIN_DRY_RUN` | `true` | While true, every write only previews, even with a confirmation token. |
| `GARMIN_TOKENSTORE` | `~/.config/garmin-mcp/tokens` | Token directory. |
| `GARMIN_EMAIL` | | Prefills the login prompt. |
| `GARMIN_APPROVAL_TTL_SECONDS` | `600` | How long a preview's token stays valid. |
| `GARMIN_DEFAULT_DEVICE_ID` | | Device for `push_workout_to_device` when none is named. Otherwise Garmin's last-used device. |
| `GARMIN_STATE_DIR` | `~/.local/state/garmin-mcp` | Server log and write log. |
| `GARMIN_LOG_LEVEL` | `INFO` | |
| `GARMIN_READ_RETRIES` | `2` | Library retries for reads that hit a 5xx or a dropped connection, with 1–10 s backoff. |

## Tools

These tools are the whole surface. Nothing else in the library is
reachable, and no tool runs code or shell commands.

| Tool | | |
| --- | --- | --- |
| `get_account_status` | read | Whether the saved session works, whether dry-run is on, and what to do if not. |
| `get_recent_activities` | read | Activities in a date range (default: last 14 days), filtered by type, newest first. |
| `get_activity_details` | read | One activity, plus laps (or, for strength, the logged sets). No GPS or HR streams. |
| `get_training_metrics` | read | Per-day `daily_summary`, `sleep`, `hrv`, `training_readiness`, `training_status`, `vo2max`. Up to 14 days and 42 Garmin calls per request. |
| `search_exercises` | read | Garmin's strength exercise catalog (≈1,500 entries), by name. |
| `list_workouts` | read | The workout library, optionally only strength or running. |
| `get_workout` | read | One workout in the canonical form (below), or why it can't be shown that way. |
| `get_workout_calendar` | read | Scheduled workouts and recorded activities between two dates (≤ 92 days). |
| `list_devices` | read | Registered devices, last-used marked. |
| `get_device_push_status` | read | Whether a pushed workout is still waiting in Garmin's queue for the device. |
| `create_workout` | write | Add a strength or running workout. |
| `update_workout` | write | Replace a workout's definition. It keeps its id and calendar entries. |
| `schedule_workout` | write | Put a workout on a date (today up to a year ahead). |
| `reschedule_workout` | write | Move a calendar entry, by its id or by workout + date. |
| `push_workout_to_device` | write | Queue a workout for a watch. |

There's no delete tool. Deleting comes later, once write verification and
recovery have been used for real.

Errors come back as MCP tool errors. The message leads with a category an
agent can act on: `authentication`, `validation`, `not_found` (with
`workout`, `activity`, `exercise`, `calendar_entry` or `device`),
`ambiguous`, `conflict`, `rate_limit`, `network`, `garmin_api`,
`unsupported` or `approval`.

## Workouts

Units are in the field names. Nothing is converted implicitly.

```json
{
  "name": "5x5 A",
  "sport": "strength",
  "exercises": [
    {"name": "Air Squat", "kind": "warmup", "sets": 1, "reps": 15},
    {"name": "Barbell Bench Press", "sets": 4, "reps": 8, "weight_lb": 135, "rest_seconds": 90},
    {"type": "circuit", "rounds": 5, "exercises": [
      {"name": "Barbell Back Squat", "reps": 5, "weight_lb": 275, "rest_seconds": 180},
      {"name": "Barbell Bench Press", "reps": 5, "weight_lb": 195, "rest_seconds": 180},
      {"name": "Weighted Pull-up", "reps": 5, "weight_lb": 45, "rest_seconds": 180}
    ]},
    {"name": "Plank", "sets": 3, "duration_seconds": 45, "rest_seconds": 30}
  ]
}
```

```json
{
  "name": "6 x 400",
  "sport": "running",
  "steps": [
    {"type": "warmup", "duration_seconds": 600, "target": {"type": "heart_rate_zone", "zone": 2}},
    {"type": "repeat", "repeat_count": 6, "steps": [
      {"type": "interval", "distance_meters": 400,
       "target": {"type": "pace", "fast_seconds": 230, "slow_seconds": 240, "per": "km"}},
      {"type": "recovery", "duration_seconds": 90}
    ]},
    {"type": "cooldown"}
  ]
}
```

- **Exercises** must match a name in Garmin's catalog. Case, spaces and
  punctuation are ignored ("push up" is "Push-up"). That's the only
  fuzziness: an unknown name is an error listing the closest names, and
  nothing similar is ever picked instead. `exercise_id` (`CATEGORY/EXERCISE`,
  from `search_exercises`) pins an exact entry. If both are given, they have
  to agree. A set is `reps` or `duration_seconds`.
- **Strength items** are an exercise done for `sets` (resting
  `rest_seconds` after each), or a `circuit`: 2–10 exercises done in turn,
  each with its own rest, the sequence repeated `rounds` times. Circuits
  cover supersets and the round-by-round layout Garmin's editor produces.
  `kind` (`exercise`, `warmup`, `cooldown`) marks Garmin's Warm Up / Cool
  Down steps.
- **Weights** are `weight_lb` or `weight_kg`. They're written to Garmin in
  pounds, the format its own editor uses (seen on a real account), so a
  `weight_kg` reads back as the equivalent `weight_lb`, to 0.1 lb.
- **Running steps** are `warmup`, `interval`, `recovery`, `cooldown` or
  `rest`. Each ends after `duration_seconds`, after `distance_meters`, or
  (with neither) on the lap button. Warmups come first and cooldowns last.
  A `repeat` block holds 1–10 steps, repeated 2–50 times, and can't nest.
- **Targets** are `pace` (a range, per km or mi), `heart_rate` (bpm range),
  `heart_rate_zone` (1–5, the user's Garmin zones) or `cadence` (spm range).
  Ranges must be ordered and plausible (pace 2:00–20:00 per km, HR 30–230).
- Limits: 40 exercises counting the ones in circuits, and 50 running
  steps counting the ones inside repeats.

The spec this was built from had a flat `target_type` + `target_value` and
a `repeat_count` on each step. Garmin targets are ranges, though, and an
interval repeats together with its recovery, so targets are typed objects
and repeats are blocks.

`get_workout` returns this same form, so an edit is: get, change, pass to
`update_workout`. Workouts that use things this form can't express (other
sports, several segments, nested repeats, lap-button rests, power targets) come
back with `definition: null` and an `unsupported_reason`. `update_workout`
refuses them rather than silently dropping those parts.

## Approval and write safety

Every write goes through one path (`services/writes.py`):

1. **Validate.** Arguments, exercise names and dates are checked, and the
   current Garmin state is read: does a workout with this name already
   exist, what's on that day. Problems are errors, and nothing is written.
   A duplicate name, or the same workout twice on a day, is a `conflict`
   unless `allow_duplicate_name` / `allow_duplicate` says it's intended.
2. **Preview.** The first call returns `status: approval_required`, a
   human-readable `preview` (an exercise list, a diff for updates, warnings
   such as "Thursday already has Easy run") and a `confirmation_token`.
3. **Approve.** The agent shows the preview to the user. Only if they
   approve does it call again with the same arguments plus the token. The
   token is an HMAC over the exact payload *and* the Garmin state the
   preview was built from. If the arguments changed, or someone edited the
   workout or the calendar in between, the token is refused and a fresh
   preview is needed. Tokens work once and expire after 10 minutes.
4. **Execute once.** The library doesn't retry POST/PUT, and neither does
   this server.
5. **Verify.** Garmin is read back and compared with what was intended,
   then the result is reported:
   - `verified`: the read-back matches.
   - `failed`: Garmin certainly didn't apply it (a 4xx, or no connection
     was ever made).
   - `unknown`: it may have been applied (a timeout, a dropped connection,
     a 5xx, or a read-back that doesn't match). The server runs a read-only
     check to see whether the change landed (`verified` if it did), but
     never retries. The agent is told not to retry either.
6. **Log.** One line per write attempt goes to
   `$GARMIN_STATE_DIR/operations.jsonl` (0600): operation, status, ids and
   dates. No workout contents, health data or tokens.

Two separate things stand between the agent and a change to the account:

- **The token** proves the call is the exact change that was previewed.
  It can't prove a person said yes.
- **The person** is Claude Code's permission prompt.
  `.claude/settings.json` auto-allows only the read tools, so Claude Code
  asks before every write call, including the confirming one. That prompt
  shows the arguments and the token. Keep write tools off any allow list.
  (`test_claude_code_auto_allows_only_reads` fails if one gets added.)

`GARMIN_DRY_RUN=true`, the default, overrides all of it: writes only
preview. A `dry_run: true` argument does the same for one call.

## Devices

Garmin has no "put this on my watch now" call. `push_workout_to_device`
adds a message to the account's device queue, and the watch fetches it at
its next sync with Garmin Connect (through the phone app, or Wi-Fi). So the
result reports two things separately:

- `upload_status`: `accepted` once a *new* message for the workout shows
  up in Garmin's queue.
- `sync_status`: `pending_on_garmin` while it's still queued, so the watch
  hasn't taken it yet. `not_in_queue` means it's gone from the queue:
  probably delivered, but Garmin also drops messages, and it doesn't say
  which.

The watch's own workout list is the only proof of delivery.
`get_device_push_status` checks the queue again later.

Scheduling a workout also queues it for the primary device: Garmin adds a
`workouts` message and a `Schedule` message by itself. So a scheduled
workout reaches the watch without a push. And because a message for the
workout may already be waiting, a push is only `verified` by a message that
appeared after it. The preview warns when one is already queued.

Before pushing, the device's own capability flags are checked
(`strengthWorkoutCapable`, `runningWorkoutCapable`, `workoutCapable`, as
reported by the Venu 4). A sport the device says it can't run is refused
as `unsupported`. A sport it says nothing about is a warning in the
preview. `list_devices` shows these flags as `workout_sports`, along with
`max_workouts` (200 on a Venu 4). Garmin doesn't report how many workouts
are on the watch now.

## Tests

```sh
uv run --package garmin-mcp pytest            # 173 unit and protocol tests, no network
uv run --package garmin-mcp mypy              # strict
uv run --package garmin-mcp ruff check . && uv run --package garmin-mcp ruff format --check .
```

The unit tests run against `tests/fakes.py`, an in-memory Garmin account
that records every write. They cover validation and the codec round trip,
exercise resolution, a strength workout in the exact shape Garmin's editor
produces (read from a real account), previews and tokens, dry run,
conflicts, device capability checks, all three
outcomes (including "timed out but landed" and "timed out and didn't"),
read-back mismatches, failure classification, session restore and token
permissions. Over the MCP protocol they cover the tool allowlist, schemas,
annotations, structured results, error categories, and the real
`garmin-mcp` command over stdio.

Live tests (`tests/test_integration.py`) are opt-in and use the real
account:

```sh
GARMIN_LIVE_TESTS=1 uv run --package garmin-mcp pytest -m live -s                       # reads
GARMIN_LIVE_TESTS=1 GARMIN_LIVE_WRITES=1 uv run --package garmin-mcp pytest -m live -s   # + writes
GARMIN_LIVE_TESTS=1 GARMIN_LIVE_WRITES=1 GARMIN_LIVE_DEVICE_ID=<id> uv run ...          # + push
```

What they write is named `[garmin-mcp test] …` and scheduled 30+ days out.
Afterwards they remove it (entries, then workouts) through the library
directly. The messages that delivery queued for the watch (two per run:
one from scheduling, one from the push) aren't removed. They point at the
deleted workout, and the watch picks them up at its next sync; what it does
with a workout that no longer exists hasn't been checked.

To poke at the tools by hand, use the MCP Inspector:

```sh
npx @modelcontextprotocol/inspector uv run --package garmin-mcp garmin-mcp
```

## Checked against a live account

On 2026-10-18, against an account with a Venu 4 registered that day.

Writes, via the live tests: creating a strength and a running workout,
scheduling, rescheduling and pushing to the Venu 4 all came back
`verified` from the read-back, and the cleanup removed everything. The
first run's push came back `unknown`, correctly: the queue check looked
for the workout id in the wrong field, and it was fixed from what the queue
actually held.

`update_workout` too: a test workout laid out like the editor's (warm-ups,
then a circuit in pounds) was scheduled, then updated (rounds, a weight, a
rest, an added cool-down). The read-back matched, it kept its id, and the
calendar entry still pointed at it. Garmin accepts the freshly built
definition without its own step ids. `test_real_workouts_untouched` then
confirmed every non-test workout matched its snapshot from before the
first write.

Reads:

- Session restore from the token store works, and the file is 0600.
- Calendar workout entries are `itemType: "workout"` with `id`, `date`,
  `workoutId`, `title` and `sportTypeKey`. Other item types seen (`weight`,
  `wellnessActivity`) are ignored. The single-entry detail has
  `calendarDate` and a nested `workout`.
- The device queue wraps its entries in `messages`. Each has a
  `messageId`, a top-level `deviceId`, `messageType` (`workouts`,
  `schedules`, `software-update`), and the workout id in
  `metaData.metaDataId`. Devices have
  `displayName`, `productDisplayName`, `deviceTypeSimpleName`, and the
  capability flags used above.
- The daily summary fields used (`totalSteps`, `restingHeartRate`,
  `averageStressLevel`, body battery, active calories, intensity minutes)
  are all present.
- Editor-made strength workouts (bare warm-up steps, a repeat of four
  exercises with a rest after each, weights in pounds) decode, and decode
  to the same thing after being re-encoded. So does a running workout with
  step notes.

## Not yet verified

- **Updating a workout made in Garmin's editor.** Only test workouts have
  been updated. Your own decode and re-encode identically (above), but the
  PUT replaces the whole workout, so try a first real edit on a copy.
- **Recorded activities:** the account had none yet, so the activity
  summary fields and how activities appear on the calendar (assumed
  `itemType: "activity"`) are unchecked.
- **Sleep, HRV, training readiness score, training status, VO2 max:** no
  history yet, so only their empty form has been seen. A wrong field name
  shows up as a metric that's always null, not as an error.
- **Kilogram weights in workouts made elsewhere** (`unitKey: "kilogram"`)
  are read as kilograms, the same way pounds are read. A real one hasn't
  been seen.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `authentication: No saved Garmin session` | Run `garmin-mcp-login`, then restart the server. |
| `authentication: Garmin didn't accept the saved session` | The session was revoked or expired. Same fix. |
| `rate_limit` | Garmin is throttling the account, often after many sign-ins. Wait several minutes; don't loop. |
| `network` | Garmin Connect is unreachable. Reads can be retried later. |
| A write says `unknown` | Don't retry. Check `get_workout` / `get_workout_calendar` or Garmin Connect, and look at `operations.jsonl`. |
| `approval: … doesn't match` | The arguments or Garmin changed since the preview, or the server restarted. Preview again. |
| Writes always come back `dry_run` | `GARMIN_DRY_RUN` is still true. Set it to false and restart. |
| A metric is always null | Garmin has no data for it on that day (watch not worn, metric unsupported on the device), or the normalizer's field names are off (see above). |
| Anything else | `$GARMIN_STATE_DIR/garmin-mcp.log`. It logs tool names, outcomes and timings, but never arguments, results or tokens. |

## Upgrading python-garminconnect

It's pinned exactly (`garminconnect[workout]==0.3.19`) because its request
shapes and auth flow change between minor releases. To upgrade, read its
changelog, bump the pin, run `uv lock`, run the unit tests, and then run
the live tests before relying on it. Everything that touches the library
is in `src/garmin_mcp/repositories/` and `auth.py`.

## Layout

```
src/garmin_mcp/
  server.py             tools: arguments, annotations, error categories
  config.py             settings
  auth.py, login.py     session restore; the interactive sign-in command
  approval.py           confirmation tokens
  oplog.py              the write log
  errors.py             error categories
  schemas/              canonical workouts, calendar, results
  services/             activities, health, workouts, calendar, devices, writes
  repositories/         the only code importing garminconnect: the client
                        wrapper, failure classification, the workout codec,
                        the exercise catalog
```

Remote access over streamable HTTP isn't implemented. It would need real
authentication and authorization in front of the account first.
