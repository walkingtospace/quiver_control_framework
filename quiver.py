from actuator_names import ActuatorNames
import metaactuators
from collection_wrapper import *
from bd_wrapper import BDWrapper

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import ntplib
from time import ctime
import csv
import pymongo
from pytz import timezone
import json
import time
import sys, os
import smtplib
import logging
import emailauth
from email.mime.text import MIMEText
import traceback
from threading import Thread #TODO: This is for future independent NTP Thread

#from multiprocessing import Process
#import threading

# NOTE:
# 1. Information is only shared through DB Collections. Each collection has lock for synchronization.
# 2. API:
#	(1) Check dependency of given batch commands with current status (status table)
#	(2) Issue batch commands. This should return ack value to higher level. Batch commands should be independent to each other, which means they should be for different zones (for current level of independency). 
#	(3) Rollback.
#	(4) Monitoring failure and notify by email
#	(5) Show current status
# 3. Every failure of validation raises a QRError

#QRError = Quiber Runtime Error
class QRError(BaseException):
	errorType = None
	value = None
	def __init__(self, errorType=None, value=None):
		self.errorType = errorType
		self.value = value

	def __str__(self):
		return self.errorType + ': ' + repr(self.value)


class Quiver:
	ntpURL = 'ntp.ucsd.edu'
	timeOffset = timedelta(0)
	ntpClient = None
	inputTimeFormat = '%m/%d/%Y %H:%M:%S'
	actuDict= dict()
	actuNames = ActuatorNames()
	futureCommColl = None 	# This is a collection for future command sequence. If some of the commands are issued, they are removed from here.
	expLogColl = None	 		# This is a collection for log of control. If a command is issued, it is added to here with relevant information.
	statColl = None		# This is a collection for rollback. If a command is issued, its corresponding rollback command is added here.
	relinquishVal = -1
	ambulanceConn = None
	ntpActivateTime = None
	dummyBeginTime = datetime(2000,1,1)
	dummyEndTime = datetime(2030,12,31,0,0,0)
	bdm = None
	ackLatency = timedelta(minutes=10)
	statusExpiration = timedelta(hours=24)

	def __init__(self):
		self.ntpClient = ntplib.NTPClient()
		client = pymongo.MongoClient()
		self.futureCommColl = CollectionWrapper('command_sequence')
		self.statColl = CollectionWrapper('status')
		self.expLogColl = CollectionWrapper('experience_log')
		self.ntpActivateTime = self.dummyBeginTime
		logging.basicConfig(filname='log/debug'+datetime.now().isoformat()[0:-7].replace(':','_') + '.log',level=logging.DEBUG)
		self.bdm = BDWrapper()
		self.update_time_offset()

		# Create pid file for monitoring
		pid = str(os.getpid())
		pidfile = "C:\\temp\\quiver.pid"
		if os.path.isfile(pidfile):
    		print "%s already exists, exiting" % pidfile
	    	sys.exit()
		else:
    		file(pidfile, 'w').write(pid)

	def __del__(self):
		pass
				
	def notify_systemfault(self):
		content = "Quiver control system bas been down at " + self.now().isoformat()
		self.notify_email(content)

	def notify_email(self, content):
		server = smtplib.SMTP(emailauth.smtpURL)
		msg = MIMEText('"'+content+'"')
		msg['Subject']='Alert: Quiver is down'
		msg['From'] = emailauth.fromaddr
		msg['To'] = ",".join(emailauth.toaddrs)
		server.starttls()
		server.login(emailauth.username, emailauth.password)
		server.sendmail(emailauth.fromaddr, emailauth.toaddrs, msg.as_string())
		server.quit()

	def update_time_offset(self):
		ntpRequest = self.ntpClient.request(self.ntpURL)
		ntpRequest.tx_time
		ntpTime = datetime.strptime(time.ctime(ntpRequest.tx_time), "%a %b %d %H:%M:%S %Y")
		self.timeOffset = ntpTime - datetime.now()
		return ntpTime
	
	def get_actuator_uuid(self, zone=None, actuType=None):
		context = dict()
		if zone != None:
			context['room']=zone
		if actuType != None:
			context['template']=actuType
		uuids = self.bdm.get_sensor_uuids(context)
		if len(uuids)>1:
			raise QRError('Many uuids are found', context)
		elif len(uuids)==0:
			raise QRError('No uuid is found', context)
		else:
			return uuids[0]
	
	def get_actuator_name(self,zone=None,actuType=None):
		context = dict()
		if zone != None:
			context['room']=zone
		if actuType != None:
			context['template']=actuType
		uuids = self.bdm.get_sensor_names(context)
		if len(uuids)>1:
			raise QRError('Many uuids are found', context)
		elif len(uuids)==0:
			raise QRError('No uuid is found', context)
		else:
			return uuids[0]


	def load_future_seq(self, beginTime, endTime):
		query = {'$and':[{'set_time':{'$lte':endTime}}, {'set_time':{'$gte':beginTime}}]}
		futureSeq = self.futureCommColl.load_dataframe(query)
		invalidCommand = self.validate_batch(futureSeq)
		if invalidCommand.empty:
			self.futureCommColl.remove_dataframe(query)
			return futureSeq
		else:
			raise QRError(errorType='A command is depedent to currently opearting actuator', value=invalidCommand)

	def load_reset_seq(self, endTime):
		query = {'reset_time':{'$lte':endTime}}
		futureSeq = self.statColl.pop_dataframe(query)
		return futureSeq

	def actuator_exist(self, uuid):
# zone(string), actuatortype(string) -> existing?(boolean)
		if uuid in self.actuDict.keys():
			return True
		else:
			return False
			
	def rollback_to_original_setting(self):
		resetSeq = self.load_reset_seq(self.dummyEndTime)
		for row in resetSeq.iterrows():
			currTime = self.now()
			zone = row[1]['zone']
			actuType = row[1]['actuator_type']
			resetVal = row[1]['reset_value']
			uuid = row[1]['uuid']
			actuator = self.actuDict[uuid]
			actuator.reset_value(resetVal, currTime)
		self.futureCommColl.remove_all()
		self.statColl.remove_all()

	def validate_command_seq_freq(self,seq):
# seq(pd.DataFrame) -> valid?(boolean)
#TODO: This does not consider reset commands. However, it should be considered later
		baseInvalidMsg = "Test sequence is invalid because "
		for row in seq.iterrows():
			zone = row[1]['zone']
			actuType = row[1]['actuator_type']
			uuid = row[1]['uuid']
			actuator  = self.actuDict[uuid]
			minLatency = actuator.minLatency
			setTime = row[1]['set_time']
			inrangeRowsIdx = np.bitwise_and(seq['set_time']<setTime+minLatency, seq['set_time']>setTime-minLatency)
			inrangeRowsIdx = np.bitwise_and(inrangeRowsIdx, seq['uuid']==uuid)
			inrangeRowsIdx[row[0]] = False
			inrangeRows = seq[inrangeRowsIdx.values.tolist()]
			resetRows = self.statColl.load_dataframe({'uuid':uuid})
			loggedRows = self.expLogColl.load_dataframe({'$and':[{'set_time':{'$gte':setTime-actuator.minLatency}},{'uuid':uuid}]})
			inrangeRows = pd.concat([inrangeRows, resetRows, loggedRows])
			for inrangeRow in inrangeRows.iterrows():
				if inrangeRow[1]['zone']==zone and inrangeRow[1]['actuator_type']==actuType:
					print baseInvalidMsg + str(row[1]) + ' is overlapped with ' + str(inrangeRow[1])
					return row[1]
		return pd.DataFrame({})
	
	def validate_command_seq_dependency(self, seq, minExpLatency):
# seq(pd.DataFrame) -> valid?(boolean)
		#baseInvalidMsg = "Test sequence is invalid because "
		for row in seq.iterrows():
			zone = row[1]['zone']
			actuType = row[1]['actuator_type']
			uuid = row[1]['uuid']
			actuator  = self.actuDict[uuid]
			minLatency = actuator.minLatency
			setTime = row[1]['set_time']
			inrangeRowsIdx = np.bitwise_and(seq['set_time']<setTime, seq['set_time']>setTime-minExpLatency)
			inrangeRows = seq[inrangeRowsIdx.values.tolist()]
			resetRows = self.statColl.load_dataframe({})
			loggedRows = self.expLogColl.load_dataframe({'set_time':{'$gte':setTime-minExpLatency}})
			inrangeRows = pd.concat([inrangeRows, resetRows, loggedRows])
			for inrangeRow in inrangeRows.iterrows():
				if inrangeRow[1]['zone']==zone and actuator.get_dependency(inrangeRow[1]['actuator_type'])!=None:
		#			print baseInvalidMsg + str(row[1]) + ' is dependent on ' + str(inrangeRow[1])
					return row[1]
		return pd.DataFrame({})

	def static_validate(self, seq):
		invalidFreqCommand = self.validate_command_seq_freq(seq)
		if not invalidFreqCommand.empty:
			return invalidFreqCommand
		invalidDepCommand = self.validate_command_seq_dependency(seq, timedelta(minutes=5)) #TODO: This minExpLatency should be set to 1 hour later
		if not invalidDepCommand.empty:
			return invalidDepCommand
		return pd.DataFrame({})

	def validate_batch(self, seq):
		# Validate each command
		for seqRow in seq.iterrows():
			zone = seqRow[1]['zone']
			actuType = seqRow[1]['actuator_type']
			setVal = seqRow[1]['set_value']
			uuid = self.get_actuator_uuid(zone, actuType)
			name= get_actuator_name(zone, actuType)
			seqRow[1]['uuid'] = uuid
			seqRow[1]['name'] = name
			
			if not uuid in self.actuDict.keys():
				self.actuDict[uuid] = metaactuators.make_actuator(uuid, name, zone, actuType)
			actuator = self.actuDict[uuid]

			# Validation 1: Check input range
			if not actuator.validate_input(setVal)
				raise QRError("Input value is not in correct range", seqRow[1])

			# Validation 2: Check if there is a dependent command in given batch
			for otherSeqRow in seq.iterrows():
				if seqRow[0]!=otherSeqRow[0]:
					if actuator.check_dependency(otherSeqRow[1]):
						raise QRError("A command is dependent on a command in the given sequence", seqRow[1], otherSeqRow[1])
					elif aotherSeqRow[1]['uuid'] == uuid:
						raise QRError('A command has same target equipment with another', seqRow[1], otherSeqRow[1])

			# Validation 3: Check if there is a dependent command in current status
			queryDep = {'set_time':{'$gte':self.now()-actuator.minLatency}}
			depCommands = self.statColl.load_dataframe(queryDep)
			for commRow in depCommands.iterrows():
				if actuator.check_dependency(commRow[1]):
					raise QRError("A command is dependent on current status", seqRow[1], commRow[1])

			seq.loc[seqRow[0]] = seqRow[1]

	def now(self):
		currTime = datetime.now()
		currTime = currTime + self.timeOffset
		return currTime

	def issue_seq(self, seq):
		self.validate_batch(seq)
		#TODO: Check if updated seq is returned

		for row in seq.iterrows():
			zone = row[1]['zone']
			setVal = row[1]['set_value']
			actuType = row[1]['actuator_type']
			uuid = row[1]['uuid']
			name = row[1]['name']
			actuator = self.actuDict[uuid]
			now = self.now()
			origVal = actuator.get_latest_value(now)
			row[1]['original_value'] = origVal
			if actuator.check_control_flag():
				query = {'uuid':uuid}
				resetVal = self.statColl.load_dataframe(query).tail(1)
				#TODO: This should become more safe. e.g., what if that is no data in statColl?
				resetVal = float(resetVal['reset_value'][0])
			else:
				resetVal = origVal
			row[1]['set_time'] = self.now()	
			seq[row[0]] = row[1]
			if setVal == -1:
				if actuType in [self.actuNames.CommonSetpoint]:
					setVal = float(self.statColl.load_dataframe({'uuid':uuid}).tail(1)['reset_value'][0])
				actuator.reset_value(setVal, setTime)
			else:
				actuator.set_value(setVal, setTime) #TODO: This should not work in test stage

#TODO: Implement this
		self.ack_issue(seq, True)

	def ack_issue(self, seq, setResetFlag):
		seq.index = range(0,len(seq)) # Just to make it sure (need to remove?)
		issueFlagList = np.array([False]*len(seq))
		uploadedTimeList = list()
		resendInterval = timedelta(minutes=6)
		maxWaitTime = resendInterval * 2
		
		# Init uploadedTimeList
		for row in seq.iterrows():
			uuid = row[1]['uuid']
			actuator = self.actuDict[uuid]
			latestVal, setTime = actuator.get_latest_value(self.now())
			setVal = row[1]['set_value']
			if (setVal==-1 and latestVal != row[1]) or (setVal!=-1 and latestVal != row[1]['set_value']):
				raise QRError('Initial upload to BD is failed', row[1])
			uploadedTimeList.append(setTime)
		uploadedTimeList = np.array(uploadedTimeList)


		# Receive ack
		maxWaitDatetime = max(uploadedTimeList)+maxWaitTime
		ackInterval = 30 # secounds
		while maxWaitDatetime>=self.now():
			for row in seq.iterrows():
				idx = row[0]
				if issueFlagList[idx]==True:
					continue
				uuid = row[1]['uuid']
				setVal = row[1]['set_value']
				actuator = self.actuDict[uuid]
						
				else:
					ackVal = row[1]['set_value']
				actuator = self.actuDict[uuid]
				currT = self.now()
				currVal, newSetTime = actuator.get_latest_value(self.now())
				if currVal==ackVal and newSetTime!=uploadedTimeList[idx]
					issueFlagList[idx] = True
				now = self.now()
				if now>=uploadedTimeList[idx]+resendInterval:
					setTime = self.now()
					if setVal==-1:
						if actuator.actuType in [self.actuNames.CommonSetpoint]:
							ackVal = row[1]['reset_value']
						actuator.reset_value(setVal,setTime)
					else:
						actuator.set_value(setVal, setTime)
					uploadedTimeList[idx] = setTime
			time.sleep(ackInterval)

		for row in seq.iterrows():
			if issueFlagList[row[0]] == True:
				self.reflect_an_issue_to_db(row[1])

		for idx, flag in enumerate(issueFlagList):
			if not flag:
				raise QRError('Some commands are unable to be uploaded', seq[idx])


	def reflect_an_issue_to_db(self, commDict):
		zone = row[1]['zone']
		setVal = row[1]['set_value']
		actuType = row[1]['actuator_type']
		uuid = row[1]['uuid']
		name = row[1]['name']
		resetVal = row[1]['reset_value']
		actuator = self.actuDict[uuid]

		statusRow = StatusRow(uuid, name, setTime=now, setVal=setVal, resetVal=resetVal, actuType=actuType, underControl=actuator.check_control_flag())
		self.statColl.store_row(statusRow)
		expLogRow = ExpLogRow(uuid, name, setTime=now, setVal=setVal, origVal=origVal)
		self.expLogColl.store_row(expLogRow)
			
	def reset_seq(self, seq):
		for row in seq.iterrows():
			#TODO: validate_in_log
			print row
			resetTime = row[1]['reset_time']
			resetVal = row[1]['reset_value']
			actuType = row[1]['actuator_type']
			uuid = row[1]['uuid']
			name = row[1]['name']
			actuator = self.actuDict[uuid]
			now = self.now()
			origVal = actuator.get_value(now-timedelta(hours=1), now).tail(1)[0]
			actuator.reset_value(resetVal, resetTime)
			expLogRow = ExpLogRow(uuid, name, setTime=None, resetTime=resetTime, setVal=None, resetVal=now, origVal=origVal)
			print expLogRow
			self.expLogColl.store_row(expLogRow)

		if not self.issue_ack(seq, False).empty:
			raise QRError('A reset command cannot be set at BACNet', seq)

	def top_ntp(self):
		ntpLatency = timedelta(minutes=30)
		if self.ntpActivateTime<=self.now():
			self.update_time_offset
			self.ntpActivateTime = self.now() + ntpLatency

	def top_ux(self,filename):
		newSeq = self.read_seqfile(filename)
		invalidCommand = self.static_validate(newSeq)
		if invalidCommand.empty:
			self.futureCommColl.store_dataframe(newSeq)
			print "Input commands are successfully stored"
			return True
		else:
			raise QRError('Invalid command', invalidCommand)

	def system_close_common_behavior(self):
		#self.futureCommColl.remove_all()
		pass

	def system_refresh(self):
		self.futureCommColl.remove_all()
		self.expLogColl.remove_all()
		self.statColl.remove_all()
	
	def emergent_rollback(self):
		queryAll = {}
		resetQueue = self.statColl.pop_dataframe(queryAll)
		resetQueue = resetQueue.sort(columns='set_time', axis='index')
		if len(resetQueue)==0:
			return None
		resetSeq= defaultdict(list)

		# Make actuators to reset and get earliest set time dependent on reset_queue
		earliestDepTime = self.dummyEndTime
		for row in resetQueue.iterrows():
			uuid = row[1]['uuid']
			name = row[1]['name']
			zone = row[1]['zone']
			actuType = row[1]['actuator_type']
			actuator = metaactuators.make_actuator(uuid,name,zone,actuType)
			if not uuid in self.actuDict.keys():
				self.actuDict['uuid'] = actuator
			setTime = row[1]['set_time']
			dependentTime = setTime - actuator.get_longest_dependency()
			if earliestDepTime > dependentTime:
				earliestDepTime = dependentTime

		logQuery = {'$and':[{'reset_time':{'$gte':earliestDepTime}},{'reset_time':{'$lte':now}}]}
		expLog = expLogColl.load_dataframe(logQuery)
		
		# Construct reset sequence (dict of list. dict's key is target time)
		#TODO: Filter resetQueue by removing redundant reset signals
		now = self.now()
		while len(resetQueue)>0:
			currResetList = dict()
			for row in resetQueue.iterrows():
				uuid = row[1]['uuid']
				zone = row[1]['zone']
				actuator = self.actuDict[uuid]
				depFlag = False
				for uuid in actuator.get_dependent_actu_list():
					if (uuid in logQuery['uuid'][logQuery['reset_time']>=now-actuator.minLatencty]) or (uuid in currResetList):
						depFlag = True
						break
				if not depFlag:
					currResetList[uuid] = row[1]['reset_value']
					resetQueue = resetQueue.drop(row[2])
			now = now + timedelta(minutes=10)
			resetSeq.append(currResetList)

		# Reset all the sensors registered at reset_queue
		for currResetList in resetSeq:
			for uuid, resetVal in currResetList.iteritems():
				actuator = self.actuDict[uuid]
				now = self.now()
				origVal = actuator.get_latest_value(now)
				expLogRow = ExpLogRow(uuid, actuator.name, now, setVal=actuator.resetVal, origVal=origVal)
				self.expLogColl.store_row(expLogRow)
				actuator.reset_value(resetVal)
			# TODO: I have to acknowledge that the value is reset. However, how can I check if the reset value is -1?? How can I know if the value is reset or just changed?
			time.sleep(self.minResetLatency)
	
	def get_currest_status(self):
		return self.statColl.load_dataframe({'under_control':True})


#		except Exception, e:
#			print sys.exc_traceback.tb_lineno 
#			print sys.exc_traceback
#			print str(e)
#			print "Unknown error: ", sys.exc_info()[0]
#			if self.statColl.get_size()!=0:
#				self.notify_systemfault()
#				print 'sent an email'
#			print '==============End of Quiver=============='