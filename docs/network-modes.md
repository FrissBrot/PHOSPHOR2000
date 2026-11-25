# Network modes, internet sharing and package updates

PHOSPHOR2000 runs in three network modes. All Pis of a cluster use the same mode. Choose it at
installation (`install.sh --network hotspot|wlan|ethernet`) or later in the dashboard
(Config → Network), which switches all Pis.

| mode | how the Pis are connected | dashboard | leader change |
|---|---|---|---|
| **hotspot** (default) | the master opens the Wi-Fi `PHOSPHOR2000`, all others join it | `http://192.168.50.1:8080/admin/` | Wi-Fi drops for 20–45 s while the hotspot moves |
| **wlan** | every Pi joins an existing Wi-Fi (SSID + password) | `http://phosphor.local:8080/admin/` (mDNS) or any Pi's address | a few seconds, the Wi-Fi stays up |
| **ethernet** | every Pi on one switch (with or without router) | `http://192.168.50.1:8080/admin/`: the master additionally opens the `PHOSPHOR2000` Wi-Fi for phones | a few seconds; the admin Wi-Fi moves with the master |

Addresses in wlan/ethernet mode: DHCP if the network has a router, **plus** a fixed cluster
address `10.20.0.1N` (pi-0N) — so a plain switch without any router works too. Cluster datagrams
only go out on the cluster interface (never on a second network the Pi is also cabled to).
Election and failover are the same as in hotspot mode, but there is no hotspot to move, so a new
master takes over after failure detection + backoff only.

## Switching from the dashboard

Config → Network: choose the mode (for an existing Wi-Fi enter name and password) → *switch all
Pis*. The Wi-Fi password is encrypted with the cluster key before it is distributed. 15 s later
every Pi reconfigures NetworkManager (root oneshot `phosphor-netmode.service`) and restarts its core.

**Safety net:** before switching, every Pi stores its previous mode. If it does not find its
cluster again within 3 minutes (e.g. wrong Wi-Fi password, cable unplugged, Wi-Fi out of range),
it returns to the previous mode by itself and reports the failure; the master then cancels the
request so nothing loops. Pis that succeeded stay in the new mode — the dashboard shows the
mode per Pi (Updates tab, "network" column). Still: switch modes during set-up, not during a show.

Hand-off from the command line: `sudo /opt/phosphor2000/scripts/configure-network.sh --mode wlan
--wlan-ssid "Venue" --wlan-psk-file /root/psk.txt` (or `--mode hotspot` / `--mode ethernet`).

## Internet for updates

* **hotspot mode:** plug an Ethernet cable with internet into the **master** (the audio Pi).
  Dashboard → Config → Network → *share the master's cable internet with all Pis*. The master's
  hotspot already NATs (NetworkManager "shared"); the switch makes the other Pis use it as
  default route (metric 700: a Pi with its own cable keeps using its cable). Only the current
  master shares — if another Pi becomes master, it needs the cable.
* **wlan / ethernet mode:** the Pis use the router of that network directly, if it has internet.
* "internet" column per Pi in Updates; *check internet* tests the package mirror.

## OS package updates (Raspberry Pi OS)

Dashboard → Updates → *update packages on this Pi* or *on all Pis* (one after the other, least
important first, master last; only in a music pause unless *also during live music* is ticked).
Each Pi runs `apt-get update && apt-get upgrade` (keeps existing configuration files) via the root
oneshot `phosphor-apt.service`; the table shows running / done / error, the number of packages
and **reboot!** when a kernel, firmware, libc or systemd update needs a reboot (use the reboot
button per Pi in the Cluster tab — not automatic, so you choose the moment). Refused on a
read-only root (`--readonly`): such updates would vanish at the next boot.

Software updates of PHOSPHOR2000 itself (Updates tab) work the same way once the Pis have internet:
each Pi can then download from GitHub directly, and new Python dependencies can be installed.

Not verified on hardware yet: NetworkManager accepting DHCP + fixed address with endless DHCP
retries on all Pi OS versions, `nmcli device reapply` for the uplink switch, avahi `phosphor.local`
on Android phones (iOS resolves .local; many Android browsers do too since Android 12).
