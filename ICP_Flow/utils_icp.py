import warnings

import torch

from ICP_Flow.utils_icp_pytorch3d import iterative_closest_point
from ICP_Flow.utils_helper import transform_points_batch, nearest_neighbor_batch

warnings.filterwarnings("ignore")


def apply_icp(args, src, dst, init_poses):
    src_tmp = transform_points_batch(src, init_poses)

    Rts = pytorch3d_icp(args, src_tmp, dst)
    Rts = torch.bmm(Rts, init_poses)

    # # # pytorch 3d icp might go wrong ! to fix!
    mask_src = src[:, :, -1] > 0.0
    _, error_init = nearest_neighbor_batch(src_tmp, dst)
    error_init = (error_init * mask_src).sum(dim=1) / mask_src.sum(dim=1)

    src_tmp = transform_points_batch(src, Rts)
    _, error_icp = nearest_neighbor_batch(src_tmp, dst)
    error_icp = (error_icp * mask_src).sum(dim=1) / mask_src.sum(dim=1)
    invalid = error_icp >= error_init
    Rts[invalid] = init_poses[invalid]

    return Rts


def pytorch3d_icp(args, src, dst):
    icp_result = iterative_closest_point(
        src,
        dst,
        init_transform=None,
        thres=args.thres_dist,
        max_iterations=100,
        relative_rmse_thr=1e-6,
        estimate_scale=False,
        allow_reflection=False,
        verbose=False,
    )

    Rs = icp_result.RTs.R
    ts = icp_result.RTs.T

    Rts = torch.cat([Rs, ts[:, None, :]], dim=1)
    Rts = torch.cat([Rts.permute(0, 2, 1), Rts.new_zeros(len(ts), 1, 4)], dim=1)
    Rts[:, 3, 3] = 1.0

    return Rts
