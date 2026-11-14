# lsyncd-webui


**!! DISCLAIMER !!**<br>
**WATCH OUT!! This is a fully vibecoded tool that I needed for something at home.**<br>
**It works great and you are free to use it, but use it AT YOUR OWN RISK!**<br>
**I am NOT responsible for ANY lost data!!**<br>

A live, one-way mirror from a main server to a backup server, with a web interface.
Every file you create, change, rename or delete on the main server shows up on the backup within seconds.
Built for Unraid, but it runs on any machine with Docker.

```
 Main server                                 Backup server
┌──────────────────────────┐   SSH over    ┌──────────────────────────┐
│ lsyncd-webui             │   your VPN    │ lsyncd-webui-backup      │
│  web UI + lsyncd         │ ────────────▶ │  SSH server + rsync      │
│  /mnt (read only)        │   port 2222   │  /mnt/user/backup        │
└──────────────────────────┘               └──────────────────────────┘
```

Image: `ghcr.io/mikaas111/lsyncd-webui:latest`

## Features

- **Live sync.** lsyncd hears about every change from the kernel and sends only that file. The backup server never scans anything, so a slow backup machine keeps up.
- **Exact copy.** Deletions and renames are mirrored too. You can switch that off.
- **Two-way folders.** For folders you choose, changes made on the backup server are copied back to the main server too.
- **Pick folders in the browser.** Tick shares or subfolders in a folder tree. Shares that are shortcuts to a pool, such as `appdata`, work too.
- **Excludes.** Skip files like `*.tmp` or whole folders like `cache/`.
- **Progress.** See how far a full sync is: items found while scanning, items compared, data sent and speed.
- **Scheduled folders.** Folders that change all the time, such as appdata, can be copied every few hours instead of live, so they do not keep sending in between.
- **Live transfer view.** The current speed and the last file that was copied in full, updated every 2 seconds.
- **Speed test.** Finds what limits the sync speed (the network, SSH encryption or the backup's disks) and switches to the fastest encryption.
- **Encryption optional over a VPN.** A VPN already encrypts everything, so SSH encryption can be switched off to spare a slow CPU. The backup still only lets in the main server's key.
- **Counts on both servers.** Folders, files and total size per folder, on the main server and on the backup, side by side.
- **Compare.** A dry run that lists every file that differs, without changing anything.
- **Nightly full sync.** Catches anything live sync could not see, such as files written straight to a disk.
- **Host independent.** Everything runs inside the containers. The host only needs Docker.

## How it works

One image runs in two roles. You run it once on each server.

1. **The main container watches your folders.** It mounts your shares read only, so it can never change anything on the main server. The Linux kernel (inotify) tells lsyncd the moment a file is created, changed, renamed or deleted in a folder you selected.
2. **Changes are collected and sent.** lsyncd waits a few seconds (the *Delay* setting, 5 by default), then sends only the changed files to the backup with rsync over SSH. A rename becomes a move on the backup, and a deletion becomes a deletion, so the backup stays an exact copy.
3. **The backup container receives.** It is a small SSH server that only accepts the main server's key and writes into the backup folder you mounted. It does not scan or index anything itself, which keeps the load on the backup server low.
4. **A nightly full sync is the safety net.** Once a night, rsync compares everything and fixes what live sync could not see, for example files written directly to a disk or changes made while the backup server was offline. A safety check skips any folder that is empty on the main server, so a missing disk cannot wipe the backup.
5. **You can check at any time.** *Count files* counts folders, files and size on both servers. *Compare* checks every file's name, size and date on both servers and lists anything that differs.

### Live, scheduled or two-way, per folder

Every selected folder has a mode:

- **Live** (default): the main server decides. lsyncd sends its changes to the backup within seconds, and anything changed on the backup is overwritten.
- **Scheduled**: also a mirror of the main server, but copied every few hours (**Settings → Scheduled folders: hours between syncs**, 6 by default) instead of live. Use it for folders that change all the time, such as appdata with its databases and logs: live sync would keep sending them in between everything else. lsyncd does not watch these folders, which also saves watches (see *Large shares*). A restart of the container does not go through them again; the schedule and the nightly full sync do.
- **Two-way**: changes on either server are copied to the other, including deletions. This runs through [Unison](https://github.com/bcpierce00/unison), which watches both servers and keeps a record of the last sync, so it can tell a new file on one side from a deleted file on the other. When the same file changed on both servers, the newest version wins and the other one is kept next to it as a conflict copy, so nothing is lost. If one whole side looks deleted, for example because a mount is missing, Unison stops instead of copying that deletion.

Two-way folders need write access on the main server, through the extra path `/mnt-rw`. Live and scheduled folders are never written.

### Shortcuts (exclusive shares)

When a share lives entirely on one pool, Unraid turns `/mnt/user/<share>` into a shortcut (symlink) to the pool, for example `appdata -> ../cache/appdata`. The main container therefore maps all of `/mnt`, not just `/mnt/user`, so these shortcuts lead to the same place as on the host. A selected share that is a shortcut is synced by what it points to: its contents end up on the backup, not the shortcut itself. Shortcuts deeper inside a folder are copied as shortcuts, like any backup tool does.

If the **Folders** tab marks a share as *shortcut, target not reachable*, the container cannot see the pool it points to. Map the host's `/mnt` to `/mnt` (see *Updating*).

Both containers carry everything they need (Alpine Linux, lsyncd, rsync, Unison, OpenSSH, Python). The host only provides Docker, the folders and the network, so it does not matter which Linux your server runs.

## What you need

- Two servers with Docker: Unraid 7, Unraid 8, or any Linux machine.
- A network path from the main server to the backup server on TCP port 2222 (and 2223 if you switch encryption off). Between two locations that is usually a VPN such as WireGuard. On the same LAN nothing extra is needed.

## Install, step by step

### Step 1: Connect the two servers

The main server must be able to reach the backup server.

- **Same network:** nothing to do. Note the backup server's LAN IP address.
- **Different locations:** set up a VPN. On Unraid, use **Settings → VPN Manager** (WireGuard) with the peer type **Server to server access** on both servers. Note the backup server's VPN IP address, for example `10.252.100.2`.

Test it from the main server's terminal:

```bash
ping -c 3 10.252.100.2
```

### Step 2: Install the backup container (on the backup server)

**Unraid 7 (template).** Open the terminal on the backup server and download the template:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-lsyncd-webui-backup.xml https://raw.githubusercontent.com/Mikaas111/lsyncd-webui/main/unraid/lsyncd-webui-backup.xml
```

Then go to **Docker → Add Container**, pick **lsyncd-webui-backup** under **Template**, check the fields and click **Apply**.

| Field | Value |
| --- | --- |
| Backup folder | Where the copy goes, for example `/mnt/user/backup` |
| Config | `/mnt/user/appdata/lsyncd-webui-backup` |
| Main server key | Leave empty for now. You fill it in at step 4. |
| SSH port | `2222` |

**Unraid 8 or any other Docker host (Compose).** Download [`compose/backup-server.yml`](compose/backup-server.yml), adjust the paths and start it:

```bash
docker compose -f backup-server.yml up -d
```

### Step 3: Install the main container (on the main server)

**Unraid 7 (template):**

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-lsyncd-webui.xml https://raw.githubusercontent.com/Mikaas111/lsyncd-webui/main/unraid/lsyncd-webui-main.xml
```

Then go to **Docker → Add Container**, pick **lsyncd-webui** under **Template** and click **Apply**.

| Field | Value |
| --- | --- |
| Storage (read only) | `/mnt`. Your shares are read through `/mnt/user`; mapping all of `/mnt` makes pool shortcuts such as `appdata` work. Never changed. |
| Storage (read-write, two-way folders) | `/mnt`. Only written for folders you set to two-way. Leave it empty if you only mirror. |
| Config | `/mnt/user/appdata/lsyncd-webui` |
| Web UI port | `8484` |
| Web UI password | Optional, but recommended |

**Unraid 8 or other hosts:** use [`compose/main-server.yml`](compose/main-server.yml) the same way.

Open the web interface at `http://MAIN-SERVER-IP:8484`. On Unraid you can also click the container icon and choose **WebUI**.

### Step 4: Let the main server in

1. In the web interface, go to **Settings** and click **Copy key** under **SSH key**.
2. On the backup server, edit the **lsyncd-webui-backup** container, paste the key into **Main server key** and click **Apply**.
3. Back in the web interface, under **Settings**, fill in:
   - **Address:** the backup server's IP from step 1
   - **SSH port:** `2222`
   - **User:** `root`
   - **Target folder on the backup:** `/backup`
4. Click **Save**, then **Test connection**. You should see **Connected**.

### Step 5: Choose what to mirror

1. Go to **Folders** and tick the shares or folders you want on the backup.
2. On the right, choose per folder **Live**, **Scheduled** or **Two-way**, then click **Save**. A full sync starts right away. Follow it on **Overview**.
3. Optionally, go to **Excludes** and add patterns for files you never want synced.

The first full sync copies everything and can take a long time. With a lot of data, you can do the first sync with both servers on the same network and move the backup server afterwards.

### Step 6: Check that both servers match

On **Overview**:

- **Count files** counts folders, files and size on both servers. Every row should say **Identical**.
- **Compare** lists every file that differs. **Identical: no differences found** means the backup is an exact copy.

Counting also runs after every full sync, including the nightly one, so the overview stays current.

## The web interface

| Tab | What it does |
| --- | --- |
| **Overview** | Current transfer speed and the last file copied, live sync status, two-way sync status, the queue of changes waiting to be sent, progress of the running task, counts on both servers and the last comparison. |
| **Folders** | Folder tree of the main server. Tick what to sync, choose Live, Scheduled or Two-way per folder, and see where each folder goes on the backup. |
| **Excludes** | Patterns for files and folders to skip, one per line. |
| **Settings** | Backup server address, delay, deletion mirroring, compression, bandwidth limit, nightly sync time, the SSH key and the speed test. |
| **Log** | Everything lsyncd, rsync and the app report. |

## Large shares: raise the watch limit

lsyncd needs one *inotify watch* per folder it watches. **Overview** shows how many folders are watched and the system limit. Folders set to **Scheduled** are not watched, so that is one way to stay under it. If the number gets close to the limit, raise it on the **main server's host**:

```bash
sysctl -w fs.inotify.max_user_watches=1048576
```

That command lasts until the next reboot. To make it stick, run it at every boot: on Unraid 7 with the *User Scripts* plugin (schedule **At Startup of Array**) or the *Tips and Tweaks* plugin; on other Linux systems with a file in `/etc/sysctl.d/`.

## Sync slower than expected? Run the speed test

Open **Settings** and click **Run speed test**. It takes about half a minute and measures each link between the servers on its own:

| Test | What it measures |
| --- | --- |
| **Network without SSH** | Internet and VPN between the servers, nothing else. The backup container opens a random port for a few seconds for this; it needs host networking, which the template uses. |
| **Without encryption** | The network plus passing the data through both containers, as with **Encryption: None** (see below). |
| **SSH with each cipher** | The network plus encryption on both servers. The fastest cipher is used for every sync from then on. |
| **Disk writes on the backup** | Writing a 1 GB test file to the backup folder, including flushing it to disk. The file is deleted right after. |

The slowest of these is the most the sync can reach. What to do about it:

- **Network.** The main server's *upload* and the backup's *download* both count, so check the internet connection at both locations, for example with a speed test site. Many connections sold as 1 Gbit upload much less. If both are clearly faster than the test, a lower MTU on the VPN tunnel (such as 1380, on both ends) sometimes helps.
- **SSH encryption.** One of the servers runs out of CPU time. The speed test already switched to the fastest cipher. A CPU without AES-NI (shown under the results) is usually the cause. Over a VPN, switch encryption off, see below.
- **Disk writes on the backup.** Unraid writes to the array with parity, which is slow by default. Set **Settings → Disk Settings → Tunable (md_write_method)** to **reconstruct write** (turbo write): writes no longer read parity first and are often much faster, but all array disks spin while writing. A cache pool for the backup share helps too.

Many small files always go slower than these numbers, because every file costs a few round trips between the servers. Run the test when nothing else is syncing; it warns you if something was.

The **Transfer** card on **Overview** shows the speed of everything this container sends and receives, and the last file that reached the other server in full.

### Encryption off over a VPN

A VPN such as WireGuard already encrypts everything between the servers, so SSH encrypts it all a second time. On a slow CPU that second layer can be what limits the speed. Under **Settings → Encryption**, choose **None, the VPN already encrypts**:

- Syncs then go to the backup container's **port without encryption** (`2223`, variable `PLAIN_PORT`) instead of SSH. lsyncd, rsync and Unison work exactly as before.
- Logging in stays as strict as with SSH: on every connection the main server signs a fresh challenge with its SSH key, and the backup only accepts the key in `AUTHORIZED_KEY`.
- The data itself is not encrypted by the containers. Only choose this when the backup address is a VPN address, never across the internet without a VPN.
- When you save, the main server first checks that it can log in this way. If the backup container is too old or the port is blocked, the setting is not saved and you see why.
- Switch back to **SSH (encrypted)** at any time.

## Unraid 8 and other systems

Unraid 8 moves from Slackware to a Fedora base. lsyncd-webui does not depend on the host system:

- lsyncd, rsync, Unison, SSH and Python all live inside the containers. The host only provides Docker, the mounted folders and the network.
- It does not use any Unraid plugin, script or host SSH setting.
- Unraid 8 adds native Docker Compose, and the files in [`compose/`](compose) work as they are.

## Updating

On Unraid: **Docker → Check for Updates → Apply Update**. With Compose: `docker compose pull && docker compose up -d`. Settings, keys and status live in `/config`, so they survive updates.

**Installed an earlier version?** Older versions mapped only `/mnt/user` (to `/data` and `/data-rw`), which hides shares that are shortcuts to a pool. Edit the **lsyncd-webui** container on the main server and, with **Add another Path, Port, Variable, Label or Device**, add:

| Container path | Host path | Access mode | Needed for |
| --- | --- | --- | --- |
| `/mnt` | `/mnt` | Read Only | All folders, including shortcuts such as `appdata` |
| `/mnt-rw` | `/mnt` | Read/Write | Two-way folders only |

Then click **Apply**. The container picks the new paths up by itself and your selected folders stay as they are. You can remove the old `/data` and `/data-rw` paths afterwards.

**Icon missing?** Containers created from an older template have no icon. Edit the container, switch on **Advanced View** at the top right, paste the URL below into **Icon URL** and click **Apply**.

| Container | Icon URL |
| --- | --- |
| lsyncd-webui | `https://raw.githubusercontent.com/Mikaas111/lsyncd-webui/main/assets/icon.png` |
| lsyncd-webui-backup | `https://raw.githubusercontent.com/Mikaas111/lsyncd-webui/main/assets/icon-backup.png` |

## Security notes

- The backup container only accepts the main server's key, and it can only write to the backup folder you mounted.
- The main container reads mirrored folders through a read-only mount. It only writes to folders you set to two-way.
- The backup server's host key is trusted on the first connection and checked on every connection after that.
- All data travels encrypted over SSH, unless you set **Encryption** to **None**. Then the VPN is what encrypts it; the login still needs the main server's key. The speed test's network check sends unencrypted zeros, straight to a port the backup container opens for a few seconds and that only accepts a one-time token.
- The backup container listens on port 2223 for transfers without encryption, with the same key check as SSH. Set `PLAIN_PORT` to `0` to close it if you never use it.
- The web interface has no login unless you set `WEBUI_PASSWORD`. Never expose port 8484 to the internet.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| **Test connection** says the key was refused | Paste the key from **Settings** into **Main server key** on the backup container and click **Apply**. |
| **Test connection** times out | Check the VPN with `ping` from the main server, and that port 2222 is not blocked. |
| "Different host key than before" | You rebuilt or moved the backup container. If that is expected, click **Forget backup host key**. |
| A folder is "empty, skipped" | The folder is empty on the main server. The full sync skips it so a missing disk cannot wipe your backup. |
| Counts differ but **Compare** finds nothing | A file changed while counting. Click **Count files** again. |
| Something was not mirrored | Files written directly to `/mnt/cache` or `/mnt/diskX` bypass live sync. The nightly full sync picks them up, or click **Full sync now**. |
| Watched folders close to the limit | See *Large shares* above. |
| **Changes waiting** is much higher than the rise in **Files copied** | Normal: a change can also be a new folder, a move, a deletion or a file that was already on the backup. **Files copied** only counts files whose contents were sent. |
| A folder with many files shows 0% for a long time | rsync first scans the folder (**Scanning the main server: … items found**), then compares every item with the backup (**… of … items compared**). Both counts keep moving while it works. |
| A full sync shows **no progress for …** | The task card shows where rsync is (**Now at …**) and what it waits for. *The disk or file system*: something in that folder answers slowly or not at all, such as a network or cloud mount, or a failing disk. *The backup server*: the backup is slow to keep up, or the connection stalled; look at the backup container's log. |
| Syncing is slower than your internet connection | Run the speed test, see *Sync slower than expected?* above. |
| Saving **Encryption: None** fails with "No connection to port 2223" | Update the backup container, and check that nothing blocks port 2223 between the servers. |
| Saving a two-way folder says the container cannot write to it | Add the `/mnt-rw` path, see *Updating*. |
| A share shows no contents, or is marked *shortcut, target not reachable* | Add the `/mnt` path, see *Updating*. |
| A file named like `report (conflict on …)` appears | That file changed on both servers. The newest version kept the normal name; this is the other version. Delete it once you have checked it. |
| **Two-way sync is not running** | Open **Log** and look at the lines marked `[unison]`. |

## Environment variables

| Variable | Role | Default | Meaning |
| --- | --- | --- | --- |
| `ROLE` | both | `main` | `main` for the main server, `backup` for the backup server |
| `WEBUI_PORT` | main | `8484` | Port of the web interface |
| `WEBUI_PASSWORD` | main | empty | Password for the web interface; any user name works |
| `SSH_PORT` | backup | `2222` | Port the backup container listens on |
| `PLAIN_PORT` | backup | `2223` | Port for transfers without SSH encryption (Encryption: None on the main server); `0` turns it off |
| `AUTHORIZED_KEY` | backup | empty | Public key of the main server |
| `TZ` | both | `UTC` | Time zone, used for the nightly sync time |
| `DATA_DIR` | main | `/mnt/user`, else `/data` | Where the shares are read. Only set it for an unusual layout. |
| `DATA_RW_DIR` | main | `/mnt-rw/user`, else `/data-rw` | Where two-way folders are written. Only set it for an unusual layout. |

## Build your own image

Every push to `main` runs the tests and publishes `ghcr.io/<owner>/lsyncd-webui` through GitHub Actions ([`.github/workflows/docker.yml`](.github/workflows/docker.yml)). To run your own build from a fork:

1. Fork this repository and let the **Build and publish image** workflow finish.
2. Make the package public: your profile → **Packages → lsyncd-webui → Package settings → Change visibility → Public**.
3. Replace `mikaas111` with your GitHub name in lowercase in [`unraid/`](unraid) and [`compose/`](compose).

Locally:

```bash
python3 -m unittest discover -s tests -v
docker build -t lsyncd-webui .
```

## License

MIT, see [LICENSE](LICENSE).
