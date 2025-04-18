from typing import Optional, Tuple

import torch
from scipy.optimize import linear_sum_assignment

from utils.misc import Obj_cache, Instance_data, get_centers_for_class


def association(
    points_t1: torch.Tensor,
    points_t2: torch.Tensor,
    config: dict,
    prev_ind: Optional[torch.Tensor] = None,
    ind_cache: Optional[Obj_cache] = None,
    flow: Optional[torch.Tensor] = None,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    This function performs association between two sets of points.
    It uses the Hungarian algorithm to find the optimal assignment of points
    from the first set to the second set based on distance of centers of clusters.

    Args:
        points_t1 (torch.Tensor): Data for time t, ego compensated xyz + features + semantic class + cluster id.
        points_t2 (torch.Tensor): Data for time t+1, ego compensated xyz + features + semantic class + cluster id.
        config (dict): Configuration dictionary containing parameters for association.
        prev_ind (Optional[torch.Tensor]): Previous indices for association.
        ind_cache (Optional[dict]): Cache for previous indices.
        flow (Optional[torch.Tensor]): Flow tensor for adjusting point positions.

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: The updated indices for points in both sets.
    """
    indices_t1 = torch.zeros(points_t1.shape[0], dtype=torch.int32)
    indices_t2 = torch.zeros(points_t2.shape[0], dtype=torch.int32)

    curr_id = 1 if ind_cache is None else ind_cache.max_id + 1

    for class_id in config["fore_classes"]:
        # Get the centers of clusters for the current class
        centers_t1, clusters_t1 = get_centers_for_class(points_t1, class_id)
        centers_t2, clusters_t2 = get_centers_for_class(points_t2, class_id)

        # If flow is provided, adjust the centers of t1
        if flow is not None:
            flow_t1, _ = get_centers_for_class(points_t1, class_id, flow)
            centers_t1 = centers_t1 + flow_t1

        # If no clusters are found, continue to the next class
        if clusters_t1.numel() == 0 and clusters_t2.numel() == 0:
            continue

        class_mask_t1 = points_t1[:, -2] == class_id
        class_mask_t2 = points_t2[:, -2] == class_id

        # If no clusters are found in t1, assign new ids to t2
        if clusters_t1.numel() == 0:
            for cluster_id in clusters_t2:
                mask = (class_mask_t2) & (points_t2[:, -1] == cluster_id)
                indices_t2[mask] = curr_id
                curr_id += 1
            continue

        # If no clusters are found in t2, assign ids to t1
        if clusters_t2.numel() == 0:
            for cluster_id in clusters_t1:
                mask = (class_mask_t1) & (points_t1[:, -1] == cluster_id)
                if prev_ind is None:  # if prev_ind is not provided, assign new ids
                    indices_t1[mask] = curr_id
                    curr_id += 1
                else:  # if prev_ind is provided, assign previous ids
                    indices_t1[mask] = prev_ind[mask][0]
            continue

        dists = torch.cdist(centers_t1, centers_t2)
        # associate using hungarian matching
        row_ind, col_ind = linear_sum_assignment(dists.cpu().numpy())
        used_row, used_col = set(row_ind), set(col_ind)
        for i, j in zip(row_ind, col_ind):
            mask_t1 = (class_mask_t1) & (points_t1[:, -1] == clusters_t1[i])
            mask_t2 = (class_mask_t2) & (points_t2[:, -1] == clusters_t2[j])
            if dists[i, j] > config["association"]["max_dist"]:  # threshold for association
                indices_t1[mask_t1] = (
                    curr_id if prev_ind is None else prev_ind[mask_t1][0]
                )
                curr_id += 1 if prev_ind is None else 0
                indices_t2[mask_t2] = curr_id
                curr_id += 1
            else:
                id_val = curr_id if prev_ind is None else prev_ind[mask_t1][0]
                indices_t1[mask_t1] = id_val
                indices_t2[mask_t2] = id_val
                curr_id += 1 if prev_ind is None else 0

        # Handle the case where the number of clusters in t1 and t2 are different
        if centers_t1.shape[0] > centers_t2.shape[0]:
            for i, cluster_id in enumerate(clusters_t1):
                if i in used_row:
                    continue
                mask = (class_mask_t1) & (points_t1[:, -1] == cluster_id)
                if prev_ind is None:
                    indices_t1[mask] = curr_id
                    curr_id += 1
                else:
                    indices_t1[mask] = prev_ind[mask][0]
        elif centers_t1.shape[0] < centers_t2.shape[0]:
            for j, cluster_id in enumerate(clusters_t2):
                if j in used_col:
                    continue
                mask = (class_mask_t2) & (points_t2[:, -1] == cluster_id)
                indices_t2[mask] = curr_id
                curr_id += 1

    indices_t1 = indices_t1.to(points_t1.device)
    indices_t2 = indices_t2.to(points_t2.device)
    return indices_t1, indices_t2

def long_association(
    points_t1: torch.Tensor,
    points_t2: torch.Tensor,
    config: dict,
    prev_ind: Optional[torch.Tensor] = None,
    obj_cache: Optional[Obj_cache] = None,
    flow: Optional[torch.Tensor] = None,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    This function performs long-term association between two sets of points.
    It uses the Hungarian algorithm to find the optimal assignment of points
    from the first set to the second set based on association cost, which includes
    distance and other features.

    Args:
        points_t1 (torch.Tensor): Data for time t, ego compensated xyz + features + semantic class + cluster id.
        points_t2 (torch.Tensor): Data for time t+1, ego compensated xyz + features + semantic class + cluster id.
        config (dict): Configuration dictionary containing parameters for association.
        prev_ind (Optional[torch.Tensor]): Previous indices for association.
        obj_cache (Optional[dict]): Cache for previous objects.
        flow (Optional[torch.Tensor]): Flow tensor for adjusting point positions.

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: The updated indices for points in both sets.
    """
    # Similar to the previous association function, but with different logic
    # for handling long-term associations

    # Update object cache -> remove old instances
    obj_cache.update_step()

    # Initialize indices for t1 and t2
    indices_t1 = torch.zeros(points_t1.shape[0], dtype=torch.int32)
    indices_t2 = torch.zeros(points_t2.shape[0], dtype=torch.int32)

    curr_id = 1 if obj_cache is None else obj_cache.max_id + 1

    for class_id in config["fore_classes"]:
        # Get the centers of clusters for the current class
        centers_t1, clusters_t1 = get_centers_for_class(points_t1, class_id)
        centers_t2, clusters_t2 = get_centers_for_class(points_t2, class_id)

        # If flow is provided, adjust the centers of t1
        if flow is not None:
            flow_t1, _ = get_centers_for_class(points_t1, class_id, flow)
            centers_t1 = centers_t1 + flow_t1
        else:
            flow_t1 = torch.zeros_like(centers_t1)

        # Get the features for the current class
        features_t1, _ = get_centers_for_class(points_t1, class_id, points_t1[:, 3:-2])
        features_t2, _ = get_centers_for_class(points_t2, class_id, points_t2[:, 3:-2])

        # If no clusters are found, continue to the next class
        if clusters_t1.numel() == 0 and clusters_t2.numel() == 0:
            continue

        class_mask_t1 = points_t1[:, -2] == class_id
        class_mask_t2 = points_t2[:, -2] == class_id

        # If no clusters are found in t1, assign new ids to t2
        if clusters_t1.numel() == 0:
            for i, cluster_id in enumerate(clusters_t2):
                mask = (class_mask_t2) & (points_t2[:, -1] == cluster_id)
                indices_t2[mask] = curr_id
                instance = Instance_data(
                    id=curr_id,
                    life=config["association"]["life"],
                    center=centers_t2[i],
                    feature=features_t2[i],
                )
                obj_cache.add_instance(class_id, instance)
                curr_id += 1
            continue

        prev_insts = obj_cache.prev_instances[class_id]
        centers_t1_o = centers_t1.clone()
        if len(prev_insts) == 0:
            for i in range(len(clusters_t1)):
                new_inst = Instance_data(
                    id=curr_id,
                    life=config["association"]["life"] - 1,
                    center=centers_t1[i],
                    feature=features_t1[i],
                )
                obj_cache.add_instance(class_id, new_inst)
                curr_id += 1
        else:
            features_t1 = torch.stack(
                [prev_insts[cluster_id].feature for cluster_id in prev_insts.keys()]
            )
            centers_t1 = torch.stack(
                [prev_insts[cluster_id].center for cluster_id in prev_insts.keys()]
            )
        prev_insts_keys = list(prev_insts.keys())

        # If no clusters are found in t2, assign ids to t1
        if clusters_t2.numel() == 0:
            dists = torch.cdist(centers_t1_o, centers_t1, p=2)
            flow_dist = torch.norm(flow_t1, dim=1)
            for i in range(len(clusters_t1)):
                min_dist = torch.argmin(dists[i])
                if dists[i, min_dist] < (flow_dist[i] + 1e-4) and \
                   prev_insts[prev_insts_keys[min_dist]].life == config["association"]["life"] - 1:
                    prev_inst = prev_insts[prev_insts_keys[min_dist]]
                    mask = (class_mask_t1) & (points_t1[:, -1] == prev_inst.id)
                    indices_t1[mask] = prev_inst.id
                else:
                    # Diagnostic prints
                    print(f"Index i: {i}")
                    print(f"min_dist index: {min_dist}")
                    print(f"Distance from dists[i, min_dist]: {dists[i, min_dist].item()}")
                    print(f"dist[i]: {dists[i]}")
                    print(f"centers_t1_o[i]: {centers_t1_o[i]}")
                    print(f"centers_t1[min_dist]: {centers_t1[min_dist]}")
                    # Compute single-pair distance
                    cdist_result = torch.cdist(centers_t1_o[i].unsqueeze(0), centers_t1[min_dist].unsqueeze(0), p=2)
                    print(f"cdist result: {cdist_result.item()}")
                    # Manual Euclidean distance for verification
                    manual_dist = torch.sqrt(torch.sum((centers_t1_o[i] - centers_t1[min_dist]) ** 2))
                    print(f"Manual Euclidean distance: {manual_dist.item()}")
                    # Check shapes and dtypes
                    print(f"Shape of centers_t1_o: {centers_t1_o.shape}")
                    print(f"Shape of centers_t1: {centers_t1.shape}")
                    print(f"Shape of dists: {dists.shape}")
                    print(f"Dtype of centers_t1_o: {centers_t1_o.dtype}")
                    print(f"Dtype of centers_t1: {centers_t1.dtype}")
                    # Check if tensors require gradients
                    print(f"centers_t1_o requires_grad: {centers_t1_o.requires_grad}")
                    print(f"centers_t1 requires_grad: {centers_t1.requires_grad}")
                    exit()
            continue

        # Calculate the association cost
        cost_dists = torch.cdist(centers_t1, centers_t2)
        cost_dists[cost_dists > config["association"]["max_dist"]] = 1e8

        features_t1_n = features_t1 / (torch.norm(features_t1, dim=1, keepdim=True) + 1e-6)
        features_t2_n = features_t2 / (torch.norm(features_t2, dim=1, keepdim=True) + 1e-6)
        cost_features = 1 - torch.matmul(features_t1_n, features_t2_n.T)  # cosine similarity
        cost_features[cost_features > config["association"]["max_feat"]] = 1e8

        assoc_cost = cost_dists + cost_features

        # associate using hungarian matching
        row_ind, col_ind = linear_sum_assignment(assoc_cost.cpu().numpy())
        used_row, used_col = set(row_ind), set(col_ind)
        add_instances = []

        for row, col in zip(row_ind, col_ind):
            prev_inst = prev_insts[prev_insts_keys[row]]
            mask_t1 = (class_mask_t1) & (points_t1[:, -1] == prev_inst.id)
            mask_t2 = (class_mask_t2) & (points_t2[:, -1] == clusters_t2[col])
            if assoc_cost[row, col] < 1e8:
                indices_t1[mask_t1] = prev_inst.id
                indices_t1[mask_t1] = prev_inst.id
                new_inst = Instance_data(
                    id=prev_inst.id,
                    life=config["association"]["life"],
                    center=centers_t2[col],
                    feature=(features_t2[col] + features_t1[row]) / 2,
                )
                add_instances.append(new_inst)
            else:
                indices_t1[mask_t1] = prev_inst.id
                indices_t2[mask_t2] = curr_id
                new_inst = Instance_data(
                    id=curr_id,
                    life=config["association"]["life"],
                    center=centers_t2[col],
                    feature=features_t2[col],
                )
                add_instances.append(new_inst)
                curr_id += 1

        # Handle the case where the number of clusters in t1 and t2 are different
        if centers_t1.shape[0] > centers_t2.shape[0]:
            for i in range(len(centers_t1)):
                if i in used_row:
                    continue
                instance = prev_insts[prev_insts_keys[i]]
                mask = (class_mask_t1) & (points_t1[:, -1] == instance.id)
                indices_t1[mask] = instance.id
        elif centers_t1.shape[0] < centers_t2.shape[0]:
            for j, cluster_id in enumerate(clusters_t2):
                if j in used_col:
                    continue
                mask = (class_mask_t2) & (points_t2[:, -1] == cluster_id)
                indices_t2[mask] = curr_id
                new_inst = Instance_data(
                    id=curr_id,
                    life=config["association"]["life"],
                    center=centers_t2[j],
                    feature=features_t2[j],
                )
                add_instances.append(new_inst)
                curr_id += 1

        # Update the object cache with new instances
        for inst in add_instances:
            obj_cache.add_instance(class_id, inst)

    indices_t1 = indices_t1.to(points_t1.device)
    indices_t2 = indices_t2.to(points_t2.device)
    return indices_t1, indices_t2
