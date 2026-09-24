#!/usr/bin/env python3
"""
ups_monitor.py

Polls one or more NUT (Network UPS Tools) UPS units for their ups.status
value, and sends an email + SMS notification whenever a UPS's status
CHANGES (e.g. OL -> OB when power is lost, OB -> LB when the battery gets
low, OB -> OL when power is restored).

Everything that varies (which UPS units, who gets emailed, which phone
numbers get SMS'd, SMTP/SMS gateway credentials) lives in config.yaml next
to this script. You should not need to edit this .py file to add a UPS,
change an email address, or change a phone number - just edit config.yaml.

Requirements:
    pip3 install pyyaml requests sdnotify --break-system-packages

Requires the NUT client tools to be installed (the "upsc" command must be
on PATH) and a running upsd that this machine can reach.

Run manually for testing:
    python3 ups_monitor.py --config config.yaml

Run continuously, auto-restarting on any crash, via the accompanying
ups-monitor.service systemd unit file. See its comments for details;
in short:

    sudo mkdir -p /opt/ups_monitor
    sudo cp ups_monitor.py config.yaml /opt/ups_monitor/
    sudo cp ups-monitor.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ups-monitor.service
    journalctl -u ups-monitor.service -f
--------------------------------------------------------------------------
"""

import argparse
import json
import logging
import os
import smtplib
import subprocess
import sys
import time
from email.mime.text import MIMEText
from email.utils import formatdate

import requests
import yaml

try:
    import sdnotify
except ImportError:
    sdnotify = None

# Fixed cadence for systemd watchdog pings, independent of poll_interval_seconds.
# This lets a long poll_interval (e.g. 5 minutes) still ping the watchdog often
# enough that ups-monitor.service's WatchdogSec (set much higher than this)
# never trips just because polling itself is slow between pings.
WATCHDOG_PING_INTERVAL = 10


class _NullNotifier:
    """Used when sdnotify isn't installed, or when not running under systemd
    (e.g. running the script by hand for testing) - notify() becomes a no-op
    instead of erroring out."""

    def notify(self, _msg):
        pass

# Status codes that NUT can report in ups.status (space-separated, a UPS
# can have more than one at once, e.g. "OB LB"). Used only to make alert
# messages friendlier - any status is still detected and reported even if
# it's not in this dict.
STATUS_DESCRIPTIONS = {
    "OL": "On line (mains power OK)",
    "OB": "On battery (mains power LOST)",
    "LB": "Low battery",
    "HB": "High battery",
    "RB": "Battery needs replacing",
    "CHRG": "Battery charging",
    "DISCHRG": "Battery discharging",
    "BYPASS": "On bypass",
    "CAL": "Runtime calibration in progress",
    "OFF": "UPS is off",
    "OVER": "Overloaded",
    "TRIM": "Trimming incoming voltage",
    "BOOST": "Boosting incoming voltage",
    "FSD": "Forced shutdown in progress",
    "ALARM": "Alarm condition",
    "UNKNOWN": "Could not read status (driver/communication problem)",
}

# These flags are ignored when deciding whether a UPS's status "changed"
# enough to alert on. CHRG/DISCHRG toggle on their own after every OB->OL
# recovery (the battery recharges, then the flag drops off again once full)
# - that's expected and not worth a separate alert, since "power is back, the
# battery will now recharge" is implied by the recovery message itself. A
# change is still detected (and alerted on) normally if the core status
# (OL/OB/LB/...) changes, even if CHRG/DISCHRG also happens to change at the
# same time.
IGNORED_STATUS_FLAGS = {"CHRG", "DISCHRG"}


def core_status(status):
    """Strips IGNORED_STATUS_FLAGS out of a ups.status string for CHANGE
    DETECTION purposes only, e.g. 'OL CHRG' -> 'OL', 'OB DISCHRG' -> 'OB'.
    Falls back to the original string if that would leave nothing (e.g. a
    status that is only "CHRG" with nothing else, which shouldn't normally
    happen but must not produce an empty comparison key)."""
    if status is None:
        return None
    parts = [p for p in status.split() if p not in IGNORED_STATUS_FLAGS]
    return " ".join(parts) if parts else status


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_file):
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        # If we can't write the log file (e.g. permissions), just log to
        # stdout - don't let logging setup crash the monitor.
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def load_state(state_file):
    """
    State is stored per UPS, e.g.:

        {"NB2A-L1": {
            "status": "OL CHRG",           # last known (effective) ups.status
            "since": 1757570000.1,         # unix time it last changed OL<->OB
            "outage_start": null,          # unix time current OB started (or null if OL)
            "charge_recovery_pending": true,  # waiting for CHRG to clear after a recovery?
            "last_outage": {               # most recent completed outage, or absent if none yet
                "start": 1757570000.1,
                "end": 1757570532.4,
                "duration_seconds": 532.3
            }
        }}

    "since" is what lets us report "was on battery for 5 min 32 s" when a
    UPS comes back on line. "last_outage"/"charge_recovery_pending" drive
    the step-3->4 "battery fully recharged" email and the `--status` CLI
    output - see main()'s poll loop and notify_charge_complete().
    """
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state_file, state):
    """
    Writes state.json ATOMICALLY: the new content goes to a temp file in
    the same directory first, then os.replace() swaps it into place in one
    filesystem operation. This matters specifically for surviving a hard
    power-loss-triggered crash/reboot (the scenario this whole file exists
    to track): a plain open(..., "w") truncates the file before writing,
    so a crash mid-write can leave state.json half-written/corrupt - and
    load_state() would then have to discard ALL history (every UPS treated
    as "first check ever" on the next boot, losing last_outage etc. right
    when you'd want it most). With os.replace(), the old, intact file is
    what's on disk right up until the new one is fully written - a crash
    at any point leaves either the complete old version or the complete
    new version, never a corrupt partial one.
    """
    try:
        directory = os.path.dirname(state_file) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{state_file}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_file)
    except OSError as exc:
        logging.warning("Could not write state file %s: %s", state_file, exc)


def get_ups_status(name, host):
    """
    Runs `upsc <name>@<host> ups.status` and returns the raw status string
    (e.g. "OL", "OB LB"), or "UNKNOWN" if upsc failed (driver not
    connected, upsd unreachable, etc.) - which is itself worth alerting on.
    """
    target = f"{name}@{host}"
    try:
        result = subprocess.run(
            ["upsc", target, "ups.status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logging.warning(
                "upsc failed for %s: %s", target, result.stderr.strip()
            )
            return "UNKNOWN"
        status = result.stdout.strip()
        return status if status else "UNKNOWN"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logging.warning("upsc error for %s: %s", target, exc)
        return "UNKNOWN"


# Some UPS/driver combinations intermittently (or permanently) fail to
# report a specific NUT variable (e.g. input.voltage) even though ups.status
# itself reads fine - see get_ups_value()/get_effective_status(). Rather
# than logging a WARNING every single poll (which floods the log at short
# poll_interval_seconds), each UPS+variable combination is only warned
# about again after this many seconds - it still shows up regularly, just
# not thousands of times an hour.
VOLTAGE_FALLBACK_WARNING_THROTTLE_SECONDS = 300
_last_voltage_fallback_warning = {}

# How often (seconds) to write a "still charging" progress line to the log
# for a UPS whose ups.status includes CHRG - lets you see the battery
# recharging over time in the log without flooding it every poll.
CHARGE_PROGRESS_LOG_INTERVAL_SECONDS = 300
_last_charge_progress_log = {}


def _throttled_warning(key, message, *args):
    """logging.warning(), but at most once every
    VOLTAGE_FALLBACK_WARNING_THROTTLE_SECONDS for the same "key" (normally
    a UPS name) - used so a persistent condition doesn't flood the log at
    short poll_interval_seconds."""
    now = time.time()
    last = _last_voltage_fallback_warning.get(key, 0)
    if now - last >= VOLTAGE_FALLBACK_WARNING_THROTTLE_SECONDS:
        logging.warning(message, *args)
        _last_voltage_fallback_warning[key] = now


def get_ups_value(name, host, varname):
    """
    Runs `upsc <name>@<host> <varname>` for an arbitrary NUT variable (e.g.
    "input.voltage"). Returns (value, None) on success, or (None, reason)
    if upsc failed - "reason" is upsc's own stderr text (or the Python
    exception) so callers can log/diagnose WHY, instead of just "it didn't
    work".
    """
    target = f"{name}@{host}"
    try:
        result = subprocess.run(
            ["upsc", target, varname],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None, (result.stderr.strip() or f"upsc exited {result.returncode}")
        value = result.stdout.strip()
        return (value, None) if value else (None, "empty response")
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return None, str(exc)


def get_effective_status(ups, config):
    """
    Returns the ups.status string to actually use for this UPS - normally
    just get_ups_status() as-is.

    Some UPS models/driver combinations (seen so far: Innova City/Unity
    3kVA units on the "Phoenixtec/Liebert HID" NUT subdriver) report OB
    (on battery) via ups.status even though mains power is present and
    input.voltage reads perfectly normal - a driver/HID-mapping quirk for
    that hardware, not a real outage. For any UPS with
    "voltage_fallback: true" set in config.yaml, this is a "belt and
    suspenders" cross-check with two independent signals:

        effective OB = input.voltage is readable AND below threshold,
                        if input.voltage IS readable this poll (the "belt" -
                        the accurate signal, and what actually fixes the
                        known driver bug)

                        otherwise: whatever the driver's own ups.status
                        says (the "suspenders" - a fallback for when the
                        belt isn't there, better than blindly assuming OL)

    IMPORTANT: this is deliberately NOT "OB if EITHER signal says OB" - that
    would defeat the whole point, since it would keep trusting the driver's
    OB claim even when voltage proves it wrong (that's the exact bug this
    exists to fix: driver said bare "OB" while input.voltage read a
    perfectly normal ~238V). Voltage wins whenever we have it; the driver's
    own status is only consulted as a fallback when voltage can't be read
    at all. Any other flags in ups.status (LB, RB, ALARM, CHRG, ...) are
    passed through unchanged either way.

    On this same hardware, input.voltage is ALSO known to become
    unreadable via HID specifically while CHRG (battery charging) is
    active - even though mains is fine and the driver correctly says OL.
    That's the common, harmless case where we fall back to the driver (and
    it agrees: OL) - no warning logged. The one case that DOES get a
    (throttled) warning is voltage being unreadable while the driver claims
    OB - there we can't independently confirm or deny that claim, so we
    fall back to trusting it (safer to over-alert on a possible real
    outage than suppress one), but you're told this happened since it
    means the cross-check isn't doing its job for that poll.
    """
    name = ups["name"]
    host = ups.get("host", "localhost")
    raw_status = get_ups_status(name, host)

    if not ups.get("voltage_fallback", False):
        return raw_status

    parts = raw_status.split()
    driver_says_ob = "OB" in parts

    voltage_str, err = get_ups_value(name, host, "input.voltage")
    voltage = None
    if voltage_str is not None:
        try:
            voltage = float(voltage_str)
        except ValueError:
            err = f"input.voltage value {voltage_str!r} is not a number"

    if voltage is None:
        # No independent signal available this poll - fall back to
        # trusting the driver's own call (the "suspenders").
        if driver_says_ob:
            _throttled_warning(
                name,
                "%s: ups.status claims OB but input.voltage could not be verified "
                "(%s) - trusting raw ups.status (%s) as-is",
                name, err, raw_status,
            )
        return raw_status

    # Voltage IS readable this poll - it's authoritative (the "belt"),
    # regardless of what the driver itself claims.
    threshold = ups.get(
        "voltage_ob_threshold",
        config.get("voltage_ob_threshold_default", 180),
    )
    effective_ob = voltage < threshold

    other_parts = [p for p in parts if p not in ("OL", "OB")]
    corrected_status = " ".join([("OB" if effective_ob else "OL")] + other_parts)

    if effective_ob != driver_says_ob:
        logging.debug(
            "%s: voltage_fallback overriding driver's ups.status ('%s') -> '%s' "
            "(input.voltage=%.1fV, threshold=%.0fV)",
            name, raw_status, corrected_status, voltage, threshold,
        )
    return corrected_status


def log_charge_progress(name, host):
    """
    Writes one INFO log line with the current battery.charge (and
    battery.runtime, if available) for a UPS that's currently charging -
    throttled to CHARGE_PROGRESS_LOG_INTERVAL_SECONDS so it doesn't flood
    the log at short poll_interval_seconds. Call this every poll while
    CHRG is in the UPS's status; it no-ops on its own between intervals.
    """
    now = time.time()
    last = _last_charge_progress_log.get(name, 0)
    if now - last < CHARGE_PROGRESS_LOG_INTERVAL_SECONDS:
        return
    _last_charge_progress_log[name] = now

    charge_str, _ = get_ups_value(name, host, "battery.charge")
    runtime_str, _ = get_ups_value(name, host, "battery.runtime")

    if charge_str is None:
        logging.debug("%s: charging, but battery.charge could not be read", name)
        return

    message = f"{name}: charging - battery.charge {charge_str}%"
    if runtime_str is not None:
        try:
            message += f" (est. runtime {int(float(runtime_str)) // 60} min)"
        except ValueError:
            pass
    logging.info(message)


def describe_status(status, descriptions=None):
    """
    Turns e.g. 'OB LB' into 'Sähkökatko - laite akkuvirralla; Akku vähissä'
    (or the built-in English default if config.yaml doesn't define its own
    status_descriptions). Any code not found in either falls back to just
    printing the raw code, e.g. an unlisted "XYZ".
    """
    if descriptions is None:
        descriptions = STATUS_DESCRIPTIONS
    parts = status.split()
    described = [descriptions.get(p, p) for p in parts]
    return "; ".join(described)


def format_duration(seconds):
    """Turns e.g. 332 into '5 min 32 s'. Returns 'unknown' if seconds is None
    (happens on the very first check, when there's no previous timestamp)."""
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)


def is_power_restored(old_status, new_status):
    """True when this transition means mains power came back - i.e. the
    previous status included OB (on battery) and the new one is on line."""
    if not old_status:
        return False
    return "OB" in old_status.split() and "OL" in new_status.split()


def send_email(email_cfg, subject, body):
    if not email_cfg.get("enabled", False):
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = ", ".join(email_cfg["to_addrs"])
    msg["Date"] = formatdate(localtime=True)

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=15) as server:
            if email_cfg.get("use_tls", True):
                server.starttls()
            if email_cfg.get("username"):
                server.login(email_cfg["username"], email_cfg["password"])
            server.sendmail(email_cfg["from_addr"], email_cfg["to_addrs"], msg.as_string())
        logging.info("Email sent: %s", subject)
    except Exception as exc:  # noqa: BLE001 - we want to log and keep going, not crash
        logging.error("Failed to send email: %s", exc)


def send_sms(sms_cfg, message):
    """
    Sends the SMS via an HTTP SMS gateway (gateway_url in config.yaml).
    Ships wired up for a gateway that expects:

        POST <gateway_url>
        Headers: token: <api_token>, Content-Type: application/json
        Body:    {"to": "<number, E.164 no '+'>", "from": "<sender_id>",
                  "content": "<message text>" [, "external_id": "..."]}

    Adjust the request below if your provider's API differs (a different
    auth header, body shape, etc.) - everything provider-specific is
    isolated to this one function.

    Sends one request per destination number in sms_cfg["to_numbers"].
    """
    if not sms_cfg.get("enabled", False):
        return

    url = sms_cfg["gateway_url"]
    headers = {
        "token": sms_cfg["api_token"],
        "Content-Type": "application/json",
    }
    external_id_prefix = sms_cfg.get("external_id_prefix", "")

    for number in sms_cfg.get("to_numbers", []):
        # This gateway wants E.164 without the leading "+" - adjust if
        # yours expects the "+" kept.
        clean_number = number.lstrip("+")

        body = {
            "to": clean_number,
            "from": sms_cfg["sender_id"],
            "content": message,
        }
        if external_id_prefix:
            # Unique-enough ID: prefix + timestamp, so repeated alerts don't collide.
            body["external_id"] = f"{external_id_prefix}-{int(time.time())}-{clean_number}"

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=15)
            resp.raise_for_status()
            logging.info("SMS sent to %s (HTTP %s): %s", clean_number, resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            logging.error("Failed to send SMS to %s: %s", clean_number, exc)


class _SafeDict(dict):
    """Used with str.format_map() so a template that references a variable
    which doesn't exist prints '{that_name}' literally instead of crashing
    the whole monitor with a KeyError - a typo in config.yaml then shows up
    plainly in the message instead of silently killing notifications."""

    def __missing__(self, key):
        return "{" + key + "}"


def render(template, template_vars):
    return str(template).format_map(_SafeDict(template_vars))


def notify(config, ups_name, description, old_status, new_status, duration_seconds):
    """
    Builds and sends the email + SMS for one UPS status change, using the
    templates in config["messages"] (see config.yaml for the full list of
    placeholders and comments).

    duration_seconds is how long the UPS was in old_status before changing
    to new_status - available as {duration} in templates. When the
    transition is specifically "power restored" (was OB, now OL), the
    "recovery_*" templates are used instead of the normal ones, if present,
    so you can call out the outage duration explicitly.
    """
    # User-supplied status_descriptions in config.yaml override the built-in
    # English defaults; any code the user didn't list still falls back to
    # the English default rather than showing a bare raw code.
    descriptions = {**STATUS_DESCRIPTIONS, **config.get("status_descriptions", {})}

    # Always UTC, regardless of the server's local timezone - avoids any
    # ambiguity about which timezone a message's {timestamp} is in.
    now_str = time.strftime("%d.%m.%Y %H:%M:%S UTC", time.gmtime())
    template_vars = {
        "ups_name": ups_name,
        "description": description,
        "old_status": old_status or "UNKNOWN",
        "new_status": new_status,
        "status_description": describe_status(new_status, descriptions),
        "old_status_description": describe_status(old_status, descriptions) if old_status else "unknown (first check)",
        "duration": format_duration(duration_seconds),
        "timestamp": now_str,
    }

    messages_cfg = config.get("messages", {})
    email_templates = messages_cfg.get("email", {})
    sms_templates = messages_cfg.get("sms", {})

    recovery = is_power_restored(old_status, new_status)

    subject_tpl = (recovery and email_templates.get("recovery_subject")) or email_templates.get(
        "subject", "[UPS ALERT] {ups_name}: {old_status} -> {new_status}"
    )
    body_tpl = (recovery and email_templates.get("recovery_body")) or email_templates.get(
        "body",
        "UPS: {ups_name} ({description})\n"
        "Previous status: {old_status}\n"
        "New status: {new_status}\n"
        "Details: {status_description}\n"
        "Time: {timestamp}\n",
    )
    sms_tpl = (recovery and sms_templates.get("recovery_text")) or sms_templates.get(
        "text", "UPS {ups_name}: {new_status} - {status_description}"
    )

    subject = render(subject_tpl, template_vars)
    body = render(body_tpl, template_vars)
    sms_text = render(sms_tpl, template_vars)

    logging.info(
        "STATUS CHANGE %s: %s -> %s (was in previous status for %s)%s",
        ups_name, old_status, new_status, format_duration(duration_seconds),
        " [power restored]" if recovery else "",
    )
    send_email(config["email"], subject, body)
    send_sms(config["sms"], sms_text)


def notify_charge_complete(config, ups_name, description, last_outage):
    """
    Sends the EMAIL-ONLY (no SMS) follow-up for the 4th step of an outage
    cycle: OL+CHRG -> OL (battery finished recharging after a real outage).
    Only called once per outage, right when CHRG clears - see main()'s
    charge_recovery_pending tracking.

    last_outage is the {"start", "end", "duration_seconds"} dict recorded
    when power was restored (see main()) - used for {outage_duration} in
    the template. If for some reason it's missing, {outage_duration} shows
    "unknown" rather than crashing.
    """
    now_str = time.strftime("%d.%m.%Y %H:%M:%S UTC", time.gmtime())
    outage_duration = format_duration(last_outage["duration_seconds"]) if last_outage else None
    template_vars = {
        "ups_name": ups_name,
        "description": description,
        "timestamp": now_str,
        "outage_duration": outage_duration,
    }

    email_templates = config.get("messages", {}).get("email", {})
    subject_tpl = email_templates.get(
        "charge_complete_subject", "[UPS OK] {ups_name}: Battery fully recharged"
    )
    body_tpl = email_templates.get(
        "charge_complete_body",
        "UPS: {ups_name} ({description})\n"
        "Battery is now fully recharged after the last outage.\n"
        "Outage duration was: {outage_duration}\n"
        "Time: {timestamp}\n",
    )

    subject = render(subject_tpl, template_vars)
    body = render(body_tpl, template_vars)

    logging.info(
        "%s: battery fully recharged after outage (outage duration %s)",
        ups_name, outage_duration,
    )
    send_email(config["email"], subject, body)


def sleep_with_watchdog(total_seconds, notifier):
    """Sleeps for total_seconds, but pings the systemd watchdog every
    WATCHDOG_PING_INTERVAL seconds along the way, so a long
    poll_interval_seconds doesn't accidentally trip ups-monitor.service's
    WatchdogSec while we're just waiting for the next poll."""
    remaining = total_seconds
    while remaining > 0:
        chunk = min(WATCHDOG_PING_INTERVAL, remaining)
        time.sleep(chunk)
        remaining -= chunk
        notifier.notify("WATCHDOG=1")


def print_status_table(config, state):
    """
    Prints a plain-text table of every UPS in config.yaml's current known
    status, straight from state.json - no upsc/NUT calls at all, so this
    works even if upsd is unreachable, and is instant regardless of
    poll_interval_seconds. Used by `--status`.
    """
    now = time.time()
    descriptions = {**STATUS_DESCRIPTIONS, **config.get("status_descriptions", {})}

    headers = ("UPS", "Kuvaus", "Tila", "Selite", "Tilassa", "Viimeisin katko")
    rows = [headers]

    for ups in config["upses"]:
        name = ups["name"]
        description = ups.get("description", "")
        entry = state.get(name, {})
        status = entry.get("status")
        since = entry.get("since")

        status_display = status or "ei tietoa"
        status_desc = describe_status(status, descriptions) if status else "-"
        duration = format_duration(now - since) if since is not None else "-"

        last_outage = entry.get("last_outage")
        if last_outage:
            start_str = time.strftime("%d.%m.%Y %H:%M:%S UTC", time.gmtime(last_outage["start"]))
            last_outage_display = f"{start_str} (kesto {format_duration(last_outage['duration_seconds'])})"
        else:
            last_outage_display = "ei tiedossa"

        rows.append((name, description, status_display, status_desc, duration, last_outage_display))

    widths = [max(len(str(row[i])) for row in rows) for i in range(len(headers))]

    def format_row(row):
        return "  ".join(str(value).ljust(width) for value, width in zip(row, widths))

    print(format_row(rows[0]))
    print(format_row(["-" * w for w in widths]))
    for row in rows[1:]:
        print(format_row(row))


def main():
    parser = argparse.ArgumentParser(description="Poll NUT UPS units and alert on status changes.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--once", action="store_true", help="Poll once and exit (for testing)")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current status of every UPS from state.json and exit - reads no upsc/NUT "
        "data live, just the monitor's own last-known state (name, status, how long it's been "
        "in that status, and the last recorded outage). Safe to run anytime, doesn't touch "
        "state.json or send any notifications.",
    )
    parser.add_argument(
        "--test-sms",
        action="store_true",
        help="Send one test SMS to every number in config.yaml's sms.to_numbers, then exit "
        "(doesn't touch UPS polling, state, or email - just checks the SMS gateway works).",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send one test email to every address in config.yaml's email.to_addrs, then exit.",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.status:
        state = load_state(config.get("state_file", "state.json"))
        print_status_table(config, state)
        return

    setup_logging(config.get("log_file", "ups_monitor.log"))

    if args.test_sms:
        sms_cfg = dict(config["sms"])
        if not sms_cfg.get("enabled", False):
            logging.warning("sms.enabled is false in config.yaml - sending the test anyway.")
            sms_cfg["enabled"] = True
        logging.info("Sending test SMS to: %s", ", ".join(sms_cfg.get("to_numbers", [])))
        send_sms(sms_cfg, "Testiviesti ups_monitor-skriptista - jos naet taman, SMS-lahetys toimii.")
        return

    if args.test_email:
        email_cfg = dict(config["email"])
        if not email_cfg.get("enabled", False):
            logging.warning("email.enabled is false in config.yaml - sending the test anyway.")
            email_cfg["enabled"] = True
        logging.info("Sending test email to: %s", ", ".join(email_cfg.get("to_addrs", [])))
        send_email(
            email_cfg,
            "[UPS monitor] Testiviesti",
            "Tama on testiviesti ups_monitor-skriptista.\n"
            "Jos sait taman, sahkopostilahetys on konfiguroitu oikein.",
        )
        return

    state_file = config.get("state_file", "state.json")
    state = load_state(state_file)

    # Talks to systemd's watchdog mechanism (WatchdogSec= in
    # ups-monitor.service). If this process stops pinging - hangs, deadlocks,
    # or otherwise goes unresponsive without actually crashing - systemd
    # notices the missed pings and restarts the service for us. When not
    # running under systemd (e.g. testing by hand), or if the "sdnotify"
    # package isn't installed, this quietly does nothing.
    notifier = sdnotify.SystemdNotifier() if sdnotify else _NullNotifier()

    logging.info("ups_monitor starting, watching %d UPS unit(s)", len(config["upses"]))
    notifier.notify("READY=1")

    while True:
        # Re-read config.yaml every cycle, so adding/removing a phone number,
        # email address, UPS unit, or editing a message template takes effect
        # on the NEXT poll - no service restart needed. If the file is
        # mid-edit and briefly invalid YAML, keep using the last good config
        # instead of crashing; the next cycle will pick up the fixed file.
        try:
            config = load_config(args.config)
        except (OSError, yaml.YAMLError) as exc:
            logging.warning("Could not reload %s (keeping previous config): %s", args.config, exc)

        changed = False
        now = time.time()
        for ups in config["upses"]:
            name = ups["name"]
            host = ups.get("host", "localhost")
            description = ups.get("description", "")

            new_status = get_effective_status(ups, config)
            previous = state.get(name, {})
            old_status = previous.get("status")
            old_since = previous.get("since")
            charge_recovery_pending = previous.get("charge_recovery_pending", False)
            outage_start = previous.get("outage_start")
            last_outage = previous.get("last_outage")

            new_parts = new_status.split()
            old_parts = old_status.split() if old_status else []
            entry = dict(previous)
            entry["status"] = new_status

            if core_status(new_status) != core_status(old_status):
                # A real OL<->OB transition (steps 1<->2 or 2<->3) - always
                # SMS + email. duration_seconds normally means "how long was
                # it in old_status" (now - old_since) - EXCEPT for a genuine
                # recovery (see below), where we use the true full outage
                # duration (now - outage_start) instead, so the recovery
                # message can't under-report the outage just because a
                # transient UNKNOWN blip (e.g. upsd/driver not ready yet
                # right after a reboot) reset "since" partway through it.
                duration_seconds = (now - old_since) if old_since is not None else None

                # Driven off outage_start (persisted state), NOT just
                # "does OB appear/disappear this poll" - so a transient
                # UNKNOWN in between can't corrupt outage tracking. E.g.
                # OB -> UNKNOWN -> OB (comms blip, power never actually
                # came back): neither branch below fires for the
                # OB -> UNKNOWN leg (new_parts has neither OB nor OL), so
                # outage_start survives untouched; UNKNOWN -> OB doesn't
                # re-fire the "outage started" branch either, since
                # outage_start is already set. Only a GENUINE new outage
                # (OB appearing while none was already tracked) or a
                # GENUINE recovery (OL specifically appearing while one WAS
                # tracked) touches outage_start/last_outage.
                if "OB" in new_parts and outage_start is None:
                    # Step 1 -> 2: outage just started. Remember when, so we
                    # can compute its duration once power comes back.
                    outage_start = now
                    charge_recovery_pending = False
                elif "OL" in new_parts and outage_start is not None:
                    # Step 2 -> 3: power genuinely came back (not just "OB
                    # stopped appearing" - has to be an explicit OL).
                    # Report - and record - the FULL outage duration from
                    # its true start, not just time since the last core
                    # change (which could be a much more recent UNKNOWN
                    # blip on the way back up).
                    duration_seconds = now - outage_start
                    last_outage = {
                        "start": outage_start,
                        "end": now,
                        "duration_seconds": duration_seconds,
                    }
                    entry["last_outage"] = last_outage
                    outage_start = None
                    charge_recovery_pending = "CHRG" in new_parts

                notify(config, name, description, old_status, new_status, duration_seconds)
                entry["since"] = now
                changed = True

                entry["outage_start"] = outage_start
                entry["charge_recovery_pending"] = charge_recovery_pending

            elif charge_recovery_pending and "CHRG" in old_parts and "CHRG" not in new_parts:
                # Step 3 -> 4: still OL throughout, but CHRG just cleared -
                # the battery finished recharging after a real outage.
                # Email only (no SMS) - see notify_charge_complete().
                notify_charge_complete(config, name, description, last_outage)
                entry["charge_recovery_pending"] = False
                changed = True

            else:
                # Nothing alert-worthy changed - either fully steady, or
                # just CHRG/DISCHRG toggling outside of a tracked recovery
                # (e.g. routine float-charge maintenance unrelated to an
                # outage). We deliberately do NOT update "since" here, so
                # the recorded duration keeps counting from the last real
                # (core) status change, not from a charging flag flipping
                # on its own.
                logging.debug("%s unchanged (core status): %s", name, new_status)

            if new_status != old_status:
                # The full status string (including non-core flags like
                # CHRG/LB/RB/ALARM) changed even if nothing above fired an
                # alert - still worth persisting to state.json, so
                # `--status` (a separate process reading the file fresh)
                # reflects it, instead of only updating in this process's
                # in-memory copy until the next real alert-worthy event.
                changed = True

            if "CHRG" in new_parts:
                log_charge_progress(name, host)

            state[name] = entry

        if changed:
            save_state(state_file, state)

        notifier.notify("WATCHDOG=1")

        if args.once:
            break

        sleep_with_watchdog(config.get("poll_interval_seconds", 30), notifier)


if __name__ == "__main__":
    main()