"""Offline tests for relay mode with active polling and the frame log.

Run from the code directory: python3 test_link.py

The fake logger behaves like the real one did in the March 2024 captures: when
a second 0x04 request arrives before it has replied to the first, it answers
only the second.
"""

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest

from frame_log import FrameLog
from parser_utils import EybondFrame, EybondStream, ModbusReadRequest, ModbusReadResponse
from shinemonitor_link import LinkEvents
from shinemonitor_relay import Relay

SHORT_FRAME = bytes(6)
LOGGER_ID = b'Q0026166460691'
TIMEOUT = 0.3


def cloud_read(transaction_id, start, count):
	return EybondFrame.inverter_request(transaction_id, ModbusReadRequest(start, count)).to_bytes()


def cloud_heartbeat(transaction_id):
	return EybondFrame(transaction_id, 1, 0xFF, 1, bytes((24, 3, 19, 1, 51, 45, 0, 35))).to_bytes()


def block_data(start, count):
	data = bytearray(count * 2)
	if start == 30100:
		data[0:4] = (4855).to_bytes(4, 'big')		# output_power
	if start == 30131:
		data[0:2] = (2301).to_bytes(2, 'big')		# grid_voltage_r, 230.1 V
	return bytes(data)


class FakeLogger:
	def __init__(self, delay=0.05, answer=None, extra_delay=None):
		self.delay = delay
		self.answer = answer or (lambda transaction_id: True)
		self.extra_delay = extra_delay or (lambda transaction_id: 0)
		self.received = []		# (time, EybondFrame)
		self.sent = []			# (time, transaction ID, bytes)
		self.dropped = []		# transaction IDs replaced by a newer request
		self.pending = None
		self.pending_id = None

	async def connect(self, port):
		self.reader, self.writer = await asyncio.open_connection('127.0.0.1', port)
		self.task = asyncio.ensure_future(self._run())

	async def _run(self):
		stream = EybondStream()
		while True:
			data = await self.reader.read(65536)
			if not data:
				return
			for frame in stream.feed(data):
				self.received.append((time.monotonic(), frame))
				if frame.command == 1:
					self._send(frame.transaction_id,
						EybondFrame(frame.transaction_id, 0x0102, 0xFF, 1, LOGGER_ID).to_bytes())
				elif frame.command == 4:
					if self.pending and not self.pending.done():
						self.pending.cancel()
						self.dropped.append(self.pending_id)
					self.pending_id = frame.transaction_id
					self.pending = asyncio.ensure_future(self._answer(frame))

	async def _answer(self, frame):
		if not self.answer(frame.transaction_id):
			return
		await asyncio.sleep(self.delay + self.extra_delay(frame.transaction_id))
		request = ModbusReadRequest.from_bytes(frame.payload)
		reply = ModbusReadResponse(block_data(request.start_register, request.register_count)).to_bytes()
		self._send(frame.transaction_id,
			EybondFrame(frame.transaction_id, frame.protocol_id, 0xFF, 4, reply).to_bytes())

	def _send(self, transaction_id, data):
		self.sent.append((time.monotonic(), transaction_id, data))
		self.writer.write(data)

	def sent_time(self, transaction_id):
		return next(t for t, tid, _ in self.sent if tid == transaction_id)

	def received_time(self, transaction_id):
		return next(t for t, frame in self.received if frame.transaction_id == transaction_id)


class FakeCloud:
	def __init__(self):
		self.bytes = b''
		self.replies = set()
		self.sent_ids = set()
		self.connected = asyncio.Event()
		self.stream = EybondStream()

	async def start(self):
		self.server = await asyncio.start_server(self._accept, '127.0.0.1', 0)
		return self.server.sockets[0].getsockname()[1]

	async def _accept(self, reader, writer):
		self.reader, self.writer = reader, writer
		self.connected.set()
		while True:
			data = await reader.read(65536)
			if not data:
				return
			self.bytes += data
			for frame in self.stream.feed(data):
				self.replies.add(frame.transaction_id)

	def send(self, *frames):
		for frame in frames:
			self.sent_ids.add(int.from_bytes(frame[0:2], 'big'))
		self.writer.write(b''.join(frames))

	async def request(self, frame, timeout=1.0):
		transaction_id = int.from_bytes(frame[0:2], 'big')
		self.send(frame)
		deadline = time.monotonic() + timeout
		while transaction_id not in self.replies:
			if time.monotonic() > deadline:
				raise AssertionError(f'no reply to cloud request {transaction_id:04x}')
			await asyncio.sleep(0.01)


class RecordingEvents(LinkEvents):
	def __init__(self):
		self.polls, self.enabled, self.holds, self.unknown = [], [], [], []
		self.cloud_timeouts = 0

	def poll_result(self, block, outcome):
		self.polls.append((block, outcome))

	def poll_enabled(self, enabled):
		self.enabled.append(enabled)

	def cloud_hold(self, seconds):
		self.holds.append(seconds)

	def cloud_timeout(self):
		self.cloud_timeouts += 1

	def unknown_frame(self, source, command):
		self.unknown.append((source, command))


class RelayTest(unittest.IsolatedAsyncioTestCase):
	async def start(self, poll_interval=0.5, frame_log=None, **logger_options):
		self.cloud = FakeCloud()
		cloud_port = await self.cloud.start()
		self.values = {}
		self.events = RecordingEvents()
		self.relay = Relay('cloud.invalid', cloud_port, upstream_ip='127.0.0.1', on_values=self.values.update,
			poll_interval=poll_interval, request_timeout=TIMEOUT,
			link_options=dict(quiet_period=0.2), events=self.events, frame_log=frame_log)
		server = await self.relay.start('127.0.0.1', 0)
		self.logger = FakeLogger(**logger_options)
		await self.logger.connect(server.sockets[0].getsockname()[1])
		await asyncio.wait_for(self.cloud.connected.wait(), 2)

	async def stop(self):
		self.logger.writer.close()
		await asyncio.sleep(0.2)
		self.relay.server.close()
		self.cloud.writer.close()
		self.cloud.server.close()
		await asyncio.sleep(0.05)

	def injected(self):
		return [frame for _, frame in self.logger.received if frame.transaction_id not in self.cloud.sent_ids]

	async def wait_for_injected(self, timeout=2.0):
		deadline = time.monotonic() + timeout
		while not self.injected():
			if time.monotonic() > deadline:
				raise AssertionError('no injected request reached the logger')
			await asyncio.sleep(0.005)
		return self.injected()[0]

	async def cloud_cycle(self, first_id):
		for offset, (start, count) in enumerate(((30000, 7), (30010, 38), (30100, 25), (30131, 17))):
			await self.cloud.request(cloud_read(first_id + offset, start, count))
		await self.cloud.request(cloud_heartbeat(first_id + 4))

	async def test_injected_replies_stay_local(self):
		await self.start()
		await asyncio.sleep(1.8)
		await self.stop()
		self.assertEqual(self.cloud.bytes, b'', 'injected replies must not reach the cloud')
		self.assertEqual(self.values.get('output_power'), 4855)
		self.assertEqual(self.values.get('grid_voltage_r'), 230.1)
		self.assertGreaterEqual(self.events.polls.count((30100, 'ok')), 2)
		for frame in self.injected():
			self.assertEqual(frame.command, 4)
			self.assertEqual(frame.payload[1], 0x03, 'only function 03 may be injected')

	async def test_cloud_traffic_unchanged_while_polling(self):
		await self.start(poll_interval=0.3)
		started = time.monotonic()
		next_id = 0x1400
		while time.monotonic() - started < 1.5:
			await self.cloud_cycle(next_id)
			next_id += 5
			await asyncio.sleep(0.05)
		await asyncio.sleep(0.3)
		await self.stop()
		expected = b''.join(data for _, tid, data in self.logger.sent if tid in self.cloud.sent_ids)
		self.assertEqual(self.cloud.bytes, expected, 'cloud must get exactly the logger replies to its own frames')
		self.assertTrue(self.injected(), 'polling should have run between cloud cycles')

	async def test_bunched_cloud_requests_are_both_answered(self):
		await self.start(poll_interval=30)
		self.cloud.send(cloud_read(0x1500, 30000, 7), cloud_read(0x1501, 30010, 38))
		await asyncio.sleep(0.5)
		await self.stop()
		self.assertEqual({0x1500, 0x1501} & self.cloud.replies, {0x1500, 0x1501})
		self.assertEqual(self.logger.dropped, [])

	async def test_bunched_cloud_requests_in_passthrough_lose_one(self):
		# Documents the logger behaviour the link protects against.
		await self.start(poll_interval=0)
		self.cloud.send(cloud_read(0x1500, 30000, 7), cloud_read(0x1501, 30010, 38))
		await asyncio.sleep(0.5)
		await self.stop()
		self.assertEqual(self.logger.dropped, [0x1500])
		self.assertNotIn(0x1500, self.cloud.replies)

	async def test_cloud_frame_waits_for_injected_read(self):
		await self.start(delay=0.2)
		injected = await self.wait_for_injected()
		self.cloud.send(cloud_read(0x1600, 30100, 25))
		await asyncio.sleep(0.6)
		await self.stop()
		self.assertIn(0x1600, self.cloud.replies)
		self.assertGreaterEqual(self.logger.received_time(0x1600), self.logger.sent_time(injected.transaction_id),
			'the cloud frame must reach the logger only after the injected reply')
		self.assertEqual(self.logger.dropped, [])
		self.assertTrue(self.events.holds and max(self.events.holds) < TIMEOUT + 0.1)

	async def test_repeated_timeouts_disable_polling(self):
		cloud_ids = set()
		await self.start(poll_interval=0.4, answer=lambda tid: tid in cloud_ids)
		cloud_ids = self.cloud.sent_ids
		await asyncio.sleep(2.5)
		await self.cloud.request(cloud_read(0x1700, 30100, 25))
		await self.stop()
		self.assertEqual(len(self.injected()), 3)
		self.assertEqual(self.events.polls, [(30100, 'timeout')] * 3)
		self.assertEqual(self.events.enabled, [True, False])

	async def test_late_injected_reply_is_dropped(self):
		cloud_ids = set()
		await self.start(poll_interval=1.0, extra_delay=lambda tid: 0 if tid in cloud_ids else 0.45)
		cloud_ids = self.cloud.sent_ids
		injected = await self.wait_for_injected()
		await asyncio.sleep(0.6)
		self.assertTrue(any(tid == injected.transaction_id for _, tid, _ in self.logger.sent), 'late reply was sent')
		self.assertEqual(self.cloud.bytes, b'')
		self.assertEqual(self.events.polls[0], (30100, 'timeout'))
		await self.cloud.request(cloud_read(0x1800, 30100, 25))
		await self.stop()

	async def test_short_frame_is_forwarded_unchanged(self):
		await self.start(poll_interval=30)
		self.logger.writer.write(SHORT_FRAME)
		await asyncio.sleep(0.2)
		await self.stop()
		self.assertEqual(self.cloud.bytes, SHORT_FRAME)

	async def test_frame_log_with_polling(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, 'frames.db')
			frame_log = FrameLog(path, batch_seconds=0.1)
			await self.start(frame_log=frame_log)
			await self.cloud.request(cloud_read(0x1900, 30100, 25))
			self.cloud.send(EybondFrame(0x1901, 1, 0xFF, 0x07, b'\x01\x02').to_bytes())
			await asyncio.sleep(1.2)
			await self.stop()
			frame_log.close()
			db = sqlite3.connect(path)
			sources = {row[0] for row in db.execute('SELECT source FROM frames')}
			unknown = db.execute('SELECT source, command FROM frames WHERE known = 0').fetchall()
			dropped = db.execute("SELECT count(*) FROM frames WHERE source = 'logger' AND forwarded = 0"
				" AND command = 4").fetchone()[0]
			ended = db.execute('SELECT ended, polling FROM connections').fetchall()
			db.close()
		self.assertEqual(sources, {'cloud', 'logger', 'relay'})
		self.assertEqual(unknown, [('cloud', 7)])
		self.assertEqual(self.events.unknown, [('cloud', 7)])
		self.assertEqual(self.events.cloud_timeouts, 1, 'the unknown frame got no reply')
		self.assertGreaterEqual(dropped, 1, 'injected replies are logged as not forwarded')
		self.assertEqual(len(ended), 1)
		self.assertIsNotNone(ended[0][0])
		self.assertEqual(ended[0][1], 1)

	async def test_frame_log_in_passthrough(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, 'frames.db')
			frame_log = FrameLog(path, batch_seconds=0.1)
			await self.start(poll_interval=0, frame_log=frame_log)
			await self.cloud.request(cloud_read(0x1A00, 30100, 25))
			await self.stop()
			frame_log.close()
			db = sqlite3.connect(path)
			rows = db.execute('SELECT source, length, forwarded FROM frames ORDER BY id').fetchall()
			db.close()
		self.assertEqual(rows, [('cloud', 16, 1), ('logger', 63, 1)])


if __name__ == '__main__':
	unittest.main(verbosity=2)
