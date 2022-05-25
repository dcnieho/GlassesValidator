#!/usr/bin/python

import sys
from pathlib import Path
import math

import cv2
import numpy as np
import pandas as pd
import warnings

import utils
import I2MC

import matplotlib.pyplot as plt
import matplotlib.image as mpimg



def process(inputDir,basePath):
    print('processing: {}'.format(inputDir.name))
    
    configDir = basePath / "config"
    # open file with information about Aruco marker and Gaze target locations
    validationSetup = utils.getValidationSetup(configDir)

    # get interval coded to be analyzed
    analyzeFrames = utils.readMarkerIntervalsFile(inputDir / "markerInterval.tsv")
    if analyzeFrames is None:
        print('  no marker intervals defined for this recording, skipping')
        return

    # Read gaze on board data
    gazeWorld = utils.GazeWorld.readDataFromFile(inputDir / 'gazeWorldPos.tsv',analyzeFrames[0],analyzeFrames[-1],True)

    # get info about markers on our board
    reference = utils.Reference(configDir, validationSetup, imHeight=-1)
    targets   = {ID: reference.targets[ID].center for ID in reference.targets}   # get centers of targets
    markerHalfSizeMm = reference.markerSize/2.
    
    # run I2MC on data in board space
    # set I2MC options
    opt = {'xres': None, 'yres': None}  # dummy values for required options
    opt['missingx']         = math.nan
    opt['missingy']         = math.nan
    opt['maxdisp']          = 50        # mm
    opt['windowtimeInterp'] = .25       # s
    opt['maxMergeDist']     = 20        # mm
    opt['maxMergeTime']     = 81        # ms
    opt['minFixDur']        = 50        # ms
    # decide what sampling frequency to tell I2MC about. It doesn't work with varying sampling frequency, nor
    # any random sampling frequency. For our purposes, getting it right is not important (internally I2MC only
    # uses sampling frequency for converting some of the time units to samples, other things are taken directly
    # from the time signal. So, we have working I2MC settings for a few sampling frequencies, and just choose
    # the nearest based on empirically determined sampling frequency.
    ts          = np.array([s.ts for v in gazeWorld.values() for s in v])
    recFreq     = np.round(np.mean(1000./np.diff(ts)))    # Hz
    knownFreqs  = [30., 50., 60., 90., 120.]
    opt['freq'] = knownFreqs[np.abs(knownFreqs - recFreq).argmin()]
    if opt['freq']==120.:
        opt['downsamples']      = [2, 3, 5]
        opt['chebyOrder']       = 7
    elif opt['freq'] in [50., 60.]:
        opt['downsamples']      = [2, 5]
        opt['downsampFilter']   = False
    else:
        # 90 Hz, 30 Hz
        opt['downsamples']      = [2, 3]
        opt['downsampFilter']   = False

    # collect data
    qHasLeft        = np.any(np.logical_not(np.isnan([s.lGaze2D          for v in gazeWorld.values() for s in v])))
    qHasRight       = np.any(np.logical_not(np.isnan([s.rGaze2D          for v in gazeWorld.values() for s in v])))
    qHasRay         = np.any(np.logical_not(np.isnan([s.gaze2DRay        for v in gazeWorld.values() for s in v])))
    qHasHomography  = np.any(np.logical_not(np.isnan([s.gaze2DHomography for v in gazeWorld.values() for s in v])))
    for ival in range(0,len(analyzeFrames)//2):
        gazeWorldToAnal = {k:v for (k,v) in gazeWorld.items() if k>=analyzeFrames[ival*2] and k<=analyzeFrames[ival*2+1]}
        data = {}
        data['time'] = np.array([s.ts for v in gazeWorldToAnal.values() for s in v])
        if qHasLeft and qHasRight:
            data['L_X']  = np.array([s.lGaze2D[0] for v in gazeWorldToAnal.values() for s in v])
            data['L_Y']  = np.array([s.lGaze2D[1] for v in gazeWorldToAnal.values() for s in v])
            data['R_X']  = np.array([s.rGaze2D[0] for v in gazeWorldToAnal.values() for s in v])
            data['R_Y']  = np.array([s.rGaze2D[1] for v in gazeWorldToAnal.values() for s in v])
        elif qHasRay:
            data['average_X']  = np.array([s.gaze2DRay[0] for v in gazeWorldToAnal.values() for s in v])
            data['average_Y']  = np.array([s.gaze2DRay[1] for v in gazeWorldToAnal.values() for s in v])
        elif qHasHomography:
            data['average_X']  = np.array([s.gaze2DHomography[0] for v in gazeWorldToAnal.values() for s in v])
            data['average_Y']  = np.array([s.gaze2DHomography[1] for v in gazeWorldToAnal.values() for s in v])
        else:
            raise RuntimeError('No data available to process')
        
        # run event classification to find fixations
        fix,dat,par = I2MC.I2MC(data,opt,False)

        # for each target, find closest fixation
        minDur      = 150       # ms
        used        = np.zeros((fix['start'].size),dtype='bool')
        selected    = np.empty((len(targets),),dtype='int')
        selected[:] = -999

        for i,t in zip(range(len(targets)),targets):
            if np.all(used):
                # all fixations used up, can't assign anything to remaining targets
                continue
            # select fixation
            dist                    = np.hypot(fix['xpos']-targets[t][0], fix['ypos']-targets[t][1])
            dist[used]              = math.inf  # make sure fixations already bound to a target are not used again
            dist[fix['dur']<minDur] = math.inf  # make sure that fixations that are too short are not selected
            iFix        = np.argmin(dist)
            selected[i] = iFix
            used[iFix]  = True

        # make plot of data overlaid on board, and show for each target which fixation
        # was selected
        f       = plt.figure(dpi=300)
        imgplot = plt.imshow(reference.getImgCopy(asRGB=True),extent=(np.array(reference.bbox)[[0,2,3,1]]),alpha=.5)
        plt.plot(fix['xpos'],fix['ypos'],'b-')
        plt.plot(fix['xpos'],fix['ypos'],'go')
        plt.xlim([reference.bbox[0]-markerHalfSizeMm, reference.bbox[2]+markerHalfSizeMm])
        plt.ylim([reference.bbox[3]-markerHalfSizeMm, reference.bbox[1]+markerHalfSizeMm])
        for i,t in zip(range(len(targets)),targets):
            if selected[i]==-999:
                continue
            plt.plot([fix['xpos'][selected[i]], targets[t][0]], [fix['ypos'][selected[i]], targets[t][1]],'r-')
       
        plt.xlabel('mm')
        plt.ylabel('mm')

        f.savefig(str(inputDir / 'targetSelection_I2MC_interval_{}.png'.format(ival)))
        plt.close(f)

        # store selected intervals
        df = pd.DataFrame()
        df.index.name = 'target'
        for i,t in zip(range(len(targets)),targets):
            if selected[i]==-999:
                continue
            df.loc[t,'marker_interval'] = ival+1
            df.loc[t,'start_timestamp'] = fix['startT'][selected[i]]
            df.loc[t,  'end_timestamp'] = fix[  'endT'][selected[i]]
        
        df.to_csv(str(inputDir / 'analysisInterval.tsv'), mode='w' if ival==0 else 'a', header=ival==0, sep='\t', na_rep='nan', float_format="%.3f")

if __name__ == '__main__':
    basePath = Path(__file__).resolve().parent
    for d in (basePath / 'data' / 'preprocced').iterdir():
        if d.is_dir():
            process(d,basePath)
