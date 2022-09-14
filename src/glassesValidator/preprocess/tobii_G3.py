"""
Cast raw Tobii data into common format.

The output directory will contain:
    - frameTimestamps.tsv: frame number and corresponding timestamps for each frame in video
    - worldCamera.mp4: the video from the point-of-view scene camera on the glasses
    - gazeData.tsv: gaze data, where all 2D gaze coordinates are represented w/r/t the world camera,
                    and 3D coordinates in the coordinate frame of the glasses (may need rotation/translation
                    to represent in world camera's coordinate frame)
    - calibration.xml: info about the camera intrinsics and transform glasses coordinate system to
                       camera coordinate system
"""

import shutil
import pathlib
import json
import gzip
import cv2
import pandas as pd
import numpy as np
import math
import datetime

from .. import utils


def preprocessData(inputDir, outputDir):
    """
    Run all preprocessing steps on tobii data
    """
    inputDir  = pathlib.Path(inputDir)
    outputDir = pathlib.Path(outputDir)
    print(f'processing: {inputDir.name}')


    ### check and copy needed files to the output directory
    print('Check and copy raw data...')
     ### check tobii recording and get export directory
    recInfo = getRecordingInfo(inputDir)
    if recInfo is None:
        raise RuntimeError(f"The folder {inputDir} is not recognized as a Tobii Glasses 3 recording.")

    # make output dir
    recInfo.proc_directory_name = utils.make_fs_dirname(recInfo, outputDir)
    newDataDir = outputDir / recInfo.proc_directory_name
    if not newDataDir.is_dir():
        newDataDir.mkdir()

    # store rec info
    recInfo.store_as_json(newDataDir / 'recording.json')


    ### copy the raw data to the output directory
    copyTobiiRecording(inputDir, newDataDir)
    print(f'Input data copied to: {newDataDir}')

    #### prep the copied data...
    print('Getting camera calibration...')
    sceneVideoDimensions = getCameraFromJson(inputDir, newDataDir)
    print('Prepping gaze data...')
    gazeDf, frameTimestamps = formatGazeData(newDataDir, sceneVideoDimensions)

    # write the gaze data to a csv file
    gazeDf.to_csv(str(newDataDir / 'gazeData.tsv'), sep='\t', na_rep='nan', float_format="%.8f")

    # also store frame timestamps
    frameTimestamps.to_csv(str(newDataDir / 'frameTimestamps.tsv'), sep='\t')


def getRecordingInfo(inputDir):
    # returns None if not a recording directory
    recInfo = utils.Recording(source_directory=inputDir, eye_tracker=utils.Type.Tobii_Glasses_3)
    
    # get recording info
    with open(inputDir / 'recording.g3', 'rb') as j:
        rInfo = json.load(j)
    recInfo.name = rInfo['name']
    recInfo.duration = int(rInfo['duration']*1000)          # in seconds, convert to ms
    time_string = rInfo['created']
    if time_string[-1:]=='Z':
        # change Z suffix to +00:00 for ISO 8601 format that datetime understands
        time_string = time_string[:-1]+'+00:00'
    recInfo.start_time = int(datetime.datetime.fromisoformat(time_string).timestamp())

    
    # get participant info
    with open(inputDir / rInfo['meta-folder'] / 'participant', 'rb') as j:
        pInfo = json.load(j)
    recInfo.participant = pInfo['name']
    
    # get system info
    recInfo.firmware_version = (inputDir / rInfo['meta-folder'] / 'RuVersion').read_text()
    recInfo.glasses_serial = (inputDir / rInfo['meta-folder'] / 'HuSerial').read_text()
    recInfo.recording_unit_serial = (inputDir / rInfo['meta-folder'] / 'RuSerial').read_text()

    # we got a valid recording and at least some info if we got here
    # return what we've got
    return recInfo


def copyTobiiRecording(inputDir, outputDir):
    """
    Copy the relevant files from the specified input dir to the specified output dir
    """
    # Copy relevent files to new directory
    shutil.copyfile(str(inputDir / 'scenevideo.mp4'), str(outputDir / 'worldCamera.mp4'))

    # Unzip the gaze data and tslv files
    for f in ['gazedata.gz']:
        with gzip.open(str(inputDir / f)) as zipFile:
            with open(outputDir / pathlib.Path(f).stem, 'wb') as unzippedFile:
                shutil.copyfileobj(zipFile, unzippedFile)

def getCameraFromJson(inputDir, outputDir):
    """
    Read camera calibration from recording information file
    """
    with open(inputDir / 'recording.g3', 'rb') as f:
        rInfo = json.load(f)
    
    camera = rInfo['scenecamera']['camera-calibration']

    # rename some fields, ensure they are numpy arrays
    camera['focalLength'] = np.array(camera.pop('focal-length'))
    camera['principalPoint'] = np.array(camera.pop('principal-point'))
    camera['radialDistortion'] = np.array(camera.pop('radial-distortion'))
    camera['tangentialDistortion'] = np.array(camera.pop('tangential-distortion'))
    
    camera['position'] = np.array(camera['position'])
    camera['resolution'] = np.array(camera['resolution'])
    camera['rotation'] = np.array(camera['rotation'])

    # turn into camera matrix and distortion coefficients as used by OpenCV
    camera['cameraMatrix'] = np.identity(3)
    camera['cameraMatrix'][0,0] = camera['focalLength'][0]
    camera['cameraMatrix'][0,1] = camera['skew']
    camera['cameraMatrix'][1,1] = camera['focalLength'][1]
    camera['cameraMatrix'][0,2] = camera['principalPoint'][0]
    camera['cameraMatrix'][1,2] = camera['principalPoint'][1]

    camera['distCoeff'] = np.zeros(5)
    camera['distCoeff'][:2]  = camera['radialDistortion'][:2]
    camera['distCoeff'][2:4] = camera['tangentialDistortion']
    camera['distCoeff'][4]   = camera['radialDistortion'][2]


    # store to file
    fs = cv2.FileStorage(str(outputDir / 'calibration.xml'), cv2.FILE_STORAGE_WRITE)
    for key,value in camera.items():
        fs.write(name=key,val=value)
    fs.release()

    return camera['resolution']


def formatGazeData(inputDir, sceneVideoDimensions):
    """
    load gazedata json file
    format to get the gaze coordinates w/r/t world camera, and timestamps for every frame of video

    Returns:
        - formatted dataframe with cols for timestamp, frame_idx, and gaze data
        - np array of frame timestamps
    """

    # convert the json file to pandas dataframe
    df = json2df(inputDir / 'gazedata', sceneVideoDimensions)

    # read video file, create array of frame timestamps
    frameTimestamps = utils.getFrameTimestampsFromVideo(inputDir / 'worldCamera.mp4')
    
    # use the frame timestamps to assign a frame number to each data point
    frameIdx = utils.tssToFrameNumber(df.index,frameTimestamps['timestamp'].to_numpy())
    df.insert(0,'frame_idx',frameIdx['frame_idx'])

    # return the gaze data df and frame time stamps array
    return df, frameTimestamps


def json2df(jsonFile,sceneVideoDimensions):
    """
    convert the livedata.json file to a pandas dataframe
    """

    with open(jsonFile, 'r') as file:
        entries = json.loads('[' + file.read().replace('\n', ',')[:-1] + ']')

    # json no longer needed, remove
    jsonFile.unlink(missing_ok=True)


    # turn gaze data into data frame
    dfR = pd.json_normalize(entries)
    # convert timestamps from s to ms and set as index
    dfR.loc[:,'timestamp'] *= 1000.0
    dfR = dfR.set_index('timestamp')
    # drop anything thats not gaze
    dfR = dfR.drop(dfR[dfR.type != 'gaze'].index)
    # manipulate data frame to expand columns as needed
    df = pd.DataFrame([],index=dfR.index)
    expander = lambda a,n: [[math.nan]*n if not isinstance(x,list) else x for x in a]
    # monocular gaze data
    for eye in ('left','right'):
        if 'data.eye'+eye+'.gazeorigin' not in dfR.columns:
            continue    # no data at all for this eye
        which_eye = eye[:1]
        df[[which_eye + '_gaze_ori_x', which_eye + '_gaze_ori_y', which_eye + '_gaze_ori_z']] = \
            pd.DataFrame(expander(dfR['data.eye'+eye+'.gazeorigin'].tolist(),3), index=dfR.index)
        df[which_eye + '_pup_diam'] = dfR['data.eye'+eye+'.pupildiameter']
        df[[which_eye + '_gaze_dir_x', which_eye + '_gaze_dir_y', which_eye + '_gaze_dir_z']] = \
            pd.DataFrame(expander(dfR['data.eye'+eye+'.gazedirection'].tolist(),3), index=dfR.index)
    
    # binocular gaze data
    df[['3d_gaze_pos_x', '3d_gaze_pos_y', '3d_gaze_pos_z']] = pd.DataFrame(expander(dfR['data.gaze3d'].tolist(),3), index=dfR.index)
    df[['vid_gaze_pos_x', 'vid_gaze_pos_y']] = pd.DataFrame(expander(dfR['data.gaze2d'].tolist(),2), index=dfR.index)
    df.loc[:,'vid_gaze_pos_x'] *= sceneVideoDimensions[0]
    df.loc[:,'vid_gaze_pos_y'] *= sceneVideoDimensions[1]

    # return the dataframe
    return df