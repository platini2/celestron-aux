#!/usr/bin/env python

"""Celestron.py: Celestron AUXBUS Scanner"""
__author__ = "Patricio Latini"
__copyright__ = "Copyright 2021, Patricio Latini"
__credits__ = "Patricio Latini"
__license__ = "GPLv3"
__version__ = "0.12.26"
__maintainer__ = "Patricio Latini"
__email__ = "p_latini@hotmail.com"
__status__ = "Production"
__mode__ = "text"

##### TKINTER CODE
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext
import serial.tools.list_ports
import re
import queue
##### TKINTER CODE

import argparse
import os
import time

import auxbus_protocol as protocol
from auxbus_session import AuxBusSession

SCANNER_ID = 0x22

session = None


def xprint(*args):
    if __mode__ == 'text':
        print(" ".join(map(str, args)))
    if __mode__ == 'UI':
        q.put(" ".join(map(str, args)))
        app.event_generate('<<AppendLine>>', when='tail')


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


def mainloop():
    printhelpmenu()

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


#### TKINTER
def validate(P):
    test = re.compile(r'(^\d{0,3}$|^\d{1,3}\.\d{0,3}$|^\d{1,3}\.\d{1,3}\.\d{0,3}$|^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{0,3}$)')
    return bool(test.match(P))


def serial_ports():
    ports = serial.tools.list_ports.comports()
    return [port.device for port in sorted(ports)]


def connect():
    if connectbutton_text.get() == "Connect":
        if radioValue.get() != ' ':
            connmode = radioValue.get()
            if radioValue.get() == 'wifi':
                port = ipadd.get()
            else:
                port = comboExample.get().split(' ', 1)[0]
        else:
            return
        session.initializeconn(connmode, port)
        session.appstartup(__version__)
        connectbutton_text.set("Disconnect")
    else:
        session.closeconn()
        connectbutton_text.set("Connect")


def appendLine(event):
    line = q.get_nowait()
    text_area.insert(tk.END, str(line) + "\n")
    text_area.see(tk.END)


def triggerbusscan():
    session.triggerscan = 'known'


def updateCBvar():
    session.keepalive = CBkeepalive.get()
    session.verbose = CBverbose.get()
    session.filecsvoutput = CBfilecsvoutput.get()
    session.rawfileoutput = CBrawfileoutput.get()
    session.emulategps = CBemulategps.get()


def tkinterinit():
    global q
    global app
    global radioValue, connectbutton_text, comboExample, ipadd, text_area
    global CBkeepalive, CBverbose, CBfilecsvoutput, CBrawfileoutput, CBemulategps

    q = queue.Queue()
    app = tk.Tk()
    app_title = tk.StringVar()
    app_title.set("Celestron AUXBUS Scanner " + __version__)
    app.geometry('1024x768')
    app.title(app_title.get())
    app.bind('<<AppendLine>>', appendLine)

    radioValue = tk.StringVar()
    radioValue.set(' ')
    radioOne = tk.Radiobutton(app, text='Serial', variable=radioValue, value="serial")
    radioTwo = tk.Radiobutton(app, text='Hand Controller', variable=radioValue, value="hc")
    radioThree = tk.Radiobutton(app, text='WiFi', variable=radioValue, value="wifi")
    connectbutton_text = tk.StringVar()
    connectbutton_text.set("Connect")
    connectbutton = tk.Button(app, textvariable=connectbutton_text, command=connect)
    scanbutton = tk.Button(app, text='Rescan AUXBUS', command=triggerbusscan)
    devicebutton = tk.Button(app, text='Device List', command=lambda: session.printactivedevices())
    comboExample = ttk.Combobox(app, values=serial_ports())
    comboExample.current(0)
    varip = tk.StringVar()
    varip.set('1.2.3.4')
    vcmd = app.register(validate)
    ipadd = tk.Entry(app, textvariable=varip, width=23, validate='key', validatecommand=(vcmd, '%P'))
    text_area = scrolledtext.ScrolledText(app, wrap=tk.WORD, width=120, height=40)
    CBkeepalive = tk.BooleanVar()
    CBverbose = tk.BooleanVar()
    CBfilecsvoutput = tk.BooleanVar()
    CBrawfileoutput = tk.BooleanVar()
    CBemulategps = tk.BooleanVar()
    checkButton1 = tk.Checkbutton(app, text='Send Keepalives', variable=CBkeepalive, onvalue=True, offvalue=False, command=updateCBvar)
    checkButton2 = tk.Checkbutton(app, text='Verbose Output', variable=CBverbose, onvalue=True, offvalue=False, command=updateCBvar)
    checkButton3 = tk.Checkbutton(app, text='CSV Output', variable=CBfilecsvoutput, onvalue=True, offvalue=False, command=updateCBvar)
    checkButton4 = tk.Checkbutton(app, text='Write rawoutput', variable=CBrawfileoutput, onvalue=True, offvalue=False, command=updateCBvar)
    checkButton5 = tk.Checkbutton(app, text='GPS Emulator', variable=CBemulategps, onvalue=True, offvalue=False, command=updateCBvar)
    radioOne.grid(column=0, row=0)
    radioTwo.grid(column=1, row=0)
    radioThree.grid(column=2, row=0)
    scanbutton.grid(column=3, row=0, rowspan=1)
    devicebutton.grid(column=3, row=1, rowspan=1)
    connectbutton.grid(column=4, row=0, rowspan=2)
    comboExample.grid(column=0, row=1, columnspan=2)
    ipadd.grid(row=1, column=2, padx=5, pady=5)
    checkButton1.grid(column=0, row=2)
    checkButton2.grid(column=1, row=2)
    checkButton3.grid(column=2, row=2)
    checkButton4.grid(column=3, row=2)
    checkButton5.grid(column=4, row=2)
    text_area.grid(row=3, column=0, pady=10, padx=10, columnspan=5)
    app.mainloop()

##### TKNTER


def main():
    global session
    session = AuxBusSession(scannerid=SCANNER_ID, xprint=xprint, starsensesave=False)
    if __mode__ == 'text':
        parser = argparse.ArgumentParser()
        parser.add_argument('connmode', help='connection mode (wifi / serial / hc)')
        parser.add_argument('port', help='connection port (ip address / COM Port)')
        args = parser.parse_args()
        session.initializeconn(args.connmode, args.port)
        session.appstartup(__version__)
        mainloop()
    if __mode__ == 'UI':
        tkinterinit()


if __name__ == '__main__':
    main()
