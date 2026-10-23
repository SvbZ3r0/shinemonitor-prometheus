"""Prometheus exporter for a ShineMonitor (Eybond / "LWIP") solar inverter logger.

Two ways to get at the data:
	relay (default)  The logger is pointed at this machine; traffic is forwarded to the
	                 real cloud unchanged and decoded on the way through.
	sniff            Passive capture; needs a machine that sees the logger's traffic.
"""

import argparse
import asyncio
import logging
import sys
import time

from prometheus_client import Counter, Gauge, Info, start_http_server

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
	sniff = ap.add_argument_group('sniff mode')
	sniff.add_argument('--logger-host', default='LWIP', help='logger hostname or IP address')
	sniff.add_argument('--remote-hosts', nargs='+', default=['www.shinemonitor.com', 'india.shinemonitor.com'])
	sniff.add_argument('--traffic-log', help='also write every packet to this file')
	args = ap.parse_args()

	logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
		format='%(asctime)s %(levelname)s %(name)s: %(message)s')

	exporter = PrometheusExporter(args.metrics_port, args.stale_after)
	exporter.start_export()

	if args.mode == 'relay':
		from shinemonitor_relay import Relay
		upstream_host, upstream_port = host_port(args.upstream, 502)
		listen_host, listen_port = host_port(args.listen, 502)
		relay_server = Relay(upstream_host, upstream_port, args.upstream_ip, exporter.data_processor,
			exporter.connection_change, exporter.crc_errors.inc)
		asyncio.run(relay_server.serve(listen_host, listen_port))
	else:
		from shinemonitor_logger import NetworkLogger	# scapy is only needed here
		NetworkLogger(args.remote_hosts, args.logger_host, data_processor=exporter.data_processor,
			traffic_log=args.traffic_log, on_crc_error=exporter.crc_errors.inc).start_monitoring()


if __name__ == '__main__':
	main()
