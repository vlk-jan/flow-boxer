import argparse
import os.path as osp
from functools import reduce

import numpy as np
from pyquaternion import Quaternion
from nuscenes.nuscenes import NuScenes
from nuscenes.utils.data_classes import LidarPointCloud
from nuscenes.utils.geometry_utils import transform_matrix


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("-sn", "--scene-number", type=int, required=True, help="Select the scene number")
    parser.add_argument("-d", "--debug", action="store_true", default=False, help="Debug mode")

    return parser.parse_args()


def main(args):
    # Load nuScenes dataset
    nusc = NuScenes(version='v1.0-trainval', dataroot='/mnt/data/Public_datasets/nuScenes', verbose=args.debug)

    # Choose a scene (modify the index as needed)
    scene_name = f"scene-{args.scene_number:04}"
    file_name = "exports/" + scene_name + "-full.npy"
    scene = next((s for s in nusc.scene if s["name"] == scene_name), None)
    if not scene:
        print("Scene not found")
        exit()

    # Get the first sample token in the scene
    current_sample_token = scene['first_sample_token']
    sample = nusc.get("sample", current_sample_token)

    # Initialize an empty list to store the aggregated point cloud
    points = np.zeros((4, 0))
    all_pc = LidarPointCloud(points)

    # Get reference pose and timestamp.
    ref_sd_token = sample['data']['LIDAR_TOP']
    ref_sd_rec = nusc.get('sample_data', ref_sd_token)
    ref_pose_rec = nusc.get('ego_pose', ref_sd_rec['ego_pose_token'])
    ref_cs_rec = nusc.get('calibrated_sensor', ref_sd_rec['calibrated_sensor_token'])

    # Homogeneous transform from ego car frame to reference frame.
    ref_from_car = transform_matrix(ref_cs_rec['translation'], Quaternion(ref_cs_rec['rotation']), inverse=True)

    # Homogeneous transformation matrix from global to _current_ ego car frame.
    car_from_global = transform_matrix(ref_pose_rec['translation'], Quaternion(ref_pose_rec['rotation']),
                                    inverse=True)

    # Aggregate current and previous sweeps.
    sample_data_token = sample['data']['LIDAR_TOP']
    current_sd_rec = nusc.get('sample_data', sample_data_token)
    while current_sd_rec:
        # Load up the pointcloud and remove points close to the sensor.
        filename = current_sd_rec['filename']
        if "samples" in filename:
            current_pc = LidarPointCloud.from_file(osp.join(nusc.dataroot, current_sd_rec['filename']))
            current_pc.remove_close(1.0)

            # Get past pose.
            current_pose_rec = nusc.get('ego_pose', current_sd_rec['ego_pose_token'])
            global_from_car = transform_matrix(current_pose_rec['translation'],
                                               Quaternion(current_pose_rec['rotation']), inverse=False)

            # Homogeneous transformation matrix from sensor coordinate frame to ego car frame.
            current_cs_rec = nusc.get('calibrated_sensor', current_sd_rec['calibrated_sensor_token'])
            car_from_current = transform_matrix(current_cs_rec['translation'], Quaternion(current_cs_rec['rotation']),
                                                inverse=False)

            # Fuse four transformation matrices into one and perform transform.
            trans_matrix = reduce(np.dot, [ref_from_car, car_from_global, global_from_car, car_from_current])
            current_pc.transform(trans_matrix)

            # Merge with key pc.
            all_pc.points = np.hstack((all_pc.points, current_pc.points))

        # Abort if there are no next sweeps.
        if current_sd_rec['next'] == '':
            break
        else:
            current_sd_rec = nusc.get('sample_data', current_sd_rec['next'])

    # Save or process the aggregated point cloud
    np.save(f"{file_name}", all_pc.points.T[:,:3])
    print(f"[INFO]: Aggregated point cloud saved as '{file_name}'")

if __name__ == "__main__":
    args = parse_args()
    main(args)
