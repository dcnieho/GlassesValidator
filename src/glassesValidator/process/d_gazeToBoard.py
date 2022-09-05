#!/usr/bin/python3

from pathlib import Path

import cv2
import numpy as np
import csv
import time
from .. import config
from .. import utils


def process(inputDir,configDir=None,showVisualization=False,showReference=True,showOnlyIntervals=True,FPSFac=1):
    # if showVisualization, draw each frame + gaze and overlay info about detected markers and board
    # if showReference, gaze in board space is also drawn in a separate window
    # if showOnlyIntervals, shows only frames in the marker intervals (if available)
    inputDir  = Path(inputDir)
    if configDir is not None:
        configDir = pathlib.Path(configDir)

    print('processing: {}'.format(inputDir.name))
    
    # open file with information about Aruco marker and Gaze target locations
    validationSetup = config.getValidationSetup(configDir)

    if showVisualization:
        cv2.namedWindow("frame")
        if showReference:
            cv2.namedWindow("reference")

        reference   = utils.Reference(configDir, validationSetup)
        centerTarget= reference.targets[validationSetup['centerTarget']].center
        i2t         = utils.Idx2Timestamp(inputDir / 'frameTimestamps.tsv')
    
    # get camera calibration info
    cameraMatrix,distCoeff,cameraRotation,cameraPosition = utils.readCameraCalibrationFile(inputDir / "calibration.xml")
    hasCameraMatrix = cameraMatrix is not None
    hasDistCoeff    = distCoeff is not None

    # get interval coded to be analyzed, if any
    analyzeFrames   = utils.readMarkerIntervalsFile(inputDir / "markerInterval.tsv")
    hasAnalyzeFrames= showOnlyIntervals and analyzeFrames is not None

    # open video, if wanted
    if showVisualization:
        cap         = cv2.VideoCapture(str(inputDir / 'worldCamera.mp4'))
        width       = float(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height      = float(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        ifi         = 1000./cap.get(cv2.CAP_PROP_FPS)/FPSFac

    # Read gaze data
    print('  gazeData')
    gazes,maxFrameIdx = utils.Gaze.readDataFromFile(inputDir / 'gazeData.tsv')

    # Read pose of marker board
    print('  boardPose')
    poses = utils.BoardPose.readDataFromFile(inputDir / 'boardPose.tsv')

    csv_file = open(inputDir / 'gazeWorldPos.tsv', 'w', newline='')
    csv_writer = csv.writer(csv_file, delimiter='\t')
    header = ['frame_idx']
    header.extend(utils.GazeWorld.getWriteHeader())
    csv_writer.writerow(header)
    
    subPixelFac = 8   # for sub-pixel positioning
    stopAllProcessing = False
    for frame_idx in range(maxFrameIdx+1):
        startTime = time.perf_counter()
        if showVisualization:
            ret, frame = cap.read()
            if (not ret) or (hasAnalyzeFrames and frame_idx > analyzeFrames[-1]):
                # done
                break

            if hasAnalyzeFrames:
                # check we're in a current interval, else skip processing
                # NB: have to spool through like this, setting specific frame to read
                # with cap.get(cv2.CAP_PROP_POS_FRAMES) doesn't seem to work reliably
                # for VFR video files
                inIval = False
                for f in range(0,len(analyzeFrames),2):
                    if frame_idx>=analyzeFrames[f] and frame_idx<=analyzeFrames[f+1]:
                        inIval = True
                        break
                if not inIval:
                    # no need to show this frame
                    continue
            if showReference:
                refImg = reference.getImgCopy()
            

        if frame_idx in gazes:
            for gaze in gazes[frame_idx]:

                # draw gaze point on scene video
                if showVisualization:
                    gaze.draw(frame, subPixelFac=subPixelFac, camRot=cameraRotation, camPos=cameraPosition, cameraMatrix=cameraMatrix, distCoeff=distCoeff)
                
                # if we have pose information, figure out where gaze vectors
                # intersect with reference board. Do same for 3D gaze point
                # (the projection of which coincides with 2D gaze provided by
                # the eye tracker)
                # store positions on marker board plane in camera coordinate frame to
                # file, along with gaze vector origins in same coordinate frame
                writeData = [frame_idx]
                if frame_idx in poses:
                    gazeWorld = utils.gazeToPlane(gaze,poses[frame_idx],cameraRotation,cameraPosition, cameraMatrix, distCoeff)
                    
                    # draw gazes on video and reference image
                    if showVisualization:
                        gazeWorld.drawOnWorldVideo(frame, cameraMatrix, distCoeff, subPixelFac)
                        if showReference:
                            gazeWorld.drawOnReferencePlane(refImg, reference, subPixelFac)

                    # store gaze-on-plane to csv
                    writeData.extend(gazeWorld.getWriteData())
                    csv_writer.writerow( writeData )

        if showVisualization:
            if showReference:
                cv2.imshow("reference", refImg)

            # if we have board pose, draw board origin on video
            if frame_idx in poses:
                if poses[frame_idx] is not None and hasCameraMatrix and hasDistCoeff:
                    a = cv2.projectPoints(np.zeros((1,3)),poses[frame_idx].rVec,poses[frame_idx].tVec,cameraMatrix,distCoeff)[0].flatten()
                else:
                    iH = np.linalg.inv(poses[frame_idx].hMat)
                    a = utils.applyHomography(iH, centerTarget[0], centerTarget[1])
                    if hasCameraMatrix and hasDistCoeff:
                        a = utils.distortPoint(*a, cameraMatrix, distCoeff)
                utils.drawOpenCVCircle(frame, a, 3, (0,255,0), -1, subPixelFac)
                utils.drawOpenCVLine(frame, (a[0],0), (a[0],height), (0,255,0), 1, subPixelFac)
                utils.drawOpenCVLine(frame, (0,a[1]), (width,a[1]) , (0,255,0), 1, subPixelFac)
                
            frame_ts  = i2t.get(frame_idx)
            cv2.rectangle(frame,(0,int(height)),(int(0.25*width),int(height)-30),(0,0,0),-1)
            cv2.putText(frame, '%8.2f [%6d]' % (frame_ts,frame_idx), (0, int(height)-5), cv2.FONT_HERSHEY_PLAIN, 2, (0,255,255))
            cv2.imshow('frame',frame)
            key = cv2.waitKey(max(1,int(round(ifi-(time.perf_counter()-startTime)*1000)))) & 0xFF
            if key == ord('q'):
                # quit fully
                stopAllProcessing = True
                break
            if key == ord('n'):
                # goto next
                break
            if key == ord('s'):
                # screenshot
                cv2.imwrite(str(inputDir / ('calc_frame_%d.png' % frame_idx)), frame)
        elif (frame_idx)%100==0:
            print('  frame {}'.format(frame_idx))

    csv_file.close()
    if showVisualization:
        cap.release()
        cv2.destroyAllWindows()

    return stopAllProcessing



if __name__ == '__main__':
    basePath = Path(__file__).resolve().parent
    for d in (basePath / 'data' / 'preprocced').iterdir():
        if d.is_dir():
            if process(d,basePath):
                break
