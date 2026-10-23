"""Decode the Eybond outer protocol and its enclosed Modbus RTU frames."""

from dataclasses import dataclass
from enum import IntEnum


def crc16_modbus(data):
	"""Return Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF)."""
	crc = 0xFFFF
	for byte in data:
		crc ^= byte
		for _ in range(8):
			crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
	return crc


class ProtocolError(ValueError):
	"""Base class for malformed Eybond or Modbus data."""


class CRCMismatch(ProtocolError):
	"""A Modbus RTU frame has an invalid checksum."""


class EybondCommand(IntEnum):
	"""Known command bytes in the outer Eybond protocol."""

	HEARTBEAT = 0x01
	LOGGER_PARAMETERS = 0x02
	INVERTER = 0x04


@dataclass(frozen=True)
class EybondFrame:
	"""One frame exchanged between the logger and the Eybond cloud.

	Its header resembles Modbus TCP, but ``command`` belongs to Eybond's
	protocol and ``protocol_id`` is not constrained to zero.
	"""

	transaction_id: int
	protocol_id: int
	unit_id: int
	command: int
	payload: bytes = b''

	HEADER_SIZE = 8
	LENGTH_PREFIX_SIZE = 6

	@property
	def length(self):
		return 2 + len(self.payload)  # unit ID + command + payload

	@classmethod
	def from_bytes(cls, data):
		if len(data) < cls.HEADER_SIZE:
			raise ProtocolError('Eybond frame is shorter than its header')
		length = int.from_bytes(data[4:6], 'big')
		expected = cls.LENGTH_PREFIX_SIZE + length
		if length < 2 or len(data) != expected:
			raise ProtocolError(f'Eybond frame length is {len(data)}, expected {expected}')
		return cls(
			int.from_bytes(data[0:2], 'big'),
			int.from_bytes(data[2:4], 'big'),
			data[6], data[7], data[8:],
		)

	def to_bytes(self):
		return b''.join((
			self.transaction_id.to_bytes(2, 'big'),
			self.protocol_id.to_bytes(2, 'big'),
			self.length.to_bytes(2, 'big'),
			bytes((self.unit_id, self.command)),
			self.payload,
		))

	@classmethod
	def inverter_request(cls, transaction_id, request, protocol_id=1, unit_id=0xFF):
		"""Wrap a read-only Modbus request for delivery to the inverter."""
		if not isinstance(request, ModbusReadRequest):
			raise TypeError('only ModbusReadRequest can be wrapped for injection')
		return cls(transaction_id, protocol_id, unit_id, EybondCommand.INVERTER, request.to_bytes())


class EybondStream:
	"""Incrementally split a TCP byte stream into complete Eybond frames."""

	def __init__(self):
		self.buffer = b''

	@staticmethod
	def split_bytes(buffer):
		frames = []
		while len(buffer) >= EybondFrame.LENGTH_PREFIX_SIZE:
			length = int.from_bytes(buffer[4:6], 'big')
			frame_length = EybondFrame.LENGTH_PREFIX_SIZE + length
			if len(buffer) < frame_length:
				break
			# Lengths below two cannot contain the unit ID and command. Consume
			# them as malformed frames so one bad prefix does not poison the stream.
			# (Six all-zero bytes in old scapy logs were Ethernet padding on bare
			# ACKs, not stream data; see shinemonitor_logger.py.)
			frames.append(buffer[:frame_length])
			buffer = buffer[frame_length:]
		return frames, buffer

	def feed(self, data):
		raw_frames, self.buffer = self.split_bytes(self.buffer + data)
		return [EybondFrame.from_bytes(frame) for frame in raw_frames
			if len(frame) >= EybondFrame.HEADER_SIZE]


@dataclass(frozen=True)
class ModbusReadRequest:
	"""Standard Modbus RTU function-03 request inside Eybond command 0x04."""

	start_register: int
	register_count: int
	slave_address: int = 1

	FUNCTION = 0x03

	def __post_init__(self):
		if not 0 <= self.start_register <= 0xFFFF:
			raise ValueError('start_register must be between 0 and 65535')
		if not 1 <= self.register_count <= 125:
			raise ValueError('register_count must be between 1 and 125')
		if not 0 <= self.slave_address <= 0xFF:
			raise ValueError('slave_address must be between 0 and 255')

	@classmethod
	def from_bytes(cls, data):
		if len(data) != 8 or data[1] != cls.FUNCTION:
			return None
		if crc16_modbus(data[:6]) != int.from_bytes(data[6:8], 'little'):
			return None
		try:
			return cls(int.from_bytes(data[2:4], 'big'), int.from_bytes(data[4:6], 'big'), data[0])
		except ValueError:
			return None

	def to_bytes(self):
		body = b''.join((
			bytes((self.slave_address, self.FUNCTION)),
			self.start_register.to_bytes(2, 'big'),
			self.register_count.to_bytes(2, 'big'),
		))
		return body + crc16_modbus(body).to_bytes(2, 'little')


@dataclass(frozen=True)
class ModbusReadResponse:
	"""Standard Modbus RTU function-03 response inside Eybond command 0x04."""

	data: bytes
	slave_address: int = 1

	FUNCTION = 0x03

	@classmethod
	def from_bytes(cls, frame):
		if len(frame) < 5 or frame[1] != cls.FUNCTION:
			return None
		byte_count = frame[2]
		if len(frame) != 5 + byte_count:
			return None
		if crc16_modbus(frame[:3 + byte_count]) != int.from_bytes(frame[3 + byte_count:], 'little'):
			raise CRCMismatch('Modbus RTU CRC mismatch')
		return cls(frame[3:3 + byte_count], frame[0])

	def to_bytes(self):
		if len(self.data) > 255:
			raise ValueError('Modbus RTU response data exceeds one-byte length')
		body = bytes((self.slave_address, self.FUNCTION, len(self.data))) + self.data
		return body + crc16_modbus(body).to_bytes(2, 'little')


@dataclass(frozen=True)
class ModbusExceptionResponse:
	"""Standard Modbus RTU exception response, such as function 0x83."""

	request_function: int
	exception_code: int
	slave_address: int = 1

	@classmethod
	def from_bytes(cls, frame):
		if len(frame) != 5 or not frame[1] & 0x80:
			return None
		if crc16_modbus(frame[:3]) != int.from_bytes(frame[3:5], 'little'):
			raise CRCMismatch('Modbus RTU CRC mismatch')
		return cls(frame[1] & 0x7F, frame[2], frame[0])

	def to_bytes(self):
		body = bytes((self.slave_address, self.request_function | 0x80, self.exception_code))
		return body + crc16_modbus(body).to_bytes(2, 'little')


@dataclass(frozen=True)
class Field:
	name: str
	register: int       # offset from the block start, in 16-bit registers
	help: str
	scale: float = 1
	words: int = 1      # 2 means a high-word-first 32-bit value
	signed: bool = False


@dataclass(frozen=True)
class Block:
	name: str
	start: int          # first inverter register requested by the cloud
	count: int          # number of 16-bit registers
	fields: tuple = ()
	strings: tuple = () # (name, byte offset, byte length) text fields


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

# Fallback for captures that contain a reply without its corresponding request.
BLOCK_BY_BYTE_COUNT = {block.count * 2: block for block in BLOCKS.values()}
STRING_FIELDS = ('firmware', 'model', 'serial', 'logger_id')


class ShinemonitorModbusTCPParser:
	"""Decode Eybond frames and their enclosed Modbus RTU data.

	The historical class name remains as a compatibility API. The protocol
	boundary is represented by ``EybondFrame`` and the Modbus classes above.
	"""

	def __init__(self):
		self.metrics = {'logger_id': 'Logger ID'}
		for block in BLOCKS.values():
			for field in block.fields:
				self.metrics[field.name] = field.help

	@staticmethod
	def split_frames(buffer):
		return EybondStream.split_bytes(buffer)

	@staticmethod
	def parse_eybond_frame(frame):
		if len(frame) < EybondFrame.HEADER_SIZE:
			return None
		return EybondFrame.from_bytes(frame)

	@staticmethod
	def parse_frame(frame):
		"""Compatibility dictionary view of an outer Eybond frame."""
		parsed = ShinemonitorModbusTCPParser.parse_eybond_frame(frame)
		if not parsed:
			return None
		return {
			'transaction_id': parsed.transaction_id,
			'protocol_id': parsed.protocol_id,
			'unit_id': parsed.unit_id,
			'function': parsed.command,
			'command': parsed.command,
			'payload': parsed.payload,
		}

	@staticmethod
	def parse_read_request(rtu):
		request = ModbusReadRequest.from_bytes(rtu)
		return (request.start_register, request.register_count) if request else None

	@staticmethod
	def parse_read_response(rtu):
		response = ModbusReadResponse.from_bytes(rtu)
		return response.data if response else None

	@staticmethod
	def parse_modbus_response(rtu):
		"""Return a typed read or exception response from an inner RTU frame."""
		return ModbusReadResponse.from_bytes(rtu) or ModbusExceptionResponse.from_bytes(rtu)

	@staticmethod
	def decode_block(block, data):
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
		outer = frame if isinstance(frame, EybondFrame) else self.parse_eybond_frame(frame)
		if not outer:
			return {}
		if outer.command == EybondCommand.INVERTER:
			response = self.parse_modbus_response(outer.payload)
			if not isinstance(response, ModbusReadResponse):
				return {}
			block = block or BLOCK_BY_BYTE_COUNT.get(len(response.data))
			return self.decode_block(block, response.data) if block and len(response.data) == block.count * 2 else {}
		if outer.command == EybondCommand.HEARTBEAT:
			text = outer.payload.decode('ascii', 'replace')
			if len(text) >= 8 and text.isalnum():
				return {'logger_id': text}
		return {}

	def interpret_response(self, response_hex):
		stream = EybondStream()
		values = {}
		for frame in stream.feed(bytes.fromhex(response_hex)):
			values.update(self.decode_response(frame))
		return values


class StreamDecoder:
	"""Decode both directions of one logger-to-cloud TCP connection."""

	def __init__(self, parser=None, on_crc_error=None, on_modbus_exception=None):
		self.parser = parser or ShinemonitorModbusTCPParser()
		self.on_crc_error = on_crc_error or (lambda: None)
		self.on_modbus_exception = on_modbus_exception or (lambda response, frame: None)
		self.streams = {'cloud': EybondStream(), 'logger': EybondStream()}
		self.pending = {}

	@property
	def buffers(self):
		"""Compatibility view of unconsumed bytes in each direction."""
		return {direction: stream.buffer for direction, stream in self.streams.items()}

	def feed(self, direction, data):
		if direction not in self.streams:
			raise ValueError("direction must be 'cloud' or 'logger'")
		values = {}
		for outer in self.streams[direction].feed(data):
			if direction == 'cloud':
				if outer.command == EybondCommand.INVERTER:
					request = ModbusReadRequest.from_bytes(outer.payload)
					if request and request.start_register in BLOCKS:
						self.pending[outer.transaction_id] = BLOCKS[request.start_register]
						if len(self.pending) > 64:
							self.pending.pop(next(iter(self.pending)))
			else:
				block = self.pending.pop(outer.transaction_id, None)
				try:
					response = self.parser.parse_modbus_response(outer.payload) \
						if outer.command == EybondCommand.INVERTER else None
					if isinstance(response, ModbusExceptionResponse):
						self.on_modbus_exception(response, outer)
					else:
						values.update(self.parser.decode_response(outer, block))
				except CRCMismatch:
					self.on_crc_error()
		return values


DataParser = ShinemonitorModbusTCPParser
