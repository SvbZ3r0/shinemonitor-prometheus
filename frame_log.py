"""Append-only SQLite log of every Eybond frame the relay handles.

The relay thread only puts frames on a bounded in-memory queue. A separate
writer thread saves them in batches. If the queue is full the frame is dropped
and counted; forwarding is never delayed by the database.

Columns of ``frames``:
	source       'cloud' or 'logger' for received frames, 'relay' for injected requests
	forwarded    1 if the frame was passed to the other side (or, for 'relay', sent)
	known        0 if the Eybond command byte is not one the parser knows
	raw          the exact bytes, so old traffic can be decoded again later
"""

import logging
import queue
import sqlite3
import threading
import time

from parser_utils import EybondCommand

log = logging.getLogger('shinemonitor.framelog')

KNOWN_COMMANDS = frozenset(int(command) for command in EybondCommand)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS connections (
	id INTEGER PRIMARY KEY,
	started REAL NOT NULL,
	ended REAL,
	peer TEXT,
	upstream TEXT,
	polling INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS frames (
	id INTEGER PRIMARY KEY,
	ts REAL NOT NULL,
	connection INTEGER,
	source TEXT NOT NULL,
	transaction_id INTEGER,
	protocol_id INTEGER,
	unit_id INTEGER,
	command INTEGER,
	length INTEGER NOT NULL,
	forwarded INTEGER NOT NULL,
	known INTEGER NOT NULL,
	raw BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS frames_ts ON frames (ts);
CREATE INDEX IF NOT EXISTS frames_unknown ON frames (known, command);
'''

_STOP = object()


class FrameLog:
	def __init__(self, path, batch_seconds=2.0, max_queue=20000, on_drop=None):
		self.path = path
		self.batch_seconds = batch_seconds
		self.queue = queue.Queue(max_queue)
		self.on_drop = on_drop or (lambda: None)
		self.dropped = 0
		self._last_connection = 0
		db = sqlite3.connect(path)		# fail at start-up on a bad path, not later
		db.execute('PRAGMA journal_mode=WAL')
		db.executescript(SCHEMA)
		db.close()
		self.thread = threading.Thread(target=self._writer, name='frame-log', daemon=True)
		self.thread.start()

	def connection_started(self, peer, upstream, polling):
		connection = max(self._last_connection + 1, time.time_ns() // 1000)
		self._last_connection = connection
		self._put(('start', (connection, time.time(), str(peer), str(upstream), int(bool(polling)))))
		return connection

	def connection_ended(self, connection):
		self._put(('end', (time.time(), connection)))

	def record(self, connection, source, raw, forwarded):
		raw = bytes(raw)
		if len(raw) >= 8:
			header = (int.from_bytes(raw[0:2], 'big'), int.from_bytes(raw[2:4], 'big'), raw[6], raw[7])
			known = int(raw[7] in KNOWN_COMMANDS)
		else:
			header = (None, None, None, None)
			known = 1		# too short to have a command byte
		self._put(('frame', (time.time(), connection, source) + header
			+ (len(raw), int(bool(forwarded)), known, raw)))

	def _put(self, item):
		try:
			self.queue.put_nowait(item)
		except queue.Full:
			self.dropped += 1
			self.on_drop()

	def close(self, timeout=10):
		self.queue.put(_STOP)
		self.thread.join(timeout)

	def _writer(self):
		db = sqlite3.connect(self.path)
		db.execute('PRAGMA journal_mode=WAL')
		db.execute('PRAGMA synchronous=NORMAL')
		stopping = False
		while not stopping:
			batch = [self.queue.get()]
			deadline = time.monotonic() + self.batch_seconds
			while batch[-1] is not _STOP:
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				try:
					batch.append(self.queue.get(timeout=remaining))
				except queue.Empty:
					break
			if batch[-1] is _STOP:
				batch.pop()
				stopping = True
			try:
				with db:
					for kind, row in batch:
						if kind == 'frame':
							db.execute('INSERT INTO frames (ts, connection, source, transaction_id, protocol_id,'
								' unit_id, command, length, forwarded, known, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)', row)
						elif kind == 'start':
							db.execute('INSERT INTO connections (id, started, peer, upstream, polling)'
								' VALUES (?,?,?,?,?)', row)
						else:
							db.execute('UPDATE connections SET ended = ? WHERE id = ?', row)
			except sqlite3.Error:
				log.exception('Could not write %d frame-log rows', len(batch))
		db.close()
