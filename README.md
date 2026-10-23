# shinemonitor-prometheus

Prometheus exporter for solar inverters that report to ShineMonitor through an Eybond / "LWIP" WiFi logger.
Tested with a KSY 10 kW three-phase inverter (firmware H910-31100-60).

## How it gets the data

The logger keeps a TCP connection open to `www.shinemonitor.com:502`. The cloud uses it to read four
blocks of inverter registers every few minutes. This exporter decodes those replies.

**Relay mode (default).** Point the logger at the machine running the exporter. Each connection is
forwarded to the real cloud unchanged, so the ShineMonitor app keeps working, and the traffic is
decoded on the way through. Two ways to point the logger here:

- the logger's own "server" setting, if its web page has one, or
- a DNS record for `www.shinemonitor.com` that only the logger uses.

The relay resolves the real cloud address through public DNS (1.1.1.1 / 8.8.8.8), so a local DNS
override doesn't loop back to itself.

**Sniff mode.** Passive packet capture with scapy. Only works on a machine that actually sees the
logger's traffic (its gateway, or a mirrored switch port).

## Running

```
pip install -r requirements.txt
python3 shinemonitor_exporter.py                    # relay on :502, metrics on :9127
python3 shinemonitor_exporter.py --mode sniff --logger-host 192.168.0.243
```

`shinemonitor-exporter.service` is a systemd unit for relay mode (adjust the user and path).

## Metrics

All inverter values are `shinemonitor_<name>` gauges, for example `shinemonitor_output_power` (W),
`shinemonitor_energy_today` and `shinemonitor_energy_total` (kWh), PV string and grid voltages and
currents, frequency and temperatures. Also:

- `shinemonitor_device_info{model, serial, firmware, logger_id}`
- `shinemonitor_rated_power`
- `shinemonitor_up`: 1 if a reading arrived in the last 10 minutes (the logger is off at night,
  and the gauges keep their last value)
- `shinemonitor_last_update_timestamp_seconds`, `shinemonitor_logger_connections`,
  `shinemonitor_crc_errors_total`

## Register map

| Block start | Registers | Contents |
|---|---|---|
| 30000 | 7 | firmware version (text) |
| 30010 | 38 | model and serial (text), rated power |
| 30100 | 25 | AC output, energy counters, status, PV strings |
| 30131 | 17 | grid voltages, currents, frequency, DC bus, insulation, temperatures |

Power, energy and run-time counters are 32-bit values over two registers, high word first. Each
reply is matched to its request by transaction ID and checked against the Modbus CRC.

`test_replay.py <dir>` replays recorded traffic (`modbus_transactions.csv`,
`full_session_traffic_log.txt`) through the decoder and the relay.
