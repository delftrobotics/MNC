#!/usr/bin/python

# --------------------------------------------------------
# Multitask Network Cascade
# Modified from py-faster-rcnn (https://github.com/rbgirshick/py-faster-rcnn)
# Copyright (c) 2016, Haozhi Qi
# Licensed under The MIT License [see LICENSE for details]
# --------------------------------------------------------

# Standard module
import os
import argparse
import time
import cv2
import numpy as np
# User-defined module
import _init_paths
import caffe
from multitask_network_cascades.mnc_config import cfg
from multitask_network_cascades.transform.bbox_transform import clip_boxes
from multitask_network_cascades.utils.blob import prep_im_for_blob, im_list_to_blob
from multitask_network_cascades.transform.mask_transform import cpu_mask_voting, gpu_mask_voting
import matplotlib.pyplot as plt
from multitask_network_cascades.utils.vis_seg import _convert_pred_to_image, _get_voc_color_map
from PIL import Image

# VOC 20 classes
CLASSES = ('o')


def parse_args():
    """Parse input arguments."""
    parser = argparse.ArgumentParser(description='MNC demo')
    parser.add_argument('--gpu', dest='gpu_id', help='GPU device id to use [0]',
                        default=0, type=int)
    parser.add_argument('--cpu', dest='cpu_mode',
                        help='Use CPU mode (overrides --gpu)',
                        action='store_true')
    parser.add_argument('--disparity', action='store_true',
                        help='set to true when processing disparity')
    parser.add_argument('--visualize', action='store_true',
                        help='set to true to visualize detection result')
    parser.add_argument('--output-directory', dest='output',
                        help='pass the output directory to write processed images to disk',
                        type=str)
    parser.add_argument('--model', dest='model',
                        help='prototxt file defining the network model',
                        type=str)
    parser.add_argument('--weights', dest='weights',
                        help='caffemodel weights to test',
                        type=str)
    parser.add_argument('--image', dest='image',
                        help='input image to process',
                        type=str)
    parser.add_argument('--background', dest='background', help='Optionally choose another background for processed result',
                        default="",
                        type=str)

    args = parser.parse_args()
    return args


def prepare_mnc_args(im, net):
    # Prepare image data blob
    blobs = {'data': None}
    processed_ims = []
    im, im_scale_factors = \
        prep_im_for_blob(im, cfg.PIXEL_MEANS, cfg.TEST.SCALES[0], cfg.TRAIN.MAX_SIZE)
    processed_ims.append(im)
    blobs['data'] = im_list_to_blob(processed_ims)
    # Prepare image info blob
    im_scales = [np.array(im_scale_factors)]
    assert len(im_scales) == 1, 'Only single-image batch implemented'
    im_blob = blobs['data']
    blobs['im_info'] = np.array(
        [[im_blob.shape[2], im_blob.shape[3], im_scales[0]]],
        dtype=np.float32)
    # Reshape network inputs and do forward
    net.blobs['data'].reshape(*blobs['data'].shape)
    net.blobs['im_info'].reshape(*blobs['im_info'].shape)
    forward_kwargs = {
        'data': blobs['data'].astype(np.float32, copy=False),
        'im_info': blobs['im_info'].astype(np.float32, copy=False)
    }
    return forward_kwargs, im_scales


def im_detect(im, net):
    forward_kwargs, im_scales = prepare_mnc_args(im, net)
    blobs_out = net.forward(**forward_kwargs)
    # output we need to collect:
    # 1. output from phase1'
    rois = net.blobs['rois'].data.copy()
    masks = net.blobs['mask_proposal'].data[...]
    scores = net.blobs['seg_cls_prob'].data[...]
    # 2. output from phase2
    if "rois_ext" in net.blobs:
        rois_phase2 = net.blobs['rois_ext'].data[...]
        rois = np.concatenate((rois, rois_phase2), axis=0)
    if "mask_proposal_ext" in net.blobs:
        masks_phase2 = net.blobs['mask_proposal_ext'].data[...]
        masks = np.concatenate((masks, masks_phase2), axis=0)
    if "seg_cls_prob_ext" in net.blobs:
        scores_phase2 = net.blobs['seg_cls_prob_ext'].data[...]
        scores = np.concatenate((scores, scores_phase2), axis=0)
    # Boxes are in resized space, we un-scale them back
    rois = rois[:, 1:5] / im_scales[0]
    rois, _ = clip_boxes(rois, im.shape)
    # concatenate two stages to get final network output
    return masks, rois, scores


def get_vis_dict(result_box, result_mask, img_name, cls_names, vis_thresh=0.5):
    box_for_img = []
    mask_for_img = []
    cls_for_img = []
    for cls_ind, cls_name in enumerate(cls_names):
        det_for_img = result_box[cls_ind]
        seg_for_img = result_mask[cls_ind]
        keep_inds = np.where(det_for_img[:, -1] >= vis_thresh)[0]
        for keep in keep_inds:
            box_for_img.append(det_for_img[keep])
            mask_for_img.append(seg_for_img[keep][0])
            cls_for_img.append(cls_ind + 1)
    res_dict = {'image_name': img_name,
                'cls_name': cls_for_img,
                'boxes': box_for_img,
                'masks': mask_for_img}
    return res_dict

if __name__ == '__main__':

    args = parse_args()

    caffe.set_mode_gpu()
    caffe.set_device(args.gpu_id)
    net = caffe.Net(args.model, caffe.TEST, weights=args.weights)

    # Allocate memory to speed things up
    im = 128 * np.ones((300, 500, 3), dtype=np.float32)
    for i in range(2):
        _, _, _ = im_detect(im, net)

    print('~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~')
    if args.disparity:
        print("Using the 'disparity' setting for reading the image.")
        im = cv2.imread(args.image, cv2.IMREAD_UNCHANGED)
        visualization = cv2.imread(args.image)
        if args.background:
            visualization = cv2.imread(args.background)
        im = im.astype(np.int16)
        im[im < -5000] = -500
        im = im.astype(np.float32, copy=False)
        im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
    else:
        im = cv2.imread(args.image)

    start = time.time()
    masks, boxes, scores = im_detect(im, net)
    end = time.time()
    print('forward time {}'.format(end-start))
    result_mask, result_box = cpu_mask_voting(masks, boxes, scores, len(CLASSES) + 1,
                                              100, im.shape[1], im.shape[0])
    pred_dict = get_vis_dict(result_box, result_mask, args.image, CLASSES)

    img_width = im.shape[1]
    img_height = im.shape[0]
    inst_img, cls_img = _convert_pred_to_image(img_width, img_height, pred_dict)
    color_map = _get_voc_color_map()
    cls_out_img = np.zeros((img_height, img_width, 3))
    for i in range(img_height):
        for j in range(img_width):
            cls_out_img[i][j] = color_map[cls_img[i][j]][::-1]
    visualization = visualization * 0.5 + cls_out_img * 0.5

    if args.visualize:
        gray = cv2.cvtColor(visualization.astype(np.uint16), cv2.COLOR_BGR2GRAY)
        min_val, max_val = cv2.minMaxLoc(gray)[0:2]
        cv2.imshow("processed", (visualization - min_val) / max_val)
        cv2.waitKey(0)

    if args.output:
        tail = os.path.split(args.image)[1]
        output_file = os.path.join(args.output, tail + 'processed.png')
        print("Writing output to: '", output_file, "'.")
        cv2.imwrite(output_file, visualization)

