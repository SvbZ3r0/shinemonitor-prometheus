import sys

from prometheus_client import start_http_server, Gauge

from parser_utils import DataParser
from shinemonitor_logger import NetworkLogger

class PrometheusExporter:
	def __init__(self, port):
		self.parser = DataParser()
		self.port = port
		self.metrics = {}
		self.initialize_metrics()

	def initialize_metrics(self):
		for data_point in self.parser.metrics.keys():
			self.metrics[data_point] = Gauge(f"shinemonitor_{data_point}", self.parser.metrics[data_point])

	def data_processor(self, data):
		"""Callback function to update Prometheus metrics."""
		parsed_data = self.parser.interpret_response(data['payload'])
		if parsed_data:
			print(parsed_data)
			for key, value in parsed_data.items():
				if key in self.metrics:
					try:
						self.metrics[key].set(value)
					except Exception as e:
						logging.error(f"Failed to post {value} as {key}: {e}")

	def start_export(self):
		start_http_server(self.port)
		print(f"Prometheus metrics server started on port {self.port}.", file=sys.stderr)

if __name__ == '__main__':
	port = 9127
	exporter = PrometheusExporter(port)
	remote_hostnames = ["www.shinemonitor.com", "india.shinemonitor.com"]
	logger_hostname = "LWIP.local"
	network_logger = NetworkLogger(remote_hostnames, logger_hostname, data_processor=exporter.data_processor)
	exporter.start_export()
	network_logger.start_monitoring()
