import numpy as np
import matplotlib.pyplot as plt
import scipy.ndimage
import scipy.misc
import scipy.interpolate

import roslib;
roslib.load_manifest('vros_display')
roslib.load_manifest('motmot_ros_utils')
import rospy

from calib.io import AllPointPickle
from calib.visualization import create_pcd_file_from_points
from rosutils.io import decode_url

import flydra
import flydra.reconstruct
import flydra.align

import exr
import display_client

rospy.init_node('calibone', anonymous=True)

LASER_PKL       = decode_url('package://flycave/calibration/laser')
FLYDRA_CALIB    = decode_url('package://flycave/calibration/flydra')

X_INDEX = 0
Y_INDEX = 1
FILT_METHOD = 'cubic'

#so, we can successfully recover a cylinder. Find the pixel coords of those points in the
#projector cameras, and then, via the projector calibration, find the corresponding pixel
#in the projector

#use the flydra calibration to get the 3d coords
fly = flydra.reconstruct.Reconstructor(cal_source=FLYDRA_CALIB)

laser = AllPointPickle()
laser.initilize_from_directory(LASER_PKL)

print "number of laser points",laser.num_points

for dsnum in (1,3):
    dsname = "ds%d" % dsnum

    #use the display server calibration to get the projector pixel coordinate
    ds = flydra.reconstruct.Reconstructor(
            cal_source=decode_url('package://flycave/calibration/triplets/%s/result' % dsname))

    flydra_cams = fly.get_cam_ids()
    ds_cams = [c for c in ds.get_cam_ids() if not c.startswith('display_server')]

    flydra_points_3d = []
    ds_points_2d = []
    ds_points_3d = []

    arr = np.zeros((768,1024,2))
    arr.fill(np.nan)

    dsc = display_client.DisplayServerProxy("/display_server1", wait=False)

    #all points visible in 2 or more cameras
    for pts in laser.get_points_in_cameras():
        flydra_ds_points_2d = []
        ds_ds_points_2d = []
        for pt in pts:
            cam, _ = pt
            if cam in flydra_cams:
                flydra_ds_points_2d.append(pt)
            if cam in ds_cams:
                ds_ds_points_2d.append(pt)

        #need at least 2 of each for 3D reconstruction in each
        if len(flydra_ds_points_2d) >= 2 and len(ds_ds_points_2d) >= 2:
            #get the projector coord
            ds_3d = ds.find3d(ds_ds_points_2d,return_line_coords=False)
            ds_2d = ds.find2d('display_server%d' % dsnum,ds_3d)

            #check bounds are realistic
            #FIXME: have I swapped u/v here????
            #FIXME: can I make this check better???
            u,v = ds_2d
            if u > dsc.width or v > dsc.height or u < 0 or v < 0:
                continue

            try:
                #get the real 3D coord
                flydra_3d = fly.find3d(flydra_ds_points_2d,return_line_coords=False)
                x,y,z = flydra_3d

                #this is just a debug image for asessing coverage
                arr[v-2:v+2,u-2:u+2,Y_INDEX] = y
                arr[v-2:v+2,u-2:u+2,X_INDEX] = x

                flydra_points_3d.append(flydra_3d)
                ds_points_3d.append(ds_3d)

                ds_points_2d.append(ds_2d)
            except IndexError:
                print "SHIT?"*10
                pass

    create_pcd_file_from_points('/tmp/%sflydra3d.pcd' % dsname,flydra_points_3d)
    create_pcd_file_from_points('/tmp/%s3d.pcd' % dsname,ds_points_3d)

    flydra_points_3d_arr = np.array(flydra_points_3d)
    ds_points_2d_arr = np.array(ds_points_2d)
    ds_points_3d_arr = np.array(ds_points_3d)

    print "%s constructed from %d points" % (dsname, len(ds_points_2d))

    if 1:
        #make EXR files via smoothing reprojected 3d points in the projecor view
        grid_y, grid_x = np.mgrid[0:dsc.height, 0:dsc.width]

        y0 = scipy.interpolate.griddata(
                ds_points_2d_arr,
                flydra_points_3d_arr[:,Y_INDEX],
                (grid_x, grid_y),
                method=FILT_METHOD,
                fill_value=np.nan)
        x0 = scipy.interpolate.griddata(
                ds_points_2d_arr,
                flydra_points_3d_arr[:,X_INDEX],
                (grid_x, grid_y),
                method=FILT_METHOD,
                fill_value=np.nan)

        if 0:
            #mask out invalid parts if the convex hull doesnt work....
            #mask is usually used for *= with an image, but here we will use boolean indexing to fill
            #invalid areas with nan
            #... so fiddle the mask a little
            mask = dsc.get_virtual_display_mask('vdisp').squeeze()
            mask = ~mask

            x0[mask] = np.nan
            y0[mask] = np.nan

        plt.figure()
        plt.subplot(211)
        plt.imshow(arr[:,:,Y_INDEX])
        plt.colorbar()
        plt.title('%s Original Y' % dsname)
        plt.subplot(212)
        plt.imshow(y0)
        plt.colorbar()
        plt.title('Filtered %s' % FILT_METHOD)

        plt.figure()
        plt.subplot(211)
        plt.imshow(arr[:,:,X_INDEX])
        plt.colorbar()
        plt.title('%s Original X' % dsname)
        plt.subplot(212)
        plt.imshow(x0)
        plt.colorbar()
        plt.title('Filtered %s' % FILT_METHOD)

        exr.save_exr('%s.exr' % dsname, r=y0, g=x0, b=np.zeros_like(x0))

    if 0:
        s,R,T = flydra.align.estsimt(flydra_points_3d_arr.T,ds_points_3d_arr.T)

plt.show()

#rospy.spin()

