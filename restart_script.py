"""Reboots every device from devices.ini, clones first, master last.

Exit codes: 0 ok, 1 a device failed, 2 nothing ran, 3 watchdog.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import requests

from anthias_common import (
    EXIT_FATAL,
    ApiClient,
    ConfigError,
    build_devices,
    finish,
    load_credentials,
    load_settings,
    setup_logging,
    start_watchdog,
)

# a rebooting device often drops the connection before it answers
TREAT_TIMEOUT_AS_ERROR = False

SECONDS_BETWEEN_DEVICES = 2


def reboot(device, timeout):
    client = ApiClient(device, timeout)
    response = client.request("POST", "/api/v2/reboot", body="reboot")
    body = " ".join((response.text or "").split())[:120]
    if response.status_code not in (200, 201, 202, 204):
        raise RuntimeError(f"HTTP {response.status_code} {body}")
    return response.status_code, body


def main():
    parser = argparse.ArgumentParser(description="Reboot all Anthias devices")
    parser.add_argument("--config", default=None, help="path to devices.ini")
    args = parser.parse_args()
    started = time.time()

    try:
        settings, entries = load_settings(args.config)
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return EXIT_FATAL

    setup_logging(settings, "restart")
    start_watchdog(settings)

    try:
        master, clones = build_devices(entries, load_credentials(settings))
    except ConfigError as exc:
        logging.error("FATAL: %s", exc)
        logging.shutdown()
        return EXIT_FATAL

    devices = clones + [master]
    timeout = settings["http_timeout"]
    failures = {}
    logging.info("rebooting %d device(s)", len(devices))

    for index, device in enumerate(devices):
        try:
            code, body = reboot(device, timeout)
            logging.info("%s: HTTP %s %s", device, code, body or "(empty response)")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if TREAT_TIMEOUT_AS_ERROR:
                failures[device.name] = f"no answer: {exc}"
                logging.error("%s: no answer to the reboot command: %s", device, exc)
            else:
                logging.warning("%s: no answer, probably already rebooting (%s)", device, exc)
        except Exception as exc:
            failures[device.name] = str(exc)
            logging.error("%s: reboot failed: %s", device, exc)

        if index < len(devices) - 1:
            time.sleep(SECONDS_BETWEEN_DEVICES)

    return finish(failures, len(devices), "device(s)", started)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_FATAL)
    except Exception:
        logging.exception("FATAL: unexpected error")
        logging.shutdown()
        sys.exit(EXIT_FATAL)
