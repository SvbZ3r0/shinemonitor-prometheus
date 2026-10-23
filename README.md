# shinemonitor-prometheus

Prometheus exporter for solar inverters that report through an Eybond / "LWIP"
Wi-Fi logger to ShineMonitor. It was developed against a 10 kW three-phase
inverter, so register addresses and scaling may differ on other hardware.

The exporter does not call the ShineMonitor web API. It reads the binary protocol
already exchanged between the logger and the ShineMonitor cloud.

## Status

Phase 1 is implemented: the project can relay or passively observe the logger's
cloud connection, decode the register blocks requested by the cloud, and expose
the resulting values to Prometheus. Without further options the relay inherits the
cloud's polling schedule, usually one read about every five minutes.

Phase 2 is implemented but off by default: with `--poll-interval` the relay also
reads the two live register blocks itself. It has passed offline tests and has not
yet had a live trial. An optional SQLite frame log records every frame. Local
heartbeats and register discovery are planned. See [Roadmap](#roadmap).

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
| Protocol ID | 2 bytes | Not necessarily zero; see the note below |
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

Observed field values: the cloud sends `0x0001` in the protocol ID field. The
logger answers `0x04` requests with `0x0001` and heartbeats with `0x0102`. Another
implementation ([ha-dessmonitor](https://github.com/andreas-glaser/ha-dessmonitor))
treats the same two bytes as a device code, so their meaning is not settled.

The cloud uses one transaction counter for all commands. It increases by one per
frame and starts at a different value in each session. Over two days of captures
it covered `0x1368` to `0x7d15`, so no ID range can be assumed to be unused.

A heartbeat request carries the current UTC time as six bytes (year minus 2000,
month, day, hour, minute, second) followed by a two-byte interval in seconds. The
captured cloud heartbeats carry 35 and arrive every 36 seconds. The logger replies
with its ID as ASCII text.

A `0x02` request lists several logger parameter numbers. The logger answers with
one frame per parameter, all with the request's transaction ID.

Older traffic logs written by sniff mode show six-byte all-zero "payloads" next to
many frames. These are Ethernet padding on bare TCP acknowledgements, not data on
the connection; tshark captures of the same traffic do not contain them. Sniff mode
now ignores the padding.
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

Phase 2 injects standard read-only Modbus function 03 requests inside outer
Eybond `0x04` frames on that same connection. A separate hardware approach would connect an RS-485 adapter
directly to the inverter's serial interface, bypassing both the Wi-Fi logger and
the cloud. That is outside this project's current scope and requires work at the
inverter.

### Components

| File | Responsibility |
|---|---|
| `shinemonitor_exporter.py` | CLI, Prometheus metrics, and mode selection |
| `shinemonitor_relay.py` | Accepts logger connections; plain forwarding, or a `LoggerLink` when polling is on |
| `shinemonitor_link.py` | Phase 2: serialised requests to the logger and active polling |
| `frame_log.py` | Optional SQLite log of every frame |
| `shinemonitor_logger.py` | Passive packet capture for sniff mode |
| `parser_utils.py` | Framing, CRC validation, request matching, and register decoding |
| `test_replay.py` | Recorded-traffic decoding and end-to-end relay checks |
| `test_link.py` | Offline tests for active polling and the frame log |
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
| `shinemonitor_unknown_frames_total{source,command}` | Frames with an Eybond command the parser does not know |
| `shinemonitor_poll_enabled` | Logger connections on which active polling is running |
| `shinemonitor_poll_requests_total{block,outcome}` | Injected reads; outcome is `ok`, `timeout`, `crc`, `exception` or `invalid` |
| `shinemonitor_cloud_hold_seconds` | Histogram of how long cloud frames waited before going to the logger |
| `shinemonitor_cloud_request_timeouts_total` | Cloud frames the logger did not answer within `--request-timeout` |
| `shinemonitor_frame_log_dropped_total` | Frames not written because the frame-log queue was full |

The polling and hold metrics only change when `--poll-interval` is set. The unknown
frame counter works in both modes.

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
--poll-interval SECONDS   Read the live blocks this often (relay mode; default 0 = off,
                          minimum 10)
--request-timeout SECONDS Wait this long for a logger reply (default 3)
--frame-db PATH           Record every frame to this SQLite file (relay mode)
--traffic-log PATH        Write captured packets in sniff mode
-v, --verbose             Enable debug logging
```

Active polling every 60 seconds, with a frame log:

```bash
python3 shinemonitor_exporter.py --poll-interval 60 --frame-db /home/gughan/shinemonitor/frames.db
```

Prometheus should scrape `/metrics` on the configured metrics port. Without polling,
a one-minute scrape interval is sufficient to retain each cloud-triggered update.
With polling, scrape at least as often as `--poll-interval`.

## Frame log

`--frame-db` records every frame the relay handles: frames from the cloud, frames
from the logger, and requests the relay injects. Each row holds the time, the
connection, the source, the header fields, whether the frame was forwarded, whether
its command is known, and the exact bytes. Keeping the bytes means stored traffic
can be decoded again when the parser learns new commands or registers.

The relay only puts frames on an in-memory queue. A separate thread writes them in
batches every two seconds. If the queue fills, frames are dropped and counted in
`shinemonitor_frame_log_dropped_total`; forwarding is never delayed by the database.

The captures suggest about 3,600 frames per day without polling and about 1,500
more with 60-second polling, roughly 1 MB per day. Nothing is pruned yet.

Useful queries:

```sql
-- commands the parser does not know
SELECT datetime(ts, 'unixepoch', 'localtime'), source, printf('0x%02x', command), hex(raw)
FROM frames WHERE known = 0 ORDER BY ts DESC LIMIT 50;

-- injected reads per connection
SELECT connection, count(*) FROM frames WHERE source = 'relay' GROUP BY connection;
```

The file contains device identifiers and operating history. Keep it out of Git.

## Testing with recorded traffic

`test_replay.py` reads an existing `modbus_transactions.csv` and
`full_session_traffic_log.txt` from a supplied directory:

```bash
python3 test_replay.py /path/to/capture-data
```

The test decodes both capture formats, starts a local relay with fake logger and
cloud endpoints, verifies that forwarded bytes remain unchanged, and checks for
representative values on the Prometheus endpoint.

`test_link.py` needs no capture data:

```bash
python3 test_link.py
```

Its fake logger answers only the newest request when two arrive close together,
as the real logger did in the captures. The tests check that:

- injected replies never reach the cloud;
- the cloud receives exactly the logger's replies to its own frames while polling runs;
- two cloud requests sent together are both answered (in passthrough mode one is lost);
- a cloud frame that arrives during an injected read waits for that read to finish;
- three failed reads in a row stop polling for the connection;
- a late injected reply is dropped;
- the frame log records frames in both modes, including unknown commands.

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

### Phase 2: optional active polling (implemented, awaiting live trial)

With `--poll-interval` set, each logger connection is handled by `LoggerLink`
instead of two byte pipes. The design follows what the captures show:

- **The logger answers only the newest request.** Six times in two days, two cloud
  requests reached the logger within 0.3 seconds of each other, and the first was
  never answered. So every frame towards the logger, the cloud's included, goes
  through one queue, with one request outstanding at a time. Cloud frames have
  priority over injected reads.
- **Cloud frames are forwarded unchanged.** The relay does not rewrite cloud
  transaction IDs. Logger replies are matched to their requester by transaction ID;
  anything that is not a reply to an injected read is forwarded to the cloud as it
  arrived.
- **Injected IDs stay away from the cloud's counter.** They start half the ID space
  away from the cloud's latest ID and jump again if the counter comes within 4,096.
  An injected ID is remembered for 60 seconds so a late reply is still dropped.
- **Frames too short to have a header** are forwarded at once. None appeared in
  the captures; the frame log will show if any do.
- **Only the live blocks are polled.** 30100 and 30131 are read every interval.
  The cloud already reads the static blocks every five minutes.
- **Polling waits for quiet.** A read is sent only when nothing is outstanding and
  the cloud has been silent for three seconds. The cloud reads its four blocks in
  about 1.3 seconds every five minutes and sends a heartbeat every 36 seconds.
  The logger replies in about 0.33 seconds (95th percentile 0.35 s), so a cloud
  frame that does arrive during an injected read normally waits less than that.
- **Failures back off.** A timeout, CRC error, exception response or malformed
  reply ends the current cycle. Three in a row stop polling for that connection.
  In the captures the logger disconnected every evening and reconnected in the
  morning, which would restart polling each day.
- **Polling starts one interval after the logger connects**, so the cloud's
  first reads go first.

Function 03 is the only request the relay can build: `EybondFrame.inverter_request`
accepts only `ModbusReadRequest`.

The live trial should start during daylight with someone present, watching
`shinemonitor_poll_requests_total`, `shinemonitor_cloud_hold_seconds`,
`shinemonitor_cloud_request_timeouts_total` and the ShineMonitor app.

### Phase 2b: local heartbeats

The relay will answer the logger's heartbeats itself and keep the logger
connection, and polling, when the cloud is unreachable. The heartbeat format is
known (see [Outer Eybond wrapper](#outer-eybond-wrapper)). Before this is built, a
capture is needed of what the logger does when a session starts and when the
cloud stops answering. The cloud session should still follow the logger session,
so the app does not show the logger online at night.

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

### Later: import of cloud history

Once the above is stable, a one-off import from the ShineMonitor web API can bring
earlier daily energy figures into local storage.

## Safety boundaries

This project is read-only. Writing registers, changing inverter settings, and
replacing the cloud service are out of scope. Answering the logger's heartbeats on
the cloud's behalf (Phase 2b) is in scope; frames from the cloud are still
forwarded to the logger unchanged. Active polling and scanning add load
to the logger even though they do not write to the inverter, so both require
rate-limiting, error back-off, offline replay tests, and a supervised live trial.

## License

GPL-3.0. See `LICENSE`.
