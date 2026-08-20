"""Writes the Samba and API logins into an encrypted file (Windows DPAPI).

Run this as the user that runs the scheduled tasks, only that user can read it
back. One set of logins per device, keyed by the section name in devices.ini.

  save_credentials.py                     menu, needs a console
  save_credentials.py --template out.json write a fill-in file (plain text)
  save_credentials.py --import out.json   encrypt that file, then wipe it
  save_credentials.py --check             can this user read the .dat?
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

import win32crypt  # pywin32

from anthias_common import (
    CREDENTIAL_FIELDS,
    ConfigError,
    credential_path,
    load_settings,
    setup_logging,
)

PROMPTS = {
    "smb_username": ("Samba user", False),
    "smb_password": ("Samba password", True),
    "api_username": ("Anthias API user", False),
    "api_password": ("Anthias API password", True),
}


def decrypt_file(path):
    with open(path, "rb") as handle:
        encrypted = handle.read()
    decrypted = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1]
    return json.loads(decrypted.decode("utf-8"))


def save_stored(credentials, path, quiet=False):
    raw = json.dumps(credentials).encode("utf-8")
    encrypted = win32crypt.CryptProtectData(raw, "Anthias credentials", None, None, None, 0)

    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(encrypted)

    if not quiet:
        print(f"\nSaved: {path}")
        print(f"Devices in the file: {', '.join(sorted(credentials)) or '(none)'}\n")


def config_devices(entries):
    """Devices as (name, ip, role), master first."""
    ordered = sorted(entries, key=lambda entry: entry["role"] != "master")
    return [(entry["name"], entry["ip"], entry["role"]) for entry in ordered]


def is_complete(entry):
    return bool(entry) and all(entry.get(field) for field in CREDENTIAL_FIELDS)


def shred(path):
    """Overwrite and delete the plain text file."""
    try:
        with open(path, "r+b") as handle:
            handle.write(b"\0" * os.path.getsize(path))
            handle.flush()
            os.fsync(handle.fileno())
        os.remove(path)
        return True
    except Exception as exc:
        logging.error("could NOT delete %s - remove it by hand: %s", path, exc)
        return False


# --- no console needed ---

def run_template(entries, path):
    skeleton = {name: {field: "" for field in CREDENTIAL_FIELDS} for name, _, _ in config_devices(entries)}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(skeleton, handle, indent=2, ensure_ascii=False)

    print(f"\nTemplate written: {path}")
    print("Fill in the passwords, then import it as the service account:")
    print(f'  python save_credentials.py --import "{path}"')
    print("The import deletes the file afterwards. Until then it holds plain text.\n")
    return 0


def run_import(settings, entries, path):
    setup_logging(settings, "credentials")
    user = getpass.getuser()
    target = credential_path(settings)
    logging.info("import running as user '%s'", user)

    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            incoming = json.load(handle)
    except Exception as exc:
        logging.error("cannot read %s: %s", path, exc)
        return 1

    if not isinstance(incoming, dict) or not incoming:
        logging.error("%s holds no device object", path)
        return 1

    known = {name for name, _, _ in config_devices(entries)}
    for name, value in incoming.items():
        if not isinstance(value, dict):
            logging.error("entry '%s' is not an object", name)
            return 1
        missing = [field for field in CREDENTIAL_FIELDS if not value.get(field)]
        if missing:
            logging.error("entry '%s' is missing: %s", name, ", ".join(missing))
            return 1
        if name not in known:
            logging.warning("'%s' is not a device in devices.ini", name)

    credentials = {}
    if os.path.exists(target):
        try:
            credentials = decrypt_file(target)
        except Exception as exc:
            # do not silently overwrite a file written by someone else
            logging.error("existing %s cannot be decrypted by '%s': %s", target, user, exc)
            return 1

    for name, value in incoming.items():
        credentials[name] = {field: str(value[field]) for field in CREDENTIAL_FIELDS}

    try:
        save_stored(credentials, target, quiet=True)
    except Exception as exc:
        logging.error("cannot write %s: %s", target, exc)
        return 1

    logging.info("credentials stored for: %s", ", ".join(sorted(incoming)))
    logging.info("devices now in the file: %s", ", ".join(sorted(credentials)))
    if shred(path):
        logging.info("plain text file %s overwritten and deleted", path)
        return 0
    return 1


def run_check(settings, entries):
    setup_logging(settings, "credentials")
    user = getpass.getuser()
    path = credential_path(settings)
    logging.info("check running as user '%s'", user)

    if not os.path.exists(path):
        logging.error("%s does not exist yet", path)
        return 1
    try:
        credentials = decrypt_file(path)
    except Exception as exc:
        logging.error("'%s' CANNOT decrypt %s: %s", user, path, exc)
        return 1

    logging.info("'%s' can decrypt %s", user, path)
    problems = 0
    for name, _, _ in config_devices(entries):
        if is_complete(credentials.get(name)):
            logging.info("  %s: ok", name)
        else:
            logging.error("  %s: credentials missing or incomplete", name)
            problems += 1

    orphans = sorted(set(credentials) - {name for name, _, _ in config_devices(entries)})
    if orphans:
        logging.warning("stored but not in devices.ini: %s", ", ".join(orphans))
    return 1 if problems else 0


# --- menu ---

def load_stored(path):
    if not os.path.exists(path):
        print(f"No credential file yet, a new one will be created:\n  {path}\n")
        return {}
    try:
        return decrypt_file(path)
    except Exception as exc:
        print(f"ERROR: {path} cannot be decrypted: {exc}")
        print(
            "You are not the user that created it. Log in as that user, or delete\n"
            "the file and enter all credentials again."
        )
        sys.exit(1)


def show_overview(entries, credentials):
    devices = config_devices(entries)
    width = max((len(name or "") for name, _, _ in devices), default=10)

    print("\nDevices from devices.ini:")
    for name, ip, role in devices:
        if is_complete(credentials.get(name)):
            state = "credentials stored"
        elif name in credentials:
            state = "INCOMPLETE"
        else:
            state = "MISSING"
        print(f"  {name:<{width}}  {ip:<15}  {role:<6}  {state}")

    orphans = sorted(set(credentials) - {name for name, _, _ in devices})
    if orphans:
        print(f"\nStored entries without a device in devices.ini: {', '.join(orphans)}")
    print()


def ask_credentials(name, existing):
    """Ask for one device. None if cancelled."""
    if existing:
        print(f"'{name}' already has credentials - press Enter to keep a value.")

    entry = {}
    for field in CREDENTIAL_FIELDS:
        label, secret = PROMPTS[field]
        current = (existing or {}).get(field)
        if secret:
            suffix = " [Enter = keep]" if current else ""
            value = getpass.getpass(f"  {label}{suffix}: ")
        else:
            suffix = f" [{current}]" if current else ""
            value = input(f"  {label}{suffix}: ").strip()

        if not value:
            if current:
                value = current
            else:
                print(f"  '{label}' must not be empty - device skipped.\n")
                return None
        entry[field] = value

    return entry


def fill_missing(entries, credentials):
    pending = [
        (name, ip)
        for name, ip, _ in config_devices(entries)
        if not is_complete(credentials.get(name))
    ]
    if not pending:
        print("\nEvery device in devices.ini already has complete credentials.\n")
        return False

    changed = False
    for name, ip in pending:
        print(f"\n--- {name} [{ip}] ---")
        entry = ask_credentials(name, credentials.get(name))
        if entry:
            credentials[name] = entry
            changed = True
            print(f"  credentials for '{name}' set.")
    print()
    return changed


def edit_device(entries, credentials):
    devices = [name for name, _, _ in config_devices(entries)]
    print("\nWhich device?")
    for index, name in enumerate(devices, start=1):
        marker = "" if is_complete(credentials.get(name)) else "   (missing)"
        print(f"  {index}) {name}{marker}")

    choice = input("> ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(devices):
        print("Cancelled.\n")
        return False

    name = devices[int(choice) - 1]
    entry = ask_credentials(name, credentials.get(name))
    if not entry:
        return False

    credentials[name] = entry
    print(f"  credentials for '{name}' set.\n")
    return True


def drop_orphans(entries, credentials):
    known = {name for name, _, _ in config_devices(entries)}
    orphans = sorted(set(credentials) - known)
    if not orphans:
        print("\nNo orphaned entries.\n")
        return False

    print(f"\nThese entries have no device in devices.ini: {', '.join(orphans)}")
    if input("Delete them? [y/N] ").strip().lower() != "y":
        print("Cancelled.\n")
        return False

    for name in orphans:
        del credentials[name]
    print(f"Deleted: {', '.join(orphans)}\n")
    return True


MENU = """What do you want to do?
  1) show overview
  2) enter missing credentials
  3) change credentials for one device
  4) delete orphaned entries
  5) save and quit
  6) quit without saving
> """


def run_menu(settings, entries):
    path = credential_path(settings)
    credentials = load_stored(path)
    changed = False

    print(f"Running as Windows user: {getpass.getuser()}")
    print("The scheduled tasks must run as this exact user.")
    show_overview(entries, credentials)

    while True:
        choice = input(MENU).strip()

        if choice == "1":
            show_overview(entries, credentials)
        elif choice == "2":
            changed = fill_missing(entries, credentials) or changed
        elif choice == "3":
            changed = edit_device(entries, credentials) or changed
        elif choice == "4":
            changed = drop_orphans(entries, credentials) or changed
        elif choice == "5":
            if not credentials:
                print("Nothing entered - nothing to save.\n")
                continue
            save_stored(credentials, path)
            return 0
        elif choice == "6":
            if changed and input("Discard unsaved changes? [y/N] ").strip().lower() != "y":
                continue
            print("Nothing saved.")
            return 0
        else:
            print("Please pick 1 - 6.\n")


def main():
    parser = argparse.ArgumentParser(description="Manage the encrypted credentials")
    parser.add_argument("--config", default=None, help="path to devices.ini")
    parser.add_argument("--template", metavar="FILE", help="write a fill-in JSON file")
    parser.add_argument("--import", dest="import_file", metavar="FILE",
                        help="encrypt that JSON file, then overwrite and delete it")
    parser.add_argument("--check", action="store_true",
                        help="test whether this user can read the credential file")
    args = parser.parse_args()

    try:
        settings, entries = load_settings(args.config)
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    if args.template:
        return run_template(entries, args.template)
    if args.import_file:
        return run_import(settings, entries, args.import_file)
    if args.check:
        return run_check(settings, entries)
    return run_menu(settings, entries)


if __name__ == "__main__":
    sys.exit(main())
