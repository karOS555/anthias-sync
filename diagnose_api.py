"""Shows how the API of each device answers, using devices.ini and the .dat.

Read-only: the reboot paths are probed with GET, which cannot trigger a reboot.
Only --reboot sends a real POST.

  diagnose_api.py                 probe every device
  diagnose_api.py --device NAME   probe one device
  diagnose_api.py --reboot NAME   really reboot that device

On a reboot path: 405 means it exists and only takes POST, 302 to /login/ means
it wants a session, 401 means the login was rejected, 404 means wrong path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import requests

from anthias_common import ConfigError, build_devices, load_credentials, load_settings

READ_PATHS = ["/api/v2/info", "/api/v2/assets"]
REBOOT_PATHS = ["/api/v2/reboot", "/api/v2/reboot/", "/api/v1/reboot", "/api/v1/reboot/"]


def show(label, status, detail):
    print(f"  {label:<34} {str(status):<5} {detail}")


def probe(session, method, url, auth=None, data=None, form=None, timeout=10):
    """form=dict posts an HTML form, data=str posts a JSON body."""
    headers = {"Content-Type": "application/json"} if data is not None else None
    try:
        response = session.request(
            method, url, auth=auth, data=form if form is not None else data,
            headers=headers, timeout=timeout, allow_redirects=False,
        )
    except Exception as exc:
        return "ERR", f"{type(exc).__name__}: {' '.join(str(exc).split())[:110]}"

    parts = []
    if response.headers.get("Location"):
        parts.append(f"-> {response.headers['Location']}")
    if response.headers.get("WWW-Authenticate"):
        parts.append(f"WWW-Authenticate: {response.headers['WWW-Authenticate']}")
    body = " ".join((response.text or "").split())
    if body and not parts:
        parts.append(body[:110])
    return response.status_code, "  ".join(parts)


def probe_login(device, timeout):
    """Try the web login and retest with the session it gives."""
    base = f"http://{device.ip}"
    session = requests.Session()
    session.headers.update({"Referer": f"{base}/login/"})

    status, detail = probe(session, "GET", f"{base}/login/", timeout=timeout)
    show("GET  /login/", status, detail)
    if status != 200:
        return

    page = session.get(f"{base}/login/", timeout=timeout)
    fields = sorted(set(re.findall(r'name="([^"]+)"', page.text)))
    print(f"  {'form fields':<34} {'':<5} {', '.join(fields) or '(none found)'}")

    token = session.cookies.get("csrftoken")
    match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', page.text)
    if match:
        token = match.group(1)

    payload = {
        "username": device.api_username,
        "password": device.api_password,
        "next": "/",
    }
    if token:
        payload["csrfmiddlewaretoken"] = token
    status, detail = probe(
        session, "POST", f"{base}/login/", form=payload, timeout=timeout,
    )
    show("POST /login/ (session)", status, detail)

    if status in (301, 302, 303, 307, 308) and "/login" not in str(detail):
        for path in ["/api/v2/info", "/api/v2/assets"]:
            status, detail = probe(session, "GET", f"{base}{path}", timeout=timeout)
            show(f"GET  {path} (session)", status, detail)


def do_reboot(device, path, timeout):
    print(f"\nSending a real reboot to {device} via {path}")
    status, detail = probe(
        requests.Session(), "POST", f"http://{device.ip}{path}",
        auth=device.api_auth, data=json.dumps("reboot"), timeout=timeout,
    )
    show("POST " + path, status, detail)
    return 0 if status in (200, 201, 202, 204) else 1


def main():
    parser = argparse.ArgumentParser(description="Probe the Anthias API auth setup")
    parser.add_argument("--config", default=None, help="path to devices.ini")
    parser.add_argument("--device", help="only this device")
    parser.add_argument("--reboot", metavar="NAME", help="really reboot this device")
    parser.add_argument("--path", default="/api/v2/reboot", help="path used by --reboot")
    parser.add_argument("--user", help="try this API user instead of the stored one")
    parser.add_argument("--password", help="try this API password instead of the stored one")
    args = parser.parse_args()

    try:
        settings, entries = load_settings(args.config)
        master, clones = build_devices(entries, load_credentials(settings))
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    devices = [master] + clones
    timeout = settings["http_timeout"]

    # try logins without writing them anywhere
    if args.user or args.password:
        for device in devices:
            device.api_username = args.user or device.api_username
            device.api_password = args.password or device.api_password
        print(f"using API user '{devices[0].api_username}' from the command line")

    if args.reboot:
        picked = [device for device in devices if device.name == args.reboot]
        if not picked:
            print(f"unknown device '{args.reboot}' - known: {', '.join(d.name for d in devices)}")
            return 2
        return do_reboot(picked[0], args.path, timeout)

    if args.device:
        devices = [device for device in devices if device.name == args.device]
        if not devices:
            print(f"unknown device '{args.device}'")
            return 2

    for device in devices:
        print(f"\n=== {device} ===")
        session = requests.Session()
        base = f"http://{device.ip}"

        for path in READ_PATHS:
            status, detail = probe(session, "GET", f"{base}{path}", auth=device.api_auth, timeout=timeout)
            show(f"GET  {path}", status, detail)

        for path in REBOOT_PATHS:
            status, detail = probe(session, "GET", f"{base}{path}", auth=device.api_auth, timeout=timeout)
            show(f"GET  {path}", status, detail)

        status, detail = probe(session, "GET", f"{base}{REBOOT_PATHS[0]}", timeout=timeout)
        show(f"GET  {REBOOT_PATHS[0]} (no auth)", status, detail)

        probe_login(device, timeout)

    print("\n405 on a reboot path = the path exists and only takes POST.")
    print("302 -> /login/ = that path wants a session; the (session) lines show")
    print("whether logging in through the web form fixes it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
