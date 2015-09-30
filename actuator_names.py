class ActuatorNames:
	commonSetpoint = 'Common Setpoint'
	occupiedCommand = 'Occupied Command'
	coolingCommand = 'Cooling Command'
	actualSupplyFlowSP = 'Actual Sup Flow SP'
	heatingCommand = 'Heating Command'
	damperCommand = 'Damper Command'
	occupiedCoolingMinimumFlow = 'Occupied Clg Min'
	actualCoolingSetpoint= 'Actual Cooling Setpoint'
	actualHeatingSetpoint= 'Actual Heating Setpoint'
	nameList = None
	
	def __init__(self):
		self.nameList = list()
		self.nameList.append(self.commonSetpoint)
		self.nameList.append(self.occupiedCommand)
		self.nameList.append(self.coolingCommand)
		self.nameList.append(self.heatingCommand)
		self.nameList.append(self.damperCommand)
		self.nameList.append(self.actualSupplyFlowSP)
		self.nameList.append(self.occupiedCoolingMinimumFlow)
		self.nameList.append(self.actualCoolingSetpoint)
		self.nameList.append(self.actualHeatingSetpoint)

	def __contains__(self, given):
		if given in self.nameList:
			return True
		else:
			return False

