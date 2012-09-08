#!/usr/bin/env python

# standard Python imports
import argparse
import time
import os.path
import math
import fnmatch
import threading
import tempfile
import datetime

import json
import yaml
import numpy as np
import numpy.linalg
import scipy.ndimage
import scipy.misc
import cv,cv2
import pprint
import random

# ROS imports
import roslib;
roslib.load_manifest('vros_display')
roslib.load_manifest('camera_trigger')
roslib.load_manifest('flycave')
roslib.load_manifest('std_srvs')
roslib.load_manifest('std_msgs')
roslib.load_manifest('motmot_ros_utils')
roslib.load_manifest('rosbag')
import rospy
import rosbag

# local vros_display imports
import display_client
import camera_trigger.srv
import std_srvs.srv
import flycave.srv
import vros_display.srv

import calib
import calib.imgproc
from calib.acquire import CameraHandler, SimultainousCameraRunner, SequentialCameraRunner
from calib.io import MultiCalSelfCam, AllPointPickle
from calib.imgproc import DotBGFeatureDetector, load_mask_image, add_crosshairs_to_nparr
from calib.sampling import gen_horiz_snake, gen_vert_snake
from rosutils.io import decode_url

import flydra.reconstruct

from vros_display.msg import Calib2DPoint, CalibMapping
from geometry_msgs.msg import Point32
from std_msgs.msg import UInt32, String

CHAN_R = 2
CHAN_G = 1
CHAN_B = 0
CALIBMAPPING_TOPIC = '/calibration/mapping'

def get_centre_of_vdisp(vdmask):
    """ returns col,row """
    rowm,colm,_ = scipy.ndimage.center_of_mass(vdmask)
    return colm,rowm

def generate_sampling_pixel_coords_5(vdmask,pts,space,img=None):
    colm,rowm = get_centre_of_vdisp(vdmask)

    m = np.array((colm,rowm))
    quadrants={(True,True):[],(True,False):[],(False,False):[],(False,True):[]}
    
    corners = [(colm,rowm)]

    if img != None:
        calib.imgproc.add_crosshairs_to_nparr(img, row=rowm, col=colm, chan=CHAN_R, sz=4)

    for pt in pts:
        col,row = pt
        p = np.array(pt)

        if img != None:
            calib.imgproc.add_crosshairs_to_nparr(img, row=row, col=col, chan=CHAN_R, sz=2)
        
        quad = m > p
        dist = numpy.linalg.norm(m-p)
        quadrants[tuple(quad)].append(tuple((dist,p)))
    
    for quad,ptdist in quadrants.items():
        dist,farpoint = sorted(ptdist, key=lambda ptd: ptd[0], reverse=True)[0]

        half = (m + farpoint) / 2
        c,r = (half + farpoint) / 2
        
        if img != None:
            calib.imgproc.add_crosshairs_to_nparr(img, row=r, col=c, chan=CHAN_R, sz=4)

        corners.append( (c,r) )
    
    return corners

def generate_sampling_pixel_coords(vdmask,pts,space,img=None):

    ncol = vdmask.shape[1]
    nrow = vdmask.shape[0]

    if img != None:
        #debugging....
        rowm,colm,_ = scipy.ndimage.center_of_mass(vdmask)
        calib.imgproc.add_crosshairs_to_nparr(img, row=rowm, col=colm, chan=0, sz=4)
        for pt in pts:
            col,row = pt
            p = np.array(pt)
            calib.imgproc.add_crosshairs_to_nparr(img, row=row, col=col, chan=1, sz=4)


    valid = []
    for col,row in gen_horiz_snake(w=ncol,h=nrow,sw=space,sh=space):
        if vdmask[row,col]:
            valid.append( (col,row) )

    for col,row in valid:
        if img != None:
            calib.imgproc.add_crosshairs_to_nparr(img, row=row, col=col, chan=2, sz=2)

    return valid

class DataIO:

    CALIBMAPPING_TOPIC = '/calibration/mapping'

    def __init__(self, directory):
        self.outdir = directory
        if not os.path.isdir(self.outdir):
            raise Exception("Dir %s does not exist" % self.outdir)

        self.num_points = 0
        
        self._pub_num_pts = rospy.Publisher('~num_points', UInt32)
        self._pub_mapping = rospy.Publisher('~mapping', CalibMapping)
        
        fn = "CALIB"+datetime.datetime.now().strftime("%Y%m%d_%H%M%S")+".bag"
        self._dest = os.path.join(self.outdir,fn)
        self._bag = rosbag.Bag(self._dest, 'w')
        rospy.loginfo("Saving to %s" % self._dest)
        
    def close(self):
        self._bag.close()
        rospy.loginfo("Saved to %s" % self._dest)

    def load(self, name, vis_callback_2d=None):
        with rosbag.Bag(name, 'r') as bag:
            for topic, msg, t in bag.read_messages(topics=[CALIBMAPPING_TOPIC]):
                self._add_mapping(msg)
                if vis_callback_2d:
                    vis_callback_2d(ds=msg.display_server, 
                                    col=msg.pixel_projector.x,
                                    row=msg.pixel_projector.y,
                                    pan=msg.pan,
                                    tilt=msg.tilt)

    def _add_mapping(self, c):
        self._bag.write(self.CALIBMAPPING_TOPIC,c)
        self.num_points += 1
        self._pub_mapping.publish(c)
        self._pub_num_pts.publish(self.num_points)

    def add_mapping(self, **kwargs):
        c = CalibMapping()
        c.points = [Calib2DPoint(camera=pt[0],
                                         pixel=Point32(x=pt[1][0],y=pt[1][1])) \
                            for pt in kwargs["points"]]
        c.display_server = kwargs["display_server"]
        c.vdisp = kwargs["vdisp"]
        c.position = Point32(*kwargs["position"])
        c.pan = kwargs["pan"]
        c.tilt= kwargs["tilt"]
        c.pixel_projector = Point32(*kwargs["pixel_projector"])
        c.pixel_ptc_laser = Point32(*kwargs["pixel_ptc_laser"])
        c.pixel_ptc_projector = Point32(*kwargs["pixel_ptc_projector"])
        
        self._add_mapping(c)

show_laser_scatter = True
laser_handle = "pantilt"

class Calib:

    MODE_SLEEP = "sleep"
    MODE_MANUAL_TRACKING = "manual_tracking"
    MODE_MANUAL_PROJECTOR = "manual_projector"
    MODE_MANUAL_CLICKED = "manual_clicked"
    MODE_DISPLAY_SERVER = "display_server"
    MODE_DISPLAY_SERVER_STOP = "display_server_stop"
    MODE_DISPLAY_SERVER_VDISP = "display_server_vdisp"
    MODE_DISPLAY_SERVER_HOME = "display_server+home"
    MODE_DISPLAY_SERVER_LASER = "display_server+laser"
    MODE_DISPLAY_SERVER_PROJECTOR = "display_server+projector"
    MODE_RESTORE = "restore"
    MODE_SET_BACKGROUND = "set_background"
    MODE_CLEAR_BACKGROUND = "clear_background"
    MODE_FINISHED = "finish"

    def __init__(self, config, show_cameras, show_display_servers, show_type, outdir, continue_calibration, debug):
        tracking_cameras = config["tracking_cameras"]
        laser_camera = config["laser_camera"]
        trigger = config["trigger"]
        laser = config["laser"]
        self.projector_sleep = config["projector_sleep"]
        self.display_servers = config["display_servers"]

        #FIXME: make __getattr__ look into config for locals starting with cfg_
        self.mask_dir = decode_url(config["mask_dir"])
        self.ptsize = int(config["projector_point_size_px"])
        self.laser_range_pan = config["laser_range_pan"]
        self.laser_range_tilt = config["laser_range_tilt"]
        self.laser_expected_detect_location = config["laser_expected_detect_location"]
        self.laser_expected_detect_hist = config["laser_expected_detect_hist"]
        self.visible_thresh = int(config["bg_thresh_visible"])
        self.laser_thresh = int(config["bg_thresh_laser"])
        self.laser_search_size = config["laser_search_size"]
        self.laser_need_n_points = config["laser_need_n_points"]
        
        self.flydra = flydra.reconstruct.Reconstructor(
                        cal_source=decode_url(config["tracking_calibration"]))

        self.data = DataIO(outdir)

        rospy.wait_for_service(trigger+'/set_framerate')
        self.trigger_proxy_rate = rospy.ServiceProxy(trigger+'/set_framerate', camera_trigger.srv.SetFramerate)
        self.trigger_proxy_once = rospy.ServiceProxy(trigger+'/trigger_once', std_srvs.srv.Empty)
        self.trigger_proxy_rate(0.0)

        self.laser_proxy_power = rospy.ServiceProxy(laser+'/set_power', flycave.srv.SetPower)
        self.laser_proxy_pan = rospy.ServiceProxy(laser+'/set_pan', flycave.srv.SetFloat)
        self.laser_proxy_tilt = rospy.ServiceProxy(laser+'/set_tilt', flycave.srv.SetFloat)
        self.laser_proxy_brightness = rospy.ServiceProxy(laser+'/set_brightness', flycave.srv.SetFloat)

        rospy.wait_for_service(laser+'/set_power')
        rospy.wait_for_service(laser+'/set_brightness')
        rospy.wait_for_service(laser+'/set_pan')
        rospy.wait_for_service(laser+'/set_tilt')

        #move laser home
        self._laser_currpan, self._laser_currtilt = config["laser_home"]
        self.laser_proxy_brightness(config["laser_brightness"])
        self.laser_proxy_power(False)
        self.laser_proxy_pan(self._laser_currpan)
        self.laser_proxy_tilt(self._laser_currtilt)

        self.pub_mode = rospy.Publisher('~mode', String)
        
        self.show_cameras = show_cameras
        self.show_display_servers = {}
        if show_cameras or show_display_servers or show_laser_scatter:
            cv2.startWindowThread()

        self.results = {}           #display_server : {vdisp : [(u,v,x,y,z),(u,v,x,y,z),...]}
        self.num_results = 0
        
        self._laser_handles = {}
        if show_laser_scatter:
            handle = laser_handle
            cv2.namedWindow(handle)
            sizepan = self.laser_range_pan[1] - self.laser_range_pan[0]
            sizetilt = self.laser_range_tilt[1] - self.laser_range_tilt[0]
            self._laser_handles[handle] = np.zeros((sizetilt+1,sizepan+1,3),dtype=np.uint8)
            cv2.imshow(handle, self._laser_handles[handle])

#        display_client.DisplayServerProxy.set_stimulus_mode("Stimulus2DBlit")

        self._light_proj_cache = tuple()
        self._click_queue = {} #display_server:[(col, row), ...]
        for d in self.display_servers:
            dsc = display_client.DisplayServerProxy(d,wait=True)
            dsc.enter_2dblit_mode()

            self.display_servers[d]["vdmask"] = {}
            for vdisp in self.display_servers[d]["virtualDisplays"]:
                vdispname = vdisp["id"]
                self.results[d] = dict(vdispname=list())
                self.display_servers[d]["vdmask"][vdispname] = dsc.get_virtual_display_mask(vdisp)

            self.display_servers[d]["display_client"] = dsc
            rospy.loginfo("Calibrating %s" % d)

            if d in show_display_servers:
                cv2.namedWindow(d)
                self._click_queue[d] = []
                cv2.setMouseCallback(d, self._display_server_window_click, d)
                self.show_display_servers[d] = {}

                #get the masks for the lot
                allmask = dsc.get_display_mask()
                img = dsc.new_image(
                            color=255, mask=~allmask, nchan=3, dtype=np.uint8)
                self.show_display_servers[d] = dict(
                        handle=d,
                        visualizeimg=img)
                cv2.imshow(d, img)

            self._black_projector(d)
            
        #tracking (flydra) cameras and acquisition
        self.tracking_cameras = {}
        cam_handlers = []
        for cam in tracking_cameras:
            fd = DotBGFeatureDetector(
                    cam,
                    method="med",
                    show=show_type if (show_cameras[0] == "all" or cam in show_cameras) else "",
                    debug=debug>1)
            self.tracking_cameras[cam] = fd
            cam_handlers.append(CameraHandler(cam,debug=debug>0))
            rospy.loginfo("Calibrating %s" % cam)
            self._set_bg_mask(cam, fd)
        self.runner = SimultainousCameraRunner(cam_handlers)
        
        #laser camera acquisition
        self.laser_camera = laser_camera
        fd = DotBGFeatureDetector(
                    laser_camera,
                    method="med",
                    show=show_type if (show_cameras[0] == "all" or laser_camera in show_cameras) else "",
                    debug=debug>1)
        self.laser_runner = SequentialCameraRunner(
                                (CameraHandler(laser_camera,debug=debug>0),),
                                queue_depth=1)
        self.laser_detector = fd
        self.laser_mask = load_mask_image(decode_url(config["laser_camera_mask"]))

        #need to recieve images (i.e. by calculating the background) before
        #being able to read the resoluions
        self._calculate_background()

        self._vdisptocalibrate = []            
        self._vdispinfo = {}

        self.mode_lock = threading.Lock()
        self.mode_args = tuple()

        if continue_calibration:
            path = decode_url(continue_calibration)
            if os.path.exists(path):
                self._load_previous_calibration(path)
            else:
                rospy.logerr("could not find requested calibration to load")

        s = rospy.Service('~calib_change_mode', vros_display.srv.CalibMode, self._change_mode)
        self.change_mode(self.MODE_SLEEP)

    def change_mode(self, mode, *service_args):
        with self.mode_lock:
            self.mode = mode
            self.mode_args = service_args
            rospy.loginfo("Changing to mode -> %s (args %s)" % (mode,repr(self.mode_args)))

    def _display_server_window_click(self, event, col, row, flags, ds):
        if flags & cv.CV_EVENT_FLAG_LBUTTON:
            rospy.loginfo("queuing point col:%s row:%s" % (col,row))
            self._click_queue[ds].append( (col,row) )
        elif flags & cv.CV_EVENT_FLAG_RBUTTON:
            rospy.loginfo("clearing queued points")
            self._click_queue[ds] = []

    def _change_mode(self, req):
        self.change_mode(req.mode, req.sa, req.fa, req.fb, req.fc)
        return vros_display.srv.CalibModeResponse()

    def _calculate_background(self):
        rospy.loginfo("Collecting backgrounds")
        #collect bg images
        self.runner.get_images(20, self.trigger_proxy_rate, [5], self.trigger_proxy_rate, [0])
        imgs = self.runner.result_as_nparray
        for cam in imgs:
            #collect the background model
            self.tracking_cameras[cam].compute_bg(imgs[cam])
            rospy.loginfo("Calculate background for %s" % cam)
        rospy.loginfo("Collecting backgrounds finished")

    def _set_bg_mask(self, cam, detector, clear_masks=False):
        if clear_masks:
            rospy.loginfo("Clearing %s mask" % cam)
            detector.clear_mask()
        else:
            mask_name = os.path.join(self.mask_dir,cam.split("/")[-1]) + ".png"
            if os.path.exists(mask_name):
                arr = load_mask_image(mask_name)
                detector.set_mask(arr)
                rospy.loginfo("Setting %s mask = %s" % (cam,mask_name))

    def _light_laser_pixel(self, pan, tilt, power):
        minpan,maxpan,npan = self.laser_range_pan
        mintilt,maxtilt,ntilt = self.laser_range_tilt
        
        pan = np.clip(pan,minpan,maxpan-1)
        tilt = np.clip(tilt,mintilt,maxtilt-1)
        
        dist = numpy.linalg.norm(
                np.array((self._laser_currpan,self._laser_currtilt)) -
                np.array((pan,tilt)))
        rospy.loginfo("laser %s: pan: %d tilt: %d dist: %.1f" % (
                            "on" if power else "off",
                            pan,tilt,dist))

        self._laser_currpan = pan
        self.laser_proxy_pan(pan)
        self._laser_currtilt = tilt
        self.laser_proxy_tilt(tilt)
        self.laser_proxy_power(power)

        if show_laser_scatter:
            handle = laser_handle
            img = self._laser_handles[handle].copy()
            add_crosshairs_to_nparr(
                        arr=img,
                        row=math.floor(tilt)+(0-self.laser_range_tilt[0]),
                        col=math.floor(pan)+(0-self.laser_range_pan[0]),
                        sz=-1,  fill=255, chan=1)
            cv2.imshow(handle, img)
        
        if dist > 200:
            rospy.sleep(2.0)
        elif dist > 100:
            rospy.sleep(1.0)
        elif dist > 50:
            rospy.sleep(0.8)
        elif dist > 10:
            rospy.sleep(0.5)
        else:
            rospy.sleep(0.3)
        
        return pan,tilt

    def _black_projector(self, ds):
        self._light_proj_pixel(ds, None, None)

    def _light_proj_pixel(self, ds, row, col):
        row = math.floor(row) if row != None else None
        col = math.floor(col) if col != None else None

        target = (ds, col, row)
        if self._light_proj_cache == target:
            rospy.loginfo("not lighting projector %s col:%s row:%s (already lit)" % target)
            return

        dsc = self.display_servers[ds]["display_client"]
        #create the image to send to the dsc
        arr = dsc.new_image(dsc.IMAGE_COLOR_BLACK, mask=None)
        
        if col != None:
            sz = self.ptsize
            ri = arr.shape[0]
            ci = arr.shape[1]
            arr[max(0,row-sz):min(row+sz,ri),max(0,col-sz):min(col+sz,ci),:3] = dsc.IMAGE_COLOR_WHITE

        dsc.show_pixels(arr)
        
        if ds in self.show_display_servers:
            handle = self.show_display_servers[ds]["handle"]
            img =  self.show_display_servers[ds]["visualizeimg"].copy()
            if col != None:
                add_crosshairs_to_nparr(arr=img, row=row, col=col, sz=-1, fill=255, chan=1)
            cv2.imshow(handle, img)

        rospy.loginfo("lighting projector %s col:%s row:%s" % target)
        self._light_proj_cache = target

        rospy.sleep(0.5)

    def _detect_points(self, runner, thresh, restrict={}):
        runner.get_images(1, self.trigger_proxy_rate, [5], self.trigger_proxy_rate, [0])
        imgs = runner.result_as_nparray
        detected = {}
        visible = 0
        for cam in imgs:
            if restrict and cam not in restrict:
                continue
            features = self.tracking_cameras[cam].detect(imgs[cam][:,:,0], thresh)
            if features:
                rospy.logdebug("detect: %s: %s" % (cam,repr(features)))
                #take the first point
                row,col = features[0]
                #numpy returns int64 here, which is not serializable, and also not needed. Just
                #convert to simple int
                #
                #convert to pixel coords (swap row/col)
                detected[cam] = (int(col),int(row))
                visible += 1

        return detected,visible

    def _detect_laser_camera_2d_point(self, thresh, msgprefix=""):
        self.laser_runner.get_images(1)
        imgs = self.laser_runner.result_as_nparray

        if thresh == self.laser_thresh:
            self.laser_detector.set_mask(self.laser_mask, copy=False)

        features = self.laser_detector.detect(
                        imgs[self.laser_camera][:,:,0],
                        thresh)

        if thresh == self.laser_thresh:
            self.laser_detector.clear_mask()

        if features:
            row,col = features[0]
            if thresh == self.laser_thresh:
                msg = msgprefix + "PTC laser"
                expected = np.array(self.laser_expected_detect_location)
                actual = np.array((col,row))
                dist = numpy.linalg.norm(expected - actual)
                if dist > self.laser_expected_detect_hist:
                    rospy.logwarn("misdetected laser location expected:%s got:%s (dist:%.1f) " % (
                        expected,actual,dist))
                    row = col = None
            elif thresh == self.visible_thresh:
                msg = msgprefix + "PTC visible"
            else:
                msg = msgprefix + "PTC (unknown threshold)"
            rospy.loginfo("detect 2D %s: col:%s row:%s" % (msg,col,row))
            return col,row

        return None,None

    def _detect_3d_point(self, runner, thresh):
        restrict = self.tracking_cameras.keys()
        detected,nvisible = self._detect_points(runner, thresh, restrict)
        xyz = None
        pts = None
        reproj = 0
        if nvisible >= 2:
            rospy.logdebug("#%d: (%d visible: %s)" % (self.num_results, nvisible, ','.join(detected.keys())))
            pts = []
            for d in detected:
                safe_name = d if d[0] != "/" else d[1:]
                pts.append( (safe_name,detected[d]) )
            xyz = self.flydra.find3d(pts,return_line_coords=False, undistort=True)
        if xyz != None:
            recon_3d = []
            for camid,(u,v) in pts:
                u2,v2 = self.flydra.find2d(camid,xyz,distorted=True)
                d = math.sqrt((u-u2)**2 + (v-v2)**2)
                recon_3d.append(d)
            reproj = np.mean(recon_3d)

            if reproj >= 10:
                xyz = None

        if xyz != None:
            rospy.loginfo("detect 3D: %s (reproj:%.1f)" % (repr(xyz),reproj))

        return xyz,pts,nvisible,reproj

    def _load_previous_calibration(self, path):
        self.data.load(path, vis_callback_2d=self._show_correspondence)
        for ds in self.show_display_servers:
            cv2.imshow(
                    self.show_display_servers[ds]["handle"],
                    self.show_display_servers[ds]["visualizeimg"])
        if show_laser_scatter:
            cv2.imshow(
                    laser_handle,
                    self._laser_handles[laser_handle])

    def _parse_ds_specified(self, args):
        try:
            spec = args[0]
            ds,vdisp = spec.split("/")
            vdispinfo = None
            centroid = []
            for vd in self.display_servers[ds]["virtualDisplays"]:
                if vd["id"] == vdisp:
                    vdispinfo = vd
                    colm = int(args[1])
                    rowm = int(args[2])
                    if colm > 0 and rowm > 0:
                        centroid = [colm,rowm]
            if not vdispinfo:
                rospy.logwarn("vdisp %s not found in display server %s" % (vdisp,ds))
                raise ValueError
        except (ValueError, KeyError):
            rospy.logwarn("invalid display_server/vdisp specifier")
            raise

        return ds,vdisp,vdispinfo,centroid

    def _show_correspondence(self, ds, col, row, pan, tilt):
        handle = self.show_display_servers[ds]["handle"]
        img =  self.show_display_servers[ds]["visualizeimg"]
        add_crosshairs_to_nparr(
            arr=img,
            row=row,
            col=col,
            sz=1, fill=255, chan=0)

        if show_laser_scatter:
            handle = laser_handle
            img = self._laser_handles[handle]
            try:
                t = math.floor(tilt+(0-self.laser_range_tilt[0]))
                p = math.floor(pan+(0-self.laser_range_pan[0]))
                img[t,p,:] = 255
            except:
                rospy.logerr("could not plot pan/tilt: tilt:%s pan:%s" % (tilt,pan))

    def run(self):
        while not rospy.is_shutdown():
            with self.mode_lock:
                mode = self.mode
                service_args = self.mode_args
            if mode == self.MODE_FINISHED:
                break

            elif mode == self.MODE_SLEEP:
                pass
                
            elif mode == self.MODE_MANUAL_TRACKING:
                xyz,pts,nvisible,reproj = self._detect_3d_point(self.runner, self.laser_thresh)
                col,row = self._detect_laser_camera_2d_point(self.laser_thresh)

            elif mode == self.MODE_MANUAL_PROJECTOR:
                try:
                    ds,vdisp,vdispinfo,centroid = self._parse_ds_specified(service_args)
                except (ValueError, KeyError):
                    self.change_mode(self.MODE_SLEEP)
                    continue

                if centroid:                 
                    col,row = centroid
                else:
                    dsc = self.display_servers[ds]["display_client"]
                    vdmask = dsc.get_virtual_display_mask(vdisp)
                    col,row = get_centre_of_vdisp(vdmask)

                self._light_proj_pixel(ds, row=row, col=col)
                col,row = self._detect_laser_camera_2d_point(self.visible_thresh)

            elif mode == self.MODE_MANUAL_CLICKED:
                tocal = []
                for ds,pts in self._click_queue.items():    
                    if pts:
                        for vdisp in self.display_servers[ds]["virtualDisplays"]:
                            dsc = self.display_servers[ds]["display_client"]
                            vdispname = vdisp["id"]
                            vdmask = dsc.get_virtual_display_mask(vdispname)
                            for col,row in pts:
                                if vdmask[row,col]:
                                    rospy.loginfo("clicked to select col:%s row:%s in %s" % (col,row,vdispname))
                                    tocal.append( (ds,vdispname,vdisp.copy(),(col,row)) )

                for ds in self.show_display_servers:
                    self._click_queue[ds] = []

                if tocal:
                    self._vdisptocalibrate = tocal
                    self.change_mode(self.MODE_DISPLAY_SERVER_VDISP)
                    continue

                self.change_mode(self.MODE_SLEEP)

            elif mode == self.MODE_DISPLAY_SERVER:
                tocal = []
                for ds in ["display_server0"]:
                    for vdisp in self.display_servers[ds]["virtualDisplays"]:
                        vdispname = vdisp['id']
                        dsc = self.display_servers[ds]["display_client"]

                        if ds in self.show_display_servers:
                            handle = self.show_display_servers[ds]["handle"]
                            img =  self.show_display_servers[ds]["visualizeimg"]
                        else:
                            img = None

                        vdmask = dsc.get_virtual_display_mask(vdispname)
                        vdpts = dsc.get_virtual_display_points(vdispname)

                        centroids = generate_sampling_pixel_coords(vdmask,vdpts,60,img)
                        for c in centroids:
                            tocal.append( (ds,vdispname,vdisp.copy(),c) )

                self._vdisptocalibrate = tocal
                self.change_mode(self.MODE_DISPLAY_SERVER_VDISP)

            elif mode == self.MODE_DISPLAY_SERVER_STOP:
                self._vdisptocalibrate = []
                self.change_mode(self.MODE_DISPLAY_SERVER_VDISP)

            elif mode == self.MODE_DISPLAY_SERVER_VDISP:
                if len(self._vdisptocalibrate):
                    ds,vdisp,vdispinfo,centroid = self._vdisptocalibrate.pop()
                #did the user request a vdisp calibration
                elif service_args and service_args[0]:
                    try:
                        ds,vdisp,vdispinfo,centroid = self._parse_ds_specified(service_args)
                    except (ValueError, KeyError):
                        self.change_mode(self.MODE_SLEEP)
                        continue
                    self._vdisptocalibrate = []
                else:
                    rospy.loginfo("nothing to do")
                    self.change_mode(self.MODE_SLEEP)
                    continue

                dsc = self.display_servers[ds]["display_client"]
                vdmask = dsc.get_virtual_display_mask(vdisp)
                vdpts = dsc.get_virtual_display_points(vdisp)
                    
                self._vdispinfo = vdispinfo

                if not centroid:
                    #find the centre of the vdisp by default
                    centroid = get_centre_of_vdisp(vdmask)
                    
                rospy.loginfo("Calibrating %s:%s@%s" % (ds,vdisp,repr(centroid)))
                colmid,rowmid = centroid

                #we have found the pixel location in the projector around which we
                #should search, using the laser. Now we get the laser close
                #enough to the pixel that subsequently we can later move the pixel
                #to the laser
                self._light_proj_pixel(ds, row=rowmid, col=colmid)

                found = False
                
                #start search at current location (twice for reliability)
                searchpath = [(self._laser_currpan,self._laser_currtilt),
                              (self._laser_currpan,self._laser_currtilt)]
                
                minpan,maxpan,npan = self.laser_range_pan
                mintilt,maxtilt,ntilt = self.laser_range_tilt
                searchpath.extend( gen_vert_snake(
                                        w=maxpan,h=maxtilt,
                                        startw=minpan,starth=mintilt,
                                        sw=npan,sh=ntilt,
                                        linspace=True) )

                for pan,tilt in searchpath:
                    pan,tilt = self._light_laser_pixel(pan=pan,tilt=tilt,power=False)
                    col,row = self._detect_laser_camera_2d_point(self.visible_thresh)
                    if col:
                        expected = np.array(self.laser_expected_detect_location)
                        expdist = numpy.linalg.norm(expected - np.array((col,row)))
                        fine_dist = expdist
                        if expdist < 150:

                            #we have a rough estimate, refine it                            
                            tries = 40
                            print "ROUGH n%d" % tries,expdist,expected - np.array((col,row))
                            
                            while (tries > 0):
                                diffcol,diffrow = expected - np.array((col,row))
                                newpan  = pan  + (np.sign(diffcol) * self._vdispinfo["p_laser_col"])
                                newtilt = tilt + (np.sign(diffrow) * self._vdispinfo["p_laser_row"])
                                pan,tilt = self._light_laser_pixel(pan=newpan,tilt=newtilt,power=False)
                                oldcol = col
                                oldrow = row
                                col,row = self._detect_laser_camera_2d_point(self.visible_thresh)
                                #we lost the pixel
                                if not col:
                                    col = oldcol
                                    row = oldrow
                                    tries -= 1
                                    print "ROUGH LOST"
                                else:
                                    fine_dist = numpy.linalg.norm(expected - np.array((col,row)))
                                    print "FINE n%d" % tries,fine_dist,expected - np.array((col,row))
                                    if fine_dist < 3:
                                        tries = 0
                                        found = True
                                    else:
                                        tries -= 1
                                        
                            if not found and (fine_dist < 40):
                                print "AVERAGE DIST", fine_dist
                                found = True
                                        
                            break
                        else:
                            rospy.loginfo("2D pixel too far from start location (dist:%.1f)" % (expdist))

                if found:
                    rospy.loginfo("found starting pixel: col:%s row:%s pan:%s tilt:%s (dist:%.1f)" % (
                                    colmid,rowmid,pan,tilt,fine_dist))
                    self._vdispinfo["panmid"] = pan
                    self._vdispinfo["tiltmid"] = tilt

                    self._vdispinfo["colmid"] = int(colmid)
                    self._vdispinfo["rowmid"] = int(rowmid)

                    self._vdispinfo["vdmask"] = vdmask
                    self._vdispinfo["dsc"] = dsc
                    self._vdispinfo["ds"] = ds
                    self._vdispinfo["width"] = self.display_servers[ds]["width"]
                    self._vdispinfo["height"] = self.display_servers[ds]["height"]

                    self._vdispinfo["pointsneeded"] = int(self.laser_need_n_points)
                    
                    self._vdispinfo["homeattempt"] = 30

                    self.change_mode(self.MODE_DISPLAY_SERVER_HOME)
                else:
                    rospy.logwarn("could not find find starting pixel col:%s row:%s" %(
                                    colmid,rowmid))
                
                if ds in self.show_display_servers:
                    handle = self.show_display_servers[ds]["handle"]
                    img =  self.show_display_servers[ds]["visualizeimg"]
                    cv2.imshow(handle, img)

            elif mode == self.MODE_DISPLAY_SERVER_HOME:
                ds = self._vdispinfo["ds"]
                self._light_laser_pixel(self._vdispinfo["panmid"], self._vdispinfo["tiltmid"], power=True)
                self._black_projector(ds)
                
                self._vdispinfo["homeattempt"] -= 1
                if self._vdispinfo["homeattempt"] < 0:
                    rospy.logwarn("giving up, could not get laser home location (maybe reflection)")
                    self.change_mode(self.MODE_DISPLAY_SERVER_VDISP)
                    continue
                
                col,row = self._detect_laser_camera_2d_point(self.laser_thresh)
                if col != None:
                    self.laser_proxy_power(False)
                    
                    #generate N random points about the start - and include the
                    #start point. This sampling could be better done as a grid...
                    #
                    #this is the maximum number of points we will test, so be generous
                    #because this loop is exited when we have collected enough points,
                    #not when this list is empty (so we will not necessarily take this long)
                    self._vdispinfo["currattempt3d"] = [(self._vdispinfo["panmid"],self._vdispinfo["tiltmid"])]
                    self._vdispinfo["currattempt3d"].extend(
                            [(self._vdispinfo["panmid"]  + random.randint(*self.laser_search_size),
                              self._vdispinfo["tiltmid"] + random.randint(*self.laser_search_size)) \
                                for i in range(150)])

                    self.change_mode(self.MODE_DISPLAY_SERVER_LASER)

            elif mode == self.MODE_DISPLAY_SERVER_LASER:
                try:
                    pan,tilt = self._vdispinfo["currattempt3d"].pop()
                    pan,tilt = self._light_laser_pixel(pan, tilt, power=True)
                    self._vdispinfo["currpan"] = pan
                    self._vdispinfo["currtilt"] = tilt
                except IndexError:
                    rospy.logwarn("giving up, could not get a 3D reconstruction")
                    self.change_mode(self.MODE_DISPLAY_SERVER_VDISP)                    

                ds = self._vdispinfo["ds"]
                self._black_projector(ds)
                
                xyz,pts,nvisible,reproj = self._detect_3d_point(self.runner, self.laser_thresh)
                #always do this detection to keep the basler camera updated... even
                #if there is a chance we throw away the result
                col,row = self._detect_laser_camera_2d_point(self.laser_thresh)

                if xyz == None:
                    rospy.loginfo("no 3d point (visible in %d cams, reproj error: %f)" % (nvisible,reproj))
                    continue

                if col != None:
                    self._vdispinfo["targetcol"] = col
                    self._vdispinfo["targetrow"] = row
                    self._vdispinfo["currxyz"] = xyz
                    self._vdispinfo["currpts"] = pts
                    #start at the previously detected middle of the field of
                    #view of the ptc camera
                    self._vdispinfo["projcol"] = self._vdispinfo["colmid"]
                    self._vdispinfo["projrow"] = self._vdispinfo["rowmid"]
                    self._vdispinfo["currattempt"] = 30
                    self.laser_proxy_power(False)
                    self.change_mode(self.MODE_DISPLAY_SERVER_PROJECTOR)

            elif mode == self.MODE_DISPLAY_SERVER_PROJECTOR:
                ds = self._vdispinfo["ds"]
                self.laser_proxy_power(False)
                
                self._vdispinfo["currattempt"] -= 1
                if self._vdispinfo["currattempt"] < 0:
                    rospy.logwarn("giving up, could not get a 3D reconstruction")
                    self.change_mode(self.MODE_DISPLAY_SERVER_LASER)
                    continue
    
                self._light_proj_pixel(
                        ds,
                        row=self._vdispinfo["projrow"],
                        col=self._vdispinfo["projcol"])

                col,row = self._detect_laser_camera_2d_point(
                                    self.visible_thresh,
                                    msgprefix="attempt %d " % self._vdispinfo["currattempt"])

                #missed the projector pixel. try again (we are safe from looping
                #because of the currattempt test)
                if col == None:
                    continue
                else:
                    targetcol = self._vdispinfo["targetcol"]
                    targetrow = self._vdispinfo["targetrow"]
                    projcol = self._vdispinfo["projcol"]
                    projrow = self._vdispinfo["projrow"]

                    colfound = rowfound = False
                    if abs(targetcol - col) > 1:
                        diff = abs(targetcol - col)
                        if diff > 5:
                            #p-control
                            adj = int(self._vdispinfo["dcol"] * diff * 0.3)
                        else:
                            #i-control
                            adj = self._vdispinfo["dcol"]
                        if targetcol > col:
                            projcol = self._vdispinfo["projcol"] + adj
                        else:
                            projcol = self._vdispinfo["projcol"] - adj
                    else:
                        colfound = True

                    if abs(targetrow - row) > 1:
                        diff = abs(targetrow - row)
                        if diff > 5:
                            #p-control
                            adj = int(self._vdispinfo["drow"] * diff * 0.3)
                        else:
                            #i-control
                            adj = self._vdispinfo["drow"]
                        if targetrow > row:
                            projrow = self._vdispinfo["projrow"] + adj
                        else:
                            projrow = self._vdispinfo["projrow"] - adj
                    else:
                        rowfound = True
                        
                    #clamp the projection range and then
                    self._vdispinfo["projcol"] = np.clip(projcol,0,self._vdispinfo["width"]-1)
                    self._vdispinfo["projrow"] = np.clip(projrow,0,self._vdispinfo["height"]-1)

                    if colfound and rowfound:
                        if ds in self.show_display_servers or show_laser_scatter:
                            self._show_correspondence(
                                    ds=ds,
                                    col=self._vdispinfo["projcol"],
                                    row=self._vdispinfo["projrow"],
                                    pan=self._vdispinfo["currpan"],
                                    tilt=self._vdispinfo["currtilt"])
                            if ds in self.show_display_servers:
                                cv2.imshow(
                                        self.show_display_servers[ds]["handle"],
                                        self.show_display_servers[ds]["visualizeimg"])
                            if show_laser_scatter:
                                cv2.imshow(
                                        laser_handle,
                                        self._laser_handles[laser_handle])

                        rospy.loginfo("FOUND CORRESPONDENCE")
                        self.data.add_mapping(
                                points=self._vdispinfo["currpts"],
                                display_server=ds,
                                vdisp=self._vdispinfo["id"],
                                position=self._vdispinfo["currxyz"].tolist(),
                                pan=self._vdispinfo["currpan"],
                                tilt=self._vdispinfo["currtilt"],
                                pixel_projector=(self._vdispinfo["projcol"],
                                                 self._vdispinfo["projrow"],
                                                 0),
                                pixel_ptc_laser=(self._vdispinfo["targetcol"],
                                                 self._vdispinfo["targetrow"],
                                                 0),
                                pixel_ptc_projector=(col,
                                                     row,
                                                     0))

                        self._vdispinfo["pointsneeded"] -= 1
                        if self._vdispinfo["pointsneeded"] > 0:
                            self.change_mode(self.MODE_DISPLAY_SERVER_LASER)
                        else:
                            self.change_mode(self.MODE_DISPLAY_SERVER_VDISP)

            elif mode == self.MODE_RESTORE:
                self._load_previous_calibration(self.outdir)
                self.change_mode(self.MODE_SLEEP)

            #publish state
            self.pub_mode.publish(self.mode)

            rospy.sleep(0.1)

        #clean up all state
        if self.laser_proxy_power:
            self.laser_proxy_power(False)

        if self.show_cameras or self.show_display_servers:
            cv2.destroyAllWindows()

        self.data.close()

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--show-cameras', type=str, default=("",),
        help='show images with the given topics (or "all"). See --show-type for a description of the available images types',
        metavar="/Basler_NNN", nargs='*')
    parser.add_argument(
        '--show-type', type=str, default='F',
        help='which images types to show. %s' % (
                ', '.join(["%s=%s" % i for i in DotBGFeatureDetector.WIN_TYPES.items()])))
    parser.add_argument(
        '--calib-config', type=str, default='package://flycave/conf/calib-all.yaml',
        help='path to calibration configuration yaml file')
    parser.add_argument(
        '--save-dir', type=str, default=os.path.expanduser('~/FLYDRA/vros-calibration'),
        help='path to save calibration data')
    parser.add_argument(
        '--continue-calibration', type=str,
        help='path to previous calibration bag file')
    parser.add_argument(
        '--show-display-servers', type=str, default=("",),
        help='show display servers with the given names (or "all") calibration in process',
        metavar="display_serverN", nargs='*')
    parser.add_argument(
        '--debug', type=int, default=0,
        help='print image debugging >0=acquisition, >1=bgdiff')

    argv = rospy.myargv()
    args = parser.parse_args(argv[1:])

    outdir = os.path.expanduser(os.path.abspath(args.save_dir))
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    
    rospy.init_node('calibration')

    config = {}
    conffile = decode_url(args.calib_config)
    with open(conffile, 'r') as f:
        config = yaml.load(f)
        for k in ("tracking_cameras", "display_servers", "trigger", "mask_dir"):
            if not config.has_key(k):
                parser.error("malformed calibration config, missing %s" % k)

    c = Calib(config,
              show_cameras=args.show_cameras,
              show_display_servers=args.show_display_servers,
              show_type=set(args.show_type),
              outdir=outdir,
              continue_calibration=args.continue_calibration,
              debug=args.debug)
    c.run()

