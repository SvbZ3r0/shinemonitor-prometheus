"""Prometheus exporter for a ShineMonitor (Eybond / "LWIP") solar inverter logger.

Two ways to get at the data:
	relay (default)  The logger is pointed at this machine; traffic is forwarded to the
	                 real cloud unchanged and decoded on the way through. With
	                 --poll-interval the relay also reads the live blocks itself.
	sniff            Passive capture; needs a machine that sees the logger's traffic.
"""

import argparse
import asyncio
import logging
import sys
import time

from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server

from parser_utils import STRING_FIELDS, ShinemonitorModbusTCPParser

log = logging.getLogger('shinemonitor')


class PrometheusExporter:
	def __init__(self, port, stale_after=600):
		self.parser = ShinemonitorModbusTCPParser()
		self.port = port
		self.stale_after = stale_after
		self.metrics = {name: Gauge(f'shinemonitor_{name}', help_text)
			for name, help_text in self.parser.metrics.items() if name not in STRING_FIELDS}
		self.device = Info('shinemonitor_device', 'Inverter model, serial and firmware, and the logger ID')
		self.device_labels = {}
		self.last_update = Gauge('shinemonitor_last_update_timestamp_seconds', 'Time the last inverter reading was received')
		self.last_seen = 0
		self.up = Gauge('shinemonitor_up', f'1 if an inverter reading arrived in the last {stale_after} s')
		self.up.set_function(lambda: 1 if time.time() - self.last_seen < self.stale_after else 0)
		self.connections = Gauge('shinemonitor_logger_connections', 'Logger connections currently open (relay mode)')
		self.crc_errors = Counter('shinemonitor_crc_errors', 'Inverter responses with a bad checksum (ignored)')

	def data_processor(self, values):
		"""Callback with decoded {name: value}."""
		log.debug('%s', values)
		reading = False
		for name, value in values.items():
			if name in STRING_FIELDS:
				if self.device_labels.get(name) != value:
					self.device_labels[name] = value
					self.device.info(self.device_labels)
			elif name in self.metrics:
				self.metrics[name].set(value)
				reading = True
		if reading:
			self.last_seen = time.time()
			self.last_update.set(self.last_seen)

	def connection_change(self, change):
		self.connections.inc(change)

	def start_export(self):
		start_http_server(self.port)
		log.info('Prometheus metrics on port %s', self.port)


class PrometheusLinkEvents:
	"""Metrics for active polling, the cloud queue and the frame log."""

	def __init__(self, exporter):
		self.exporter = exporter
		self.poll_requests = Counter('shinemonitor_poll_requests', 'Injected read requests by block and outcome',
			['block', 'outcome'])
		self.polling = Gauge('shinemonitor_poll_enabled', 'Open logger connections with active polling running')
		self.hold = Histogram('shinemonitor_cloud_hold_seconds', 'Time a cloud frame waited before going to the logger',
			buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5))
		self.cloud_timeouts = Counter('shinemonitor_cloud_request_timeouts',
			'Cloud frames the logger did not answer within the request timeout')
		self.unknown = Counter('shinemonitor_unknown_frames', 'Frames with an Eybond command the parser does not know',
			['source', 'command'])
		self.frame_log_dropped = Counter('shinemonitor_frame_log_dropped', 'Frames not written because the log queue was full')

	def poll_result(self, block, outcome):
		self.poll_requests.labels(str(block), outcome).inc()

	def poll_enabled(self, enabled):
		self.polling.inc() if enabled else self.polling.dec()

	def cloud_hold(self, seconds):
		self.hold.observe(seconds)

	def cloud_timeout(self):
		self.cloud_timeouts.inc()

	def unknown_frame(self, source, command):
		self.unknown.labels(source, f'0x{command:02x}').inc()

	def crc_error(self):
		self.exporter.crc_errors.inc()


def host_port(text, default_port):
	host, _, port = text.rpartition(':') if ':' in text else (text, '', default_port)
	return host, int(port)


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument('--mode', choices=('relay', 'sniff'), default='relay')
	ap.add_argument('--metrics-port', type=int, default=9127)
	ap.add_argument('--stale-after', type=int, default=600, help='seconds without data before shinemonitor_up drops to 0')
	ap.add_argument('-v', '--verbose', action='store_true')
	relay = ap.add_argument_group('relay mode')
	relay.add_argument('--listen', default='0.0.0.0:502', help='address the logger connects to')
	relay.add_argument('--upstream', default='www.shinemonitor.com:502', help='real cloud server (resolved via public DNS)')
	relay.add_argument('--upstream-ip', help='skip DNS and use this cloud IP address')
	relay.add_argument('--poll-interval', type=float, default=0,
		help='seconds between injected reads of the live blocks; 0 (default) disables polling')
	relay.add_argument('--request-timeout', type=float, default=3.0,
		help='seconds to wait for a logger reply before releasing the next request')
	relay.add_argument('--frame-db', help='SQLite file that records every frame (off by default)')
	sniff = ap.add_argument_group('sniff mode')
	sniff.add_argument('--logger-host', default='LWIP', help='logger hostname or IP address')
	sniff.add_argument('--remote-hosts', nargs='+', default=['www.shinemonitor.com', 'india.shinemonitor.com'])
	sniff.add_argument('--traffic-log', help='also write every packet to this file')
	args = ap.parse_args()

	if args.poll_interval and args.poll_interval < 10:
		ap.error('--poll-interval below 10 s is not supported')

	logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
		format='%(asctime)s %(levelname)s %(name)s: %(message)s')

	exporter = PrometheusExporter(args.metrics_port, args.stale_after)
	exporter.start_export()

	if args.mode == 'relay':
		from shinemonitor_relay import Relay
		upstream_host, upstream_port = host_port(args.upstream, 502)
		listen_host, listen_port = host_port(args.listen, 502)
		events = PrometheusLinkEvents(exporter)
		frame_log = None
		if args.frame_db:
			from frame_log import FrameLog
			frame_log = FrameLog(args.frame_db, on_drop=events.frame_log_dropped.inc)
			log.info('Recording frames to %s', args.frame_db)
		relay_server = Relay(upstream_host, upstream_port, args.upstream_ip, exporter.data_processor,
			exporter.connection_change, exporter.crc_errors.inc, poll_interval=args.poll_interval,
			request_timeout=args.request_timeout, events=events, frame_log=frame_log)
		try:
			asyncio.run(relay_server.serve(listen_host, listen_port))
		finally:
			if frame_log:
				frame_log.close()
	else:
		from shinemonitor_logger import NetworkLogger	# scapy is only needed here
		NetworkLogger(args.remote_hosts, args.logger_host, data_processor=exporter.data_processor,
			traffic_log=args.traffic_log, on_crc_error=exporter.crc_errors.inc).start_monitoring()


if __name__ == '__main__':
	main()
