import argparse
import os
import os.path as osp
from collections import Counter

import cv2
import numpy as np
import pandas as pd
import surface_distance
import tqdm
from bao.config import system_config
from bao.metrics import mask_utils
from bao.metrics.ssim import ssim
from bao.metrics.utils import *
from scipy.ndimage.measurements import label
from scipy.spatial.distance import directed_hausdorff
from sklearn import metrics


def intersection_and_union(img1, img2):
    """
    Arguments
    ---------

    img1, img2  (np.ndarray) : Boolean np.ndarrays
    """
    intersection = img1 & img2
    union = img1 | img2
    return intersection, union


def iou(intersection, union):
    return np.sum(intersection) / np.sum(union)


def iomin(intersection, img1, img2):
    area_min = min(img1.sum(), img2.sum())
    return np.sum(intersection) / area_min


def iomax(intersection, img1, img2):
    area_max = max(img1.sum(), img2.sum())
    return np.sum(intersection) / area_max


def dice(intersection, img1, img2, smooth=1):
    area_sum = img1.sum() + img2.sum()
    return (2 * np.sum(intersection) + smooth) / (area_sum + smooth)


def inter_over_metrics(img1, img2, single_metric=False):
    """
    Arguments
    ---------

    img1, img2  (np.ndarray) : Boolean np.ndarrays
    """
    intersection, union = intersection_and_union(img1, img2)
    if single_metric:
        tmp = {"dice": dice(intersection, img1, img2)}
    else:
        tmp = {
            "iou": iou(intersection, union),
            "iomin": iomin(intersection, img1, img2),
            "iomax": iomax(intersection, img1, img2),
            "dice": dice(intersection, img1, img2),
        }
    return tmp


def binary_feature(img_expert, img_model):
    """
    Arguments
    ---------

    img_expert, img_model  (np.ndarray) : Boolean np.ndarrays
    """
    gt = np.sum(img_expert) > 0
    pred = np.sum(img_model) > 0
    tmp = {
        "true": gt == pred,
        "positive_gt": gt,
    }
    return tmp


def accuracy_features(img_expert, img_model):
    structure = np.ones((3, 3), dtype=np.int)
    labeled, ncomponents = label(img_expert, structure)
    labeled_model, ncomponents_model = label(img_model, structure)

    tmp = {
        "ncomponents": ncomponents,
        "ncomponents_model": ncomponents_model,
        "ncomponents_abs_diff": np.abs(ncomponents - ncomponents_model),
    }

    all_labels = [(i + 1) for i in range(ncomponents)]
    pred_labels = [(i + 1) for i in range(ncomponents_model)]
    ious = {true_label: 0.0 for true_label in all_labels}
    ious_pred = {pred_label: 0.0 for pred_label in pred_labels}
    for pred_label in pred_labels:
        correct_pred = labeled[np.bitwise_and(labeled > 0, labeled_model == pred_label)]
        intersection = Counter(correct_pred)
        for true_label in np.unique(correct_pred):
            union = np.sum(np.bitwise_or(labeled == true_label, labeled_model == pred_label))
            iou = intersection[true_label] / union
            if iou > ious.get(true_label):
                ious[true_label] = iou
            if iou > ious_pred.get(pred_label):
                ious_pred[pred_label] = iou

    iou_thresholds = (0.0, 0.25, 0.5)
    for iou_threshold in iou_thresholds:
        tp = len([obj_label for obj_label in all_labels if ious[obj_label] > iou_threshold])
        fp = len([obj_label for obj_label in pred_labels if ious_pred[obj_label] <= iou_threshold])
        recall = 1.0 if ncomponents == 0 else tp / ncomponents
        precision = 1.0 if (tp + fp) == 0 else tp / (tp + fp)
        tmp[f"recall_{iou_threshold}"] = recall
        tmp[f"precision_{iou_threshold}"] = precision
        if precision + recall == 0.0:
            tmp[f"f1_{iou_threshold}"] = 0.0
        else:
            tmp[f"f1_{iou_threshold}"] = (2 * precision * recall) / (precision + recall)

    return tmp


def accuracy_features_legacy(img_expert, img_model):
    structure = np.ones((3, 3), dtype=np.int)
    labeled, ncomponents = label(img_expert, structure)
    labeled_model, ncomponents_model = label(img_model, structure)

    tmp = {}

    correct_pred = labeled[np.bitwise_and(labeled > 0, img_model > 0)]
    intersection = Counter(correct_pred)
    union = Counter(labeled[np.bitwise_or(labeled > 0, img_model > 0)])
    true_labels = np.unique(correct_pred)
    ious = {}
    for obj_label in true_labels:
        ious[obj_label] = intersection.get(obj_label, 0.0) / union.get(obj_label, 0.01)

    iou_thresholds = (0.0, 0.25)
    for iou_threshold in iou_thresholds:
        tp = len([obj_label for obj_label in true_labels if ious[obj_label] > iou_threshold])
        recall = 1.0 if ncomponents == 0 else tp / ncomponents
        precision = 1.0 if max(ncomponents_model, ncomponents) == 0 else tp / max(ncomponents_model, ncomponents)
        if precision + recall == 0.0:
            tmp[f"leg_f1_{iou_threshold}"] = 0.0
        else:
            tmp[f"leg_f1_{iou_threshold}"] = (2 * precision * recall) / (precision + recall)

    return tmp


def hausdorff_distance(img_expert, img_model):
    """
    Arguments
    ---------

    img_expert, img_model  (np.ndarray) : Boolean np.ndarrays
    """
    tmp = {
        "hausdorff": directed_hausdorff(img_expert, img_model, seed=24)[0],
        "hausdorff_inv": directed_hausdorff(img_model, img_expert, seed=24)[0],
    }
    return tmp


def ssims(img_expert, img_model):
    """
    Arguments
    ---------

    img_expert, img_model  (np.ndarray) : Boolean np.ndarrays
    """
    tmp = {"ssim": ssim(img_expert, img_model).mean()}
    return tmp


def surface_distances(img_expert, img_model):
    surface_distances = surface_distance.compute_surface_distances(img_expert, img_model, (0.1, 0.1))
    robust_hausdorff = surface_distance.compute_robust_hausdorff(surface_distances, 95)
    dist, dist_inv = surface_distance.compute_average_surface_distance(surface_distances)
    dice_at_tolerance = surface_distance.compute_surface_dice_at_tolerance(surface_distances, tolerance_mm=1.0)
    tmp = {
        "dist": dist,
        "dist_inv": dist_inv,
        "robust_hausdorff": robust_hausdorff,
        "dice_at_tolerance": dice_at_tolerance,
    }
    return tmp


def area_features(img_expert, img_model):
    area_expert = img_expert.sum()
    area_model = img_model.sum()
    tmp = {"area_abs_diff": np.abs(area_expert - area_model), "area_expert": area_expert, "area_model": area_model}
    return tmp


def pixel_accuracy_features(img_expert, img_model):
    expert_arr = img_expert.flatten()
    model_arr = img_model.flatten()
    tmp = {
        "pixel_accuracy": metrics.accuracy_score(expert_arr, model_arr),
        "pixel_recall": metrics.recall_score(expert_arr, model_arr),
        "pixel_precision": metrics.precision_score(expert_arr, model_arr),
        "pixel_f1": metrics.f1_score(expert_arr, model_arr),
    }
    return tmp


def area_out_of_lungs(img_origin, img_expert, img_model):
    # Calculate part of mask out from lungs
    lungs_mask_union = lungs_finder_segmentator(img_origin)
    out_of_lungs_union = area_out_of(lungs_mask_union, img_model)

    lungs_mask_lr = lungs_finder_segmentator(img_origin, is_union=False)
    out_of_lungs_lr = area_out_of(lungs_mask_lr, img_model)

    tmp = {
        "out_of_lungs_lr": out_of_lungs_lr,
        "out_of_lungs_union": out_of_lungs_union,
    }
    return tmp


def positional_features(img_origin, img_expert, img_model):

    lungs_mask_union = np.array(lungs_finder_segmentator(img_origin), dtype=np.uint8)
    img_expert = img_expert.astype(np.uint8)
    img_model = img_model.astype(np.uint8)

    x_e, y_e = get_center_of_mass(img_expert)
    x_m, y_m = get_center_of_mass(img_model)
    x_l, y_l = get_center_of_mass(lungs_mask_union)

    centers_e = get_centers_of_mass(img_expert)
    centers_m = get_centers_of_mass(img_model)

    dists = get_nearest_neighbor_dist(centers_e, centers_m)
    w, h = get_lungs_size(img_origin)
    max_dist = (w ** 2 + h ** 2) ** 0.5

    mean_centroid_dist = np.mean(dists) / max_dist if len(dists) > 0 else 0
    max_centroid_dist = np.max(dists) / max_dist if len(dists) > 0 else 0
    min_centroid_dist = np.min(dists) / max_dist if len(dists) > 0 else 0

    tmp = {
        "x_diff_center": np.abs(x_m - x_e) / w,
        "y_diff_center": np.abs(y_m - y_e) / h,
        "x_diff_center_lungs": np.abs(x_l - x_m) / w,
        "y_diff_center_lungs": np.abs(y_l - y_m) / h,
        "mean_centroid_dist": mean_centroid_dist,
        "max_centroid_dist": max_centroid_dist,
        "min_centroid_dist": min_centroid_dist,
    }
    return tmp


def _read_png(fpath):
    return cv2.imread(fpath)[:, :, ::-1]


def _read_mask(fpath):
    mask = cv2.imread(fpath).astype(np.bool)
    if len(mask.shape) == 3:
        mask = mask[:, :, 0]
    return mask


def read_files(args):
    fnames = [osp.splitext(fpath)[0] for fpath in os.listdir(args.folder_origin)]
    data = []
    for fname in fnames:
        data.append(
            {
                "fname": fname,
                "orig": _read_png(osp.join(args.folder_origin, f"{fname}.png")),
                "expert": _read_mask(osp.join(args.folder_expert, f"{fname}_expert.png")),
                "s1": _read_mask(osp.join(args.folder_1, f"{fname}_s1.png")),
                "s2": _read_mask(osp.join(args.folder_2, f"{fname}_s2.png")),
                "s3": _read_mask(osp.join(args.folder_3, f"{fname}_s3.png")),
            }
        )
    return data


def prepare_markup(fpath):
    mark = pd.read_csv(fpath)
    markup = pd.wide_to_long(mark, stubnames="Sample ", i="Case", j="sample_name").reset_index()
    markup = markup.rename(columns={"Sample ": "y"})
    markup["sample_name"] = markup["sample_name"].astype(str)
    markup["fname"] = markup["Case"].map(lambda x: osp.splitext(x)[0])
    markup["id"] = markup[["fname", "sample_name"]].agg("_".join, axis=1)
    return markup[["id", "y"]]


def _add_key_postfix(dictionary, postfix):
    """
    Adds postfix to every key in dictionary
    """
    key_pairs = {old_key: f"{old_key}{postfix}" for old_key in list(dictionary.keys())}
    new_dict = {}
    for old_key, value in dictionary.items():
        new_dict[key_pairs[old_key]] = value
    return new_dict


def calc_metrics(data_expert, data_nn, data_orig, form_dict=None, gt="expert"):
    tmp = {"gt": gt}
    if form_dict:
        form_mode = "original"

    for metric in [
        "inter_over_metrics",
        "binary_feature",
        "hausdorff_distance",
        "ssims",
        "accuracy_features",
        "accuracy_features_legacy",
        "surface_distances",
        "area_features",
        "area_out_of_lungs",
        "positional_features",
        "pixel_accuracy_features",
    ]:
        if metric in [
            "inter_over_metrics",
            "binary_feature",
            "hausdorff_distance",
            "ssims",
            "accuracy_features",
            "accuracy_features_legacy",
            "surface_distances",
            "area_features",
            "pixel_accuracy_features",
        ]:
            tmp.update(eval(metric)(data_expert, data_nn))

        if metric in [
            "area_out_of_lungs",
            "positional_features",
        ]:
            tmp.update(eval(metric)(data_orig, data_expert, data_nn))

        if metric in ["inter_over_metrics"] and form_mode == "original":
            tmp_tmp = eval(metric)(form_dict["expert_ellipse"], form_dict["model_ellipse"], single_metric=True)
            tmp_tmp = _add_key_postfix(tmp_tmp, "_el")
            tmp.update(tmp_tmp)
            tmp_tmp = eval(metric)(form_dict["expert_rect"], form_dict["model_rect"], single_metric=True)
            tmp_tmp = _add_key_postfix(tmp_tmp, "_rect")
            tmp.update(tmp_tmp)

    return tmp


def get_metrics(data, markup=None, form_mode="original"):
    """
    Arguments
    ---------

    data    (list) : list of dicts {
                    "fname": str,
                    "orig": RGB 3-channel image,
                    "expert", "m_1", "m_2", "m_3": 2D boolean arrays
                    }
    form_mode   (str) : If `original`, add features for ellipses and
                    rectangles for selected metrics, if `rect` -
                    generate features for rectangle masks,
                    if `ellipse` - generate features for ellipsoid masks
    """

    out_data = []
    sample_name_dict = {"s1": "1", "s2": "2", "s3": "3"}
    for data_dict in tqdm.tqdm(data, desc="Generating metrics"):
        if form_mode in ["rect", "ellipse"]:
            for markup_key in ["expert", "s1", "s2", "s3"]:
                if form_mode == "rect":
                    data_dict[markup_key] = mask_utils.convert_to_rectangles(data_dict[markup_key])
                elif form_mode == "ellipse":
                    data_dict[markup_key] = mask_utils.convert_to_ellipses(data_dict[markup_key])

        form_dict = {}
        if form_mode == "original":
            form_dict["expert_ellipse"] = mask_utils.convert_to_ellipses(data_dict["expert"])
            form_dict["expert_rect"] = mask_utils.convert_to_rectangles(data_dict["expert"])

        for s_key in ["s1", "s2", "s3"]:

            tmp = {
                "id": f"{data_dict['fname']}_{sample_name_dict[s_key]}",
                "fname": data_dict["fname"],
                "sample_name": sample_name_dict[s_key],
            }

            if form_mode == "original":
                form_dict["model_ellipse"] = mask_utils.convert_to_ellipses(data_dict[s_key])
                form_dict["model_rect"] = mask_utils.convert_to_rectangles(data_dict[s_key])

            # Check similarity of scores and generate new features if exist model having score 5
            # If there are several models with score 5, their intersection is not removed now
            if not isinstance(markup, type(None)) and tmp["id"] in markup["id"].values:
                index = pd.Index(markup["id"]).get_loc(tmp["id"])
                y = markup["y"][index]
                tmp.update({"y": y})
                if y == 5:
                    interest_samples = ["s1", "s2", "s3"]
                    interest_samples.remove(s_key)
                    for interest_s_key in interest_samples:
                        interest_tmp = tmp.copy()
                        interest_tmp.update(
                            calc_metrics(
                                data_dict[s_key],
                                data_dict[interest_s_key],
                                data_dict["orig"],
                                form_dict,
                                gt=tmp["sample_name"],
                            )
                        )
                        out_data.append(interest_tmp)

            tmp.update(calc_metrics(data_dict["expert"], data_dict[s_key], data_dict["orig"], form_dict))
            out_data.append(tmp)

    return pd.DataFrame(out_data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Gather metrics")

    parser.add_argument("--task_name", default="sample")

    parser.add_argument("--folder_origin", default=osp.join(system_config.data_dir, "Dataset", "Origin"))
    parser.add_argument("--folder_expert", default=osp.join(system_config.data_dir, "Dataset", "Expert"))
    parser.add_argument("--folder_1", default=osp.join(system_config.data_dir, "Dataset", "sample_1"))
    parser.add_argument("--folder_2", default=osp.join(system_config.data_dir, "Dataset", "sample_2"))
    parser.add_argument("--folder_3", default=osp.join(system_config.data_dir, "Dataset", "sample_3"))

    parser.add_argument("--markup", default=osp.join(system_config.data_dir, "Dataset", "OpenPart.csv"))
    parser.add_argument("--add_markup", action="store_true")
    parser.add_argument(
        "--form_mode",
        default="original",
        help="If `original`, add features for ellipses and rectangles for selected "
        + "metrics, if `rect` generate features for rectangle masks, if `ellipse`"
        + " generate features for ellipsoid masks",
    )

    parser.add_argument("--output_dir", default=osp.join(system_config.data_dir, "interim"))
    parser.add_argument("--regenerate_data", action="store_true")

    args = parser.parse_args()

    output_file = osp.join(args.output_dir, f"{args.task_name}.csv")
    if not osp.exists(output_file) or args.regenerate_data:
        data = read_files(args)

        if args.add_markup:
            markup = prepare_markup(args.markup)
            metrics_df = get_metrics(data, markup, form_mode=args.form_mode)
        else:
            metrics_df = get_metrics(data, form_mode=args.form_mode)

        os.makedirs(args.output_dir, exist_ok=True)
        metrics_df.to_csv(output_file, index=False)
