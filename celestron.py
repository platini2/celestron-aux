#!/usr/bin/env python

"""Celestron.py: Celestron AUXBUS Scanner"""
__author__ = "Patricio Latini"
__copyright__ = "Copyright 2021, Patricio Latini"
__credits__ = "Patricio Latini"
__license__ = "GPLv3"
__version__ = "0.9.36"
__maintainer__ = "Patricio Latini"
__email__ = "p_latini@hotmail.com"
__status__ = "Production"

import argparse
import os
import time

import auxbus_protocol as protocol
from auxbus_session import AuxBusSession

SCANNER_ID = 0x2f


def xprint(*args):
    print(" ".join(map(str, args)))


def printhelpmenu():
    xprint("-----------------------")
    xprint("      Commands         ")
    xprint("-----------------------")
    xprint("d) show Device list    ")
    xprint("c) send Command to device")
    xprint("k) toggle Keepalive send")
    xprint("s) reScan AUXBUS       ")
    xprint("a) rescan AUXBUS for All")
    xprint("v) toggle Verbose output")
    xprint("f) toggle csv File output")
    xprint("g) toggle GPS simulator")
    xprint("ss) toggle Starsense Image Save")
    xprint("3) inject test Starsense capture (debug)")
    xprint("4) trigger Starsense Image captura")
    xprint("8) Read raw capture from file rawinput.txt")
    xprint("9) Write raw capture to file rawoutput.txt")
    xprint("r) Reset Packet Timer  ")
    xprint("o) Out of frame counter")
    xprint("h) print this Help menu")
    xprint("q) Quit                ")
    xprint("-----------------------")


def mainloop(session):
    while True:
        inputkey = input("Enter Command:")

        if inputkey == "d":
            session.printactivedevices()

        if inputkey == "c":
            session.printactivedevices()
            time.sleep(0.2)
            print("-----------------------")
            print("Choose device")
            key1 = input("Enter Device:")
            listactivedevices = list(session.activedevices)
            device_id = int(listactivedevices[int(key1)], 16)
            filtercommands = [(k[1], v) for k, v in protocol.COMMANDS.items() if k[0] == device_id]
            for index, command in enumerate(filtercommands):
                print(chr(97 + index) + ") " + str(hex(command[0])) + " (" + str(command[1]) + ") ")
            time.sleep(0.2)
            print("-----------------------")
            print("Choose command")
            key2 = input("Enter Command:")
            session.transmitmsg('3b', '', device_id, filtercommands[ord(key2) - 97][0], '')

        if inputkey == "k":
            print("-----------------------")
            print("   Toggle Keepalive    ")
            print("-----------------------")
            session.keepalive = not session.keepalive
            print("   Keepalive " + ("Enabled" if session.keepalive else "Disabled") + "    ")

        if inputkey == "g":
            print("-----------------------")
            print(" Toggle GPS Emulation  ")
            print("-----------------------")
            if session.emulategps:
                session.disable_gps_emulation()
                print("  GPS Emulation Disabled    ")
            else:
                gpslat = float(input("Enter GPS Latitude in Decimal Format: "))
                gpslon = float(input("Enter GPS Longitude in Decimal Format: "))
                session.enable_gps_emulation(gpslat, gpslon)
                print("  GPS Emulation Enabled    ")

        if inputkey == "s":
            session.start_rescan('known')

        if inputkey == "a":
            session.start_rescan('all')

        if inputkey == "i":
            session.identifymount()

        if inputkey == "v":
            session.verbose = not session.verbose

        if inputkey == "f":
            session.toggle_csv_logging()

        if inputkey == "ss":
            session.starsensesave = not session.starsensesave

        if inputkey == "r":
            session.resettime()

        if inputkey == "h":
            printhelpmenu()

        if inputkey == "o":
            print("Out of Frame bytes : ", session.oof)

        if inputkey == "3":
            session.transmitmsg('3c', '', '', '', '')

        if inputkey == "4":
            session.trigger_starsense_capture()

        if inputkey == "8":
            if os.path.isfile("rawinput.txt"):
                session.fileplayback("rawinput.txt")
            else:
                print("rawinput.txt is not present")

        if inputkey == "9":
            session.toggle_raw_file_output()

        if inputkey == "q":
            session.shutdown()
            break

        time.sleep(.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('connmode', help='connection mode (wifi / serial / hc)')
    parser.add_argument('port', help='connection port (ip address / COM Port)')
    args = parser.parse_args()

    session = AuxBusSession(scannerid=SCANNER_ID, xprint=xprint, starsensesave=True)
    session.initializeconn(args.connmode, args.port)
    session.appstartup(__version__)
    mainloop(session)


if __name__ == '__main__':
    main()
