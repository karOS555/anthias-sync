# anthias-sync

Keeps several [Anthias](https://anthias.screenly.io/) digital signage players in sync.
One Raspberry Pi is the master. Whatever you upload and configure there gets copied
to any number of clones, so you only ever maintain one playlist. A second script
reboots all players once a day, which clears the display and playback glitches that
build up when a Pi runs for weeks.

The scripts do not run on the Pis. They run on one machine that can reach them and
are started from the outside, so the scheduler is up to you. I use Windows Task
Scheduler and the setup below shows that, but cron or anything else works the same
way as long as it can run a Python script and read an exit code.


## Scripts

| File | What it is for |
|---|---|
| `anthias_sync.py` | Copies media files and playlist settings from the master to the clones. Scheduled. |
| `restart_script.py` | Reboots every player. Scheduled, once a day. |
| `save_credentials.py` | Stores the logins encrypted. Run by hand when a device changes. |
| `diagnose_api.py` | Shows how each player answers. Run by hand when something is wrong. |
| `anthias_common.py` | Shared code. Not run directly. |
| `devices.ini` | Copy and put your players in. |

## How the sync works

1. Read the master's asset list and compare it with each clone. Clones that already
   match are skipped, so a run with no changes takes a few seconds.
2. For clones that differ, copy the media files from the master's Samba share.
3. Then write the asset list over the Anthias API.

Step 3 always waits for step 2. If it did not, a clone would end up with playlist
entries pointing at files that are not there yet.

## Requirements

- Windows on the machine running the scripts. The logins are encrypted with the
  Windows Data Protection API, which binds them to one user account on one machine.
- Python 3.9 or newer.
- Raspberry Pis running Anthias, with the media folder shared over Samba.

## Raspberry Pi setup

Do this once per player.

### Install Anthias

1. Flash Raspberry Pi OS Lite (64 bit) with the Raspberry Pi Imager. Set hostname,
   user and Wi-Fi in the imager settings before writing, and enable SSH.
2. Boot the Pi, log in, and run the Anthias installer:

   ```bash
   bash <(curl -sL https://install-anthias.srly.io)
   ```

   Let it manage the network, pick the latest version, and allow the full system
   upgrade. Reboot when it is done.
3. Open the IP shown on the screen in a browser, go to Settings and set a username
   and password. The scripts use this login for the API.

### Share the media folder

The sync copies the files over SMB, so the assets folder has to be shared.

```bash
sudo apt-get update
sudo apt-get install samba samba-common-bin
sudo nano /etc/samba/smb.conf
```

```ini
[global]
workgroup = WORKGROUP
server string = Samba Server %v
netbios name = <device name>
security = user
map to guest = never
dns proxy = no

[PiShare]
comment = Anthias assets
path = /home/admin/screenly_assets/
writeable = yes
browseable = yes
valid users = admin
create mask = 0777
directory mask = 0777
```

Adjust `path` and `valid users` if your Pi user is not `admin`. Then set the Samba
password and reboot:

```bash
sudo smbpasswd -a admin
sudo reboot
```

Check it from the machine that will run the scripts by opening
`\\<ip>\PiShare` in Explorer. If assets are already uploaded you will see them,
named after their asset ID rather than the original filename. That is normal.

## Script setup

### 1. Install the packages

```cmd
"C:\Program Files\Python313\python.exe" -m pip install pywin32 requests pysmb
```

Use an elevated prompt so they land system wide. If the scheduled task runs as a
service account, that account has to see the packages too.

### 2. Put the files somewhere

Copy the whole repository into one folder, for example
`C:\Scripts\anthias-sync\`. The scripts find their config next to themselves,
so the folder can be anywhere.

### 3. Fill in devices.ini

Copy `devices.ini` and enter your players. Adding one later
is two lines:

```ini
[player-lobby]
ip   = 192.168.1.10
role = master

[player-canteen]
ip   = 192.168.1.11
```

Exactly one device is the master, everything else is a clone. The section name is
the device name and is also the key for its logins, so renaming a device means
entering its logins again.

The optional `[settings]` block in the example file covers timeouts, the run time
limit and where the log goes. Every line has a default, so you can leave it out.

### 4. Store the logins

Run this as the user that will run the scheduled tasks. The file it writes can only
be decrypted by that user on that machine.

```cmd
python save_credentials.py
```

Pick "enter missing credentials", type the Samba and API login for each device, then
save. Each device gets its own set.

If the account cannot log in interactively, there is a way around it. Write a
template, fill it in, and import it from a one off scheduled task running as that
account:

```cmd
python save_credentials.py --template C:\Temp\creds.json
python save_credentials.py --import C:\Temp\creds.json
```

The import wipes the plain text file afterwards. `--check` tells you whether the
current user can read the encrypted file.

### 5. Test

```cmd
python anthias_sync.py
python restart_script.py
```

Watch the log at `logs\anthias.log`. Run the sync a second time without changing
anything, it should say `every clone is already up to date` and finish in seconds.

## Scheduling with Windows Task Scheduler

Use "Create Task", not "Create Basic Task". The basic wizard hides the settings that
matter.

**General**

- Run as the user whose logins you stored in step 4.
- "Run whether user is logged on or not", and leave "Do not store password"
  unchecked. Without the stored password the task cannot decrypt the login file.

**Triggers**

- Sync: once, repeat every hour, indefinitely.
- Reboot: daily, at a time when nobody is looking at the screens.

Leave enough room between the reboot and the next sync. A Pi that is still booting
counts as a failed device.

**Actions**

- Program: `C:\Program Files\Python313\python.exe`
- Arguments: `"C:\Scripts\anthias-sync\anthias_sync.py"` with the quotes
- Start in: `C:\Scripts\anthias-sync` without quotes

**Settings**

- "Stop the task if it runs longer than": 1 hour. The default is 3 days, which is
  how a stuck run blocks every following one.
- "If the task is already running": do not start a new instance.
- Turn off "Run task as soon as possible after a scheduled start is missed",
  otherwise the machine catches up on every missed run at once after downtime.

Repeat for `restart_script.py`.

## Exit codes

Both scheduled scripts report through their exit code, which Task Scheduler shows as
"Last Run Result". Point your monitoring at anything other than 0.

| Code | Meaning |
|---|---|
| 0 | Everything worked |
| 1 | At least one device failed, the rest went through |
| 2 | Nothing ran. Bad config, wrong logins, or the master is unreachable |
| 3 | The run took too long and stopped itself |

If the master is unreachable the run stops before touching anything, so a clone is
never wiped when nothing can be copied back.

## What counts as a change

Anthias assigns its own asset ID when an asset is created through the API, renames
the media file to match and sets its own dates. The same file is therefore called
something different on every player, and comparing IDs or filenames cannot work.

What gets compared instead is what you actually set in the web interface: name,
duration, type, enabled, nocache, skip asset check, and the order of the playlist.
Video duration is left out, because Anthias measures it itself.

If anything differs, that clone gets a full copy: every file, then the whole asset
list. Updating single assets is not possible, because deleting an asset through the
API deletes its media file as well.

## Troubleshooting

Run `diagnose_api.py`. It reads `devices.ini` and the stored logins and shows how
each player answers, without changing anything.

| Log message | Cause |
|---|---|
| `could not read ...dat` | Task runs as a different user than the one that stored the logins |
| `web login failed` | Wrong API user or password for that device |
| `no credentials stored for 'x'` | Device is in `devices.ini` but not in the credential file |
| `cannot reach the master share` | Master offline. Nothing is touched, exit code 2 |
| `SMB connect/clean failed` | One clone offline. The rest continues, exit code 1 |
| `media file missing on the share` | A master asset points at a file that is not in the share |
| `WATCHDOG: still running after ...` | Run hung and stopped itself, exit code 3 |

A 302 to `/login/` from the API usually means the login was rejected. Anthias
redirects instead of answering 401.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
