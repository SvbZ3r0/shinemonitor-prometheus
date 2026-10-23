"""Logger link for relay mode with optional active polling (Phase 2).

One LoggerLink owns one logger connection and the matching cloud connection.
Every frame from the cloud goes through one queue, and only one request is
outstanding at the logger at a time. When two requests reach the logger close
together it answers only the newer one, so this rule protects the cloud's reads
as well as the injected ones.

The poller adds read-only function-03 requests for the live blocks while the
link is idle. Their replies are decoded here and never forwarded to the cloud.
Cloud frames, and the logger's replies to them, are forwarded byte for byte.
Only their timing can change: a cloud frame waits while an earlier request is
outstanding. Frames too short to have a header are forwarded at once.
"""

import asyncio
import collections
import logging
import time

from parser_utils import (BLOCKS, CRCMismatch, EybondCommand, EybondFrame, EybondStream,
	ModbusExceptionResponse, ModbusReadRequest, ModbusReadResponse, ProtocolError,
	ShinemonitorModbusTCPParser, StreamDecoder)

log = logging.getLogger('shinemonitor.link')

POLL_BLOCKS = (30100, 30131)		# live blocks; the cloud already reads the static ones
KNOWN_COMMANDS = frozenset(int(command) for command in EybondCommand)
MAX_FRAME = 4096					# a longer declared length is treated as garbage
INJECTED_ID_GAP = 0x1000			# keep injected IDs at least this far from the cloud counter
INJECTED_ID_MEMORY = 60.0			# seconds an injected ID is remembered for late replies


def frame_command(raw):
	"""Return the Eybond command byte, or None for frames too short to have a header."""
	return raw[7] if len(raw) >= EybondFrame.HEADER_SIZE else None


def split_raw(buffer):
	"""Split complete raw frames off ``buffer``.

	Returns (frames, rest). If the next declared length is implausible, the
	whole buffer is returned as one raw chunk so the stream cannot stall.
	"""
	frames, rest = EybondStream.split_bytes(buffer)
	if len(rest) >= EybondFrame.LENGTH_PREFIX_SIZE:
		declared = EybondFrame.LENGTH_PREFIX_SIZE + int.from_bytes(rest[4:6], 'big')
		if declared > MAX_FRAME:
			frames.append(rest)
			rest = b''
	return frames, rest


class LinkEvents:
	"""Hooks for metrics. The default does nothing."""

	def poll_result(self, block, outcome):		# ok, timeout, crc, exception, invalid
		pass

	def poll_enabled(self, enabled):
		pass

	def cloud_hold(self, seconds):
		pass

	def cloud_timeout(self):
		pass

	def unknown_frame(self, source, command):
		pass

	def crc_error(self):
		pass


class FrameTap:
	"""Split one direction of a passthrough stream into frames for the frame log."""

	def __init__(self, source, connection, frame_log, events):
		self.source = source
		self.connection = connection
		self.frame_log = frame_log
		self.events = events
		self.buffer = b''

	def feed(self, data):
		frames, self.buffer = split_raw(self.buffer + data)
		for raw in frames:
			command = frame_command(raw)
			if command is not None and command not in KNOWN_COMMANDS:
				self.events.unknown_frame(self.source, command)
			if self.frame_log:
				self.frame_log.record(self.connection, self.source, raw, True)


class _Request:
	__slots__ = ('origin', 'raw', 'transaction_id', 'expects_reply', 'queued_at', 'reply')

	def __init__(self, origin, raw, expects_reply):
		self.origin = origin
		self.raw = raw
		self.transaction_id = int.from_bytes(raw[0:2], 'big') if len(raw) >= 2 else None
		self.expects_reply = expects_reply
		self.queued_at = time.monotonic()
		self.reply = asyncio.get_running_loop().create_future()


class LoggerLink:
	def __init__(self, logger_reader, logger_writer, cloud_reader, cloud_writer, *,
			poll_interval=0, request_timeout=3.0, quiet_period=3.0, max_failures=3, on_values=None, events=None, frame_log=None, connection=None):
		self.logger_reader = logger_reader
		self.logger_writer = logger_writer
		self.cloud_reader = cloud_reader
		self.cloud_writer = cloud_writer
		self.poll_interval = poll_interval
		self.request_timeout = request_timeout
		self.quiet_period = quiet_period
		self.max_failures = max_failures
		self.on_values = on_values or (lambda values: None)
		self.events = events or LinkEvents()
		self.frame_log = frame_log
		self.connection = connection

		self.parser = ShinemonitorModbusTCPParser()
		self.decoder = StreamDecoder(self.parser, on_crc_error=self.events.crc_error)
		self.cloud_queue = collections.deque()
		self.poll_queue = collections.deque()
		self.work = asyncio.Event()
		self.active = None
		self.poll_ids = {}				# injected transaction ID -> time it is forgotten
		self.next_poll_id = None
		self.last_cloud_id = 0
		self.last_cloud_frame = 0.0
		self.inverter_protocol_id = 1	# copied from the cloud's own 0x04 requests
		self.inverter_unit_id = 0xFF

	# ---- lifecycle

	async def run(self):
		main = [asyncio.ensure_future(self._from_cloud()),
			asyncio.ensure_future(self._from_logger()),
			asyncio.ensure_future(self._sender())]
		poller = asyncio.ensure_future(self._poller()) if self.poll_interval > 0 else None
		try:
			done, _ = await asyncio.wait(main, return_when=asyncio.FIRST_COMPLETED)
			for task in done:
				if not task.cancelled() and task.exception():
					error = task.exception()
					if isinstance(error, (ConnectionError, asyncio.IncompleteReadError)):
						log.info('Connection closed: %s', error)
					else:
						log.error('Logger link failed', exc_info=error)
		finally:
			tasks = main + ([poller] if poller else [])
			for task in tasks:
				task.cancel()
			await asyncio.gather(*tasks, return_exceptions=True)
			for request in list(self.cloud_queue) + list(self.poll_queue) + [self.active]:
				if request and not request.reply.done():
					request.reply.cancel()
			for writer in (self.logger_writer, self.cloud_writer):
				writer.close()

	# ---- frame log

	def _record(self, source, raw, forwarded):
		command = frame_command(raw)
		if command is not None and command not in KNOWN_COMMANDS:
			self.events.unknown_frame(source, command)
		if self.frame_log:
			self.frame_log.record(self.connection, source, raw, forwarded)

	# ---- cloud side

	async def _from_cloud(self):
		buffer = b''
		while True:
			data = await self.cloud_reader.read(65536)
			if not data:
				return
			frames, buffer = split_raw(buffer + data)
			for raw in frames:
				self._record('cloud', raw, True)
				expects_reply = EybondFrame.HEADER_SIZE <= len(raw) <= MAX_FRAME
				if expects_reply:
					self.last_cloud_id = int.from_bytes(raw[0:2], 'big')
					self.last_cloud_frame = time.monotonic()
					if raw[7] == EybondCommand.INVERTER:
						self.inverter_protocol_id = int.from_bytes(raw[2:4], 'big')
						self.inverter_unit_id = raw[6]
					try:
						self.decoder.feed('cloud', raw)
					except Exception:
						log.exception('Failed to decode cloud frame (forwarding continues)')
				self.cloud_queue.append(_Request('cloud', raw, expects_reply))
				self.work.set()

	# ---- logger side

	async def _from_logger(self):
		buffer = b''
		while True:
			data = await self.logger_reader.read(65536)
			if not data:
				return
			frames, buffer = split_raw(buffer + data)
			for raw in frames:
				transaction_id = int.from_bytes(raw[0:2], 'big') if len(raw) >= EybondFrame.HEADER_SIZE else None
				if transaction_id is not None and self._is_injected(transaction_id):
					self._record('logger', raw, False)
					self._resolve(transaction_id, raw)
					continue
				self._record('logger', raw, True)
				self.cloud_writer.write(raw)
				await self.cloud_writer.drain()
				if transaction_id is None:
					continue
				self._resolve(transaction_id, raw)
				try:
					values = self.decoder.feed('logger', raw)
					if values:
						self.on_values(values)
				except Exception:
					log.exception('Failed to decode logger frame (forwarding continues)')

	def _is_injected(self, transaction_id):
		now = time.monotonic()
		for stale in [tid for tid, forget_at in self.poll_ids.items() if forget_at < now]:
			del self.poll_ids[stale]
		return transaction_id in self.poll_ids

	def _resolve(self, transaction_id, raw):
		request = self.active
		if request and request.transaction_id == transaction_id and not request.reply.done():
			request.reply.set_result(raw)

	# ---- the single sender towards the logger

	async def _next_request(self):
		while not (self.cloud_queue or self.poll_queue):
			self.work.clear()
			await self.work.wait()
		return (self.cloud_queue or self.poll_queue).popleft()

	async def _sender(self):
		while True:
			request = await self._next_request()
			if request.origin == 'cloud':
				self.events.cloud_hold(time.monotonic() - request.queued_at)
			else:
				self._record('relay', request.raw, True)
			self.logger_writer.write(request.raw)
			await self.logger_writer.drain()
			if not request.expects_reply:
				continue
			self.active = request
			try:
				await asyncio.wait_for(asyncio.shield(request.reply), self.request_timeout)
			except asyncio.TimeoutError:
				if request.origin == 'poll' and not request.reply.done():
					request.reply.set_exception(asyncio.TimeoutError())		# the poller reads this
				if request.origin == 'cloud':
					request.reply.cancel()		# nobody waits on cloud replies
					self.events.cloud_timeout()
					log.info('No logger reply to cloud frame %04x within %.1f s',
						request.transaction_id, self.request_timeout)
			except asyncio.CancelledError:
				raise
			except Exception:
				pass		# cancelled or failed reply; the requester handles it
			finally:
				self.active = None

	# ---- active polling

	def _injected_id(self):
		cloud = self.last_cloud_id
		while True:
			if self.next_poll_id is None:
				self.next_poll_id = (cloud + 0x8000) & 0xFFFF
			self.next_poll_id = (self.next_poll_id + 1) & 0xFFFF
			candidate = self.next_poll_id
			ahead = (candidate - cloud) & 0xFFFF
			behind = (cloud - candidate) & 0xFFFF
			if candidate and candidate not in self.poll_ids and min(ahead, behind) >= INJECTED_ID_GAP:
				return candidate
			self.next_poll_id = None		# the cloud counter has come close; jump away again

	async def _wait_until_quiet(self):
		while True:
			idle = time.monotonic() - self.last_cloud_frame
			if not self.cloud_queue and not self.active and idle >= self.quiet_period:
				return
			await asyncio.sleep(max(0.05, min(self.quiet_period - idle, self.quiet_period / 4)))

	async def _poll_block(self, start):
		block = BLOCKS[start]
		transaction_id = self._injected_id()
		frame = EybondFrame.inverter_request(transaction_id, ModbusReadRequest(block.start, block.count),
			protocol_id=self.inverter_protocol_id, unit_id=self.inverter_unit_id)
		request = _Request('poll', frame.to_bytes(), True)
		self.poll_ids[transaction_id] = time.monotonic() + INJECTED_ID_MEMORY
		self.poll_queue.append(request)
		self.work.set()
		try:
			raw = await request.reply
		except asyncio.TimeoutError:
			return 'timeout'
		try:
			reply = EybondFrame.from_bytes(raw)
			if reply.command != EybondCommand.INVERTER:
				return 'invalid'
			response = self.parser.parse_modbus_response(reply.payload)
		except CRCMismatch:
			self.events.crc_error()
			return 'crc'
		except ProtocolError:
			return 'invalid'
		if isinstance(response, ModbusExceptionResponse):
			return 'exception'
		if not isinstance(response, ModbusReadResponse) or len(response.data) != block.count * 2:
			return 'invalid'
		values = self.parser.decode_block(block, response.data)
		if values:
			self.on_values(values)
		return 'ok'

	async def _poller(self):
		failures = 0
		enabled = False
		try:
			await asyncio.sleep(self.poll_interval)		# let the cloud's first reads go first
			enabled = True
			self.events.poll_enabled(True)
			log.info('Active polling every %s s', self.poll_interval)
			while True:
				cycle_started = time.monotonic()
				for start in POLL_BLOCKS:
					await self._wait_until_quiet()
					outcome = await self._poll_block(start)
					self.events.poll_result(start, outcome)
					if outcome == 'ok':
						failures = 0
						continue
					failures += 1
					log.warning('Injected read of %s failed: %s (%d in a row)', start, outcome, failures)
					if failures >= self.max_failures:
						log.warning('Active polling disabled for this connection')
						return
					break		# skip the rest of this cycle
				await asyncio.sleep(max(0.0, self.poll_interval - (time.monotonic() - cycle_started)))
		finally:
			if enabled:
				self.events.poll_enabled(False)
