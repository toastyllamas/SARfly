# Raspberry Pi 5 Bring-Up Checklist

First-time setup notes for deploying this project on a Raspberry Pi 5 (4GB),
starting from a blank SD card. Once the host prerequisites below are done,
everything in the main [`README.md`](../README.md) applies unchanged — same
`docker compose up --build`, no compose file edits needed.

## 1. Flash the OS

- **Raspberry Pi OS Lite (64-bit, Bookworm)**. 64-bit is required to match
  the multi-arch (amd64/arm64) image this project already builds; Lite is
  fine since this runs headless.
- Use **Raspberry Pi Imager** and its gear-icon "Edit Settings" before
  writing:
  - Set hostname (e.g. `sar-pi`)
  - Enable SSH (password or your public key)
  - Set Wi-Fi SSID/password if not using Ethernet
  - Set username/password
- This gets you straight to `ssh <user>@sar-pi.local` with no
  monitor/keyboard needed.

## 2. Power

Pi 5 is sensitive to underpowered supplies, and this project adds a BLE USB
adapter + USB GPS on top of normal draw. Use the **official 27W USB-C PD
supply** — a marginal supply can produce flaky Bluetooth/USB behavior that
looks like a software bug but isn't.

## 3. Storage

SD card is fine to get started. If this becomes a semi-permanent field unit,
consider a USB3 SSD or the M.2 HAT later for better tolerance of repeated
hard power-cycles in the field — not needed for initial bring-up.

## 4. Host prerequisites (same as README, on the Pi this time)

```
sudo apt update && sudo apt install -y bluez gpsd gpsd-clients

# GPS device path -- adjust if it enumerates differently
sudo sed -i 's|^DEVICES=.*|DEVICES="/dev/ttyUSB0"|' /etc/default/gpsd
sudo systemctl enable --now gpsd.socket gpsd.service
cgps -s   # confirm a fix

# Sena UD100
lsusb | grep -i sena
hciconfig -a
systemctl status bluetooth
```

Docker + Compose plugin:

```
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out/in (or reboot) for the group change to take effect
```

## 5. Deploy

```
git clone git@github.com:toastyllamas/SARfly.git
cd SARfly
docker compose up --build
```

Open `http://sar-pi.local:8080` (or the Pi's IP) from another machine on the
same network for the ground-station UI.

## 6. Known gaps to expect on first Pi run

Carried over from the main README's "Known limitations" section — none of
these are Pi-specific, but they'll be the first things to hit:

- No auth on the ground-station UI — keep it on a trusted field LAN.
- Map tiles need internet; markers/heatmap work fine offline.
- `privileged: true` on the scanner is broad — fine for bring-up, worth
  narrowing later.
- This is the **first real Pi deployment** — the stack has only been
  validated on an x86_64 dev laptop so far, so treat the first run as a
  bring-up/debug session, not a known-good drop-in.
