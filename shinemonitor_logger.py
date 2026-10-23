"""Passive packet-sniffing mode.

Only works on a machine that actually sees the logger's traffic (the logger's gateway,
or a mirrored switch port). On an ordinary switched network use the relay mode instead.
"""

import ipaddress
import logging
import socket
import threading
import time

from dns import resolver
from scapy.all import sniff, IP, TCP

from parser_utils import StreamDecoder

log = logging.getLogger('shinemonitor.sniffer')


class NetworkLogger:
	def __init__(self, remote_hostnames, logger_host, dns_server='1.1.1.1', data_processor=None,
			traffic_log=None, on_crc_error=None):
		self.data_processor = data_processor or (lambda values: None)
		self.remote_hostnames = remote_hostnames
		self.dns_server = dns_server
		self.logger_ip = self.resolve_local(logger_host)
		self.remote_ips = set()
		self.decoder = StreamDecoder(on_crc_error=on_crc_error)
		self.traffic_log = None
		if traffic_log:
			self.traffic_log = logging.getLogger('shinemonitor.traffic')
			handler = logging.FileHandler(traffic_log)
			handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
			self.traffic_log.addHandler(handler)
			self.traffic_log.propagate = False

	@staticmethod
	def resolve_local(host):
		"""The logger is on the LAN, so use the system resolver (or accept an IP address)."""
		try:
			return {str(ipaddress.ip_address(host))}
		except ValueError:
			pass
		try:
			return {info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)}
		except socket.gaierror as e:
			raise SystemExit(f'Cannot resolve the logger address {host!r}: {e}. Pass its IP address instead.')

	def resolve_remote(self, hostname):
		"""The cloud servers are resolved through public DNS."""
		res = resolver.Resolver(configure=False)
		res.nameservers = [self.dns_server]
		try:
			return {answer.to_text() for answer in res.resolve(hostname, 'A')}
		except Exception as e:
			log.error('Failed to resolve %s using %s: %s', hostname, self.dns_server, e)
			return set()

	def update_remote_ips(self, update_interval=300):
		"""Periodically add the current cloud addresses."""
		while True:
			for hostname in self.remote_hostnames:
				self.remote_ips.update(self.resolve_remote(hostname))
			log.debug('%s -> %s', self.remote_hostnames, self.remote_ips)
			time.sleep(update_interval)

	@staticmethod
	def tcp_payload(packet):
		"""TCP data only. A bare ACK is padded to the 60-byte Ethernet minimum, and
		scapy returns those padding bytes as payload; the IP length excludes them."""
		ip, tcp = packet[IP], packet[TCP]
		size = ip.len - ip.ihl * 4 - tcp.dataofs * 4
		return bytes(tcp.payload)[:max(size, 0)]

	def packet_filter(self, packet):
		if IP in packet and TCP in packet and self.tcp_payload(packet):
			src, dst = packet[IP].src, packet[IP].dst
			return (src in self.logger_ip and dst in self.remote_ips) or \
				(src in self.remote_ips and dst in self.logger_ip)
		return False

	def packet_callback(self, packet):
		payload = self.tcp_payload(packet)
		direction = 'logger' if packet[IP].src in self.logger_ip else 'cloud'
		if self.traffic_log:
			self.traffic_log.info('%s:%s -> %s:%s | Payload size: %d | Payload: %s', packet[IP].src, packet[TCP].sport,
				packet[IP].dst, packet[TCP].dport, len(payload), payload.hex())
		values = self.decoder.feed(direction, payload)
		if values:
			self.data_processor(values)

	def start_monitoring(self):
		log.info('Logger %s; watching traffic to %s', self.logger_ip, self.remote_hostnames)
		threading.Thread(target=self.update_remote_ips, args=(300,), daemon=True).start()
		sniff(prn=self.packet_callback, lfilter=self.packet_filter, store=False)
