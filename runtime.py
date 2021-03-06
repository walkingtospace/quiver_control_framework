from quiver import *

from datetime import datetime, timedelta


class Runtime():
	quiv = None
	commandSeq = None
	dummyBeginTime = datetime(2010,1,1,0,0,0)
	dummeEndTime = datetime(2030,1,1,0,0,0)
	inputTimeFormat = '%m/%d/%Y %H:%M:%S'

	def __init__(self):
		self.quiv = Quiver()
		
	def read_seqfile(self, filename):
# filename(string, excel) -> seqList(pd.DataFrame)
		seqList = pd.read_excel(filename)
		decDatetime = lambda x: datetime.strptime(x, self.inputTimeFormat)
		seqList['set_time'] = seqList['set_time'].map(decDatetime)
		self.commandSeq = seqList

	def load_command_seq(self, beginTime, endTime):
		boolIdx = np.logical_and(self.commandSeq['set_time']<endTime, self.commandSeq['set_time']>=beginTime)
		if True in boolIdx.tolist():
			pass
		idx = [i for i, elem in enumerate(boolIdx) if elem]
		futureCommands = self.commandSeq[boolIdx]
		self.commandSeq = self.commandSeq.drop(idx)
		self.commandSeq.index = range(0,len(self.commandSeq))
		return futureCommands

	def top_dynamic_control(self):
#		controlInterval = 5*60 # in seconds
		controlInterval = 5# in seconds
		currTime = self.quiv.now()
		beforeTime = currTime - timedelta(seconds=controlInterval)
		while(True):
			self.quiv.top_ntp()
			currTime = self.quiv.now()
			print currTime
			futureCommands = self.load_command_seq(beforeTime, currTime)
			self.quiv.issue_seq(futureCommands)
			time.sleep(controlInterval)
			beforeTime = currTime
			currTime = self.quiv.now()
			if len(self.commandSeq)==0:
				self.quiv.output_exp_log()
				return

	def top(self, filename):
		#self.quiv.system_refresh()
		try:
			print '=============Begin of Quiver============='
			self.read_seqfile(filename)
			self.top_dynamic_control()
			print 'All commands are completed'
			print '==============End of Quiver=============='
		except QRError as e:
			print e
			self.quiv.notify_systemfault()
			self.quiv.system_close_common_behavior()
			print '==============End of Quiver=============='
		except KeyboardInterrupt:
			self.quiv.system_close_common_behavior()
			for frame in traceback.extract_tb(sys.exc_info()[2]):
				fname,lineno,fn,text = frame
				print "Error in %s on line %d" % (fname, lineno)
			print "Normally finished by a user interrupt"
			print '==============End of Quiver=============='
