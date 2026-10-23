"""Decoder for the traffic between a ShineMonitor (Eybond / "LWIP") WiFi logger and the ShineMonitor cloud.

The logger opens a TCP connection to www.shinemonitor.com:502. Over that connection the
cloud sends requests and the logger answers. Every message is a Modbus-TCP style frame:

	transaction id  2 bytes
	protocol id     2 bytes
	length          2 bytes   number of bytes that follow
	unit id         1 byte    always 0xFF
	function        1 byte    0x04 = pass-through to the inverter; payload is a Modbus RTU frame
	                          0x01 / 0x02 = logger-level commands
	payload         length - 2 bytes

Inverter data is read with RTU "read holding registers" (slave 1, function 3). The cloud
reads four register blocks; their layout below was worked out from captured traffic of a
KSY 10 kW three-phase inverter (firmware H910-31100-60).
"""

from dataclasses import dataclass


def crc16_modbus(data):
	"""Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF). Transmitted low byte first."""
	crc = 0xFFFF
	for byte in data:
		crc ^= byte
		for _ in range(8):
			crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
	return crc


@dataclass(frozen=True)
class Field:
	name: str
	register: int		# offset from the start of the block, in 16-bit registers
	help: str
	scale: float = 1
	words: int = 1		# 2 = 32-bit value spread over two registers, high word first
	signed: bool = False


@dataclass(frozen=True)
class Block:
	name: str
	start: int			# first register address the cloud asks for
	count: int			# number of registers
	fields: tuple = ()
	strings: tuple = ()	# (name, byte offset, byte length) for text fields


BLOCKS = {
	30000: Block('firmware', 30000, 7, strings=(('firmware', 0, 14),)),
	30010: Block('device', 30010, 38,
		fields=(Field('rated_power', 26, 'Rated power of the inverter in watts'),),
		strings=(('model', 16, 16), ('serial', 32, 16))),
	30100: Block('output', 30100, 25, fields=(
		Field('output_power', 0, 'AC output power in watts', words=2, signed=True),
		Field('output_s', 2, 'AC apparent power in VA', words=2, signed=True),
		Field('output_pf', 6, 'Power factor', scale=0.01, signed=True),
		Field('instrument_power', 8, 'Instrument power consumption in watts'),
		Field('energy_today', 9, 'Energy generated today in kWh', scale=0.1, words=2),
		Field('energy_total', 11, 'Total energy generated in kWh', words=2),
		Field('cumulative_time', 13, 'Cumulative operating time in hours', words=2),
		Field('inverter_status', 15, 'Inverter status code'),
		Field('waiting_time', 17, 'Waiting time since last event in minutes'),
		Field('pv1_voltage', 19, 'Voltage of PV1 in volts', scale=0.1),
		Field('pv1_current', 20, 'Current of PV1 in amperes', scale=0.1),
		Field('pv2_voltage', 21, 'Voltage of PV2 in volts', scale=0.1),
		Field('pv2_current', 22, 'Current of PV2 in amperes', scale=0.1),
		Field('pv3_voltage', 23, 'Voltage of PV3 in volts', scale=0.1),
		Field('pv3_current', 24, 'Current of PV3 in amperes', scale=0.1),
	)),
	30131: Block('grid', 30131, 17, fields=(
		Field('grid_voltage_r', 0, 'Grid voltage phase R in volts', scale=0.1),
		Field('grid_current_r', 1, 'Grid current phase R in amperes', scale=0.1),
		Field('grid_voltage_s', 2, 'Grid voltage phase S in volts', scale=0.1),
		Field('grid_current_s', 3, 'Grid current phase S in amperes', scale=0.1),
		Field('grid_voltage_t', 4, 'Grid voltage phase T in volts', scale=0.1),
		Field('grid_current_t', 5, 'Grid current phase T in amperes', scale=0.1),
		Field('grid_line_voltage_rs', 6, 'Grid line voltage between R and S in volts', scale=0.1),
		Field('grid_line_voltage_st', 7, 'Grid line voltage between S and T in volts', scale=0.1),
		Field('grid_line_voltage_tr', 8, 'Grid line voltage between T and R in volts', scale=0.1),
		Field('grid_frequency', 9, 'Grid frequency in Hz', scale=0.01),
		Field('bus_voltage', 10, 'DC bus voltage in volts', scale=0.1),
		Field('iso', 11, 'Isolation resistance in ohms'),
		Field('dci', 12, 'DC injection in mA', signed=True),
		Field('ghci', 13, 'Ground fault current in mA', signed=True),
		Field('internal_ambient_temp', 15, 'Internal ambient temperature in degrees Celsius', scale=0.1, signed=True),
		Field('internal_radiator_temp', 16, 'Internal radiator temperature in degrees Celsius', scale=0.1, signed=True),
	)),
}

# Used when a response arrives without its request (e.g. the first frame after a restart).
BLOCK_BY_BYTE_COUNT = {block.count * 2: block for block in BLOCKS.values()}

STRING_FIELDS = ('firmware', 'model', 'serial', 'logger_id')


class ShinemonitorModbusTCPParser:
	"""Decodes complete frames into metric values. Stateless; see StreamDecoder for TCP streams."""

	def __init__(self):
		self.metrics = {'logger_id': 'Logger ID'}
		for block in BLOCKS.values():
			for field in block.fields:
				self.metrics[field.name] = field.help

	@staticmethod
	def split_frames(buffer):
		"""Split a byte buffer into complete frames. Returns (frames, leftover bytes)."""
		frames = []
		while len(buffer) >= 6:
			length = int.from_bytes(buffer[4:6], 'big')
			if len(buffer) < 6 + length:
				break
			frames.append(buffer[:6 + length])
			buffer = buffer[6 + length:]
		return frames, buffer

	@staticmethod
	def parse_frame(frame):
		"""Split a frame into its header fields. Returns None for frames without a function code."""
		if len(frame) < 8:
			return None		# e.g. the 6-byte all-zero frame the logger sends before each reply
		return {
			'transaction_id': int.from_bytes(frame[0:2], 'big'),
			'protocol_id': int.from_bytes(frame[2:4], 'big'),
			'unit_id': frame[6],
			'function': frame[7],
			'payload': frame[8:],
		}

	@staticmethod
	def parse_read_request(rtu):
		"""Decode an RTU 'read holding registers' request. Returns (start register, count) or None."""
		if len(rtu) == 8 and rtu[1] == 0x03 and crc16_modbus(rtu[:6]) == int.from_bytes(rtu[6:8], 'little'):
			return int.from_bytes(rtu[2:4], 'big'), int.from_bytes(rtu[4:6], 'big')
		return None

	@staticmethod
	def parse_read_response(rtu):
		"""Decode an RTU 'read holding registers' response. Returns the data bytes, or None if invalid."""
		if len(rtu) < 5 or rtu[1] != 0x03:
			return None
		byte_count = rtu[2]
		if len(rtu) != 5 + byte_count:
			return None
		if crc16_modbus(rtu[:3 + byte_count]) != int.from_bytes(rtu[3 + byte_count:], 'little'):
			raise ValueError('CRC mismatch')
		return rtu[3:3 + byte_count]

	@staticmethod
	def decode_block(block, data):
		"""Turn the data bytes of a register block into {name: value}."""
		values = {}
		for field in block.fields:
			start = field.register * 2
			raw = data[start:start + 2 * field.words]
			if len(raw) == 2 * field.words:
				values[field.name] = round(int.from_bytes(raw, 'big', signed=field.signed) * field.scale, 2)
		for name, offset, length in block.strings:
			text = data[offset:offset + length].replace(b'\x00', b'').decode('ascii', 'replace').strip()
			if text:
				values[name] = text
		return values

	def decode_response(self, frame, block=None):
		"""Decode one logger-to-cloud frame. Pass the requested block if known."""
		info = self.parse_frame(frame)
		if not info:
			return {}
		payload = info['payload']
		if info['function'] == 0x04:
			data = self.parse_read_response(payload)
			if data is None:
				return {}
			block = block or BLOCK_BY_BYTE_COUNT.get(len(data))
			return self.decode_block(block, data) if block and len(data) == block.count * 2 else {}
		if info['function'] == 0x01:
			text = payload.decode('ascii', 'replace')
			if len(text) >= 8 and text.isalnum():
				return {'logger_id': text}
		return {}

	def interpret_response(self, response_hex):
		"""Decode a single frame given as hex (kept for the packet-sniffing mode)."""
		frames, _ = self.split_frames(bytes.fromhex(response_hex))
		values = {}
		for frame in frames:
			values.update(self.decode_response(frame))
		return values


class StreamDecoder:
	"""Decodes both directions of one logger<->cloud TCP connection.

	Requests from the cloud are remembered by transaction id, so each response is decoded
	using the register block that was actually asked for.
	"""

	def __init__(self, parser=None, on_crc_error=None):
		self.parser = parser or ShinemonitorModbusTCPParser()
		self.on_crc_error = on_crc_error or (lambda: None)
		self.buffers = {'cloud': b'', 'logger': b''}
		self.pending = {}		# transaction id -> Block

	def feed(self, direction, data):
		"""Feed bytes sent by 'cloud' or 'logger'. Returns {name: value} decoded from complete frames."""
		frames, self.buffers[direction] = self.parser.split_frames(self.buffers[direction] + data)
		values = {}
		for frame in frames:
			info = self.parser.parse_frame(frame)
			if not info:
				continue
			if direction == 'cloud':
				if info['function'] == 0x04:
					request = self.parser.parse_read_request(info['payload'])
					if request and request[0] in BLOCKS:
						self.pending[info['transaction_id']] = BLOCKS[request[0]]
						if len(self.pending) > 64:		# never answered; don't grow forever
							self.pending.pop(next(iter(self.pending)))
			else:
				block = self.pending.pop(info['transaction_id'], None)
				try:
					values.update(self.parser.decode_response(frame, block))
				except ValueError:
					self.on_crc_error()
		return values


# Earlier versions of the exporter imported this name.
DataParser = ShinemonitorModbusTCPParser
