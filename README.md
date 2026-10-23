# shinemonitor-prometheus

Prometheus exporter for solar inverters that report through an Eybond / "LWIP"
Wi-Fi logger to ShineMonitor. It was developed against a 10 kW three-phase
inverter, so register addresses and scaling may differ on other hardware.

The exporter does not call the ShineMonitor web API. It reads the binary protocol
already exchanged between the logger and the ShineMonitor cloud.

## Status

Phase 1 is implemented: the project can relay or passively observe the logger's
cloud connection, decode the register blocks requested by the cloud, and expose
the resulting values to Prometheus.

The relay currently inherits the cloud's polling schedule, usually one read about
every five minutes. Active read-only polling and register discovery are planned but
not implemented. See [Roadmap](#roadmap).

## Architecture

The Wi-Fi logger opens a long-lived TCP connection to the ShineMonitor service on
port 502. The cloud sends requests over that connection; the logger answers after
reading the inverter over its serial link.

Relay mode places this exporter between those two endpoints:

```text
                                      decoded values
                                            |
                                            v
+--------+      TCP :502      +---------------------------+      TCP :502      +--------------------+
| logger | <----------------> | relay + stream decoder    | <----------------> | ShineMonitor cloud |
+--------+                     +---------------------------+                    +--------------------+
                                            |
                                            v
                                  Prometheus HTTP :9127
```

The forwarding path is independent of decoding. Bytes received from either side
are sent to the other side unchanged. A copy is passed to the stream decoder;
malformed or unknown data is logged and does not interrupt forwarding. This allows
the ShineMonitor application to continue working while Prometheus collects local
metrics.

To route the logger through the relay, either change its server setting or give
only that logger a DNS answer that maps `www.shinemonitor.com` to the relay host.
The relay resolves the real cloud address through public DNS, avoiding a loop when
local DNS contains such an override. `--upstream-ip` can bypass upstream DNS.

### Protocol layers

There are two distinct protocols on this path. The outer layer is Eybond's own
logger-to-cloud format. The inner layer is standard Modbus RTU between the logger
and the inverter.

#### Outer Eybond wrapper

The outer frame borrows the Modbus TCP header layout and uses the usual Modbus port,
502. It is not Modbus TCP. Modbus TCP requires a protocol ID of zero, while observed
Eybond traffic uses other values, including `0x0102` in heartbeat replies. The unit
ID is normally `0xff`.

| Field | Size | Notes |
|---|---:|---|
| Transaction ID | 2 bytes | Set by the requester and copied into the reply |
| Protocol ID | 2 bytes | Eybond protocol value; not necessarily zero |
| Length | 2 bytes | Number of bytes following the length field |
| Unit ID | 1 byte | Normally `0xff` |
| Command | 1 byte | Eybond command code, not a Modbus function code |
| Payload | variable | Command-specific data |

Known outer command codes are:

| Command | Purpose |
|---:|---|
| `0x01` | Heartbeat and time synchronization |
| `0x02` | Logger parameters |
| `0x04` | Pass an enclosed Modbus RTU frame to the inverter |

The connection direction also differs from a normal Modbus TCP deployment. The
logger does not listen as a Modbus server. It opens an outbound connection and
waits for the cloud to send requests on that connection.

#### Inner Modbus RTU frame

The payload of an outer `0x04` command is a complete Modbus RTU frame, including
its CRC. A known read request has this form:

```text
01 03 <start register> <register count> <CRC-16>
```

Here, `01` is the inverter's Modbus slave address and `03` is the standard
read-holding-registers function. The logger removes the Eybond wrapper, sends the
inner frame to the inverter over its serial connection, and wraps the inverter's
reply before returning it to the cloud.

Register addresses such as 30100 and 30131, along with their value layouts and
scaling, belong to the inverter manufacturer. They are not defined by Eybond.

`StreamDecoder` buffers partial TCP reads, splits concatenated Eybond frames, and
tracks outstanding requests by outer transaction ID. When a logger reply arrives,
the decoder uses the start address from the corresponding inner Modbus request to
select a register block. It validates the inner RTU CRC before publishing values.
A byte-count fallback can decode some replies captured without their requests.

#### Why the relay is necessary

A standard Modbus TCP client cannot query this logger. The logger does not accept
inbound connections, and plain Modbus TCP frames do not contain the Eybond command
wrapper it expects. The relay joins the outbound connection opened by the logger
and understands both layers.

Phase 2 will inject standard read-only Modbus function 03 requests inside outer
Eybond `0x04` frames. A separate hardware approach would connect an RS-485 adapter
directly to the inverter's serial interface, bypassing both the Wi-Fi logger and
the cloud. That is outside this project's current scope and requires work at the
inverter.

### Components

| File | Responsibility |
|---|---|
| `shinemonitor_exporter.py` | CLI, Prometheus metrics, and mode selection |
| `shinemonitor_relay.py` | Bidirectional asynchronous TCP forwarding |
| `shinemonitor_logger.py` | Passive packet capture for sniff mode |
| `parser_utils.py` | Framing, CRC validation, request matching, and register decoding |
| `test_replay.py` | Recorded-traffic decoding and end-to-end relay checks |
| `shinemonitor-exporter.service` | Example systemd service for relay mode |

## Data currently decoded

The observed cloud requests cover 74 registers in four blocks:

| Start | Count | Contents |
|---:|---:|---|
| 30000 | 7 | Firmware version |
| 30010 | 38 | Model, serial number, and rated power |
| 30100 | 25 | AC output, energy counters, state, and PV strings |
| 30131 | 17 | Three-phase grid measurements, DC bus, insulation, fault currents, and temperatures |

Power, energy, and operating-time counters that span two registers are decoded as
32-bit, high-word-first values. Signed fields and per-field scale factors are
defined in `parser_utils.py`.

The current map is based on one inverter and firmware family. Treat readings from
other models as unverified until they have been compared with the inverter display
or another trusted source.

## Metrics

All numerical inverter readings are Prometheus gauges named
`shinemonitor_<field>`. Examples:

```text
shinemonitor_output_power
shinemonitor_energy_today
shinemonitor_energy_total
shinemonitor_pv1_voltage
shinemonitor_grid_voltage_r
shinemonitor_grid_frequency
shinemonitor_internal_ambient_temp
```

The exporter also provides:

| Metric | Meaning |
|---|---|
| `shinemonitor_device_info{model,serial,firmware,logger_id}` | Static device identity |
| `shinemonitor_rated_power` | Inverter rated power |
| `shinemonitor_up` | `1` when a numerical reading arrived within `--stale-after` seconds |
| `shinemonitor_last_update_timestamp_seconds` | Unix time of the latest numerical reading |
| `shinemonitor_logger_connections` | Open logger connections in relay mode |
| `shinemonitor_crc_errors_total` | Replies discarded because their RTU CRC was invalid |

Gauges retain their last values when the inverter stops reporting. Check
`shinemonitor_up` or the last-update timestamp before treating a value as current.

## Installation

Python 3 is required. Relay mode needs `prometheus_client` and `dnspython`; sniff
mode also needs Scapy and packet-capture privileges.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Listening on port 502 usually requires root or `CAP_NET_BIND_SERVICE` on Linux.
The example systemd unit grants that capability without running the process as
root. Review its user and paths before installing it.

## Running

Relay mode is the default:

```bash
python3 shinemonitor_exporter.py
```

Equivalent explicit settings:

```bash
python3 shinemonitor_exporter.py \
  --mode relay \
  --listen 0.0.0.0:502 \
  --upstream www.shinemonitor.com:502 \
  --metrics-port 9127
```

Passive sniffing is available when the host can see the logger's traffic, such as
on the network gateway or a mirrored switch port:

```bash
python3 shinemonitor_exporter.py \
  --mode sniff \
  --logger-host 192.0.2.10 \
  --metrics-port 9127
```

`192.0.2.10` is a documentation address; replace it with the logger's LAN address.
An ordinary machine on a switched network generally cannot use sniff mode.

Useful options:

```text
--stale-after SECONDS     Age at which shinemonitor_up becomes 0 (default: 600)
--upstream-ip ADDRESS     Connect to this cloud address instead of resolving DNS
--traffic-log PATH        Write captured packets in sniff mode
-v, --verbose             Enable debug logging
```

Prometheus should scrape `/metrics` on the configured metrics port. With Phase 1,
a one-minute scrape interval is sufficient to retain each cloud-triggered update.

## Testing with recorded traffic

`test_replay.py` reads an existing `modbus_transactions.csv` and
`full_session_traffic_log.txt` from a supplied directory:

```bash
python3 test_replay.py /path/to/capture-data
```

The test decodes both capture formats, starts a local relay with fake logger and
cloud endpoints, verifies that forwarded bytes remain unchanged, and checks for
representative values on the Prometheus endpoint.

Capture files may contain device identifiers, private addresses, and operational
history. They are ignored by Git and should not be published without review.

## Failure behavior

- Decode failures do not stop byte forwarding.
- Bad-CRC replies are ignored and counted.
- If the upstream cloud is unreachable, the logger connection is closed so it can
  reconnect normally.
- Upstream DNS answers are cached for an hour. If refresh fails, the last known
  address is reused.
- `shinemonitor_up` reports freshness, not process health or cloud availability.

Relay mode is transparent at the application layer, but it is still an additional
network dependency. Keep a way to remove the DNS override or restore the logger's
original server setting.

## Roadmap

### Phase 2: optional active polling

The planned relay extension will request the two live blocks itself, typically
every 30 seconds. It will be disabled by default with `--poll-interval 0`, keeping
the current relay behavior unchanged unless explicitly enabled.

The design constraints are:

- Read-only Modbus function 03. There will be no code path for register writes.
- Injected requests use a transaction-ID range not used by observed cloud traffic.
- Only one request may be outstanding at the logger. Cloud traffic has priority;
  if an injected read is in flight, a cloud request is held briefly and then sent.
- Injected replies are decoded locally and never forwarded to the cloud.
- A timeout, CRC error, or reconnect pauses polling. Repeated timeouts disable it
  until restart.
- Static identification blocks are read once per connection; live output and grid
  blocks are read on the configured interval.

Offline tests must prove that injected replies cannot reach the fake cloud, cloud
requests are delayed only while an injected request is outstanding, and timeout
back-off works. A live trial should start during daylight with an operator present.
Prometheus must scrape at least as often as the polling interval to retain the
additional samples.

### Phase 3: read-only register discovery

After Phase 2 is stable, the same request machinery can support a one-off scanner
for unknown register ranges. This will not run as part of the service.

The proposed scan reads small ranges with function 03, waits for an idle link, and
stops after repeated exceptions or timeouts. Two passes at different operating
conditions can separate changing measurements from static settings. Results belong
in a CSV containing register address, raw values, exception state, and any tentative
interpretation.

Unknown values must not become named metrics until they have been checked against
the inverter display, ShineMonitor, or another reliable reference.

## Safety boundaries

This project is read-only. Writing registers, changing inverter settings, and
replacing the cloud service are out of scope. Active polling and scanning add load
to the logger even though they do not write to the inverter, so both require
rate-limiting, error back-off, offline replay tests, and a supervised live trial.

## License

GPL-3.0. See `LICENSE`.
