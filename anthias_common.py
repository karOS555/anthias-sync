"""Shared code for the other scripts: config, credentials, API, logging."""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

import requests
import win32crypt  # pywin32

# Task Scheduler shows these as "Last Run Result".
EXIT_OK = 0
EXIT_PARTIAL = 1  # at least one device failed, the rest went through
EXIT_FATAL = 2    # nothing ran at all
EXIT_TIMEOUT = 3  # watchdog killed the run

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "devices.ini")
CREDENTIAL_FIELDS = ("smb_username", "smb_password", "api_username", "api_password")

DEFAULTS = {
    "share_name": "PiShare",
    "smb_port": 139,
    "smb_timeout": 60,
    "http_timeout": 20,
    "delay_between_phases": 5,
    "max_runtime_minutes": 45,
    "copy_only_changed": True,
    "memory_buffer_mb": 64,
    "temp_dir": "",
    "credential_file": "anthias_secure_credentials.dat",
    "log_file": "logs/anthias.log",
}


class ConfigError(Exception):
    """devices.ini or the credential file cannot be used."""


class FatalError(Exception):
    """Failure that makes the rest of the run pointless."""


class ApiError(Exception):
    """An API call did not do what it was supposed to do."""


@dataclass
class Device:
    name: str
    ip: str
    smb_username: str
    smb_password: str
    api_username: str
    api_password: str

    @property
    def smb_auth(self):
        return self.smb_username, self.smb_password

    @property
    def api_auth(self):
        return self.api_username, self.api_password

    @property
    def assets_url(self):
        return f"http://{self.ip}/api/v2/assets"

    @property
    def reboot_url(self):
        return f"http://{self.ip}/api/v2/reboot"

    def __str__(self):
        return f"{self.name} [{self.ip}]"


REDIRECTS = (301, 302, 303, 307, 308)


class ApiClient:
    """Basic auth first, web form login if the device redirects to /login/."""

    def __init__(self, device, timeout):
        self.device = device
        self.timeout = timeout
        self.base = f"http://{device.ip}"
        self.session = requests.Session()
        self.logged_in = False

    def request(self, method, path, body=None, retry=True):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        token = self.session.cookies.get("csrftoken")
        if token:
            headers["X-CSRFToken"] = token
            headers["Referer"] = f"{self.base}/"

        response = self.session.request(
            method,
            self.base + path,
            auth=None if self.logged_in else self.device.api_auth,
            data=json.dumps(body) if body is not None else None,
            headers=headers or None,
            timeout=self.timeout,
            # a redirect would turn the POST into a GET
            allow_redirects=False,
        )

        location = response.headers.get("Location") or ""
        if response.status_code in REDIRECTS and "/login" in location:
            if retry and not self.logged_in:
                self.login()
                return self.request(method, path, body, retry=False)
            raise ApiError(f"{method} {path}: login required, credentials rejected")
        if response.status_code in REDIRECTS:
            raise ApiError(f"{method} {path}: HTTP {response.status_code} -> {location}")
        if response.status_code == 401:
            raise ApiError(f"{method} {path}: HTTP 401, credentials rejected")
        return response

    def login(self):
        page = self.session.get(f"{self.base}/login/", timeout=self.timeout)
        if page.status_code != 200:
            raise ApiError(f"GET /login/: HTTP {page.status_code}")

        token = self.session.cookies.get("csrftoken")
        match = re.search(
            r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']', page.text
        )
        if match:
            token = match.group(1)

        payload = {
            "username": self.device.api_username,
            "password": self.device.api_password,
            "next": "/",
        }
        if token:
            payload["csrfmiddlewaretoken"] = token

        response = self.session.post(
            f"{self.base}/login/",
            data=payload,  # a real form post, not JSON
            headers={"Referer": f"{self.base}/login/"},
            timeout=self.timeout,
            allow_redirects=False,
        )
        location = response.headers.get("Location") or ""
        if response.status_code not in REDIRECTS or "/login" in location:
            raise ApiError(
                f"web login failed: HTTP {response.status_code} - check the API "
                "user and password for this device"
            )

        self.logged_in = True
        logging.info("%s: logged in through the web form", self.device)

    def json(self, method, path, body=None, expect=(200, 201)):
        response = self.request(method, path, body)
        if response.status_code not in expect:
            text = " ".join((response.text or "").split())[:120]
            raise ApiError(f"{method} {path}: HTTP {response.status_code} {text}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ApiError(f"{method} {path}: answer is not JSON")


def load_settings(path=None):
    """devices.ini -> (settings, device entries)."""
    path = os.path.abspath(path or DEFAULT_CONFIG_PATH)
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        # utf-8-sig, Notepad likes to leave a BOM
        with open(path, "r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except (configparser.Error, UnicodeDecodeError) as exc:
        raise ConfigError(f"config file is broken ({path}): {exc}")

    raw = parser["settings"] if parser.has_section("settings") else {}
    settings = {}
    for key, default in DEFAULTS.items():
        value = str(raw.get(key, default)).strip()
        if isinstance(default, bool):
            if value.lower() in ("yes", "true", "1", "on"):
                settings[key] = True
            elif value.lower() in ("no", "false", "0", "off"):
                settings[key] = False
            else:
                raise ConfigError(f"[settings] {key} must be yes or no, not '{value}'")
        elif isinstance(default, int):
            try:
                settings[key] = int(value)
            except ValueError:
                raise ConfigError(f"[settings] {key} must be a whole number, not '{value}'")
        else:
            settings[key] = value
    settings["_base_dir"] = os.path.dirname(path)

    entries = []
    for section in parser.sections():
        if section.strip().lower() == "settings":
            continue
        ip = parser[section].get("ip", "").strip()
        role = parser[section].get("role", "clone").strip().lower()
        if not ip:
            raise ConfigError(f"[{section}] has no 'ip' line")
        if role not in ("master", "clone"):
            raise ConfigError(f"[{section}] role must be master or clone, not '{role}'")
        entries.append({"name": section.strip(), "ip": ip, "role": role})

    masters = [entry for entry in entries if entry["role"] == "master"]
    if len(masters) != 1:
        raise ConfigError(
            f"exactly one device must have 'role = master', found {len(masters)}"
        )
    if not any(entry["role"] == "clone" for entry in entries):
        raise ConfigError("no clone devices in the config file")

    seen = {}
    for entry in entries:
        if entry["ip"] in seen:
            raise ConfigError(
                f"IP {entry['ip']} is used by both '{seen[entry['ip']]}' and '{entry['name']}'"
            )
        seen[entry["ip"]] = entry["name"]

    return settings, entries


def resolve_path(settings, value):
    if not value:
        return None
    if not os.path.isabs(value):
        value = os.path.join(settings.get("_base_dir", BASE_DIR), value)
    return os.path.abspath(value)


def credential_path(settings):
    return resolve_path(settings, settings.get("credential_file"))


def load_credentials(settings):
    """Decrypt the .dat: {"<device name>": {smb_username, api_password, ...}}."""
    path = credential_path(settings)
    try:
        with open(path, "rb") as handle:
            encrypted = handle.read()
    except FileNotFoundError:
        raise ConfigError(
            f"credential file not found: {path} - run save_credentials.py as the "
            "user the scheduled task runs as"
        )

    try:
        decrypted = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1]
        credentials = json.loads(decrypted.decode("utf-8"))
    except Exception as exc:
        raise ConfigError(
            f"could not read {path}: {exc} - only the Windows user that created "
            "the file, on this machine, can decrypt it"
        )

    if not isinstance(credentials, dict) or not credentials:
        raise ConfigError("credential file contains no devices")
    return credentials


def build_devices(entries, credentials):
    """Device entries + credentials -> (master, clones). Fails early on a typo."""
    devices = {"master": None, "clones": []}
    for entry in entries:
        name = entry["name"]
        stored = credentials.get(name)
        if stored is None:
            known = ", ".join(sorted(credentials)) or "(none)"
            raise ConfigError(
                f"no credentials stored for '{name}' - run save_credentials.py "
                f"(devices in the credential file: {known})"
            )
        missing = [field for field in CREDENTIAL_FIELDS if not stored.get(field)]
        if missing:
            raise ConfigError(
                f"credentials for '{name}' are incomplete: {', '.join(missing)}"
            )

        device = Device(
            name=name,
            ip=entry["ip"],
            **{field: stored[field] for field in CREDENTIAL_FIELDS},
        )
        if entry["role"] == "master":
            devices["master"] = device
        else:
            devices["clones"].append(device)

    return devices["master"], devices["clones"]


def setup_logging(settings, script_name):
    """Log to the file and, if there is one, to the console."""
    handlers = []

    log_file = resolve_path(settings, settings.get("log_file"))
    if log_file:
        try:
            folder = os.path.dirname(log_file)
            if folder:
                os.makedirs(folder, exist_ok=True)
            handlers.append(
                RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
            )
        except Exception as exc:
            print(f"WARNING: cannot write {log_file}: {exc}", file=sys.stderr)

    # a scheduled task has no console, and an umlaut should not kill the run
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{script_name}] %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers or [logging.NullHandler()],
    )

    # pysmb logs its whole handshake at INFO level
    for noisy in ("SMB", "NMB", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def start_watchdog(settings):
    """Kill the process if the run takes too long.

    A stuck socket blocks the main thread forever, which is what leaves a task
    running for days. os._exit cannot be blocked.
    """
    minutes = settings.get("max_runtime_minutes", 0)
    if minutes <= 0:
        return

    # backstop for sockets created without their own timeout
    socket.setdefaulttimeout(max(settings.get("smb_timeout", 60), 30))

    def kill():
        time.sleep(minutes * 60)
        logging.error("WATCHDOG: still running after %d min - killing the process", minutes)
        logging.shutdown()
        os._exit(EXIT_TIMEOUT)

    threading.Thread(target=kill, name="watchdog", daemon=True).start()
    logging.info("watchdog armed: hard stop after %d min", minutes)


def finish(failures, total, subject, started):
    """Closing summary plus the exit code."""
    runtime = int(time.time() - started)
    if failures:
        logging.error(
            "finished after %d s with %d problem(s) on %d configured %s:",
            runtime, len(failures), total, subject,
        )
        for name, reason in failures.items():
            logging.error("  %s -> %s", name, reason)
        logging.shutdown()
        return EXIT_PARTIAL

    logging.info("all %d %s done, %d s total", total, subject, runtime)
    logging.shutdown()
    return EXIT_OK
