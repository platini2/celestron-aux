#!/usr/bin/env python

"""auxbus_session.py: stateful AUXBUS connection/decode engine.

Shared by celestron.py and celestronui.py. Each app builds its own
AuxBusSession (own scannerid, own xprint sink) and drives it through its own
menu/GUI.

The serial handshake in transmitmsg()/initializeconn() is timing-sensitive
real hardware depends on the exact sleep durations and RTS toggle sequence
below being unchanged from the original script.
"""

import binascii
import socket
import threading
import time
from datetime import datetime, timezone

import serial
from PIL import Image, ImageDraw

import auxbus_protocol as protocol

SERVER_PORT = 2000
BUFFER_SIZE = 100
KEEP_ALIVE_INTERVAL = 10

# One canonical canned Starsense capture frame, used by the "inject test
# Starsense capture" debug command (see AuxBusSession.encodemsg3c).
_DEBUG_STARSENSE_CAPTURE = (
    "01922744b63b96436e90cb4307c13c44136e8a4305c58943ef2721448c572f4442988b44"
    "b392904319110944a3b563445f38424412e29543726fbd430666d5426032354403490343"
    "ea882b44b2ce30444b68c043a1352a44fcad11442194e443f6e040441bd6fa43b36f8f44"
    "92ecdc416698ac438ab3dc43d4010b42e8fe1a44abc09c43ee6cd1433c981644019a4944"
    "c316a843a0e70943c58493425ea70844534ba64306071442f804c24390011244d4fd5744"
    "d0ab0b427679cd431286c4437beaba4348d7c14356d93a4488a17543a7b20144400e1e44"
    "2f4024446a430e441e99b442693820447fe55c4480865644ecc00744524de44395599a44"
    "317aaf4390b9454413bd454496239444b84a51448bfe9a44b6e8c2434c41d942985464442"
    "00d19444af003442b8a3e4431469c4321b4d6439bbb1744003721445c2d6244d1798844"
    "967fd7439904ca422ecd2b448944d5436df46044ef158b4380c46244f25e4b4462c2dc43"
    "c7d84344b78f1e442b8ba4437e49f2435a7a664472ff0b4342834544b53b85435d1fca43"
    "02475042cfb56b445916604370345a4406825944a8cb9a44bd83f8439c2447446ebc1344"
    "363b7444a253e243f08951433ec184430e44114341ab82430b7378441160de42ec5d5e44"
    "163c5643367a7e448da2a443c62e8944c82b3c44e7b33543a8bd3d44fe1d3a440fc6a243"
    "4e6a39448ebded436a6c4544aaf9634486d41844302b2144a30e4044abb14c44d31d264"
    "4850ef7413026fd434d08b0438d6203445e2cf343b15c994425432844fb011644cae9df"
    "4288b5f143c77c2c442b7fd343bc4e0044d28e5c44433c3344d12cb843d4119143ee06e"
    "c43f053df429870b443d8edec437ad47242f2b51b444d306543ae4ac843debb5444d343"
    "4943d3596e44eb751b443a200b44435d3a432721364430f2ad4207d0854359bad14350e"
    "8a8431c2a3544d6fb88449261604410cc7d440a57b743583c7d44b315c74340155c44c8"
    "da5c44a86b5f441db19c4353e88d445b4f2044b0a0e0437e85fc43c7028d43172e5c44a"
    "0a84f448c9baf43b7976f44b5d75f440e0f654447c22444d923024427bc5644717edf43"
    "a3ce24440000000000000000")


class AuxBusSession:
    """Owns one AUXBUS connection (serial / hand-controller / WiFi), the
    background receive/keepalive threads, and encode/decode dispatch."""

    def __init__(self, scannerid, xprint=print, starsensesave=True):
        self.scannerid = scannerid
        self.xprint = xprint

        # Real hardware devices, plus this app's own identity on the bus.
        self.devices = dict(protocol.DEVICES)
        self.devices[scannerid] = 'AUXBUS Scanner'
        self.mounts = protocol.MOUNTS
        self.controllers = protocol.BASE_CONTROLLERS + [scannerid]

        self.connmode = None
        self.ser = None
        self.sock = None

        self.msgqueue = ''
        self.msgqueue_lock = threading.RLock()
        self.endthread = False
        self.keepalive = False
        self.triggerscan = ' '

        self.emulategps = False
        self.gpslat = 0.0
        self.gpslon = 0.0

        self.mount = ''
        self.activedevices = {}

        self.verbose = False
        self.starsensesave = starsensesave
        self.filecsvoutput = False
        self.rawfileoutput = False
        self.oof = 0
        self.starttime = 0.0

        self.t0 = None
        self.t2 = None

    # ---- connection lifecycle --------------------------------------------

    def initializeconn(self, connmodearg, port):
        self.connmode = connmodearg
        if self.connmode == 'wifi':
            self.keepalive = True
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((port, SERVER_PORT))
        if self.connmode == 'serial' or self.connmode == 'hc':
            self.keepalive = False
            self.ser = serial.Serial()
            self.ser.port = port
            if self.connmode == 'serial':
                self.ser.baudrate = 19200
            if self.connmode == 'hc':
                self.ser.baudrate = 9600
            self.ser.bytesize = serial.EIGHTBITS
            self.ser.parity = serial.PARITY_NONE
            self.ser.stopbits = serial.STOPBITS_ONE
            self.ser.timeout = 0
            self.ser.xonxoff = False
            self.ser.open()
            if self.ser.isOpen():
                self.ser.flushInput()
                self.ser.flushOutput()
            if self.connmode == 'hc':
                data = b'\x56'
                self.ser.write(data)
                time.sleep(.5)
                # Should return 0x05 0x1f 0x23
                self.ser.read(self.ser.inWaiting())
                data = b'\x8a'
                self.ser.write(data)
                time.sleep(.5)
                # Should return 0xf8
                self.ser.read(self.ser.inWaiting())
                self.ser.close
                self.ser.baudrate = 115200
                self.ser.open
                time.sleep(1)
                data = b'\x0a\x00\x02\x08\x00\x4b\x00\x00\xd0\xc0'
                self.ser.write(data)
                time.sleep(.5)
                # Should return 0x05 0x00 0x06 0x38 0xc0
                self.ser.read(self.ser.inWaiting())

    def closeconn(self):
        self.xprint("-----------------------")
        self.xprint("      Closing          ")
        self.xprint("-----------------------")
        if self.connmode == 'wifi':
            self.sock.close()
        if self.connmode == 'serial' or self.connmode == 'hc':
            self.ser.close()

    def shutdown(self):
        self.endthread = True
        self.transmitmsg('3b', '', 0x10, 0xfe, '')
        if self.t0:
            self.t0.join()
        if self.t2:
            self.t2.join()
        self.closeconn()

    def launchthreads(self):
        self.t0 = threading.Thread(target=self.receivedata)
        self.t0.daemon = True
        self.t0.start()
        self.t2 = threading.Thread(target=self.sendingdata, args=(KEEP_ALIVE_INTERVAL,))
        self.t2.daemon = True
        self.t2.start()

    def appstartup(self, version):
        self.xprint("-------------------------------")
        self.xprint(" AUXBUS SCANNER VERSION", version)
        self.xprint("-------------------------------")
        self.starttime = time.time()
        self.launchthreads()
        self.xprint("-----------------------")
        self.xprint("Starting AUXBUS Monitor")
        self.xprint("-----------------------")
        self.triggerscan = 'known'

    def resettime(self):
        self.starttime = time.time()

    # ---- framing / encode --------------------------------------------------

    def encodemsg(self, sender, receiver, command, value):
        if sender == '':
            sender = self.scannerid
        output = protocol.build_frame(sender, receiver, command, value)
        if (self.connmode == "hc" or self.connmode == "wifi") and not self.emulategps:
            with self.msgqueue_lock:
                self.msgqueue = self.msgqueue + output
                self.processmsgqueue()
        return binascii.unhexlify(output)

    def encodemsg3c(self):
        """Build the canned debug Starsense-capture frame (0x3c preamble)."""
        data = _DEBUG_STARSENSE_CAPTURE
        length = "{:08x}".format(int(len(data) / 2))
        data = length + data
        payload_bytes = [int(data[c:c + 2], 16) for c in range(0, len(data), 2)]
        chk = protocol.checksum(payload_bytes)
        # Escape any literal preamble byte inside the payload, matching the
        # '3b0202' -> '3b' unescape done on receive in processmsgqueue().
        c = 0
        while c < len(data):
            if int(data[c:c + 2], 16) == protocol.PREAMBLE:
                data = data[:c + 2] + '0202' + data[c + 2:]
            c = c + 2
        output = "{:02x}".format(protocol.PREAMBLE2) + data + "{:02x}".format(chk)
        if self.connmode == "hc":
            with self.msgqueue_lock:
                self.msgqueue = self.msgqueue + output
                self.processmsgqueue()
        return binascii.unhexlify(output)

    # ---- decode / dispatch ---------------------------------------------------

    def processmsgqueue(self):
        with self.msgqueue_lock:
            preamble_hex = format(protocol.PREAMBLE, '02x')
            preamble2_hex = format(protocol.PREAMBLE2, '02x')
            if len(self.msgqueue) > 1:
                while len(self.msgqueue) > 1 and self.msgqueue[0:2] != preamble_hex and self.msgqueue[0:2] != preamble2_hex:
                    self.oof = self.oof + 1
                    self.msgqueue = self.msgqueue[2:]
            emptyqueue = 0
            while len(self.msgqueue) >= (2 * 6) and (self.msgqueue[0:2] == preamble_hex or self.msgqueue[0:2] == preamble2_hex) and emptyqueue == 0:
                if self.msgqueue[0:2] == preamble_hex:
                    length = int(self.msgqueue[2:4], 16)
                    if len(self.msgqueue) >= (2 * (length + 3)):
                        frame = self.msgqueue[0:2 * (length + 3)]
                        self.msgqueue = self.msgqueue[2 * (length + 3):]
                        self._safe_dispatch(self.decodemsg, frame)
                    else:
                        emptyqueue = 1
                if self.msgqueue[0:2] == preamble2_hex:
                    self.msgqueue = self.msgqueue.replace('3b0202', '3b')
                    length = int(self.msgqueue[6:10], 16)
                    if len(self.msgqueue) >= (2 * (length + 6)):
                        frame = self.msgqueue[0:2 * (length + 6)]
                        self.msgqueue = self.msgqueue[2 * (length + 6):]
                        self._safe_dispatch(self.decodemsg3c, frame)
                    else:
                        emptyqueue = 1

    def _safe_dispatch(self, decoder, frame):
        """Never let a malformed/unexpected frame kill the caller (background
        receive thread or a synchronous send from the menu) — log and move on."""
        try:
            decoder(frame)
        except Exception as e:
            self.xprint("Error decoding frame (ignored, monitoring continues):", repr(e))

    def decodemsg(self, msg):
        frame = protocol.parse_frame(msg)
        if not frame.checksum_ok:
            output = str(hex(frame.sender) + " -> " + hex(frame.receiver) + " --- " + hex(frame.command) + " --- " + ' '.join(map(str, frame.commandvalue)) + " CRC FAIL!")
            self.xprint(output)
            return

        sender, receiver, command, commandvalue = frame.sender, frame.receiver, frame.command, frame.commandvalue
        if sender == self.scannerid or receiver == self.scannerid:
            device = receiver if sender == self.scannerid else sender
        else:
            device = sender if sender not in self.controllers else receiver

        if len(commandvalue) > 0:
            if hex(command) == '0xfe':
                commandvalue = protocol.decode_command_value(sender, device, command, commandvalue)
                if len(commandvalue) > 0 and hex(sender) not in self.activedevices:
                    self.activedevices[hex(sender)] = commandvalue
            else:
                commandvalue = protocol.decode_command_value(sender, device, command, commandvalue)
                if hex(command) == '0x5' and hex(sender) == '0x10':
                    self.mount = int(commandvalue, 16)

        commandtext = protocol.COMMANDS.get((device, command), 'UNKNOWN')
        sendertext = self.devices.get(sender, 'UNKNOWN')
        receivertext = self.devices.get(receiver, 'UNKNOWN')
        dumptext = ' --- ' + str(msg) if self.verbose else ''
        output = (str(format(round(time.time() - self.starttime, 6), '14.6f')) + " - "
                  + "{:<21}".format(sendertext) + " (0x" + format(sender, '02x') + ") "
                  + "-> " + "{:<21}".format(receivertext) + " (0x" + format(receiver, '02x') + ") "
                  + "--- " + "{:<21}".format(commandtext) + " (0x" + format(command, '02x') + ") "
                  + "--- " + str(commandvalue) + dumptext)
        self.xprint(output)

        if self.filecsvoutput:
            fileoutput = (str(format(round(time.time() - self.starttime, 6), '14.6f')) + "," + sendertext + ","
                          + str(hex(sender)) + "," + receivertext + "," + str(hex(receiver)) + ","
                          + commandtext + "," + str(hex(command)) + "," + str(commandvalue) + "," + str(msg))
            with open('auxbuslog.csv', 'a') as f:
                print(fileoutput, file=f)

        if self.emulategps and hex(receiver) == '0xb0':
            self._emulate_gps_reply(sender, receiver, command)

    def _emulate_gps_reply(self, sender, receiver, command):
        cmd = hex(command)
        if cmd == '0x36':
            self.transmitmsg('3b', receiver, sender, command, '01')
        if cmd == '0xfe':
            self.transmitmsg('3b', receiver, sender, command, '0b01')
        if cmd == '0x33':
            now = datetime.now(timezone.utc)
            self.transmitmsg('3b', receiver, sender, command, format(now.hour, '02x') + format(now.minute, '02x') + format(now.second, '02x'))
        if cmd == '0x3':
            now = datetime.now(timezone.utc)
            self.transmitmsg('3b', receiver, sender, command, format(now.month, '02x') + format(now.day, '02x'))
        if cmd == '0x4':
            now = datetime.now(timezone.utc)
            self.transmitmsg('3b', receiver, sender, command, format(now.year, '04x'))
        if cmd == '0x37':
            self.transmitmsg('3b', receiver, sender, command, '01')
        if cmd == '0x2':
            if self.gpslon >= 0:
                gpslonhex = format(int(self.gpslon / 360 * pow(2, 24)), '06x')
            else:
                gpslonhex = format(int((self.gpslon + 360) / 360 * pow(2, 24)), '06x')
            self.transmitmsg('3b', receiver, sender, command, gpslonhex)
        if cmd == '0x1':
            if self.gpslat >= 0:
                gpslathex = format(int(self.gpslat / 360 * pow(2, 24)), '06x')
            else:
                gpslathex = format(int((self.gpslat + 360) / 360 * pow(2, 24)), '06x')
            self.transmitmsg('3b', receiver, sender, command, gpslathex)

    def decodemsg3c(self, msg):
        starlen = 2 * 8
        pixellist = []
        xres, yres = protocol.STARSENSE_XRES, protocol.STARSENSE_YRES

        if len(msg) < 2 * 6:
            self.xprint("Starsense HC Data - CRC FAIL!")
            return
        lengthH = int(msg[6:8], 16)
        lengthL = int(msg[8:10], 16)
        length = lengthH * 256 + lengthL
        payload_bytes = [int(msg[c:c + 2], 16) for c in range(2, len(msg) - 2, 2)]
        checksum_byte = int(msg[-2:], 16)
        checksumok = protocol.checksum(payload_bytes) == checksum_byte

        if not checksumok:
            self.xprint("Starsense HC Data - CRC FAIL!")
            return

        dumptext = ' --- ' + str(msg) if self.verbose else ''
        output = (str(format(round(time.time() - self.starttime, 6), '14.6f')) + " - Starsense HC Data - "
                  + str(int(length) / 8) + " Stars " + msg[5 * 2:-2] + dumptext)
        self.xprint(output)
        data = msg[5 * 2:-2]
        stars = [data[i:i + starlen] for i in range(0, len(data), starlen)]
        for i, star in enumerate(stars):
            if star != "0000000000000000":
                self.xprint("                 Star", i + 1, " - ", protocol.decode_starsense_star(star))
                pixellist.append(protocol.starsense_pixel(star, xres, yres))

        if self.starsensesave:
            img = Image.new('L', (xres, yres))
            draw = ImageDraw.Draw(img)
            for pixel in pixellist:
                shape = [pixel[0], pixel[1], pixel[0] + pixel[2] - 1, pixel[1] + pixel[3]]
                draw.rectangle(shape, fill="white", outline="white")
            imgfilename = 'starsense-image-' + datetime.now().strftime("%Y%m%d-%H%M%S") + '.png'
            img.save(imgfilename)

        if self.filecsvoutput:
            fileoutput = (str(format(round(time.time() - self.starttime, 6), '14.6f')) + "," + "Starsense Camera" + ","
                          + "0xb4" + "," + "All" + "," + "0x00" + "," + "Data" + "," + "0x00" + "," + "[]" + "," + str(msg))
            with open('auxbuslog.csv', 'a') as f:
                print(fileoutput, file=f)

    # ---- transport (timing-sensitive) --------------------------------------

    def transmitmsg(self, msgtype, sender, receiver, command, value):
        if msgtype == '3b':
            data = self.encodemsg(sender, receiver, command, value)
        if msgtype == '3c':
            data = self.encodemsg3c()
        if self.rawfileoutput:
            fileoutput = str(round(time.time() - self.starttime, 6)) + " " + str(binascii.hexlify(data), 'utf-8')
            with open('rawoutput.txt', 'a') as f:
                print(fileoutput, file=f)
        if self.connmode == 'wifi':
            self.sock.send(data)
        if self.connmode == 'serial':
            self.ser.rtscts = True
            self.ser.rts = True
            self.ser.write(data)
            self.ser.rts = False
            self.ser.rts = True
            self.ser.rts = False
            time.sleep(.1)
            self.ser.read(self.ser.inWaiting())
            self.ser.rtscts = False
        if self.connmode == 'hc':
            self.ser.write(data)
        time.sleep(0.15)

    def trigger_starsense_capture(self):
        """Timing matches real device response latency — do not shorten."""
        self.transmitmsg('3b', '', 0xb4, 0x90, '03e800')
        time.sleep(4)
        self.transmitmsg('3b', '', 0xb4, 0x94, '00')
        time.sleep(4)
        self.transmitmsg('3b', '', 0xb4, 0x92, '')

    def keep_alive(self, interval):
        while not self.endthread:
            try:
                if self.keepalive:
                    self.transmitmsg('3b', '', 0x10, 0xfe, '')
            except Exception as e:
                self.xprint("Error in keep_alive (ignored):", repr(e))
            time.sleep(interval)
        self.xprint("Finished Keep Alive")

    def receivedata(self):
        data = ''
        while not self.endthread:
            try:
                time.sleep(.05)
                if self.connmode == 'wifi':
                    data = self.sock.recv(BUFFER_SIZE)
                if self.connmode == 'serial' or self.connmode == 'hc':
                    if self.ser.inWaiting() > 0:
                        data = self.ser.read(self.ser.inWaiting())
                if len(data) > 0:
                    stringdata = binascii.hexlify(data)
                    with self.msgqueue_lock:
                        self.msgqueue = self.msgqueue + str(stringdata, 'utf-8')
                    if self.rawfileoutput:
                        fileoutput = str(round(time.time() - self.starttime, 6)) + " " + str(stringdata, 'utf-8')
                        with open('rawoutput.txt', 'a') as f:
                            print(fileoutput, file=f)
                    self.processmsgqueue()
                    data = ''
            except Exception as e:
                self.xprint("Error in receive loop (ignored, monitoring continues):", repr(e))
                data = ''
        self.xprint("Finished Receive Data")

    def sendingdata(self, interval):
        lasttx = 0
        while not self.endthread:
            try:
                time.sleep(.05)
                if self.triggerscan == 'known' or self.triggerscan == 'all':
                    self.scanauxbus(self.triggerscan)
                    self.triggerscan = ''
                    lasttx = time.time()
                if self.keepalive:
                    if time.time() - lasttx > interval:
                        self.transmitmsg('3b', '', 0x10, 0xfe, '')
                        lasttx = time.time()
            except Exception as e:
                self.xprint("Error in send loop (ignored, monitoring continues):", repr(e))

    def fileplayback(self, filename):
        self.resettime()
        lasttime = 0.0
        with open(filename, "r") as f:
            for linenum, line in enumerate(f.read().splitlines(), start=1):
                line2 = line.split()
                if len(line2) == 2:
                    if linenum != 1:
                        time.sleep(float(line2[0]) - lasttime)
                    lasttime = float(line2[0])
                    data = line2[1]
                else:
                    data = line2[0]
                with self.msgqueue_lock:
                    self.msgqueue = self.msgqueue + data
                    self.processmsgqueue()
        self.xprint("Finished File Processing")

    # ---- scan / device tracking ---------------------------------------------

    def scanauxbus(self, target):
        self.xprint("-----------------------")
        self.xprint("Initiating AUXBUS SCAN ")
        self.xprint("-----------------------")
        self.xprint("     Timestamp - Sender                  Hex   -> Receiver                Hex   --- Command      Hex   --- Value")
        if target == 'known':
            for device in self.devices:
                self.transmitmsg('3b', '', device, 0xfe, '')
        if target == 'all':
            for device in range(0x01, 0xff):
                self.transmitmsg('3b', '', device, 0xfe, '')
        time.sleep(0.5)
        self.identifymount()
        time.sleep(1)
        self.xprint("-----------------------")
        self.xprint(" Finished AUXBUS SCAN  ")
        self.xprint("-----------------------")
        self.printactivedevices()

    def identifymount(self):
        device = 0x10
        if str(hex(device)) in self.activedevices:
            self.transmitmsg('3b', '', device, 0x05, '')

    def printactivedevices(self):
        if self.mount in self.mounts:
            mounttext = self.mounts[self.mount]
        else:
            mounttext = ('UNKNOWN' + " (" + str(hex(self.mount)) + ")") if len(str(self.mount)) > 0 else 'NOT DETECTED'
        self.xprint("-----------------------")
        self.xprint(" Mount :", mounttext)
        self.xprint("-----------------------")
        self.xprint("-----------------------")
        self.xprint("   Detected Devices    ")
        self.xprint("-----------------------")
        listactivedevices = list(self.activedevices)
        for device in self.activedevices:
            devicetext = self.devices.get(int(device, 16), 'UNKNOWN')
            output = (str(listactivedevices.index(device)) + ") " + "{:<21}".format(devicetext)
                      + " (0x" + format(int(device, 16), '02x') + ") - " + self.activedevices[device])
            self.xprint(output)

    # ---- menu-backing helpers (toggles with a side effect) ------------------

    def enable_gps_emulation(self, lat, lon):
        if hex(0xb0) not in self.activedevices:
            self.activedevices[hex(0xb0)] = '11.1'
        self.gpslat = lat
        self.gpslon = lon
        self.emulategps = True

    def disable_gps_emulation(self):
        self.emulategps = False

    def toggle_csv_logging(self):
        self.filecsvoutput = not self.filecsvoutput
        if self.filecsvoutput:
            header = 'timestamp,sender,sender_id,receiver,receiver_id,command,command_id,command_data,raw_packet'
            with open('auxbuslog.csv', 'w') as f:
                print(header, file=f)
        return self.filecsvoutput

    def toggle_raw_file_output(self):
        self.rawfileoutput = not self.rawfileoutput
        if self.rawfileoutput:
            open('rawoutput.txt', 'w').close()
        return self.rawfileoutput

    def start_rescan(self, target):
        self.activedevices = {}
        self.mount = ''
        self.triggerscan = target
