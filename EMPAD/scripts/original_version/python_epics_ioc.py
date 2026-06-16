import sys, os, time, datetime, copy, socket, math, shutil, errno
import numpy as np
import numpy.matlib
import glob
from pathlib import Path
from matplotlib import pyplot as plt
from epics import caget, caput, cainfo

FILE_READ_PAUSE = 0.01
rawfiledir = '/tmp/ramdisk/'
rawfilename = rawfiledir + 'im{}'   # followed by, eg. '1.raw'
tempfilename = rawfiledir + 'temp'
print('****** Beginning EMPAD Python to EPICS IOC ********')

# --------------------------------------------------------
# Initialize variables
widthmax = 128
heightmax = 128
bg_data = np.zeros((heightmax+2, widthmax))

# Default Acquisition Parameters
n_frames = int(round(caget('EMPAD:cam1:n_frames')))

old_file_t = os.stat(rawfiledir).st_mtime - 1   # use the directory creation time as the default

print('****** Entering Main Loop ********')
while(True):
    # Make sure that width parameters are reasonable
    w = int(round(caget('EMPAD:cam1:GC_Width')))
    h = int(round(caget('EMPAD:cam1:GC_Height')))
    wm = int(round(caget('EMPAD:cam1:GC_WidthMax')))
    hm = int(round(caget('EMPAD:cam1:GC_HeightMax')))
    offx = int(round(caget('EMPAD:cam1:GC_OffsetX')))
    offy = int(round(caget('EMPAD:cam1:GC_OffsetY')))
        
    offx = min(max(offx, 0), wm - 1)
    offy = min(max(offy, 0), hm - 1)

    w = min(max(w, 1), wm - offx)
    h = min(max(h, 1), hm - offy)
        
    caput('EMPAD:cam1:GC_Width_RBV', w)
    caput('EMPAD:cam1:GC_Height_RBV', h)
    caput('EMPAD:cam1:GC_OffsetX_RBV', offx)
    caput('EMPAD:cam1:GC_OffsetY_RBV', offy)

    # Get other EPICS values
    n_frames = int(round(caget('EMPAD:cam1:n_frames')))
    cyclepump = int(caget('EMPAD:cam1:cyclepump'))
    threshold_enable = caget('EMPAD:cam1:hw_threshold_enable') == 1
    threshold = caget('EMPAD:cam1:hw_threshold')
    save_bg = caget('EMPAD:cam1:hw_save_bg') == 1
    subtract_bg = caget('EMPAD:cam1:hw_subtract_bg') == 1

    # Check file time
    fullfilename = "{}_x{}.raw".format(rawfilename.format(''), int(n_frames))
    if (os.path.exists(fullfilename)):
        file_t = os.stat(fullfilename).st_mtime
    else:
        file_t = old_file_t

	# Read file if it's new
    if (file_t > old_file_t):
        old_file_t = file_t
        shutil.copy(fullfilename, tempfilename) # to avoid race condition with camserver
        data = np.fromfile(tempfilename, dtype=np.float32)
	    
        if (len(data) == (heightmax+2)*widthmax*n_frames):
            if (n_frames%2 == 0) and cyclepump == 1:
                nlaserstates = 2
            else:
                nlaserstates = 1
            raw_data = data.reshape(int(n_frames/nlaserstates), nlaserstates, heightmax+2, widthmax) #4D array
            frame_count = raw_data[0,0,-1,1]
            parity = int(frame_count%2)
            
            data = copy.copy(raw_data)
            if subtract_bg:
                bg_repmat = bg_data[np.newaxis,np.newaxis,:,:] #expand bg_data from 2D to 4D
                bg_repmat = np.tile(bg_repmat, (int(n_frames/nlaserstates), nlaserstates, 1, 1))
                data = data - bg_repmat
            if (threshold > 0 and threshold_enable):
                data[data < threshold] = 0.0
            if save_bg:
                caput('EMPAD:cam1:hw_save_bg', 0)
                bg_data = np.mean(np.mean(raw_data, axis=0), axis=0) #2D array
                print('Saving BG...')
            data = np.transpose(data[:,:,:-2, :], [0,1,3,2])
            dim0, dim1, dim2, dim3 = data.shape
            if cyclepump == 2 and int(dim0%8)==0:
                #-------------------------
                # Put table of images into epics  #   data size = [n_frames, 1, 128, 128]  
                dim5 = int(dim0/8)   #  want table to have 8 rows (?)
                data = data.reshape(8,dim5,dim2, dim3)
                data = np.block([[data[i, j, :, :] for j in range(0,dim5)] for i in range(0,8)])
                caput('EMPAD:image1:ArrayData', data.flatten('F').astype(int))
                newheight = 8*dim2
                newwidth = dim5*dim3
                caput('EMPAD:cam1:GC_WidthMax', newwidth)
                caput('EMPAD:cam1:GC_WidthMax_RBV', newwidth)
                caput('EMPAD:cam1:GC_HeightMax', newheight)
                caput('EMPAD:cam1:GC_HeightMax_RBV', newheight)
            else:
                #-----------------------------
                # Put sum of images into epics    #   data size = [n_frames, 2, 128, 128]  (?)
                data = np.sum(data, axis=0) #3D array
                # --------------------------------------------------------------
                # Truncate data to correct ROI
                data = data[:, offx:offx+w, offy:offy+h]

                #  Put data in EPICS
                if nlaserstates == 2:
                    caput('EMPAD:image1:ArrayData_hot', data[parity].flatten('F').astype(int))
                    caput('EMPAD:image1:ArrayData', data[1-parity].flatten('F').astype(int))
                else:
                    caput('EMPAD:image1:ArrayData', np.sum(data, axis=0).flatten('F').astype(int))
                
                caput('EMPAD:cam1:GC_WidthMax', widthmax)
                caput('EMPAD:cam1:GC_WidthMax_RBV', widthmax)
                caput('EMPAD:cam1:GC_HeightMax', heightmax)
                caput('EMPAD:cam1:GC_HeightMax_RBV', heightmax)
    else:
        time.sleep(FILE_READ_PAUSE)

	
	


