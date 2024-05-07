class ShinemonitorModbusTCPParser:
	def __init__(self):
		self.identifiers = {
			'panel': '010332', 
			'grid': '010322', 
			'logger': 'ff01'
			}

		self.metrics = {
			'logger_name': 'Logger ID',
			'output_power': 'Power output in watts',
			'output_s': 'Output apparent power in VA',
			'output_pf': 'Power factor',
			'instrument_power': 'Instrument power consumption in watts',
			'energy_today': 'Energy generated today in kWh',
			'energy_total': 'Total energy generated in kWh',
			'cumulative_time': 'Cumulative operating time in hours',
			'inverter_status': 'Inverter status code',
			'waiting_time': 'Waiting time since last event in minutes',
			'pv1_voltage': 'Voltage of PV1 in volts',
			'pv1_current': 'Current of PV1 in amperes',
			'pv2_voltage': 'Voltage of PV2 in volts',
			'pv2_current': 'Current of PV2 in amperes',
			'pv3_voltage': 'Voltage of PV3 in volts',
			'pv3_current': 'Current of PV3 in amperes',
			'grid_voltage_r': 'Grid voltage phase R in volts',
			'grid_current_r': 'Grid current phase R in amperes',
			'grid_voltage_s': 'Grid voltage phase S in volts',
			'grid_current_s': 'Grid current phase S in amperes',
			'grid_voltage_t': 'Grid voltage phase T in volts',
			'grid_current_t': 'Grid current phase T in amperes',
			'grid_line_voltage_rs': 'Grid line voltage between R and S in volts',
			'grid_line_voltage_st': 'Grid line voltage between S and T in volts',
			'grid_line_voltage_tr': 'Grid line voltage between T and R in volts',
			'grid_frequency': 'Grid frequency in Hz',
			'bus_voltage': 'DC Bus voltage in volts',
			'iso': 'Isolation resistance in ohms',
			'dci': 'DC injection in mA',
			'ghci': 'Ground fault current in mA',
			'internal_ambient_temp': 'Internal ambient temperature in degrees Celsius',
			'internal_radiator_temp': 'Internal radiator temperature in degrees Celsius',
		}

		self.data_points = {
			# (name, start_byte, multiplier)
			'grid': [
				('grid_voltage_r', 6, 0.1), ('grid_current_r', 10, 0.1),
				('grid_voltage_s', 14, 0.1), ('grid_current_s', 18, 0.1),
				('grid_voltage_t', 22, 0.1), ('grid_current_t', 26, 0.1),
				('grid_line_voltage_rs', 30, 0.1), ('grid_line_voltage_st', 34, 0.1),
				('grid_line_voltage_tr', 38, 0.1), ('grid_frequency', 42, 0.01),
				('bus_voltage', 46, 0.1), ('iso', 50, 1),
				('dci', 54, 1), ('ghci', 58, 1),
				('internal_ambient_temp', 66, 0.1), ('internal_radiator_temp', 70, 0.1),
			],
			'panel': [
				('output_power', 10, 0.1), ('output_s', 18, 0.1),
				('output_pf', 30, 0.01), ('instrument_power', 38, 1),
				('energy_today', 46, 0.1), ('energy_total', 54, 1),
				('cumulative_time', 62, 1), ('inverter_status', 66, 1),
				('waiting_time', 74, 1), ('pv1_voltage', 82, 0.1),
				('pv1_current', 86, 0.1), ('pv2_voltage', 90, 0.1),
				('pv2_current', 94, 0.1), ('pv3_voltage', 98, 0.1),
				('pv3_current', 102, 0.1),
			]
		}

	def parse_message(self, message_hex):
		"""Parse any Modbus message to extract basic information."""
		fields = ['Transaction ID', 'Message Type', 'Length', 'Function Code', 'Data']
		parts = [message_hex[i:i+4] for i in range(0, 16, 4)] + [message_hex[16:]]
		return dict(zip(fields, parts))

	def interpret_response(self, response_hex):
		response_info = self.parse_message(response_hex)
		fn_code = response_info['Function Code']
		data = response_info['Data']
		if data.startswith(self.identifiers['panel']):
			return self.parse_numerical_data(data, self.data_points['panel'])
		elif data.startswith(self.identifiers['grid']):
			return self.parse_numerical_data(data, self.data_points['grid'])
		elif fn_code == self.identifiers['logger']:
			return self.parse_logger_name(data)
		return {}

	def parse_logger_name(self, response_hex):
		return {"logger_name": bytearray.fromhex(response_hex).decode()}

	def parse_numerical_data(self, response_hex, data_points):
		# each data point is 2 bytes long
		msg_length = 4
		return {name: round(self.hex_to_signed_int(response_hex[start:start+msg_length]) * multiplier, 2)
				for name, start, multiplier in data_points}

	def hex_to_signed_int(self, hex_str):
		"""Convert hex string to a signed integer."""
		return int.from_bytes(bytes.fromhex(hex_str), byteorder='big', signed=True)
