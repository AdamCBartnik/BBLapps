import sys, os, time, datetime, copy, socket, math, shutil, errno
import numpy as np
import numpy.matlib
import glob
from pathlib import Path
from matplotlib import pyplot as plt
from epics import caget, caput, cainfo


STATUSPV = "EMPAD:cam1:Status"
TINHIBITPV = "trigger_inhibit"
EXTRATRIGGERSLEEP = 0.01

print('****** Beginning EMPAD Python IOC ********')
caput(STATUSPV, "starting ipython ioc")

# --------------------------------------------------------
# Initialize variables

PAUSE_WHEN_NOT_ACQUIRING = 0.03
PAUSE_RETRY_TO_CONNECT = 1.0
MAX_ATTEMPTS = 3
FILE_READ_PAUSE = 0.04
# Raw frames live on the ramdisk; SSD archival + logging were removed.
rawfilename = '/tmp/ramdisk/im{}'   # followed by, eg. '1.raw'
transitfilename = "{}_t.raw".format(rawfilename.format(''))
IMSIZE = 4*130*128
RAMSIZE = 3.9e9 ## 100 MB overhead
file_t = -1
raw_t =  0
missed_triggers = 0
caught_triggers = 0
missed_triggers_in_a_row = 0
# --------------------------------------------------------
# Load default EPICS settings

# Default ROI Parameter
defaultwidthmax = 128
defaultheightmax = 128
widthmax=defaultwidthmax
heightmax=defaultheightmax

# Geometry/ROI is owned by empad_ioc.py now (it serves cam1:MinX/MinY/SizeX/
# SizeY and the read-only _RBVs), so this controller no longer initializes or
# mirrors geometry. It only sets the acquisition setpoints below.

# Default Acquisition Parameters
n_frames = 64    # 64
Nfiles = int(math.floor(RAMSIZE/(IMSIZE*n_frames)))
exposure_time = 0.000998     #  0.000998
acquire_period = 0.0019950   #  0.0019950
trigger_sleep = 0.02

max_exposure_time = 10

caput('EMPAD:cam1:n_frames', n_frames)
caput('EMPAD:cam1:AcquireTime', exposure_time)
caput('EMPAD:cam1:AcquirePeriod', acquire_period)
caput('EMPAD:cam1:trigger_sleep', trigger_sleep)

# --------------------------------------------------------
# Initializing EMPAD

print('Initializing communcation with EMPAD, loading default settings')
print('Maximum number of files in ramdisk: {}'.format(Nfiles))
print('If IOC hangs, enter password in b29')

TCP_IP = '127.0.0.1'
TCP_PORT = 41234
BUFFER_SIZE = 256 

def mysend(socket, msg):
    try:
        socket.send(msg)
    except (ConnectionResetError, BrokenPipeError) as e:
        print(e, file=sys.stderr)
        return False
    return True

def myrecv(socket):
    msg = ''
    try:
        msg = socket.recv(BUFFER_SIZE)
    except (ConnectionResetError, BrokenPipeError) as e:
        print(e, file=sys.stderr)
        return False, msg
    print(msg)
    return True, msg

socket_connected = False
connection_attempts = 0

print('****** Entering Main Loop ********')
while(True):
    
    if connection_attempts > MAX_ATTEMPTS:
        msg = "Failed to connect after {} attempts".format(max_attempts)
        print(msg, file=sys.stderr)
        exit()
    if not socket_connected:
        connection_attempts = connection_attempts + 1
        caput(STATUSPV, 'camserver connecting..')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((TCP_IP, TCP_PORT))
        #print('woo')
        socket_connected = mysend(s, "ldcmndfile /home/millenium/ioc/scripts/startupbetter.cmd\n\x18".encode())
        #print('woo1')
        socket_connected, msg = myrecv(s)   
        #print('woo2')     
        socket_connected = mysend(s, "mmpadcommand mildisp {} {} {}\n\x18".format(0, 1, 40).encode())
        #print('woo3')
        socket_connected, msg = myrecv(s)
        #print('woo4')
        socket_connected = mysend(s, "filestore 1 5\n\x18".encode())
        #print('woo5')
        socket_connected, msg = myrecv(s)
        #print('woo6')
        ### check that triggers are suppresed before set_take_n
        caput(STATUSPV, 'camserver done..')
        caput(TINHIBITPV, 0)
        time.sleep(trigger_sleep)
        caput(TINHIBITPV, 1)
        time.sleep(trigger_sleep)
        print('Sending set_take_n command')
        socket_connected = mysend(s, "Set_Take_N {} {} {}\n\x18".format(exposure_time, acquire_period, n_frames).encode())
        socket_connected, msg = myrecv(s)
        socket_connected, msg = myrecv(s)
    if socket_connected:
        caput(STATUSPV, 'running ioc')
        connection_attempts = 0
    else:
        time.sleep(PAUSE_RETRY_TO_CONNECT)
    acquire = False   # always enter loop at least once
    while ((not acquire) and socket_connected):	 
        # ROI/geometry is owned by empad_ioc.py now (cam1:MinX/SizeX/...); this
        # controller no longer reads or mirrors it.
        # --------------------------------------------------------------
        # Check exposure parameters
        
        exposure_time_new = np.min([max_exposure_time, np.abs(caget('EMPAD:cam1:AcquireTime'))])
        max_n_frames = np.ceil(max_exposure_time / exposure_time_new)
        n_frames_new = np.min([max_n_frames, np.ceil(np.abs(caget('EMPAD:cam1:n_frames')))])
        acquire_period_new = np.abs(caget('EMPAD:cam1:AcquirePeriod'))

        trigger_sleep = np.abs(caget('EMPAD:cam1:trigger_sleep'))

        send_acquire_parameters = False

        if (n_frames_new != n_frames):
            send_acquire_parameters = True
            n_frames = n_frames_new
            # If number of frames is changing, then remove old raw data file
            for file in glob.glob('/tmp/ramdisk/im*.raw'):
                os.remove(file)
            caput('EMPAD:cam1:n_frames', n_frames)
            Nfiles = math.floor(RAMSIZE/(n_frames*IMSIZE))

        if (exposure_time_new != exposure_time):
            send_acquire_parameters = True
            exposure_time = exposure_time_new
            caput('EMPAD:cam1:AcquireTime', exposure_time)

        if (acquire_period_new != acquire_period):
            send_acquire_parameters = True
            acquire_period = acquire_period_new
            caput('EMPAD:cam1:AcquirePeriod', acquire_period)
        
        if (send_acquire_parameters):
            print('Sending set_take_n command')
            socket_connected = mysend(s, "Set_Take_N {} {} {}\n\x18".format(exposure_time, acquire_period, n_frames).encode())
            socket_connected, msg = myrecv(s)
        
        time.sleep(PAUSE_WHEN_NOT_ACQUIRING)
        acquire = (caget('EMPAD:cam1:Acquire') == 1)
	
    # (run saving to SSD + the B29 beamline-PV provenance log were removed —
    # never used.)
    
    caput(TINHIBITPV, 0) ### ENTER THE ACQUIRE LOOP WITH TRIGGER INHIBITED
    time.sleep(trigger_sleep)
    caput(TINHIBITPV, 1) 
    time.sleep(trigger_sleep)
    
    fullfilename = "{}_x{}.raw".format(rawfilename.format(''), int(n_frames))
    with open(fullfilename, "wb") as f:
        pass
    raw_t = os.stat(fullfilename).st_mtime
    serve_file = False
    EMPAD_exposing = False

    min_acquisition_time = 0.10
    expected_acquisition_time = np.max([(acquire_period*n_frames) + FILE_READ_PAUSE, min_acquisition_time])
    while(acquire and socket_connected):
        # --------------------------------------------------------------
        # Send exposure command
        if not EMPAD_exposing:
       	    print('Sending exposure command') 
            socket_connected = mysend(s, "Exposure {}\n\x18".format(rawfilename.format('')).encode())
            socket_connected, msg = myrecv(s)
        #print("press any key to continue")
        #input()       
        caput(TINHIBITPV, 0)
        time.sleep(trigger_sleep)
        caput(TINHIBITPV, 1)
        start_time = time.time()
        # --------------------------------------------------------------
        # confirm file write
        if not EMPAD_exposing: ### listen for 5OK
            socket_connected, msg = myrecv(s)
            response_time = time.time()-start_time
            extra_time = np.max([expected_acquisition_time - response_time, 0])
            #print(extra_time)
            time.sleep(extra_time)
        else:
            time.sleep(expected_acquisition_time)

        print("looking for {} after {} seconds".format(fullfilename, time.time()-start_time))
        serve_file = False
        read_attempts = 0
        while((not serve_file) and (read_attempts < 10)):
            file_t = os.stat(fullfilename).st_mtime
            if file_t > raw_t:
                shutil.copy(fullfilename, transitfilename) ### to avoid race condition with camserver
                serve_file = True
                EMPAD_exposing = False
                raw_t = file_t
                print("found file after {} failed attempts".format(read_attempts))
                caught_triggers = caught_triggers + 1
                missed_triggers_in_a_row = 0
            else:
                time.sleep(FILE_READ_PAUSE)
                read_attempts = read_attempts + 1
        if not serve_file:
            print("Missed trigger")
            missed_triggers = missed_triggers + 1
            missed_triggers_in_a_row = missed_triggers_in_a_row + 1
            EMPAD_exposing = True
        if (missed_triggers_in_a_row > 10):
            sys.exit(0)
        # --------------------------------------------------------------
        # Check if user has cancelled Acquire

        print("total time: {}".format(time.time()-start_time))
        acquire = caget('EMPAD:cam1:Acquire') == 1

