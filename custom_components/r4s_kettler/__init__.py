
#!/usr/local/bin/python3
# coding: utf-8

from bluepy import btle
import binascii

from time import sleep
import time
from datetime import datetime
from textwrap import wrap
import logging

from datetime import timedelta

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.util.color as color_util
from homeassistant.const import (
    CONF_DEVICE,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL
)

CONF_MIN_TEMP = 40
CONF_MAX_TEMP = 100
CONF_TARGET_TEMP = 100

DEFAULT_TIMEOUT = 2

_LOGGER = logging.getLogger(__name__)

SUPPORTED_DOMAINS = ["water_heater", "sensor", "light", "switch"]

DOMAIN = "r4s_kettler"

async def async_setup(hass, config):
    return True

async def async_setup_entry(hass, config_entry):
    hass.data[DOMAIN] = {}

    config = config_entry.data

    mac = config.get(CONF_MAC)
    device = config.get(CONF_DEVICE)
    password = config.get(CONF_PASSWORD)
    scan_delta = timedelta(
        seconds=config.get(CONF_SCAN_INTERVAL)
    )

    device_registry = await dr.async_get_registry(hass)
    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        name="SkyKettle",
        model="G200S",
        manufacturer="Redmond"
    )

    kettler = hass.data[DOMAIN]["kettler"] = RedmondKettler(
        hass,
        mac,
        password,
        device
    )

    try:
        await kettler.firstConnect()
    except:
        _LOGGER.error("Connect to Kettler %s through device %s failed", mac, device)
        return False

    async_track_time_interval(hass, kettler.async_update, scan_delta)
    for domain in SUPPORTED_DOMAINS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(config_entry, domain)
        )

    return True


async def async_remove_entry(hass, entry):
    """Unload a config entry."""
    try:
        for domain in SUPPORTED_DOMAINS:
            await hass.config_entries.async_forward_entry_unload(entry, domain)
    except ValueError:
        pass


class BTLEConnection(btle.DefaultDelegate):

    def __init__(self, mac):
        btle.DefaultDelegate.__init__(self)
        self._conn = None
        self._mac = mac
        self._callbacks = {}

    def __enter__(self):
        try:
            self._conn = btle.Peripheral(deviceAddr=self._mac, addrType=btle.ADDR_TYPE_RANDOM)
            self._conn.withDelegate(self)
#            self._conn.connect(addr=self._mac, addrType=btle.ADDR_TYPE_RANDOM)
        except btle.BTLEException as ex:
            self.__exit__()
            self.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            self._conn.disconnect()
        self._conn = None

    def handleNotification(self, handle, data):
        if handle in self._callbacks:
            self._callbacks[handle](data)

    @property
    def mac(self):
        return self._mac

    def set_callback(self, handle, function):
        self._callbacks[handle] = function

    def make_request(self, handle, value, timeout=DEFAULT_TIMEOUT, with_response=True):
        try:
            self._conn.writeCharacteristic(handle, value, withResponse=with_response)
            if timeout:
                self._conn.waitForNotifications(timeout)
        except btle.BTLEException as ex:
            raise



class RedmondKettler:

    def __init__(self, hass, addr, key, device):
        self.hass = hass
        self._mac = addr
        self._key = key
        self._device = device
        self._mntemp = CONF_MIN_TEMP
        self._mxtemp = CONF_MAX_TEMP
        self._tgtemp = CONF_TARGET_TEMP
        self._temp = 0
        self._time_upd = '00:00'
        self._boiltime = '80'
        self._rgb1 = '0000ff'
        self._rgb2 = 'ff0000'
        self._rand = '5e'
        self._mode = '00' # '00' - boil, '01' - heat to temp, '03' - backlight
        self._status = '00' #may be '00' - OFF or '02' - ON
        self._iter = 0
        self._connected = False
        self._is_busy = False
        self._lastCmd = False
        self._conn = BTLEConnection(self._mac)
        self._conn.set_callback(11, self.handle_notification)



    def handle_notification(self, data):
        s = binascii.b2a_hex(data).decode("utf-8")
        arr = [s[x:x+2] for x in range (0, len(s), 2)]
        if self.hexToDec(arr[1]) == self._iter: # answer on our request
            if arr[2] == 'ff' or arr[2] == '03'  or arr[2] == '04'  or arr[2] == '05': ### sendAuth  sendOn    sendOff    sendMode
                if arr[3] == '01':
                    self._lastCmd = True
            if arr[2] == '6e'  or arr[2] == '37' or arr[2] == '32': ### sendSync   sendUseBacklight   sendSetLights
                if arr[3] == '00':
                    self._lastCmd = True
            if arr[2] == '06': ### sendStatus
                self._status = str(arr[11])
                self._temp = self.hexToDec(str(arr[8]))
                self._mode = str(arr[3])
                tgtemp = str(arr[5])
                if tgtemp != '00':
                    self._tgtemp = self.hexToDec(tgtemp)
                else:
                    self._tgtemp = 100
                self._lastCmd = True
            if arr[2] == '33': ### sendGetLights
                self._rand = str(arr[5])
                self._lastCmd = True
                if arr[3] == '01':
                    self._rgb1 = str(arr[6]) + str(arr[7]) + str(arr[8])
                    self._rgb2 = str(arr[16]) + str(arr[17]) + str(arr[18])

    def calcMidColor(self, rgb1, rgb2):
        try:
            hs1 = self.rgbhex_to_hs(rgb1)
            hs2 = self.rgbhex_to_hs(rgb2)
            hmid = int((hs1[0]+hs2[0])/2)
            smid = int((hs1[1]+hs2[1])/2)
            hsmid = (hmid,smid)
            rgbmid = self.hs_to_rgbhex(hsmid)
        except:
            rgbmid = '00ff00'
        return rgbmid

    def rgbhex_to_hs(self, rgbhex):
        rgb = color_util.rgb_hex_to_rgb_list(rgbhex)
        return color_util.color_RGB_to_hs(*rgb)

    def hs_to_rgbhex(self, hs):
        rgb = color_util.color_hs_to_RGB(*hs)
        return color_util.color_rgb_to_hex(*rgb)

    def theLightIsOn(self):
        if self._status == '02' and self._mode == '03':
            return True
        return False

    def theKettlerIsOn(self):
        if self._status == '02':
            if self._mode == '00' or self._mode == '01':
                return True
        return False

    def iterase(self): # counter
        self._iter+=1
        if self._iter >= 100: self._iter = 0

    def hexToDec(self, chr):
        return int(str(chr), 16)

    def decToHex(self, num):
        char = str(hex(int(num))[2:])
        if len(char) < 2:
            char = '0' + char
        return char



    async def async_update(self, now, **kwargs) -> None:
        try:
            await self.modeUpdate()
        except:
            return
        async_dispatcher_send(self.hass, DOMAIN)



    def sendResponse(self, conn):
        str2b = binascii.a2b_hex(bytes('0100', 'utf-8'))
        conn.make_request(12, str2b)
        return True

    def sendAuth(self,conn):
        self._lastCmd = False
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + 'ff' + self._key + 'aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendOn(self,conn):
        self._lastCmd = False
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '03aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendOff(self,conn):
        self._lastCmd = False
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '04aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendSync(self, conn, timezone = 4):
        self._lastCmd = False
        tmz_hex_list = wrap(str(self.decToHex(timezone*60*60)), 2)
        tmz_str = ''
        for i in reversed(tmz_hex_list):
            tmz_str+=i
        timeNow_list = wrap(str(self.decToHex(time.mktime(datetime.now().timetuple()))), 2)
        timeNow_str = ''
        for i in reversed(timeNow_list):
            timeNow_str+=i
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '6e' + timeNow_str + tmz_str + '0000aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendStat(self,conn):
        self._lastCmd = False
        return True

    def sendStatus(self,conn):
        self._lastCmd = False
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '06aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendMode(self, conn, mode, temp):   # 00 - boil 01 - heat to temp 03 - backlight (boil by default)    temp - in HEX
        self._lastCmd = False
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '05' + mode + '00' + temp + '00000000000000000000800000aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendUseBackLight(self, conn, use = True):
        self._lastCmd = False
        onoff="00"
        if use:
            onoff="01"
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '37c8c8' + onoff + 'aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendGetLights(self, conn, boilOrLight = "01"): # night light by default
        self._lastCmd = False
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '33' + boilOrLight + 'aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

    def sendSetLights(self, conn, boilOrLight = '00', rgb1 = '0000ff', rgb2 = 'ff0000'): # 00 - boil light    01 - backlight
        self._lastCmd = False
        if rgb1 == '0000ff' and rgb2 == 'ff0000':
            rgb_mid = '00ff00'
        else:
            rgb_mid = self.calcMidColor(rgb1,rgb2)
        if boilOrLight == "00":
            scale_light = ['28', '46', '64']
        else:
            scale_light = ['00', '32', '64']
        str2b = binascii.a2b_hex(bytes('55' + self.decToHex(self._iter) + '32' + boilOrLight + scale_light[0] + self._rand + rgb1 + scale_light[1] + self._rand + rgb_mid + scale_light[2] + self._rand + rgb2 + 'aa', 'utf-8'))
        conn.make_request(14, str2b)
        self.iterase()
        return self._lastCmd

### composite methods
    async def readNightColor(self,i=0):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            try:
                with self._conn as conn:
                    if self.sendResponse(conn):
                        if self.sendAuth(conn):
                            if self.sendGetLights():
                                answ = True
            except:
                pass
            if not answ:
                i=i+1
                if i<3:
                    self._is_busy = False
                    answ = await self.readNightColor(i)
                else:
                    _LOGGER.warning('three attempts of readNightColor failed')
            self._is_busy = False
            return answ
        else:
            return False

    async def startNightColor(self, i=0):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            try:
                with self._conn as conn:
                    if self.sendResponse(conn):
                        if self.sendAuth(conn):
                            if self.sendSetLights(conn, '01', self._rgb1, self._rgb1):
                                if self.sendMode(conn, '03', '00'):
                                    if self.sendOn(conn):
                                        if self.sendStatus(conn):
                                            self._time_upd = time.strftime("%H:%M")
                                            answ = True
            except:
                pass
            if not answ:
                i=i+1
                if i<3:
                    self._is_busy = False
                    answ = await self.startNightColor(i)
                else:
                    _LOGGER.warning('three attempts of startNightColor failed')
            self._is_busy = False
            return answ
        else:
            return False

    async def stopNightColor(self):
        await self.modeOff()

    async def modeOn(self, mode = "00", temp = "00", i=0):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            try:
                with self._conn as conn:
                    if self.sendResponse(conn):
                        if self.sendAuth(conn):
                            if self.sendMode(conn, mode, temp):
                                if self.sendOn(conn):
                                    if self.sendStatus(conn):
                                        self._time_upd = time.strftime("%H:%M")
                                        answ = True
            except:
                pass
            if not answ:
                i=i+1
                if i<3:
                    self._is_busy = False
                    answ = await self.modeOn(mode,temp,i)
                else:
                    _LOGGER.warning('three attempts of modeOn failed')
            self._is_busy = False
            return answ
        else:
            return False

    async def modeOff(self, i=0):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            try:
                with self._conn as conn:
                    if self.sendResponse(conn):
                        if self.sendAuth(conn):
                            if self.sendOff(conn):
                                if self.sendStatus(conn):
                                    self._time_upd = time.strftime("%H:%M")
                                    answ = True
            except:
                pass
            if not answ:
                i=i+1
                if i<3:
                    self._is_busy = False
                    answ = await self.modeOff(i)
                else:
                    _LOGGER.warning('three attempts of modeOff failed')
            self._is_busy = False
            return answ
        else:
            return False

    async def firstConnect(self, i=0):
        self._is_busy = True
        iter = 0
        answ = False
        try:
            with self._conn as conn:
                while iter < 10:  # 10 attempts to auth
                    answer = False
                    if self.sendResponse(conn):
                        if self.sendAuth(conn):
                            answer = True
                            break
                    sleep(1)
                    iter+=1
                if answer:
                    if self.sendUseBackLight(conn):
                        if self.sendSync(conn):
                            if self.sendStatus(conn):
                                self._time_upd = time.strftime("%H:%M")
                                answ = True
                if answ:
                    self._connected = True
        except:
            pass
        if not answ:
            i=i+1
            if i<3:
                await self.firstConnect(i)
            else:
                _LOGGER.warning('three attempts of firstConnect failed')
        self._is_busy = False

    async def modeUpdate(self, i=0):
        if not self._is_busy:
            self._is_busy = True
            answ = False
            try:
                with self._conn as conn:
                    if self.sendResponse(conn):
                        if self.sendAuth(conn):
                            if self.sendSync(conn):
                                if self.sendStatus(conn):
                                    self._time_upd = time.strftime("%H:%M")
                                    answ = True
            except:
                pass
            if not answ:
                i=i+1
                if i<3:
                    self._is_busy = False
                    answ = await self.modeUpdate(i)
                else:
                    _LOGGER.warning('three attempts of modeUpdate failed')
            self._is_busy = False
            return answ
        else:
            return False
