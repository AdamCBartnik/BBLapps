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
STORAGE_ROOT = datetime.datetime.today().strftime("/media/ssd1/%Y/%m/%Y-%m-%d")
if not os.path.isdir(STORAGE_ROOT):
    os.makedirs(STORAGE_ROOT)

### initialize the file counter
initial_fileNs = [int(name[2:]) for name in os.listdir(STORAGE_ROOT) if name[:2] == "im" and name[2:].isdigit()]
initial_fileNs.sort()
tempfilecounter = 0
tempfilereads = 0
if len(initial_fileNs) == 0:
    filecounter = 0
else:
    filecounter = initial_fileNs[-1]+1
# other file settings
rawfilename = '/tmp/ramdisk/im{}'   # followed by, eg. '1.raw'
transitfilename = "{}_t.raw".format(rawfilename.format(''))
savefilename = os.path.join(STORAGE_ROOT, "im{}")
logfilename = os.path.join(STORAGE_ROOT, "log.txt")
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

caput('EMPAD:cam1:GC_WidthMax', widthmax)
caput('EMPAD:cam1:GC_WidthMax_RBV', widthmax)
caput('EMPAD:cam1:GC_Width', widthmax)
caput('EMPAD:cam1:GC_Width_RBV', widthmax)
caput('EMPAD:cam1:GC_HeightMax', heightmax)
caput('EMPAD:cam1:GC_HeightMax_RBV', heightmax)
caput('EMPAD:cam1:GC_Height', heightmax)
caput('EMPAD:cam1:GC_Height_RBV', heightmax)
caput('EMPAD:cam1:GC_OffsetX', 0)
caput('EMPAD:cam1:GC_OffsetX_RBV', 0)
caput('EMPAD:cam1:GC_OffsetY', 0)
caput('EMPAD:cam1:GC_OffsetY_RBV', 0)

bg_data = np.zeros((1,heightmax+2, widthmax))

# Default Acquisition Parameters
n_frames = 64    # 64
Nfiles = int(math.floor(RAMSIZE/(IMSIZE*n_frames)))
exposure_time = 0.000998     #  0.000998
acquire_period = 0.0019950   #  0.0019950
trigger_sleep = 0.02

max_exposure_time = 10

caput('EMPAD:cam1:n_frames', n_frames)
caput('EMPAD:cam1:n_frames_RBV', n_frames)
caput('EMPAD:cam1:GC_ExposureTime', exposure_time)
caput('EMPAD:cam1:GC_ExposureTime_RBV', exposure_time)
caput('EMPAD:cam1:AcquirePeriod', acquire_period)
caput('EMPAD:cam1:AcquirePeriod_RBV', acquire_period)
caput('EMPAD:cam1:trigger_sleep', trigger_sleep)
cyclepump = caget('EMPAD:cam1:cyclepump')
savefile = caget('EMPAD:cam1:Save')

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
        # --------------------------------------------------------------
        # Make sure that width parameters are reasonable
        w = int(round(caget('EMPAD:cam1:GC_Width')))
        h = int(round(caget('EMPAD:cam1:GC_Height')))
        offx = int(round(caget('EMPAD:cam1:GC_OffsetX')))
        offy = int(round(caget('EMPAD:cam1:GC_OffsetY')))
            
        offx = min(max(offx, 0), widthmax - 1)
        offy = min(max(offy, 0), heightmax - 1)
        
        w = min(max(w, 1), widthmax - offx)
        h = min(max(h, 1), heightmax - offy)
            
        caput('EMPAD:cam1:GC_Width_RBV', w)
        caput('EMPAD:cam1:GC_Height_RBV', h)
        caput('EMPAD:cam1:GC_OffsetX_RBV', offx)
        caput('EMPAD:cam1:GC_OffsetY_RBV', offy)
        
        # --------------------------------------------------------------
        # Check exposure parameters
        
        exposure_time_new = np.min([max_exposure_time, np.abs(caget('EMPAD:cam1:GC_ExposureTime'))])
        max_n_frames = np.ceil(max_exposure_time / exposure_time_new)
        n_frames_new = np.min([max_n_frames, np.ceil(np.abs(caget('EMPAD:cam1:n_frames')))])
        acquire_period_new = np.abs(caget('EMPAD:cam1:AcquirePeriod'))
        
        trigger_sleep = np.abs(caget('EMPAD:cam1:trigger_sleep'))
        
        send_acquire_parameters = False
       
        savefile = int(caget("EMPAD:cam1:Save"))
 
        if (n_frames_new != n_frames):
            send_acquire_parameters = True
            n_frames = n_frames_new
            # If number of frames is changing, then remove old raw data file
            for file in glob.glob('/tmp/ramdisk/im*.raw'):
                os.remove(file)
            caput('EMPAD:cam1:n_frames', n_frames)
            caput('EMPAD:cam1:n_frames_RBV', n_frames)
            Nfiles = math.floor(RAMSIZE/(n_frames*IMSIZE)) 
            
        if (exposure_time_new != exposure_time):
            send_acquire_parameters = True
            exposure_time = exposure_time_new
            caput('EMPAD:cam1:GC_ExposureTime', exposure_time)
            caput('EMPAD:cam1:GC_ExposureTime_RBV', exposure_time)
            
        if (acquire_period_new != acquire_period):
            send_acquire_parameters = True
            acquire_period = acquire_period_new
            caput('EMPAD:cam1:AcquirePeriod', acquire_period)
            caput('EMPAD:cam1:AcquirePeriod_RBV', acquire_period)
        
        if (send_acquire_parameters):
            print('Sending set_take_n command')
            socket_connected = mysend(s, "Set_Take_N {} {} {}\n\x18".format(exposure_time, acquire_period, n_frames).encode())
            socket_connected, msg = myrecv(s)
        
        time.sleep(PAUSE_WHEN_NOT_ACQUIRING)
        acquire = (caget('EMPAD:cam1:Acquire') == 1)
	
    if savefile == 1 and socket_connected:
        logf = open(logfilename, "a")
        print(datetime.datetime.today().strftime("\n******%Y-%m-%d:%H:%M:%S******\n"), file=logf)
        print("Images start at N = {}".format(filecounter), file=logf)
        print("Tag: {}".format(caget("EMPAD:cam1:Tag")), file=logf)
        print("Frames per file: {}".format(n_frames), file=logf)
        print("Exposure: {}".format(exposure_time), file=logf)
        print("Acquire period: {}".format(acquire_period), file=logf)
        print("Trigger sleep: {}".format(trigger_sleep), file=logf)
        print("Cycle pump: {}".format(cyclepump), file=logf)
        print("Probe shutter: {}".format(caget("B29Shutter_cmd")), file=logf)
        print("Pump shutter: {}".format(caget("B29IRShutter_cmd")), file=logf)
        print("IR Delay: {}".format(caget("B29_IR_delay_cmd")), file=logf)
        print("Knife Edge H: {}".format(caget("knife_horz")), file=logf)
        print("Knife Edge V: {}".format(caget("knife_vert")), file=logf)
        print("Laser rep rate: {}".format(caget("B29LFB_rep_rate")), file=logf)
        print("Pump laser room power: {}".format(caget("B29_sample_power")), file=logf)
        print("Pump laser filter: {}".format(caget("B29_sample_ND")), file=logf)
        print("Pump laser size: {}".format(caget("SampAvgSize")), file=logf)
        print("Pump laser fluence: {}".format(caget("B29_sample_fluence")), file=logf)
        print("Probe laser pulse energy: {}".format(caget("B29LFB_target_energy")), file=logf)
        for PV in ["B29CC1H_out", "B29CC1V_out", "B29S1_out", "B29CC2H_out", "B29CC2V_out",
                   "B29S2_out", "B29Q3_out", "B29Q4_out", "B29SX1_out", "B29CC3H_out",
                   "B29CC3V_out", "B29CC4H_out", "B29CC4V_out", "B29Q5_out", "B29CC5H_out",
                   "B29CC5V_out", "B29O1_out", "B29CC6H_out", "B29CC6V_out", "B29O2_out"]:
            print("{}: {}".format(PV, caget(PV)), file=logf)

        logf.close()
    
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
        # handle file read from previous loop iteration
        if serve_file:
            if savefile == 1:
                shutil.move(transitfilename, savefilename.format(filecounter))
                filecounter = filecounter + 1
            
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

