import sys
import time
import logging
import threading

from dns import resolver
from scapy.all import sniff, IP, TCP

from parser_utils import DataParser
from prometheus_client import start_http_server, Gauge

class NetworkLogger:
	def __init__(self, remote_hostnames, logger_hostname, dns_server='1.1.1.1', data_processor=None):
		self.data_processor = data_processor
		self.remote_hostnames = remote_hostnames
		self.logger_hostname = logger_hostname
		self.dns_server = dns_server
		self.logger_ip = self.resolve_ip(self.logger_hostname)
		self.remote_ips = set()
		self.setup_logging()

	def resolve_ip(self, hostname, dns_server=None):
		resolver.nameservers = [self.dns_server]
		try:
			return set([answer.to_text() for answer in resolver.resolve(hostname, 'A')])
		except Exception as e:
			logging.error(f"Failed to resolve {hostname} using {self.dns_server}: {e}")
			return set()

	def update_remote_ips(self, update_interval=300):
		"""Periodically update remote IPs."""
		while True:
			for remote_hostname in self.remote_hostnames:
				self.remote_ips.update(self.resolve_ip(remote_hostname, self.dns_server))
			print(f"{self.remote_hostnames} -> {self.remote_ips}")
			time.sleep(update_interval)

	def packet_callback(self, packet):
		logging.info(f"{packet[IP].src}:{packet[TCP].sport} -> {packet[IP].dst}:{packet[TCP].dport} | Payload size: {len(packet[TCP].payload)} | Payload: {packet[TCP].payload.original.hex()}")
		if self.data_processor:
			self.data_processor({"src_ip": packet[IP].src, "dst_ip": packet[IP].dst, "payload": packet[TCP].payload.original.hex()})

	def packet_filter(self, packet):
		if IP in packet and TCP in packet:
			ip_layer = packet[IP]
			_filter = ip_layer.src in self.logger_ip and ip_layer.dst in self.remote_ips
			return _filter
		return False

	def start_monitoring(self):
		ip_updater = threading.Thread(target=self.update_remote_ips, args=(300,), daemon=True)
		ip_updater.start()
		print("Starting full session traffic monitoring...")
		sniff(prn=self.packet_callback, lfilter=self.packet_filter, store=False)

	def setup_logging(self):
		logging.basicConfig(filename='full_session_traffic_log.txt', level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')