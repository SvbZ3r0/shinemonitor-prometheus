import unittest

from parser_utils import (
	CRCMismatch,
	EybondCommand,
	EybondFrame,
	EybondStream,
	ModbusExceptionResponse,
	ModbusReadRequest,
	ModbusReadResponse,
	StreamDecoder,
)


class ProtocolObjectTests(unittest.TestCase):
	def test_read_request_and_outer_frame_round_trip(self):
		request = ModbusReadRequest(30100, 25)
		self.assertEqual(ModbusReadRequest.from_bytes(request.to_bytes()), request)
		outer = EybondFrame.inverter_request(0xF000, request)
		self.assertEqual(EybondFrame.from_bytes(outer.to_bytes()), outer)

	def test_read_request_rejects_invalid_bounds(self):
		for address, count in ((-1, 1), (65536, 1), (0, 0), (0, 126)):
			with self.subTest(address=address, count=count):
				with self.assertRaises(ValueError):
					ModbusReadRequest(address, count)

	def test_short_malformed_frame_is_skipped_without_losing_stream_state(self):
		good = EybondFrame.inverter_request(7, ModbusReadRequest(30100, 25)).to_bytes()
		malformed = b'\x00\x01\x00\x01\x00\x01\xff'
		stream = EybondStream()
		self.assertEqual(stream.feed(good + malformed + good[:5]), [EybondFrame.from_bytes(good)])
		self.assertEqual(stream.feed(good[5:]), [EybondFrame.from_bytes(good)])

	def test_exception_response_is_reported(self):
		exception = ModbusExceptionResponse(0x03, 0x02)
		outer = EybondFrame(9, 1, 0xFF, EybondCommand.INVERTER, exception.to_bytes())
		seen = []
		decoder = StreamDecoder(on_modbus_exception=lambda response, frame: seen.append((response, frame)))
		self.assertEqual(decoder.feed('logger', outer.to_bytes()), {})
		self.assertEqual(seen, [(exception, outer)])

	def test_bad_response_crc_is_reported(self):
		response = ModbusReadResponse(bytes(50))
		outer = EybondFrame(9, 1, 0xFF, EybondCommand.INVERTER, response.to_bytes())
		bad = bytearray(outer.to_bytes())
		bad[-1] ^= 1
		errors = []
		decoder = StreamDecoder(on_crc_error=lambda: errors.append(1))
		self.assertEqual(decoder.feed('logger', bytes(bad)), {})
		self.assertEqual(errors, [1])

	def test_exception_crc_is_validated(self):
		bad = bytearray(ModbusExceptionResponse(0x03, 0x02).to_bytes())
		bad[-1] ^= 1
		with self.assertRaises(CRCMismatch):
			ModbusExceptionResponse.from_bytes(bytes(bad))


if __name__ == '__main__':
	unittest.main()
