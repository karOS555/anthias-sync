"""Copies the media files and asset settings from the master to every clone.

Phase 1 copies the files over SMB, phase 2 writes the asset list over the API.
Phase 2 only starts once phase 1 is done.

Exit codes: 0 ok, 1 a clone failed, 2 nothing ran, 3 watchdog.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time

from smb.SMBConnection import SMBConnection

from anthias_common import (
    EXIT_FATAL,
    EXIT_PARTIAL,
    ApiClient,
    ConfigError,
    FatalError,
    build_devices,
    finish,
    load_credentials,
    load_settings,
    resolve_path,
    setup_logging,
    start_watchdog,
)

# an asset without its media file is never written to a clone
MISSING_FILE_IS_ERROR = True

TEMP_PREFIX = "anthias_sync_"

ASSET_FIELDS = (
    "asset_id",
    "name",
    "uri",
    "start_date",
    "end_date",
    "duration",
    "mimetype",
    "is_enabled",
    "nocache",
    "play_order",
    "skip_asset_check",
    "is_active",
    "is_processing",
)


def prepare_temp(settings):
    """Temp folder for the copy buffer, minus leftovers from a killed run."""
    folder = resolve_path(settings, settings.get("temp_dir")) or tempfile.gettempdir()
    try:
        os.makedirs(folder, exist_ok=True)
        cutoff = time.time() - 3600
        for name in os.listdir(folder):
            if not name.startswith(TEMP_PREFIX):
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    logging.info("removed leftover temp file %s", name)
            except OSError:
                pass
    except Exception as exc:
        logging.warning("temp folder %s unusable, falling back to the default: %s", folder, exc)
        folder = tempfile.gettempdir()
    return folder


# --- phase 1: files ---

def connect_smb(device, settings):
    # empty NetBIOS names, that is what the Pis accept
    conn = SMBConnection(device.smb_username, device.smb_password, "", "", use_ntlm_v2=True)
    conn.connect(device.ip, settings["smb_port"], timeout=settings["smb_timeout"])
    return conn


def list_files(conn, settings):
    """{filename: size} of the share root."""
    entries = conn.listPath(settings["share_name"], "/", timeout=settings["smb_timeout"])
    return {
        entry.filename: entry.file_size
        for entry in entries
        if entry.filename not in (".", "..") and not entry.isDirectory
    }


def plan(clone_files, master_files, only_changed):
    """What to delete on the clone and what to copy to it."""
    if not only_changed:
        return list(clone_files), list(master_files)
    to_delete = [name for name, size in clone_files.items() if master_files.get(name) != size]
    to_copy = [name for name, size in master_files.items() if clone_files.get(name) != size]
    return to_delete, to_copy


def log_differences(device, clone_files, master_files, to_copy):
    """Why files count as different."""
    shared = set(clone_files) & set(master_files)
    if not shared:
        # normal case: Anthias renames every file on import
        logging.info(
            "%s: no file name matches the master (Anthias renames on import), copying all %d",
            device, len(to_copy),
        )
        return

    logging.info(
        "%s: %d of %d file name(s) also on the master, %d to copy",
        device, len(shared), len(clone_files), len(to_copy),
    )
    for name in sorted(set(to_copy) & shared)[:5]:
        logging.info(
            "  differs: %s  master=%s  clone=%s",
            name, master_files.get(name), clone_files.get(name),
        )


def run_phase1(settings, master, clones, failures):
    """Make every clone share match the master. Returns (working clones, filenames)."""
    share = settings["share_name"]
    timeout = settings["smb_timeout"]
    spool_bytes = max(settings["memory_buffer_mb"], 1) * 1024 * 1024
    temp_dir = prepare_temp(settings)
    logging.info(
        "--- phase 1: media files (SMB), copy_only_changed=%s ---",
        settings["copy_only_changed"],
    )

    try:
        master_conn = connect_smb(master, settings)
    except Exception as exc:
        raise FatalError(f"cannot reach the master share on {master}: {exc}")

    connections = {}
    wanted = {}
    try:
        try:
            master_files = list_files(master_conn, settings)
        except Exception as exc:
            raise FatalError(f"cannot list the master share on {master}: {exc}")
        logging.info("master %s holds %d file(s)", master, len(master_files))

        for clone in clones:
            try:
                conn = connect_smb(clone, settings)
                clone_files = list_files(conn, settings)
                to_delete, to_copy = plan(
                    clone_files, master_files, settings["copy_only_changed"]
                )
                if to_copy and settings["copy_only_changed"]:
                    log_differences(clone, clone_files, master_files, to_copy)
                for name in to_delete:
                    conn.deleteFiles(share, "/" + name, timeout=timeout)
                connections[clone.name] = conn
                wanted[clone.name] = set(to_copy)
                logging.info(
                    "%s: %d deleted, %d to copy, %d unchanged",
                    clone, len(to_delete), len(to_copy), len(master_files) - len(to_copy),
                )
            except Exception as exc:
                failures[clone.name] = f"SMB connect/clean: {exc}"
                logging.error("%s: SMB connect/clean failed: %s", clone, exc)

        if not connections:
            logging.error("no clone could be reached over SMB")
            return [], set()

        # pull once from the master, push to every clone that needs it
        todo = sorted({name for names in wanted.values() for name in names})
        for index, name in enumerate(todo, start=1):
            targets = [key for key in connections if name in wanted[key]]
            if not targets:
                continue
            logging.info("copying %d/%d: %s -> %d device(s)", index, len(todo), name, len(targets))
            try:
                with tempfile.SpooledTemporaryFile(
                    max_size=spool_bytes, prefix=TEMP_PREFIX, dir=temp_dir
                ) as buffer:
                    master_conn.retrieveFile(share, "/" + name, buffer, timeout=timeout)
                    for key in targets:
                        try:
                            buffer.seek(0)
                            connections[key].storeFile(share, "/" + name, buffer, timeout=timeout)
                        except Exception as exc:
                            failures[key] = f"copying {name}: {exc}"
                            logging.error("%s: copying %s failed, device skipped: %s", key, name, exc)
                            connections.pop(key, None)
            except Exception as exc:
                failures["master"] = f"reading {name} from master: {exc}"
                logging.error("could not read %s from the master: %s", name, exc)

            if not connections:
                logging.error("all clones dropped out during the file copy")
                break

        healthy = [clone for clone in clones if clone.name in connections]
        return healthy, set(master_files)

    finally:
        for conn in list(connections.values()) + [master_conn]:
            try:
                conn.close()
            except Exception:
                pass


# --- phase 2: settings ---

def get_assets(client):
    return client.json("GET", "/api/v2/assets", expect=(200,))


def local_filename(asset):
    """The file this asset uses on the share, or None for web and stream assets."""
    uri = str(asset.get("uri") or "").strip()
    if not uri or uri.lower().startswith(("http://", "https://", "rtsp://", "rtmp://")):
        return None
    return uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def build_payload(asset):
    payload = {field: asset.get(field) for field in ASSET_FIELDS}
    if asset.get("mimetype") == "video":
        # must be 0 for videos, otherwise they do not sync
        payload["duration"] = 0
    return payload


# Anthias hands out its own asset_id on import, renames the media file to match
# and sets its own dates, so asset_id, uri, start_date, end_date, is_active and
# is_processing can never match between two devices and are left out here.
COMPARE_FIELDS = ("name", "duration", "mimetype", "is_enabled", "nocache", "skip_asset_check")


def field_value(field, asset):
    value = asset.get(field)
    if field == "duration":
        # we send 0 for videos and Anthias fills in the real length itself
        if asset.get("mimetype") == "video":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return value
    return value


def by_name(assets):
    """Assets keyed by name, the only handle that survives an import."""
    return {
        str(asset.get("name")): {field: field_value(field, asset) for field in COMPARE_FIELDS}
        for asset in assets
    }


def play_order(assets):
    """Only the sequence of names, the numbers differ per device."""
    ordered = sorted(assets, key=lambda a: (a.get("play_order") or 0, str(a.get("name"))))
    return [str(asset.get("name")) for asset in ordered]


def same_assets(current, wanted):
    return by_name(current) == by_name(wanted) and play_order(current) == play_order(wanted)


def log_asset_difference(device, current, wanted):
    """What keeps the asset lists from matching."""
    here, there = by_name(current), by_name(wanted)

    only_master = sorted(set(there) - set(here))
    only_device = sorted(set(here) - set(there))
    if only_master:
        logging.info("%s: only on the master: %s", device, ", ".join(only_master[:5]))
    if only_device:
        logging.info("%s: only on the device: %s", device, ", ".join(only_device[:5]))

    changed = []
    for name in sorted(set(here) & set(there)):
        fields = [f for f in COMPARE_FIELDS if here[name].get(f) != there[name].get(f)]
        if fields:
            changed.append((name, fields))

    if changed:
        logging.info("%s: %d asset(s) changed", device, len(changed))
        for name, fields in changed[:3]:
            detail = ", ".join(
                f"{f} device={here[name].get(f)!r} master={there[name].get(f)!r}" for f in fields
            )
            logging.info("  '%s': %s", name, detail)

    if not (only_master or only_device or changed):
        mine, master = play_order(current), play_order(wanted)
        moved = [name for a, name in zip(mine, master) if a != name]
        logging.info(
            "%s: same assets, but the play order changed (%d position(s), first: %s)",
            device, len(moved), moved[0] if moved else "?",
        )


def clones_to_sync(settings, payloads, clones):
    """Leave out clones that already match the master."""
    if not settings["copy_only_changed"]:
        return list(clones)

    timeout = settings["http_timeout"]
    pending = []
    for clone in clones:
        try:
            current = get_assets(ApiClient(clone, timeout))
        except Exception as exc:
            logging.warning("%s: cannot read the asset list, syncing anyway: %s", clone, exc)
            pending.append(clone)
            continue

        if same_assets(current, payloads):
            logging.info("%s: already in sync, nothing to do", clone)
        else:
            log_asset_difference(clone, current, payloads)
            pending.append(clone)
    return pending


def write_assets(client, current, payloads):
    """Wipe the device list, then write the master list in play order."""
    for asset in current:
        asset_id = asset.get("asset_id")
        client.json("DELETE", f"/api/v2/assets/{asset_id}", expect=(200, 204))

    errors = []
    for payload in payloads:
        try:
            client.json("POST", "/api/v2/assets", body=payload, expect=(200, 201))
        except Exception as exc:
            errors.append(f"{payload.get('name')}: {exc}")

    if errors:
        raise RuntimeError(f"{len(errors)} asset(s) failed - first: {errors[0]}")


def run_phase2(settings, all_payloads, clones, available_files, failures):
    timeout = settings["http_timeout"]
    logging.info("--- phase 2: asset settings (API) ---")

    payloads, orphans = [], []
    for payload in all_payloads:
        name = local_filename(payload)
        if name and name not in available_files:
            orphans.append(f"{payload.get('name')} ({name})")
            continue
        payloads.append(payload)

    logging.info("%d master asset(s), %d ready to sync", len(all_payloads), len(payloads))
    if orphans:
        logging.error("media file missing on the share, asset not synced: %s", "; ".join(orphans))
        if MISSING_FILE_IS_ERROR:
            failures["assets"] = f"{len(orphans)} asset(s) without a media file"

    for clone in clones:
        try:
            client = ApiClient(clone, timeout)
            current = get_assets(client)
            write_assets(client, current, payloads)
            logging.info("%s: %d asset(s) replaced by %d", clone, len(current), len(payloads))
        except Exception as exc:
            failures[clone.name] = f"API: {exc}"
            logging.error("%s: copying the asset settings failed: %s", clone, exc)


def main():
    parser = argparse.ArgumentParser(description="Sync the Anthias master to all clones")
    parser.add_argument("--config", default=None, help="path to devices.ini")
    parser.add_argument("--force", action="store_true",
                        help="copy everything even if nothing changed")
    args = parser.parse_args()
    started = time.time()

    try:
        settings, entries = load_settings(args.config)
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return EXIT_FATAL

    setup_logging(settings, "sync")
    start_watchdog(settings)

    try:
        master, clones = build_devices(entries, load_credentials(settings))
    except ConfigError as exc:
        logging.error("FATAL: %s", exc)
        logging.shutdown()
        return EXIT_FATAL

    logging.info("start: master %s, %d clone(s)", master, len(clones))
    failures = {}
    if args.force:
        settings["copy_only_changed"] = False

    # decide up front which clones need anything at all
    try:
        master_assets = get_assets(ApiClient(master, settings["http_timeout"]))
    except Exception as exc:
        logging.error("FATAL: cannot read the asset list from the master %s: %s", master, exc)
        logging.shutdown()
        return EXIT_FATAL

    payloads = [
        build_payload(asset)
        for asset in sorted(master_assets, key=lambda item: item.get("play_order") or 0)
    ]
    logging.info("master holds %d asset(s)", len(payloads))

    pending = clones_to_sync(settings, payloads, clones)
    if not pending:
        logging.info("every clone is already up to date")
        return finish(failures, len(clones), "clone(s)", started)

    try:
        healthy, available_files = run_phase1(settings, master, pending, failures)
    except FatalError as exc:
        logging.error("FATAL: %s", exc)
        logging.shutdown()
        return EXIT_FATAL

    skipped = [clone.name for clone in pending if clone not in healthy]
    if skipped:
        logging.warning("skipped in phase 2 after phase 1 errors: %s", ", ".join(skipped))
    if not healthy:
        logging.error("no clone made it through phase 1, phase 2 is pointless")
        logging.shutdown()
        return EXIT_PARTIAL

    if settings["delay_between_phases"]:
        time.sleep(settings["delay_between_phases"])

    try:
        run_phase2(settings, payloads, healthy, available_files, failures)
    except FatalError as exc:
        logging.error("FATAL: %s", exc)
        logging.shutdown()
        return EXIT_FATAL

    return finish(failures, len(clones), "clone(s)", started)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_FATAL)
    except Exception:
        # never end without an exit code the monitoring can see
        logging.exception("FATAL: unexpected error")
        logging.shutdown()
        sys.exit(EXIT_FATAL)
