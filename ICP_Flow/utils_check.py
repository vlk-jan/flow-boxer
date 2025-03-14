import warnings

import torch

from ICP_Flow.utils_helper import get_bbox_tensor

warnings.filterwarnings("ignore")


def sanity_check(config, src_points, dst_points, src_labels, dst_labels, pairs):
    pairs_true = []
    for pair in pairs:
        src = src_points[src_labels == pair[0]]
        dst = dst_points[dst_labels == pair[1]]

        # scenario 1: either src or dst does not exist, return None
        # scenario 2: both src or dst exist, but they are not matchable because of ground points/too few points/size mismatch, return False
        # scenario 3: both src or dst exist, and they are are matchable, return True
        if min(len(src), len(dst)) < config["min_cluster_size"]:
            continue
        if min(pair[0], pair[1]) < 0:
            continue  # ground or non-clustered points

        mean_src = src.mean(0)
        mean_dst = dst.mean(0)
        if torch.linalg.norm((mean_dst - mean_src)[0:2]) > config["translation_frame"]:
            continue  # x/y translation

        src_bbox = get_bbox_tensor(src)
        dst_bbox = get_bbox_tensor(dst)
        if min(src_bbox[0], dst_bbox[0]) < config["thres_box"] * max(
            src_bbox[0], dst_bbox[0]
        ):
            continue
        if min(src_bbox[1], dst_bbox[1]) < config["thres_box"] * max(
            src_bbox[1], dst_bbox[1]
        ):
            continue
        if min(src_bbox[2], dst_bbox[2]) < config["thres_box"] * max(
            src_bbox[2], dst_bbox[2]
        ):
            continue

        pairs_true.append(pair)
    if len(pairs_true) > 0:
        return torch.vstack(pairs_true)
    else:
        return torch.zeros((0, 2))


def check_transformation(config, translation, rotation, iou):
    # # # check translation
    if torch.linalg.norm(translation) > config["translation_frame"]:
        return False

    # # # check iou
    if iou < config["thres_iou"]:
        return False

    # # # check rotation, in degrees, almost no impact on final result
    max_rot = config["thres_rot"] * 90.0
    if torch.abs(rotation[1:3]).max() > max_rot:  # roll and pitch
        return False

    return True
