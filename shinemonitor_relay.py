"""TCP relay between the ShineMonitor logger and the ShineMonitor cloud.

The logger is pointed at this relay (by DNS or by its own server setting). For every
connection the relay opens a connection to the real cloud server and copies bytes in
both directions unchanged, so the ShineMonitor app keeps working. A copy of the traffic
is decoded on the way through. Decoding errors are logged and never affect forwarding.

With polling off (the default) the two directions are plain byte pipes. With polling
on, each connection is handled by LoggerLink, which serialises requests to the logger
and adds read-only requests of its own.
"""

import asyncio
import logging
import time

from dns import resolver

from parser_utils import StreamDecoder
from shinemonitor_link import FrameTap, LinkEvents, LoggerLink

log = logging.getLogger('shinemonitor.relay')


class UpstreamResolver:
	"""Resolves the cloud hostname through public DNS.

	The local DNS server may point that same hostname at this relay, so the system
	resolver can't be used for it.
	"""

	def __init__(self, hostname, nameservers=('1.1.1.1', '8.8.8.8'), ttl=3600):
		self.hostname = hostname
		self.resolver = resolver.Resolver(configure=False)
		self.resolver.nameservers = list(nameservers)
		self.resolver.lifetime = 5
		self.ttl = ttl
		self.address = None
		self.resolved_at = 0

	def get(self):
		if self.address and time.monotonic() - self.resolved_at < self.ttl:
			return self.address
		try:
			self.address = self.resolver.resolve(self.hostname, 'A')[0].to_text()
			self.resolved_at = time.monotonic()
		except Exception as e:
			if not self.address:
				raise
			log.warning('Could not re-resolve %s (%s); using %s', self.hostname, e, self.address)
		return self.address


class Relay:
	def __init__(self, upstream_host, upstream_port=502, upstream_ip=None,
			on_values=None, on_connection=None, on_crc_error=None,
			poll_interval=0, request_timeout=3.0, link_options=None, events=None, frame_log=None):
		self.upstream_port = upstream_port
		self.upstream = None if upstream_ip else UpstreamResolver(upstream_host)
		self.upstream_ip = upstream_ip
		self.on_values = on_values or (lambda values: None)
		self.on_connection = on_connection or (lambda change: None)
		self.on_crc_error = on_crc_error
		self.poll_interval = poll_interval
		self.request_timeout = request_timeout
		self.link_options = link_options or {}
		self.events = events or LinkEvents()
		self.frame_log = frame_log
		self.server = None

	async def handle(self, logger_reader, logger_writer):
		peer = logger_writer.get_extra_info('peername')
		try:
			address = self.upstream_ip or await asyncio.get_running_loop().run_in_executor(None, self.upstream.get)
			cloud_reader, cloud_writer = await asyncio.wait_for(
				asyncio.open_connection(address, self.upstream_port), timeout=15)
		except Exception as e:
			log.error('Logger %s connected but the cloud is unreachable: %s', peer, e)
			logger_writer.close()
			return

		polling = self.poll_interval > 0
		log.info('Relaying %s <-> %s:%s%s', peer, address, self.upstream_port,
			' with active polling' if polling else '')
		connection = self.frame_log.connection_started(peer, f'{address}:{self.upstream_port}', polling) \
			if self.frame_log else None
		self.on_connection(+1)
		try:
			if polling:
				await LoggerLink(logger_reader, logger_writer, cloud_reader, cloud_writer,
					poll_interval=self.poll_interval, request_timeout=self.request_timeout,
					on_values=self.on_values, events=self.events, frame_log=self.frame_log,
					connection=connection, **self.link_options).run()
			else:
				decoder = StreamDecoder(on_crc_error=self.on_crc_error)
				await asyncio.gather(
					self.pipe(logger_reader, cloud_writer, decoder, 'logger',
						FrameTap('logger', connection, self.frame_log, self.events)),
					self.pipe(cloud_reader, logger_writer, decoder, 'cloud',
						FrameTap('cloud', connection, self.frame_log, self.events)),
				)
		finally:
			self.on_connection(-1)
			if self.frame_log:
				self.frame_log.connection_ended(connection)
			log.info('Connection from %s closed', peer)

	async def pipe(self, reader, writer, decoder, direction, tap=None):
		try:
			while True:
				data = await reader.read(65536)
				if not data:
					break
				writer.write(data)
				await writer.drain()
				try:
					values = decoder.feed(direction, data)
					if values:
						self.on_values(values)
				except Exception:
					log.exception('Failed to decode %s data (forwarding continues)', direction)
				if tap:
					try:
						tap.feed(data)
					except Exception:
						log.exception('Failed to log %s data (forwarding continues)', direction)
		except (ConnectionError, asyncio.IncompleteReadError) as e:
			log.info('%s side closed: %s', direction, e)
		finally:
			writer.close()		# closing one side ends the other pipe too

	async def start(self, host, port):
		self.server = await asyncio.start_server(self.handle, host, port)
		log.info('Relay listening on %s:%s', host, port)
		return self.server

	async def serve(self, host, port):
		server = await self.start(host, port)
		async with server:
			await server.serve_forever()
