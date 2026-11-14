# lsyncd-webui

A live, one-way mirror from a main server to a backup server, with a web interface.
Every file you create, change, rename or delete on the main server shows up on the backup within seconds.
Built for Unraid, but it runs on any machine with Docker.

!! DISCLAIMER !!
WATCH OUT!! This is a fully vibecoded tool that I needed for something at home.
It works great and you are free to use it, but use it AT YOUR OWN RISK!
I am NOT responsible for ANY lost data!!

```
 Main server                                 Backup server
┌──────────────────────────┐   SSH over    ┌──────────────────────────┐
│ lsyncd-webui             │   your VPN    │ lsyncd-webui-backup      │
│  web UI + lsyncd         │ ────────────▶ │  SSH server + rsync      │
│  /mnt/user → /data (ro)  │   port 2222   │  /mnt/user/backup        │
└──────────────────────────┘               └──────────────────────────┘
```

Image: `ghcr.io/mikaas111/lsyncd-webui:latest`

## Features

- **Live sync.** lsyncd hears about every change from the kernel and sends only that file. The backup server never scans anything, so a slow backup machine keeps up.
- **Exact copy.** Deletions and renames are mirrored too. You can switch that off.
- **Pick folders in the browser.** Tick shares or subfolders in a folder tree.
- **Excludes.** Skip files like `*.tmp` or whole folders like `cache/`.
- **Progress.** See how far a full sync is, with speed and time left.
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

Both containers carry everything they need (Alpine Linux, lsyncd, rsync, OpenSSH, Python). The host only provides Docker, the folders and the network, so it does not matter which Linux your server runs.

## What you need

- Two servers with Docker: Unraid 7, Unraid 8, or any Linux machine.
- A network path from the main server to the backup server on TCP port 2222. Between two locations that is usually a VPN such as WireGuard. On the same LAN nothing extra is needed.

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
| Shares (read only) | `/mnt/user`. The container can read your shares but never change them. |
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

1. Go to **Folders**, tick the shares or folders you want on the backup, and click **Save**. A full sync starts right away. Follow it on **Overview**.
2. Optionally, go to **Excludes** and add patterns for files you never want on the backup.

The first full sync copies everything and can take a long time. With a lot of data, you can do the first sync with both servers on the same network and move the backup server afterwards.

### Step 6: Check that both servers match

On **Overview**:

- **Count files** counts folders, files and size on both servers. Every row should say **Identical**.
- **Compare** lists every file that differs. **Identical: no differences found** means the backup is an exact copy.

Counting also runs after every full sync, including the nightly one, so the overview stays current.

## The web interface

| Tab | What it does |
| --- | --- |
| **Overview** | Live sync status, the queue of changes waiting to be sent, progress of the running task, counts on both servers and the last comparison. |
| **Folders** | Folder tree of the main server. Tick what to mirror and see where each folder goes on the backup. |
| **Excludes** | Patterns for files and folders to skip, one per line. |
| **Settings** | Backup server address, delay, deletion mirroring, compression, bandwidth limit, nightly sync time and the SSH key. |
| **Log** | Everything lsyncd, rsync and the app report. |

## Large shares: raise the watch limit

lsyncd needs one *inotify watch* per folder it watches. **Overview** shows how many folders are watched and the system limit. If the number gets close to the limit, raise it on the **main server's host**:

```bash
sysctl -w fs.inotify.max_user_watches=1048576
```

That command lasts until the next reboot. To make it stick, run it at every boot: on Unraid 7 with the *User Scripts* plugin (schedule **At Startup of Array**) or the *Tips and Tweaks* plugin; on other Linux systems with a file in `/etc/sysctl.d/`.

## Unraid 8 and other systems

Unraid 8 moves from Slackware to a Fedora base. lsyncd-webui does not depend on the host system:

- lsyncd, rsync, SSH and Python all live inside the containers. The host only provides Docker, the mounted folders and the network.
- It does not use any Unraid plugin, script or host SSH setting.
- Unraid 8 adds native Docker Compose, and the files in [`compose/`](compose) work as they are.

## Updating

On Unraid: **Docker → Check for Updates → Apply Update**. With Compose: `docker compose pull && docker compose up -d`. Settings, keys and status live in `/config`, so they survive updates.

## Security notes

- The backup container only accepts the main server's key, and it can only write to the backup folder you mounted.
- The main container mounts your shares read only.
- The backup server's host key is trusted on the first connection and checked on every connection after that.
- All data travels encrypted over SSH.
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

## Environment variables

| Variable | Role | Default | Meaning |
| --- | --- | --- | --- |
| `ROLE` | both | `main` | `main` for the main server, `backup` for the backup server |
| `WEBUI_PORT` | main | `8484` | Port of the web interface |
| `WEBUI_PASSWORD` | main | empty | Password for the web interface; any user name works |
| `SSH_PORT` | backup | `2222` | Port the backup container listens on |
| `AUTHORIZED_KEY` | backup | empty | Public key of the main server |
| `TZ` | both | `UTC` | Time zone, used for the nightly sync time |

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
