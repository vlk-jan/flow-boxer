import os
import yaml
import torch
import random
import warnings
import argparse
import numpy as np
import ScaLR.utils.transforms as tr
from waffleiron import Segmenter
from ScaLR.datasets import LIST_DATASETS, Collate

import matplotlib.pyplot as plt

from utils import get_clusters, get_points_in_box, remove_ego_vehicle
from point_fitting import icp_transform, good_match


def get_default_parser():
    parser = argparse.ArgumentParser(description="Training")
    parser.add_argument(
        "--dataset",
        type=str,
        help="Path to dataset",
        default="nuscenes",
    )
    parser.add_argument(
        "--path_dataset",
        type=str,
        help="Path to dataset",
        default="/mnt/data/Public_datasets/nuScenes/",
    )
    parser.add_argument(
        "--log_path", type=str, required=False, default='demo_log', help="Path to log folder"
    )
    parser.add_argument(
        "--restart", action="store_true", default=False, help="Restart training"
    )
    parser.add_argument(
        "--seed", default=None, type=int, help="Seed for initializing training"
    )
    parser.add_argument(
        "--gpu", default=None, type=int, help="Set to any number to use gpu 0"
    )
    parser.add_argument(
        "--multiprocessing-distributed",
        action="store_true",
        help="Use multi-processing distributed training to launch "
        "N processes per node, which has N GPUs. This is the "
        "fastest way to use PyTorch for either single node or "
        "multi node data parallel training",
    )
    parser.add_argument(
        "--fp16",
        action="store_true",
        default=False,
        help="Enable autocast for mix precision training",
    )
    parser.add_argument(
        "--config_pretrain",
        type=str,
        required=False,
        default='ScaLR/configs/pretrain/WI_768_pretrain.yaml',
        help="Path to config for pretraining",
    )
    parser.add_argument(
        "--config_downstream",
        type=str,
        required=False,
        default='ScaLR/configs/downstream/nuscenes/WI_768_finetune_100p.yaml',
        help="Path to model config downstream",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        default=False,
        help="Run validation only",
    )
    parser.add_argument(
        "--pretrained_ckpt",
        type=str,
        default='ScaLR/logs/linear_probing/WI_768-DINOv2_ViT_L_14-NS_KI_PD/nuscenes/ckpt_last.pth',
        help="Path to pretrained ckpt",
    )
    parser.add_argument(
        "--linprob",
        action="store_true",
        default=False,
        help="Linear probing",
    )
    parser.add_argument("--eps", type=float, default=2.5, help="DBSCAN epsilon")
    parser.add_argument(
        "--min_points", type=int, default=15, help="DBSCAN minimum points"
    )

    return parser


def get_datasets(config, args):

    # Shared parameters
    kwargs = {
        "rootdir": args.path_dataset,
        "input_feat": config["embedding"]["input_feat"],
        "voxel_size": config["embedding"]["voxel_size"],
        "num_neighbors": config["embedding"]["neighbors"],
        "dim_proj": config["waffleiron"]["dim_proj"],
        "grids_shape": config["waffleiron"]["grids_size"],
        "fov_xyz": config["waffleiron"]["fov_xyz"],
    }

    # Get datatset
    DATASET = LIST_DATASETS.get(args.dataset.lower())
    if DATASET is None:
        raise ValueError(f"Dataset {args.dataset.lower()} not available.")

    # Train dataset
    train_dataset = DATASET(
        phase="train",
        # train_augmentations=get_train_augmentations(config),
        **kwargs,
    )

    # Validation dataset
    val_dataset = DATASET(
        phase="val",
        **kwargs,
    )

    return train_dataset, val_dataset


def get_dataloader(train_dataset, val_dataset, args):

    # if args.distributed:
        # train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        # val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)
    # else:
    train_sampler = None
    val_sampler = None

    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        # shuffle=(train_sampler is None),
        shuffle=False,
        num_workers=args.workers,
        pin_memory=True,
        sampler=train_sampler,
        drop_last=True,
        collate_fn=Collate(),
    )
    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=True,
        sampler=val_sampler,
        drop_last=False,
        collate_fn=Collate(),
    )

    return train_loader, val_loader, train_sampler


def load_model_config(file):
    with open(file, "r") as f:
        config = yaml.safe_load(f)
    return config


if __name__ == "__main__":

    parser = get_default_parser()
    args = parser.parse_args()

    # Load config files
    config = load_model_config(args.config_downstream)
    config_pretrain = load_model_config(args.config_pretrain)

    # Merge config files
    # Embeddings
    config["embedding"] = {}
    config["embedding"]["input_feat"] = config_pretrain["point_backbone"][
        "input_features"
    ]
    config["embedding"]["size_input"] = config_pretrain["point_backbone"]["size_input"]
    config["embedding"]["neighbors"] = config_pretrain["point_backbone"][
        "num_neighbors"
    ]
    config["embedding"]["voxel_size"] = config_pretrain["point_backbone"]["voxel_size"]
    # Backbone
    config["waffleiron"]["depth"] = config_pretrain["point_backbone"]["depth"]
    config["waffleiron"]["num_neighbors"] = config_pretrain["point_backbone"][
        "num_neighbors"
    ]
    config["waffleiron"]["dim_proj"] = config_pretrain["point_backbone"]["dim_proj"]
    config["waffleiron"]["nb_channels"] = config_pretrain["point_backbone"][
        "nb_channels"
    ]
    config["waffleiron"]["pretrain_dim"] = config_pretrain["point_backbone"]["nb_class"]
    config["waffleiron"]["layernorm"] = config_pretrain["point_backbone"]["layernorm"]

    # For datasets which need larger FOV for finetuning...
    if config["dataloader"].get("new_grid_shape") is not None:
        # ... overwrite config used at pretraining
        config["waffleiron"]["grids_size"] = config["dataloader"]["new_grid_shape"]
    else:
        # ... otherwise keep default value
        config["waffleiron"]["grids_size"] = config_pretrain["point_backbone"][
            "grid_shape"
        ]
    if config["dataloader"].get("new_fov") is not None:
        config["waffleiron"]["fov_xyz"] = config["dataloader"]["new_fov"]
    else:
        config["waffleiron"]["fov_xyz"] = config_pretrain["point_backbone"]["fov"]

    # --- Build network
    model = Segmenter(
        input_channels=config["embedding"]["size_input"],
        feat_channels=config["waffleiron"]["nb_channels"],
        depth=config["waffleiron"]["depth"],
        grid_shape=config["waffleiron"]["grids_size"],
        nb_class=config["classif"]["nb_class"],
        drop_path_prob=config["waffleiron"]["drop_path"],
        layer_norm=config["waffleiron"]["layernorm"],
    )

    args.batch_size = 2
    args.workers = 0

    # --- Build nuScenes dataset
    train_dataset, val_dataset = get_datasets(config, args)
    trn_loader, val_loader, _ = get_dataloader(train_dataset, val_dataset, args)
    
    # Load pretrained model
    ckpt = torch.load(args.pretrained_ckpt, map_location="cpu")
    ckpt = ckpt['net']
    new_ckpt = {}
    for k in ckpt.keys():
        if k.startswith("module"):
            new_ckpt[k[len("module.") :]] = ckpt[k]
        else:
            new_ckpt[k] = ckpt[k]

    # Adding classification layer
    model.classif = torch.nn.Conv1d(
        config["waffleiron"]["nb_channels"], config["waffleiron"]["pretrain_dim"], 1
    )

    classif = torch.nn.Conv1d(
        config["waffleiron"]["nb_channels"], config["classif"]["nb_class"], 1
    )
    torch.nn.init.constant_(classif.bias, 0)
    torch.nn.init.constant_(classif.weight, 0)
    model.classif = torch.nn.Sequential(
        torch.nn.BatchNorm1d(config["waffleiron"]["nb_channels"]),
        classif,
    )

    model.load_state_dict(new_ckpt)

    model = model.cuda()
    model.eval()

    for i, batch in enumerate(trn_loader):

        # Network inputs
        feat = batch["feat"].cuda()
        labels = batch["labels_orig"].cuda()
        batch["upsample"] = [
            up.cuda() for up in batch["upsample"]
        ]
        cell_ind = batch["cell_ind"].cuda()
        occupied_cell = batch["occupied_cells"].cuda()
        neighbors_emb = batch["neighbors_emb"].cuda()
        net_inputs = (feat, cell_ind, occupied_cell, neighbors_emb)

        # Get prediction and loss
        with torch.autocast("cuda"):

            with torch.no_grad():
                out, tokens = model(*net_inputs)
            # Upsample to original resolution
            out_upsample = []
            for id_b, closest_point in enumerate(batch["upsample"]):
                temp = out[id_b, :, closest_point]
                out_upsample.append(temp.T)
            # Loss

        # reconstruct point clouds
        for i in range(batch['feat'].shape[0]):
            predictions = out_upsample[i].argmax(dim=1)

        break

    # box branch 
    tokens_channels = tokens.shape[1]
    box_reg = torch.nn.Conv1d(tokens_channels, 7, 1)    # x, y, z, l, w, h, yaw
    box_reg = box_reg.half().cuda() # float16

    box_features = box_reg(tokens)
    print('box_features - B x C x N: ', box_features.shape)
    # print(box_features[1, :, 0])

    # instance branch
    K = 20
    instance_reg = torch.nn.Conv1d(tokens_channels, K, 1)    # K instances
    instance_reg = instance_reg.half().cuda() # float16

    instance_features = instance_reg(tokens).softmax(dim=1) 
    instance_class = instance_features.argmax(dim=1)
    print('instance_features - B x K x N: ', instance_features.shape)

    # box to point matching (dynamic to static)
    pcd = batch["feat"][-1, :, :out_upsample[-1].shape[0]].T.cuda()
    pred = out_upsample[1].argmax(dim=1)
    pcd = torch.cat((pcd, instance_class[-1].unsqueeze(1), pred.unsqueeze(1)), axis=1)
    _, mask = remove_ego_vehicle(pcd, "nuscenes")
    pcd, pred = pcd[mask], pred[mask]

    unique_vals, counts = torch.unique(pred, return_counts=True)
    max_class = unique_vals[torch.argmax(counts)]

    if False:
        for class_id in unique_vals:
            fig, ax = plt.subplots(1, 2)
            pts = pcd.cpu().numpy()
            mask = (pred == class_id).cpu().numpy()
            labels = get_clusters(pts[mask][:,:3], eps=args.eps, min_points=args.min_points)
            for i in range(len(labels)):
                labels[i] += 1
            colors = plt.cm.get_cmap('Set1', config["classif"]["nb_class"])
            pred_colors = colors(pred.cpu().numpy())
            colors = plt.get_cmap("Set1")(labels / (labels.max() if labels.max() > 0 else 1))
            ax[0].scatter(pts[mask][:,1], pts[mask][:,2], c=pred_colors[mask], s=0.1, marker='.', facecolors='r')
            ax[1].scatter(pts[mask][:,1], pts[mask][:,2], c=colors, s=0.1, marker='.', facecolors='r')
            fig.tight_layout()
            fig.savefig(f"figures/kitti_class_{class_id}.pdf", dpi=500)

    if False:  # visualize waffleiron predictions used for box-point fitting
        colors = plt.cm.get_cmap('tab20', config["classif"]["nb_class"])
        pred_colors = colors(pred.cpu().numpy())
        fig, ax = plt.subplots(1,2)
        mask = (pred == max_class).cpu().numpy()
        pts = pcd.cpu().numpy()
        ax[0].scatter(pts[:,1], pts[:,2], c=pred_colors, s=0.1, marker='.', facecolors='r')
        ax[1].scatter(pts[mask][:,1], pts[mask][:,2], c=pred_colors[mask], s=0.1, marker='.', facecolors='r')

        ax[0].set_title("Waffleiron predictions")
        ax[1].set_title(f"Most common class: {int(max_class.data)}")

        fig.tight_layout()
        fig.savefig('test.png', dpi=500)

    pts = pcd.cpu().numpy()
    pts = pts[:, [1, 2, 3]]
    import open3d as o3d
    # full = o3d.geometry.PointCloud()
    # full.points = o3d.utility.Vector3dVector(pts)
    # o3d.io.write_point_cloud("exports/full.ply", full)
    mask = (pred == 3).cpu().numpy()
    labels = get_clusters(pts[mask], eps=args.eps, min_points=args.min_points)
    ground_truth = pts[mask][labels == 0]
    for label in range(1, max(labels)+1):
        if label == -1:
            continue
        sample = pts[mask][labels == label]
        icp_res = icp_transform(ground_truth, sample)
        is_class = good_match(ground_truth, sample, icp_res, f"exports/cloud_{label}.ply")
        print(f"Object is same class: {is_class}\n")

    box = ...  # TODO: get box features
    box_r = torch.tensor(
        [
            box["tx_m"],
            box["ty_m"],
            box["tz_m"],
            box["length_m"],
            box["width_m"],
            box["height_m"],
            box["yaw"]
        ]
    )
    # get instance class of points in box
    box_points = get_points_in_box(pcd, box_r)
    unique_vals, counts = torch.unique(box_points[:, -1], return_counts=True)
    box_class = unique_vals[torch.argmax(counts)]

    # get clusters for points of same instance class
    class_pcd = pcd[box_class]
    labels = get_clusters(class_pcd, eps=args.eps, min_points=args.min_points)

    # find static clusters for dynamic object box
    for label in np.unique(labels):
        sample = class_pcd[labels == label]

        icp_res = icp_transform(box_points, sample)
        is_class = good_match(box_points, sample, icp_res)
        print(f"Object is same class: {is_class}")
