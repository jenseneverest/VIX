from time import time
from os import path

from enigma import eServiceCenter, eServiceReference, eTimer, pNavigation, getBestPlayableServiceReference, iServiceInformation, iPlayableService, setPreferredTuner, eDVBLocalTimeHandler, iRecordableServicePtr

from Components.ParentalControl import parentalControl
from Components.config import config
from Components.SystemInfo import SystemInfo
from Tools.BoundFunction import boundFunction
from Tools.StbHardware import getFPWasTimerWakeup
import RecordTimer
import PowerTimer
from ServiceReference import ServiceReference  # noqa: F401
import Screens.Standby
import NavigationInstance
from Screens.InfoBar import InfoBar
from Components.Sources.StreamService import StreamServiceList
from Screens.InfoBarGenerics import streamrelay

# TODO: remove pNavgation, eNavigation and rewrite this stuff in python.


class Navigation:
	def __init__(self, nextRecordTimerAfterEventActionAuto=False, nextPowerManagerAfterEventActionAuto=False):
		if NavigationInstance.instance is not None:
			raise NavigationInstance.instance

		NavigationInstance.instance = self
		self.ServiceHandler = eServiceCenter.getInstance()

		import Navigation as Nav
		Nav.navcore = self

		self.pnav = pNavigation()
		self.pnav.m_event.get().append(self.dispatchEvent)
		self.pnav.m_record_event.get().append(self.dispatchRecordEvent)
		self.event = []
		self.record_event = []
		self.currentlyPlayingServiceReference = None
		self.currentlyPlayingServiceOrGroup = None
		self.originalPlayingServiceReference = None
		self.currentlyPlayingService = None
		self.currentServiceIsStreamRelay = False
		self.skipServiceReferenceReset = False
		self.RecordTimer = RecordTimer.RecordTimer()
		self.PowerTimer = PowerTimer.PowerTimer()
		self.__wasTimerWakeup = False
		self.__nextRecordTimerAfterEventActionAuto = nextRecordTimerAfterEventActionAuto
		self.__nextPowerManagerAfterEventActionAuto = nextPowerManagerAfterEventActionAuto
		if getFPWasTimerWakeup():
			self.__wasTimerWakeup = True
			self._processTimerWakeup()

	def _processTimerWakeup(self):
		now = time()
		wakeup = ""
		timeHandlerCallbacks = eDVBLocalTimeHandler.getInstance().m_timeUpdated.get()
		if self.__nextRecordTimerAfterEventActionAuto and now < eDVBLocalTimeHandler.timeOK:  # 01.01.2004
			print("[Navigation] RECTIMER: wakeup to standby but system time not set.")
			if self._processTimerWakeup not in timeHandlerCallbacks:
				timeHandlerCallbacks.append(self._processTimerWakeup)
			return
		if self._processTimerWakeup in timeHandlerCallbacks:
			timeHandlerCallbacks.remove(self._processTimerWakeup)

		if self.__nextRecordTimerAfterEventActionAuto and abs(self.RecordTimer.getNextRecordingTime() - now) <= 360:
			wakeup = "/tmp/was_rectimer_wakeup"  # set wakeup flags as RecordTimer woke the box to record, place the box in standby.
		elif self.__nextPowerManagerAfterEventActionAuto:
			wakeup = "/tmp/was_powertimer_wakeup"  # set wakeup flags as a PowerTimer WakeToStandby was actioned.
		if wakeup:
			fwakeup = open(f"{wakeup}", "w")  # set wakeup timer type
			fwakeup.write("1")
			fwakeup.close()
			if path.exists(f"{wakeup}"):
				print(f"[Navigation] TIMER: wakeup to standby detected, flag set: {wakeup}.")
			fcec = open("/tmp/was_cectimer_wakeup", "w")  # tell Cec was timer wakeup, so don't issue Standby to TV
			fcec.write("1")
			fcec.close()
			self.standbytimer = eTimer()
			self.standbytimer.callback.append(self.gotostandby)
			self.standbytimer.start(15000, True)

	def wasTimerWakeup(self):
		return self.__wasTimerWakeup

	def pluginTimerWakeupName(self):
		return self.wasTimerWakeup() and config.misc.pluginWakeupName.value or None

	def clearPluginTimerWakeupName(self):
		config.misc.pluginWakeupName.value = ""

	def gotostandby(self):
		print("[Navigation] TIMER: now entering standby")
		import Tools.Notifications
		Tools.Notifications.AddNotification(Screens.Standby.Standby)

	def dispatchEvent(self, i):
		for x in self.event:
			x(i)
		if i == iPlayableService.evEnd:
			if not self.skipServiceReferenceReset:
				self.currentlyPlayingServiceReference = None
				self.currentlyPlayingServiceOrGroup = None
			self.currentlyPlayingService = None

	def dispatchRecordEvent(self, rec_service, event):
		# print "record_event", rec_service, event
		for x in self.record_event:
			try:
				x(rec_service, event)
			except:
				pass

	def restartService(self):
		self.playService(self.currentlyPlayingServiceOrGroup, forceRestart=True)

	def playService(self, ref, checkParentalControl=True, forceRestart=False, adjust=True):
		oldref = self.currentlyPlayingServiceOrGroup
		if ref and oldref and ref == oldref and not forceRestart:
			print("[Navigation] ignore request to play already running service(1)")
			return 1
		print("[Navigation] playing ref", ref and ref.toString())
		if path.exists("/proc/stb/lcd/symbol_signal") and config.lcd.mode.value == "1":
			try:
				if "0:0:0:0:0:0:0:0:0" not in ref.toString():
					signal = 1
				else:
					signal = 0
				f = open("/proc/stb/lcd/symbol_signal", "w")
				f.write(str(signal))
				f.close()
			except:
				f = open("/proc/stb/lcd/symbol_signal", "w")
				f.write("0")
				f.close()
		elif path.exists("/proc/stb/lcd/symbol_signal") and config.lcd.mode.value == "0":
			f = open("/proc/stb/lcd/symbol_signal", "w")
			f.write("0")
			f.close()

		if ref is None:
			self.stopService()
			return 0
		from Components.ServiceEventTracker import InfoBarCount
		InfoBarInstance = InfoBarCount == 1 and InfoBar.instance
		if not checkParentalControl or parentalControl.isServicePlayable(ref, boundFunction(self.playService, checkParentalControl=False, forceRestart=forceRestart, adjust=adjust)):
			if ref.flags & eServiceReference.isGroup:
				oldref = self.currentlyPlayingServiceReference or eServiceReference()
				playref = getBestPlayableServiceReference(ref, oldref)
				print("[Navigation] playref", playref)
				if playref and oldref and playref == oldref and not forceRestart:
					print("[Navigation] ignore request to play already running service(2)")
					return 1
				if not playref:
					alternativeref = getBestPlayableServiceReference(ref, eServiceReference(), True)
					self.stopService()
					if alternativeref and self.pnav:
						self.currentlyPlayingServiceReference = alternativeref
						self.currentlyPlayingServiceOrGroup = ref
						if self.pnav.playService(alternativeref):
							print("[Navigation] Failed to start: ", alternativeref.toString())
							self.currentlyPlayingServiceReference = None
							self.currentlyPlayingServiceOrGroup = None
							if oldref and "://" in oldref.getPath():
								print("[Navigation] Streaming was active -> try again")  # use timer to give the streamserver the time to deallocate the tuner
								self.retryServicePlayTimer = eTimer()
								self.retryServicePlayTimer.callback.append(boundFunction(self.playService, ref, checkParentalControl, forceRestart, adjust))
								self.retryServicePlayTimer.start(500, True)
						else:
							print("[Navigation] alternative ref as simulate: ", alternativeref.toString())
					return 0
				elif checkParentalControl and not parentalControl.isServicePlayable(playref, boundFunction(self.playService, checkParentalControl=False)):
					if self.currentlyPlayingServiceOrGroup and InfoBarInstance and InfoBarInstance.servicelist.servicelist.setCurrent(self.currentlyPlayingServiceOrGroup, adjust):
						self.currentlyPlayingServiceOrGroup = InfoBarInstance.servicelist.servicelist.getCurrent()
					return 1
			else:
				playref = ref
			if self.pnav:
				if not SystemInfo["FCCactive"]:
					self.pnav.stopService()
				else:
					self.skipServiceReferenceReset = True
				self.currentlyPlayingServiceReference = playref
				playref = streamrelay.streamrelayChecker(playref)
				self.currentlyPlayingServiceOrGroup = ref
				if InfoBarInstance and InfoBarInstance.servicelist.servicelist.setCurrent(ref, adjust):
					self.currentlyPlayingServiceOrGroup = InfoBarInstance.servicelist.servicelist.getCurrent()
				setPriorityFrontend = False
				if SystemInfo["DVB-T_priority_tuner_available"] or SystemInfo["DVB-C_priority_tuner_available"] or SystemInfo["DVB-S_priority_tuner_available"] or SystemInfo["ATSC_priority_tuner_available"]:
					str_service = playref.toString()
					if '%3a//' not in str_service and not str_service.rsplit(":", 1)[1].startswith("/"):
						type_service = playref.getUnsignedData(4) >> 16
						if type_service == 0xEEEE:
							if SystemInfo["DVB-T_priority_tuner_available"] and config.usage.frontend_priority_dvbt.value != "-2":
								if config.usage.frontend_priority_dvbt.value != config.usage.frontend_priority.value:
									setPreferredTuner(int(config.usage.frontend_priority_dvbt.value))
									setPriorityFrontend = True
							if SystemInfo["ATSC_priority_tuner_available"] and config.usage.frontend_priority_atsc.value != "-2":
								if config.usage.frontend_priority_atsc.value != config.usage.frontend_priority.value:
									setPreferredTuner(int(config.usage.frontend_priority_atsc.value))
									setPriorityFrontend = True
						elif type_service == 0xFFFF:
							if SystemInfo["DVB-C_priority_tuner_available"] and config.usage.frontend_priority_dvbc.value != "-2":
								if config.usage.frontend_priority_dvbc.value != config.usage.frontend_priority.value:
									setPreferredTuner(int(config.usage.frontend_priority_dvbc.value))
									setPriorityFrontend = True
							if SystemInfo["ATSC_priority_tuner_available"] and config.usage.frontend_priority_atsc.value != "-2":
								if config.usage.frontend_priority_atsc.value != config.usage.frontend_priority.value:
									setPreferredTuner(int(config.usage.frontend_priority_atsc.value))
									setPriorityFrontend = True
						else:
							if SystemInfo["DVB-S_priority_tuner_available"] and config.usage.frontend_priority_dvbs.value != "-2":
								if config.usage.frontend_priority_dvbs.value != config.usage.frontend_priority.value:
									setPreferredTuner(int(config.usage.frontend_priority_dvbs.value))
									setPriorityFrontend = True
				if config.misc.softcam_streamrelay_delay.value and self.currentServiceIsStreamRelay:
					self.currentServiceIsStreamRelay = False
					self.currentlyPlayingServiceReference = None
					self.currentlyPlayingServiceOrGroup = None
					print("[Navigation] Streamrelay was active -> delay the zap till tuner is freed")
					self.retryServicePlayTimer = eTimer()
					self.retryServicePlayTimer.callback.append(boundFunction(self.playService, ref, checkParentalControl, forceRestart, adjust))
					self.retryServicePlayTimer.start(config.misc.softcam_streamrelay_delay.value, True)
				elif self.pnav.playService(playref):
					# print("[Navigation] Failed to start", playref)
					self.currentlyPlayingServiceReference = None
					self.currentlyPlayingServiceOrGroup = None
					if oldref and "://" in oldref.getPath():
						print("[Navigation] Streaming was active -> try again")  # use timer to give the streamserver the time to deallocate the tuner
						self.retryServicePlayTimer = eTimer()
						self.retryServicePlayTimer.callback.append(boundFunction(self.playService, ref, checkParentalControl, forceRestart, adjust))
						self.retryServicePlayTimer.start(500, True)
				self.skipServiceReferenceReset = False
				if setPriorityFrontend:
					setPreferredTuner(int(config.usage.frontend_priority.value))
				if self.currentlyPlayingServiceReference and self.currentlyPlayingServiceReference.toString() in streamrelay.data:
					self.currentServiceIsStreamRelay = True
				return 0
		elif oldref and InfoBarInstance and InfoBarInstance.servicelist.servicelist.setCurrent(oldref, adjust):
			self.currentlyPlayingServiceOrGroup = InfoBarInstance.servicelist.servicelist.getCurrent()
		return 1

	def getCurrentlyPlayingServiceReference(self):
		return self.currentlyPlayingServiceReference

	def getCurrentlyPlayingServiceOrGroup(self):
		return self.originalPlayingServiceReference or self.currentlyPlayingServiceOrGroup

	def getCurrentServiceRef(self):
		curPlayService = self.getCurrentService()
		info = curPlayService and curPlayService.info()
		return info and info.getInfoString(iServiceInformation.sServiceref)

	def recordService(self, ref, simulate=False):
		service = None
		if not simulate:
			print("[Navigation] recording service:", (ref and ref.toString()))
		if ref:
			if ref.flags & eServiceReference.isGroup:
				ref = getBestPlayableServiceReference(ref, eServiceReference(), simulate)
			ref = streamrelay.streamrelayChecker(ref)
			service = ref and self.pnav and self.pnav.recordService(ref, simulate)
			if service is None:
				print("[Navigation] record returned non-zero")
		return service

	def stopRecordService(self, service):
		ret = -1
		if service and isinstance(service, iRecordableServicePtr):
			ret = self.pnav and self.pnav.stopRecordService(service)
		return ret

	def getRecordings(self, simulate=False):
		recs = self.pnav and self.pnav.getRecordings(simulate)
		if not simulate and StreamServiceList:
			for rec in recs[:]:
				if rec.__deref__() in StreamServiceList:
					recs.remove(rec)
		return recs

	def getCurrentService(self):
		if not self.currentlyPlayingService:
			self.currentlyPlayingService = self.pnav and self.pnav.getCurrentService()
		return self.currentlyPlayingService

	def stopService(self):
		if self.pnav:
			self.pnav.stopService()
		self.currentlyPlayingServiceReference = None
		self.currentlyPlayingServiceOrGroup = None
		if path.exists("/proc/stb/lcd/symbol_signal"):
			f = open("/proc/stb/lcd/symbol_signal", "w")
			f.write("0")
			f.close()

	def pause(self, p):
		return self.pnav and self.pnav.pause(p)

	def shutdown(self):
		self.RecordTimer.shutdown()
		self.PowerTimer.shutdown()
		self.ServiceHandler = None
		self.pnav = None

	def stopUserServices(self):
		self.stopService()
