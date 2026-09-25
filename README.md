# Livestock LoRa Server — Raspberry Pi 5 + RYLR993 DIRECT

This project uses the **RYLR993 directly on the Raspberry Pi 5**. There is no ESP32 receiver between the Pi and LoRa module. The ESP32-C6 remains on each animal collar as the LoRa transmitter.

## Architecture

```text
Animal collar ESP32-C6
       |
       | 868 MHz LoRa
       v
RYLR993 receiver module
       | UART (3.3-V TTL)
       v
Raspberry Pi 5
       |
       +-- Python RYLR993 gateway
       +-- SQLite database
       +-- FastAPI web server
       +-- WebSocket live updates
       v
PC / phone / tablet browser
```

## What the Pi does

The Pi now performs everything your supplied ESP32 receiver sketch did:

- configures the RYLR993
- listens for `+RCV=...` packets
- extracts sender address, payload, RSSI and SNR
- sends `AT+SEND=<sender>,3,ACK`
- parses the telemetry fields
- stores telemetry in SQLite
- associates the collar with an animal
- broadcasts new telemetry to the browser in real time

Your supplied sketch uses 9600 baud, 8N1, point-to-point mode, 868 MHz, and receiver address 2; the Pi gateway uses those same defaults.

## RYLR993 -> Raspberry Pi wiring

If your RYLR993 exposes 3.3-V TTL UART:

```text
RYLR993 TX  -> Raspberry Pi RXD
RYLR993 RX  <- Raspberry Pi TXD
RYLR993 GND -> Raspberry Pi GND
RYLR993 VCC -> correct RYLR993 supply
```

**Do not connect a 5-V UART signal to the Pi GPIO UART.** Verify the voltage level of the exact RYLR993 board you have before wiring power or UART.

For easier first testing, a USB-to-3.3-V-TTL UART adapter can also be used. Linux will usually expose it as `/dev/ttyUSB0`. Set `LORA_PORT` accordingly.

## Raspberry Pi UART

For the Pi GPIO UART:

```bash
sudo raspi-config
```

Enable the serial hardware and disable the serial login shell. Then reboot:

```bash
sudo reboot
```

Check:

```bash
ls -l /dev/serial0
```

The included service uses `/dev/serial0`.

## Install

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
sudo useradd --system --create-home --shell /usr/sbin/nologin livestock || true
sudo usermod -aG dialout livestock
sudo mkdir -p /opt/livestock-monitor
sudo chown -R livestock:dialout /opt/livestock-monitor
```

Copy `app`, `static`, `requirements.txt`, and `systemd` into `/opt/livestock-monitor`, then:

```bash
cd /opt/livestock-monitor
sudo -u livestock python3 -m venv .venv
sudo -u livestock .venv/bin/pip install -r requirements.txt
```

## Test the LoRa gateway first

Before setting up automatic startup:

```bash
cd /opt/livestock-monitor
sudo -u livestock LORA_PORT=/dev/serial0 .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

You should see something similar to:

```text
[LORA] Opening /dev/serial0 @ 9600 8N1
[RYLR993 TX] AT
[RYLR993 TX] AT+OPMODE=1
[RYLR993 TX] AT+BAND=868000000
[RYLR993 TX] AT+ADDRESS=2
[RYLR993] Direct Pi gateway ready
```

When a collar transmits:

```text
[LORA RX] +RCV=1,...
[RYLR993 TX] AT+SEND=1,3,ACK
```

That confirms the Pi is directly operating the RYLR993.

## Web application

Find the Pi IP:

```bash
hostname -I
```

Open:

```text
http://PI_IP_ADDRESS:8000
```

The dashboard provides the foundation for:

- individual animal records
- live temperature
- live behavior
- GPS location
- movement path
- IMU statistics
- RSSI/SNR
- vaccination history
- disease/diagnostic history
- geofence data
- live browser updates

## Animal identity

The RYLR993 sender address identifies the collar, not permanently the animal. The database therefore keeps a mapping such as:

```text
LoRa address     Animal ID
1                COW-001
2                COW-002
3                GOAT-001
```

Unknown collars are automatically created as `ANIMAL-001`, etc., so their telemetry is not silently lost. Later, the animal-management interface should allow reassignment of a collar to the correct animal without deleting historical animal records.

## Current telemetry format

The server matches the receiver sketch you provided. The RYLR993 packet is:

```text
+RCV=<sender>,<length>,<payload>,<RSSI>,<SNR>
```

The payload currently contains:

```text
LIVE/RET,
boot_count,
temperature,
latitude,
longitude,
behavior_id,
ax_mean,
ay_mean,
az_mean,
ax_sqsum,
ay_sqsum,
az_sqsum,
ax_std,
ay_std,
az_std
```

Behavior IDs are currently:

```text
0 Feeding
1 Rumination
2 Standing
3 Lying
4 Walking
```

The raw payload is also stored. This makes it possible to extend the system later for the second IMU, battery voltage, GPS speed, collar timestamp, sequence number, etc.

## Production roadmap

The next version should add:

1. proper Animal Management screen
2. add/edit animal and collar assignment
3. vaccination forms
4. diagnostic forms
5. interactive geofence editor
6. geofence violation events
7. live map with all collars
8. historical path/date filters
9. temperature and IMU charts
10. behavior-duration analysis
11. battery and collar health
12. offline/last-seen status
13. CSV export
14. automatic database backups
15. second-IMU telemetry fields
16. collar-side timestamps and packet sequence numbers
17. local map tiles if the farm has no Internet
