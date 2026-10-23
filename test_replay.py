"""Replay recorded logger traffic through the decoder and the relay. Run from the code directory."""
import asyncio, csv, re, socket, sys, time, urllib.request
from parser_utils import StreamDecoder, crc16_modbus

DATA = sys.argv[1]   # directory with modbus_transactions.csv and full_session_traffic_log.txt

def check(name, readings):
	if not readings:
		print(f"[{name}] skipped: no decodable readings in input")
		return
	energy = [r['energy_total'] for _, r in readings if 'energy_total' in r]
	power = [r['output_power'] for _, r in readings if 'output_power' in r]
	print(f"[{name}] readings={len(readings)} energy_total {min(energy)}..{max(energy)} kWh, "
		f"monotonic={all(b >= a for a, b in zip(energy, energy[1:]))}, max power {max(power)} W, min power {min(power)} W")
	# integrate power within each day and compare with the inverter's own counter
	days = {}
	for t, r in readings:
		if 'output_power' in r:
			days.setdefault(time.strftime('%Y-%m-%d', time.localtime(t)), []).append((t, r))
	for day, rs in sorted(days.items())[:6]:
		integ = sum(a[1]['output_power'] * (b[0] - a[0]) for a, b in zip(rs, rs[1:]) if b[0] - a[0] < 900) / 3.6e6
		today = [r['energy_today'] for _, r in rs]
		print(f"   {day}: integrated power {integ:6.2f} kWh vs energy_today {min(today):5.1f}->{max(today):5.1f}  ({len(rs)} samples)")

# 1. CSV (pcap of 17 Mar 2024): request column = cloud, response column = logger
crc_errors = []
dec = StreamDecoder(on_crc_error=lambda: crc_errors.append(1))
readings, info = [], {}
for row in csv.DictReader(open(f'{DATA}/modbus_transactions.csv')):
	t = float(row['time'])
	dec.feed('cloud', bytes.fromhex(row['request_payload'].replace(':', '')))
	v = dec.feed('logger', bytes.fromhex(row['response_payload'].replace(':', '')))
	if v:
		info.update({k: x for k, x in v.items() if isinstance(x, str)})
		readings.append((t, v))
check('csv', readings)
print('   device info:', info, '| crc errors:', len(crc_errors))
sample = next(r for _, r in readings if 'grid_voltage_r' in r)
print('   grid sample:', {k: sample[k] for k in ('grid_voltage_r', 'grid_current_r', 'grid_frequency', 'dci', 'internal_ambient_temp')})

# 2. Long traffic log from the Pi-hole (both directions, April 2024)
# (this recording only contains the logger-to-cloud direction, so blocks are matched by size)
rx = re.compile(r'(\S+ \S+) - (\S+):(\d+) -> (\S+):(\d+) \| Payload size: \d+ \| Payload: ([0-9a-f]*)')
dec = StreamDecoder(on_crc_error=lambda: crc_errors.append(1))
readings, crc_errors[:] = [], []
for line in open(f'{DATA}/full_session_traffic_log.txt', errors='replace'):
	m = rx.match(line)
	if not m or not m.group(6):
		continue
	t = time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S'))
	v = dec.feed('logger' if m.group(5) == '502' else 'cloud', bytes.fromhex(m.group(6)))
	if v:
		readings.append((t, v))
check('traffic log', readings)
print('   crc errors:', len(crc_errors))

# 3. Relay end-to-end with the March capture (both directions): the fake cloud sends each recorded
#    request and waits for the recorded reply; the fake logger answers each request in small pieces.
from parser_utils import ShinemonitorModbusTCPParser
split = ShinemonitorModbusTCPParser.split_frames
requests, replies = [], {}
for row in csv.DictReader(open(f'{DATA}/modbus_transactions.csv')):
	requests += split(bytes.fromhex(row['request_payload'].replace(':', '')))[0]
	for f in split(bytes.fromhex(row['response_payload'].replace(':', '')))[0]:
		replies[f[:2]] = f
pairs = [(q, replies[q[:2]]) for q in requests if q[:2] in replies]
print(f'[relay] {len(pairs)} recorded request/reply pairs')

async def relay_test():
	from shinemonitor_exporter import PrometheusExporter
	from shinemonitor_relay import Relay
	exp = PrometheusExporter(19127)
	exp.start_export()
	relay = Relay('unused', 15021, '127.0.0.1', exp.data_processor, exp.connection_change, exp.crc_errors.inc)
	server = await asyncio.start_server(relay.handle, '127.0.0.1', 15020)
	results = {'cloud_ok': 0, 'logger_ok': 0}

	async def fake_cloud(reader, writer):
		for req, reply in pairs:
			writer.write(req); await writer.drain()
			got = await reader.readexactly(len(reply))
			results['cloud_ok'] += got == reply
		writer.close()
	cloud = await asyncio.start_server(fake_cloud, '127.0.0.1', 15021)

	r, w = await asyncio.open_connection('127.0.0.1', 15020)
	for req, reply in pairs:
		got = await asyncio.wait_for(r.readexactly(len(req)), 5)
		results['logger_ok'] += got == req
		for i in range(0, len(reply), 7):		# small pieces, to exercise frame reassembly
			w.write(reply[i:i + 7]); await w.drain()
	await asyncio.sleep(0.5)
	w.close(); server.close(); cloud.close()
	print(f"[relay] requests arriving unchanged at the logger: {results['logger_ok']}/{len(pairs)}, "
		f"replies arriving unchanged at the cloud: {results['cloud_ok']}/{len(pairs)}")
	body = urllib.request.urlopen('http://127.0.0.1:19127/metrics').read().decode()
	for name in ('shinemonitor_output_power ', 'shinemonitor_energy_total ', 'shinemonitor_grid_frequency ', 'shinemonitor_up ',
			'shinemonitor_device_info', 'shinemonitor_crc_errors_total', 'shinemonitor_logger_connections '):
		print('  ', next((l for l in body.splitlines() if l.startswith(name)), f'{name}MISSING'))

asyncio.run(relay_test())
