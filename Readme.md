# ups_monitor

Polls one or more UPS units - anything supported by [NUT](https://networkupstools.org/)
(Network UPS Tools), regardless of brand/model - for their `ups.status`,
and sends an email + SMS alert whenever a UPS's status **changes**: power
lost, power restored, low battery, driver/communication problems, etc.
Built for monitoring several UPS units on one host (no automatic
shutdown/power-management - status alerting only).

Runs continuously as a systemd service, auto-restarting on crash or hang
(systemd watchdog), and reloads its own configuration on every poll cycle
so numbers/addresses/templates can be edited live without a restart.

## How it works

- Every `poll_interval_seconds`, the script runs `upsc <name>@<host>
  ups.status` for each configured UPS.
- If a UPS's *core* status (OL/OB/LB/...) differs from what it was last
  poll, an email and an SMS are sent. CHRG/DISCHRG toggling on their own
  (e.g. once a battery finishes recharging) doesn't count as a core change
  and doesn't alert by itself.
- The full lifecycle of a real outage is tracked in 4 steps, and
  `state.json` (see below) remembers exactly where in that lifecycle each
  UPS currently is, across restarts:
  1. `OL` - mains fine, no message.
  2. `OB` - mains lost -> **SMS + email**.
  3. `OB` -> `OL`/`OL CHRG` - mains back, battery now charging ->
     **SMS + email** (a `recovery_*` template, reporting the true outage
     duration).
  4. `OL CHRG` -> `OL` - battery finished recharging -> **email only**
     (a `charge_complete_*` template) - only fires if step 3 actually
     happened first, so routine float-charge maintenance unrelated to a
     real outage never sends this.
- If `upsc` itself fails (driver not connected, `upsd` unreachable), the
  status is treated as `UNKNOWN` and alerted on too - so the monitor also
  catches its own monitoring chain breaking, not just real power events. A
  transient `UNKNOWN` (e.g. right after a reboot, before `upsd`/drivers are
  ready) can't corrupt an in-progress outage's recorded start time or
  duration - see `outage_start` in the state schema below.
- Some UPS/driver combinations are known to misreport `ups.status` as `OB`
  even though mains power is fine (a HID-mapping quirk in certain NUT
  subdrivers, not a real outage). Per-UPS `voltage_fallback: true` works
  around this by reading `input.voltage` directly and using it as the
  authoritative OL/OB signal whenever it's readable, falling back to the
  driver's own `ups.status` only when voltage can't be read at all - see
  `get_effective_status()` in `ups_monitor.py`.
- All wording is template-driven from `config.yaml` - no code changes
  needed to reword messages, add UPS units, or change recipients.

## Requirements

- NUT installed and configured (`upsc` on `PATH`, `upsd` reachable -
  typically `localhost:3493`). This script only *reads* UPS status over
  NUT's TCP protocol; it does not touch USB devices or NUT driver sockets
  directly, so it needs no special group membership for that.
- Python 3
- Python packages:
  ```
  pip3 install pyyaml requests sdnotify --break-system-packages
  ```
  `sdnotify` is optional - if it's not installed the script keeps working,
  it just skips systemd watchdog pings (see [Watchdog](#watchdog) below).
- An SMTP server/account for outbound email.
- Access to an SMS gateway. This project ships wired up for the
  [Setera](https://setera.com) SMSGW (`POST
  https://sms-gw.setera.com:8040/sms`, JSON body, `token` header) - see
  `send_sms()` in `ups_monitor.py` if you need to adapt it to a different
  provider.

## Files

| File                    | Purpose                                                            |
|-------------------------|---------------------------------------------------------------------|
| `ups_monitor.py`        | The monitor itself                                                 |
| `config.yaml`           | All configuration: UPS list, email/SMS settings, message templates |
| `config.example.yaml`   | Sanitized template for `config.yaml` - copy and fill in real values |
| `ups-monitor.service`   | systemd unit for running it as a background service                 |
| `ups-monitor.logrotate` | logrotate config for the monitor's own log file (see below)        |
| `ups-status`            | Optional one-line wrapper for `--status` (see below)               |
| `reset_since.py`        | Optional one-off utility to reset `state.json`'s "since" timestamps (see below) |

## Configuration

Everything that varies lives in `config.yaml` - see the comments in that
file (or `config.example.yaml`) for the full picture. In short:

- `upses`: list of UPS units to watch (`name` must match the section name
  in NUT's `ups.conf`, `host` is usually `localhost`). Add/remove entries
  freely - no code changes needed when more UPS units arrive.
  Optional per-UPS `voltage_fallback: true` (+ optional
  `voltage_ob_threshold`) for units with the OL/OB-misreporting quirk
  described above.
- `email` / `sms`: SMTP and SMS gateway settings, and recipient lists.
- `status_descriptions`: maps NUT's status codes (`OL`, `OB`, `LB`, ...) to
  human-readable text - edit freely, any language, to keep alerts
  non-technical.
- `messages`: email subject/body and SMS text templates, with
  placeholders like `{ups_name}`, `{status_description}`, `{duration}` -
  see the comments in `config.yaml` for the full placeholder list.
  Separate `recovery_*` templates are used for the "power restored" step,
  and `charge_complete_*` templates (email only) for the "battery fully
  recharged" step - see "How it works" above.
- `poll_interval_seconds`, `state_file`, `log_file`,
  `voltage_ob_threshold_default`: operational settings.

`config.yaml` is re-read on every poll cycle, so editing phone numbers,
email addresses, UPS units, or message wording takes effect on the next
poll without restarting the service. If the file is briefly invalid
mid-edit, the monitor logs a warning and keeps using the last good config
instead of crashing.

**Note:** `config.yaml` contains credentials (SMTP password, SMS API
token) - keep its permissions restricted (see [Running as a dedicated
user](#running-as-a-dedicated-user) below), and don't commit a filled-in
copy to a public repo. Commit `config.example.yaml` instead (already in
this repo) and keep `config.yaml` in `.gitignore`.

### State file (`state.json`)

Tracks, per UPS: its last known status, when that status last changed
(`since`), whether an outage is currently in progress (`outage_start`),
the most recently completed outage (`last_outage`: start/end/duration),
and whether a `charge_complete_*` email is still pending after a recovery
(`charge_recovery_pending`). Written atomically (temp file + rename), so a
crash or power loss mid-write can never leave it half-written/corrupted -
worth knowing since this file is exactly what needs to survive a real
outage that outlasts this machine's own UPS and takes it down mid-poll.

**`reset_since.py`** (included) is a one-off utility for the one case you
might legitimately want to hand-edit this file: after a server
reboot/migration, `--status`'s "time in current status" column keeps
counting from before the reboot (correctly - the status itself didn't
actually change, so there's nothing for the monitor to have alerted on),
which can read as misleadingly stale right after you've just brought
things back up. Running it resets every UPS's `since` to "now" (status,
`last_outage`, etc. are left untouched):

```bash
sudo systemctl stop ups-monitor.service   # must be stopped first - see below
sudo -u ups_monitor python3 reset_since.py /var/lib/ups_monitor/state.json
sudo systemctl start ups-monitor.service
```

The service **must be stopped first**: it keeps its own copy of
`state.json` in memory and only writes it back out on a real event, so
editing the file while the service is running risks having your edit
silently overwritten the next time it does write.

### Checking current status (`--status`)

```bash
python3 ups_monitor.py --config config.yaml --status
```

Prints a table of every configured UPS: current status (with its
human-readable description), how long it's been in that status, and its
last recorded outage (start time + duration), if any. Reads only
`state.json` - no live NUT/`upsc` queries, no changes made - so it's
instant and works even if `upsd` is currently unreachable.

**`ups-status`** (included) is a one-line wrapper for the command above,
so you don't have to type the full `sudo -u ups_monitor python3
/opt/ups_monitor/... --status` invocation every time:

```bash
sudo cp ups-status /usr/local/bin/ups-status
sudo chmod 755 /usr/local/bin/ups-status
```

After that, just run `ups-status` from anywhere - it still prompts for
*your own* sudo password (needed to run as the `ups_monitor` service
account), same as any other `sudo -u` command.

## Installation

```bash
sudo mkdir -p /opt/ups_monitor
sudo cp ups_monitor.py config.yaml /opt/ups_monitor/
pip3 install pyyaml requests sdnotify --break-system-packages

# edit /opt/ups_monitor/config.yaml: UPS list, SMTP creds, SMS token, recipients
```

### Running as a dedicated user

The service does not need root, and does not need membership in the `nut`
group (it only talks to `upsd` over TCP, never touches USB devices or
NUT's driver sockets directly):

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ups_monitor

sudo mkdir -p /var/lib/ups_monitor
sudo chown ups_monitor:ups_monitor /var/lib/ups_monitor

# Log file lives in its own directory (see "Log rotation" below) so that
# directory - not /var/log itself - can be owned outright by ups_monitor.
sudo mkdir -p /var/log/ups_monitor
sudo chown ups_monitor:ups_monitor /var/log/ups_monitor
sudo chmod 750 /var/log/ups_monitor

sudo chown -R root:ups_monitor /opt/ups_monitor
sudo chmod 750 /opt/ups_monitor
sudo chmod 640 /opt/ups_monitor/config.yaml
```

`ups-monitor.service` already sets `User=ups_monitor` / `Group=ups_monitor`.

### systemd service

```bash
sudo cp ups-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ups-monitor.service
journalctl -u ups-monitor.service -f
```

Restart behaviour: `Restart=always` restarts the service no matter how it
exits (crash, exception, kill, clean exit, or a watchdog timeout - see
below), with `RestartSec=10` between attempts and effectively unlimited
retries (`StartLimitBurst=1000`). Startup ordering: `After=network-online.target
nut-server.service` so it doesn't start polling before NUT itself is up.

### Watchdog

`Type=notify` + `WatchdogSec=60`: the script pings systemd every 10
seconds (`WATCHDOG_PING_INTERVAL` in `ups_monitor.py`), including while
sleeping between polls. If those pings stop for 60 seconds - the process
hung without actually crashing - systemd kills and restarts it. Requires
the `sdnotify` Python package; if it isn't installed, the script falls
back to doing nothing for watchdog pings, so remove `WatchdogSec` from the
service file in that case (otherwise systemd will restart the service
every 60s for no reason).

### Log rotation

The log file (`log_file` in `config.yaml`, e.g.
`/var/log/ups_monitor/ups_monitor.log`) grows forever on its own - the
script has no built-in rotation, so install the included
`ups-monitor.logrotate`:

```bash
sudo cp ups-monitor.logrotate /etc/logrotate.d/ups-monitor
sudo chmod 644 /etc/logrotate.d/ups-monitor
```

Two things about it are worth understanding rather than just copying
blindly:

- **`copytruncate`**, not the default rename-and-signal approach: the
  script holds its log file open for the life of the process via a plain
  `logging.FileHandler`, with no signal handler to make it reopen the
  file. `copytruncate` copies the current content aside and truncates the
  original in place, so the already-open file descriptor just keeps
  writing to the same (now empty) file - no service restart needed after
  a rotation.
- **`su ups_monitor ups_monitor`**: logrotate normally runs as root, but
  the log file's directory (`/var/log/ups_monitor/`, per the "Running as a
  dedicated user" step above) is owned by the `ups_monitor` service
  account, not root, and isn't world/group-writable - which logrotate's
  own security check refuses to rotate into without this directive
  (`error: ... insecure permissions`) since it would otherwise need to
  create the rotated file in a directory root doesn't have free rein
  over. `su` tells logrotate to do the actual read/write/create work as
  `ups_monitor` instead, which does own that directory.

Also included: `weekly` rotation, `rotate 8` (about 2 months of history),
`compress` with `delaycompress` (the most recently rotated file, `.1`, is
left uncompressed for one more cycle so it's still easy to `grep` without
decompressing first - only `.2.gz` onward are actually `.gz`),
`missingok`/`notifempty` so a quiet period or a not-yet-existing log file
isn't treated as an error.

Test it any time without waiting for the weekly schedule:

```bash
sudo logrotate -d /etc/logrotate.d/ups-monitor   # dry run - shows what would happen
sudo logrotate -f /etc/logrotate.d/ups-monitor   # force an actual rotation now
ls -la /var/log/ups_monitor/
```

## Testing

```bash
# One poll cycle, then exit (doesn't loop or sleep)
python3 ups_monitor.py --config config.yaml --once

# Send a real test SMS to every number in config.yaml, then exit
python3 ups_monitor.py --config config.yaml --test-sms

# Send a real test email to every address in config.yaml, then exit
python3 ups_monitor.py --config config.yaml --test-email

# Print current status (see "Checking current status" above)
python3 ups_monitor.py --config config.yaml --status
```

`--test-sms` / `--test-email` send even if `enabled: false` is set in
`config.yaml` (with a warning logged) - the point of the flag is to check
the gateway/SMTP connection works.

## Troubleshooting

- **`upsc` errors / `UNKNOWN` status**: check `upsc <name>@localhost
  ups.status` manually, and that `upsd`/the NUT driver for that UPS is
  running (`upsdrvctl start`, `systemctl status nut-server`).
- **A UPS's status seems to flip to `OB` for no reason, but mains power is
  fine and `input.voltage` reads normal**: this is the known HID-mapping
  quirk described above - set `voltage_fallback: true` for that UPS in
  `config.yaml` rather than treating it as a wiring/power problem.
- **SMS not arriving**: run `--test-sms` and check the log - `send_sms()`
  logs the HTTP status and the gateway's response body, which usually
  shows the exact problem (bad token, wrong sender ID, etc.).
- **Email not arriving**: run `--test-email` and check the log for the
  SMTP error.
- **Service won't start / gets restarted every 60s**: if `sdnotify` isn't
  installed, remove `WatchdogSec=60` (and ideally `Type=notify` ->
  `Type=simple`) from `ups-monitor.service`, since nothing will be sending
  the watchdog pings it's waiting for.
- **`logrotate` refuses with "insecure permissions"**: make sure the log
  directory is owned by `ups_monitor` (see "Running as a dedicated user")
  and that `ups-monitor.logrotate` includes the `su ups_monitor
  ups_monitor` line - see "Log rotation" above.

## Changelog / errata

Notes on what changed since this was first put together, in case you're
working from an older copy:

- **Message templates became fully configurable**, with placeholders
  (`{ups_name}`, `{status_description}`, `{duration}`, `{timestamp}`,
  ...) instead of hardcoded English text, plus separate `recovery_*`
  templates for the "power restored" transition.
- **`status_descriptions` added**: every NUT status code can be mapped to
  non-technical, any-language text instead of showing raw codes like `OB`
  in alerts.
- **CHRG/DISCHRG-only changes stopped alerting on their own**
  (`IGNORED_STATUS_FLAGS`) - previously, a battery finishing its
  post-outage recharge (`OL CHRG` -> `OL`) sent a spurious duplicate
  alert; now only a real core status change (OL/OB/LB/...) does.
- **`{timestamp}` added** (always UTC, so it's unambiguous regardless of
  the server's local timezone), later extended to include **seconds**
  (`HH:MM:SS`, not just `HH:MM`) once poll intervals got short enough
  (2-3s) that minute resolution stopped being enough to tell events apart.
- **`voltage_fallback` added** to work around a real HID-driver quirk
  found on some UPS models/subdrivers: `ups.status` reporting `OB` while
  mains power and `input.voltage` were both completely normal. The final
  logic deliberately prefers `input.voltage` over the driver's own
  `ups.status` whenever voltage can be read, falling back to the driver
  only when it can't (a naive "OB if EITHER signal says so" version was
  tried first and rejected - it would have kept trusting the driver's
  wrong `OB` claims and defeated the whole fix).
- **The outage lifecycle grew from 2 steps to 4**: a `charge_complete_*`
  email-only notification was added for when a battery finishes
  recharging after a *real, tracked* outage (as opposed to routine
  float-charge maintenance, which correctly stays silent). This needed
  new persisted state (`outage_start`, `last_outage`,
  `charge_recovery_pending` in `state.json`).
- **Outage tracking made robust against a transient `UNKNOWN` status**
  (e.g. right after a reboot, before `upsd`/drivers finish starting up):
  earlier logic treated any "OB stopped appearing" as a recovery and any
  "OB appeared" as a new outage, which a mid-outage `UNKNOWN` blip could
  use to reset or truncate the recorded outage duration right when
  accuracy matters most (a long outage that outlasts this machine's own
  UPS). It's now driven off `outage_start` specifically, which only a
  genuine `OL` (for recovery) or a genuine fresh `OB` (for a new outage)
  is allowed to touch.
- **`state.json` writes made atomic** (write to a temp file, then
  `os.replace()`) for the same reason - a hard crash mid-write used to
  risk a corrupted state file, which would silently discard all history
  (every UPS treated as "first check ever" on the next boot).
- **`--status` CLI flag added**: prints current status, time-in-status,
  and last recorded outage for every UPS, read entirely from `state.json`
  (no live NUT queries).
- **Periodic charging-progress log lines added** (throttled, at most once
  per `CHARGE_PROGRESS_LOG_INTERVAL_SECONDS`): `battery.charge`/`runtime`
  get logged while a UPS is actively charging, for visibility into how a
  post-outage recharge is progressing without needing `--status` or
  `upsc`.
- **Log file moved into its own directory** (`/var/log/ups_monitor/`
  rather than directly in `/var/log/`), and **logrotate config added**
  (`ups-monitor.logrotate`) - see "Log rotation" above for why the
  directory move was necessary (logrotate's `su` directive needs a
  directory the service account actually owns).
