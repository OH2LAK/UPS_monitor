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
    State is stored per UPS as {"status": "<last known ups.status>",
    "since": <unix timestamp when it last CHANGED to that status>}, e.g.:

        {"NB2A-L1": {"status": "OB", "since": 1757570000.1}}

    "since" is what lets us report "was on battery for 5 min 32 s" when a
    UPS comes back on line.
    """
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state_file, state):
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
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
    Sends the SMS via the Setera SMSGW (sms-gw.setera.com), per Setera's
    migration notice:

        POST https://sms-gw.setera.com:8040/sms
        Headers: token: <api_token>, Content-Type: application/json
        Body:    {"to": "<number, E.164 no '+'>", "from": "<sender_id>",
                  "content": "<message text>" [, "external_id": "..."]}

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
        # Setera wants E.164 without the leading "+".
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

    now_str = time.strftime("%Y-%m-%d %H:%M:%S %z")
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


def main():
    parser = argparse.ArgumentParser(description="Poll NUT UPS units and alert on status changes.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--once", action="store_true", help="Poll once and exit (for testing)")
    parser.add_argument(
        "--test-sms",
        action="store_true",
        help="Send one test SMS to every number in config.yaml's sms.to_numbers, then exit "
        "(doesn't touch UPS polling, state, or email - just checks the Setera gateway works).",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send one test email to every address in config.yaml's email.to_addrs, then exit.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_file", "ups_monitor.log"))

    if args.test_sms:
        sms_cfg = dict(config["sms"])
        if not sms_cfg.get("enabled", False):
            logging.warning("sms.enabled is false in config.yaml - sending the test anyway.")
            sms_cfg["enabled"] = True
        logging.info("Sending test SMS to: %s", ", ".join(sms_cfg.get("to_numbers", [])))
        send_sms(sms_cfg, "Testiviesti ups_monitor-skriptistä - jos näet taman, SMS-lähetys toimii.")
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
            "Tämä on testiviesti ups_monitor-skriptistä.\n"
            "Jos sait tämän, sähkopostilähetys on konfiguroitu oikein.",
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

            new_status = get_ups_status(name, host)
            previous = state.get(name)
            old_status = previous["status"] if previous else None
            old_since = previous["since"] if previous else None

            if new_status != old_status:
                duration_seconds = (now - old_since) if old_since is not None else None
                notify(config, name, description, old_status, new_status, duration_seconds)
                state[name] = {"status": new_status, "since": now}
                changed = True
            else:
                logging.debug("%s unchanged: %s", name, new_status)

        if changed:
            save_state(state_file, state)

        notifier.notify("WATCHDOG=1")

        if args.once:
            break

        sleep_with_watchdog(config.get("poll_interval_seconds", 30), notifier)


if __name__ == "__main__":
    main()
