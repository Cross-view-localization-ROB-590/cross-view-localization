
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import torch
from torchvision import transforms
import utils
import os
import torchvision.transforms.functional as TF

# from GRU1 import ElevationEsitimate,VisibilityEsitimate,VisibilityEsitimate2,GRUFuse
from VGG import VGGUnet, VGGUnet_G2S
from jacobian import grid_sample

# from ConvLSTM import VE_LSTM3D, VE_LSTM2D, VE_conv, S_LSTM2D
from models_ford import loss_func
from RNNs import NNrefine

import math

from torchviz import make_dot

EPS = utils.EPS


# Import transformer modules
from transformer import *
from transformer.cross_view_transformer.model.model_module import ModelModule
import hydra
from hydra.utils import instantiate
from omegaconf import OmegaConf, DictConfig
from torchmetrics import MetricCollection
from pathlib import Path
from transformer.losses import MultipleLoss
from collections.abc import Callable
from typing import Tuple, Dict, Optional

import matplotlib.pyplot as plt
from torchvision.utils import save_image

# -----------------------------------

class LM_G2SP(nn.Module):
    def __init__(self, args):  # device='cuda:0',
        super(LM_G2SP, self).__init__()
        '''
        loss_method: 0: direct R T loss 1: feat loss 2: noise aware feat loss
        '''
        self.args = args.highlyaccurate
        
        self.level = args.level
        self.N_iters = args.N_iters
        self.using_weight = args.using_weight
        self.loss_method = args.loss_method

        self.SatFeatureNet = VGGUnet(self.level)
        if self.args.proj == 'nn':
            self.GrdFeatureNet = VGGUnet_G2S(self.level)
        else:
            self.GrdFeatureNet = VGGUnet(self.level)

        self.damping = nn.Parameter(self.args.damping * torch.ones(size=(1, 3), dtype=torch.float32, requires_grad=True))

        self.meters_per_pixel = []
        meter_per_pixel = utils.get_meter_per_pixel()
        for level in range(4):
            self.meters_per_pixel.append(meter_per_pixel * (2 ** (3 - level)))



        torch.autograd.set_detect_anomaly(True)
        # Running the forward pass with detection enabled will allow the backward pass to print the traceback of the forward operation that created the failing backward function.
        # Any backward computation that generate “nan” value will raise an error.

    def get_warp_sat2real(self, satmap_sidelength):
        # satellite: u:east , v:south from bottomleft and u_center: east; v_center: north from center
        # realword: X: south, Y:down, Z: east   origin is set to the ground plane

        # meshgrid the sat pannel
        i = j = torch.arange(0, satmap_sidelength).cuda()  # to(self.device)
        ii, jj = torch.meshgrid(i, j)  # i:h,j:w

        # uv is coordinate from top/left, v: south, u:east
        uv = torch.stack([jj, ii], dim=-1).float()  # shape = [satmap_sidelength, satmap_sidelength, 2]

        # sat map from top/left to center coordinate
        u0 = v0 = satmap_sidelength // 2
        uv_center = uv - torch.tensor(
            [u0, v0]).cuda()  # .to(self.device) # shape = [satmap_sidelength, satmap_sidelength, 2]

        # affine matrix: scale*R
        meter_per_pixel = utils.get_meter_per_pixel()
        meter_per_pixel *= utils.get_process_satmap_sidelength() / satmap_sidelength
        R = torch.tensor([[0, 1], [1, 0]]).float().cuda()  # to(self.device) # u_center->z, v_center->x
        Aff_sat2real = meter_per_pixel * R  # shape = [2,2]

        # Trans matrix from sat to realword
        XZ = torch.einsum('ij, hwj -> hwi', Aff_sat2real,
                          uv_center)  # shape = [satmap_sidelength, satmap_sidelength, 2]

        Y = torch.zeros_like(XZ[..., 0:1])
        ones = torch.ones_like(Y)
        sat2realwap = torch.cat([XZ[:, :, :1], Y, XZ[:, :, 1:], ones], dim=-1)  # [sidelength,sidelength,4]

        return sat2realwap

    def seq_warp_real2camera(self, ori_shift_u, ori_shift_v, ori_heading, XYZ_1, ori_camera_k, grd_H, grd_W, ori_grdH, ori_grdW, require_jac=True):
        # realword: X: south, Y:down, Z: east
        # camera: u:south, v: down from center (when heading east, need to rotate heading angle)
        # XYZ_1:[H,W,4], heading:[B,1], camera_k:[B,3,3], shift:[B,2]
        B = ori_heading.shape[0]
        shift_u_meters = self.args.shift_range_lon * ori_shift_u
        shift_v_meters = self.args.shift_range_lat * ori_shift_v
        heading = ori_heading * self.args.rotation_range / 180 * np.pi

        cos = torch.cos(-heading)
        sin = torch.sin(-heading)
        zeros = torch.zeros_like(cos)
        ones = torch.ones_like(cos)
        R = torch.cat([cos, zeros, -sin, zeros, ones, zeros, sin, zeros, cos], dim=-1)  # shape = [B,9]
        R = R.view(B, 3, 3)  # shape = [B,3,3]

        camera_height = utils.get_camera_height()
        # camera offset, shift[0]:east,Z, shift[1]:north,X
        height = camera_height * torch.ones_like(shift_u_meters)
        T = torch.cat([shift_v_meters, height, -shift_u_meters], dim=-1)  # shape = [B, 3]
        T = torch.unsqueeze(T, dim=-1)  # shape = [B,3,1]
        # T = torch.einsum('bij, bjk -> bik', R, T0)
        # T = R @ T0

        # P = K[R|T]
        camera_k = ori_camera_k.clone()
        camera_k[:, :1, :] = ori_camera_k[:, :1, :] * grd_W / ori_grdW  # original size input into feature get network/ output of feature get network
        camera_k[:, 1:2, :] = ori_camera_k[:, 1:2, :] * grd_H / ori_grdH
        # P = torch.einsum('bij, bjk -> bik', camera_k, torch.cat([R, T], dim=-1)).float()  # shape = [B,3,4]
        P = camera_k @ torch.cat([R, T], dim=-1)

        # uv1 = torch.einsum('bij, hwj -> bhwi', P, XYZ_1)  # shape = [B, H, W, 3]
        uv1 = torch.sum(P[:, None, None, :, :] * XYZ_1[None, :, :, None, :], dim=-1)
        # only need view in front of camera ,Epsilon = 1e-6
        uv1_last = torch.maximum(uv1[:, :, :, 2:], torch.ones_like(uv1[:, :, :, 2:]) * 1e-6)
        uv = uv1[:, :, :, :2] / uv1_last  # shape = [B, H, W, 2]

        mask = torch.greater(uv1_last, torch.ones_like(uv1[:, :, :, 2:]) * 1e-6)

        # ------ start computing jacobian ----- denote shift[:, 0] as x, shift[:, 1] as y below ----
        if require_jac:
            dT_dx = self.args.shift_range_lon * torch.tensor([0., 0., -1.], dtype=torch.float32, device=ori_shift_u.device, requires_grad=True).view(1, 3, 1).repeat(B, 1, 1)
            dT_dy = self.args.shift_range_lat * torch.tensor([1., 0., 0.], dtype=torch.float32, device=ori_shift_u.device, requires_grad=True).view(1, 3, 1).repeat(B, 1, 1)
            T_zeros = torch.zeros([B, 3, 1], dtype=torch.float32, device=ori_shift_u.device, requires_grad=True)
            dR_dtheta = self.args.rotation_range / 180 * np.pi * torch.cat([sin, zeros, cos, zeros, zeros, zeros, -cos, zeros, sin], dim=-1).view(B, 3, 3)
            R_zeros = torch.zeros([B, 3, 3], dtype=torch.float32, device=ori_shift_u.device, requires_grad=True)
            dP_dx = camera_k @ torch.cat([R_zeros, dT_dx], dim=-1) # [B, 3, 4]
            dP_dy = camera_k @ torch.cat([R_zeros, dT_dy], dim=-1) # [B, 3, 4]
            dP_dtheta = camera_k @ torch.cat([dR_dtheta, T_zeros], dim=-1) # [B, 3, 4]
            duv1_dx = torch.sum(dP_dx[:, None, None, :, :] * XYZ_1[None, :, :, None, :], dim=-1)
            duv1_dy = torch.sum(dP_dy[:, None, None, :, :] * XYZ_1[None, :, :, None, :], dim=-1)
            duv1_dtheta = torch.sum(dP_dtheta[:, None, None, :, :] * XYZ_1[None, :, :, None, :], dim=-1)
            # duv1_dx = torch.einsum('bij, hwj -> bhwi', camera_k @ torch.cat([R_zeros, R @ dT0_dx], dim=-1), XYZ_1)
            # duv1_dy = torch.einsum('bij, hwj -> bhwi', camera_k @ torch.cat([R_zeros, R @ dT0_dy], dim=-1), XYZ_1)
            # duv1_dtheta = torch.einsum('bij, hwj -> bhwi', camera_k @ torch.cat([dR_dtheta, dR_dtheta @ T0], dim=-1), XYZ_1)

            duv_dx = duv1_dx[..., 0:2]/uv1_last - uv1[:, :, :, :2] * duv1_dx[..., 2:] /(uv1_last**2)
            duv_dy = duv1_dy[..., 0:2]/uv1_last - uv1[:, :, :, :2] * duv1_dy[..., 2:] /(uv1_last**2)
            duv_dtheta = duv1_dtheta[..., 0:2]/uv1_last - uv1[:, :, :, :2]* duv1_dtheta[..., 2:] /(uv1_last**2)

            duv_dx1 = torch.where(mask, duv_dx, torch.zeros_like(duv_dx))
            duv_dy1 = torch.where(mask, duv_dy, torch.zeros_like(duv_dy))
            duv_dtheta1 = torch.where(mask, duv_dtheta, torch.zeros_like(duv_dtheta))

            return uv, duv_dx1, duv_dy1, duv_dtheta1, mask
            
            # duv_dshift = torch.stack([duv_dx1, duv_dy1], dim=0)  # [ 2(pose_shift), B, H, W, 2(coordinates)]
            # duv_dtheta1 = duv_dtheta1.unsqueeze(dim=0) # [ 1(pose_heading), B, H, W, 2(coordinates)]
            # return uv, duv_dshift, duv_dtheta1, mask

            # duv1_dshift = torch.stack([duv1_dx, duv1_dy], dim=0)
            # duv1_dtheta = duv1_dtheta.unsqueeze(dim=0)
            # return uv1, duv1_dshift, duv1_dtheta, mask
        else:
            return uv, mask
            # return uv1

    def project_grd_to_map(self, grd_f, grd_c, shift_u, shift_v, heading, camera_k, satmap_sidelength, ori_grdH, ori_grdW):
        # inputs:
        #   grd_f: ground features: B,C,H,W
        #   shift: B, S, 2
        #   heading: heading angle: B,S
        #   camera_k: 3*3 K matrix of left color camera : B*3*3
        # return:
        #   grd_f_trans: B,S,E,C,satmap_sidelength,satmap_sidelength

        B, C, H, W = grd_f.size()

        XYZ_1 = self.get_warp_sat2real(satmap_sidelength)  # [ sidelength,sidelength,4]

        if self.args.proj == 'geo':
            uv, jac_shiftu, jac_shiftv, jac_heading, mask = self.seq_warp_real2camera(shift_u, shift_v, heading, XYZ_1, camera_k, H, W, ori_grdH, ori_grdW, require_jac=True)  # [B, S, E, H, W,2]
            # [B, H, W, 2], [2, B, H, W, 2], [1, B, H, W, 2]
            # # --------------------------------------------------------------------------------------------------
            # def seq_warp_real2camera(ori_shift, ori_heading, ori_camera_k):
            #     # realword: X: south, Y:down, Z: east
            #     # camera: u:south, v: down from center (when heading east, need to rotate heading angle)
            #     # XYZ_1:[H,W,4], heading:[B,1], camera_k:[B,3,3], shift:[B,2]
            #     B = ori_heading.shape[0]
            #
            #     shift = ori_shift * self.args.shift_range
            #     heading = ori_heading * self.args.rotation_range / 180. * np.pi
            #
            #     cos = torch.cos(-heading)
            #     sin = torch.sin(-heading)
            #     zeros = torch.zeros_like(cos)
            #     ones = torch.ones_like(cos)
            #     R = torch.cat([cos, zeros, -sin, zeros, ones, zeros, sin, zeros, cos], dim=-1)  # shape = [B,9]
            #     R = R.view(B, 3, 3)  # shape = [B,3,3]
            #
            #     camera_height = utils.get_camera_height()
            #     # camera offset, shift[0]:east,Z, shift[1]:north,X
            #     height = camera_height * torch.ones_like(shift[:, :1])
            #     T = torch.cat([shift[:, 1:], height, -shift[:, :1]], dim=-1)  # shape = [B, 3]
            #     T = torch.unsqueeze(T, dim=-1)  # shape = [B,3,1]
            #     # T = torch.einsum('bij, bjk -> bik', R, T0)
            #
            #     # P = K[R|T]
            #     camera_k = ori_camera_k.clone()
            #     camera_k[:, :1, :] = ori_camera_k[:, :1,
            #                          :] * W / ori_grdW  # original size input into feature get network/ output of feature get network
            #     camera_k[:, 1:2, :] = ori_camera_k[:, 1:2, :] * H / ori_grdH
            #     P = torch.einsum('bij, bjk -> bik', camera_k, torch.cat([R, T], dim=-1)).float()  # shape = [B,3,4]
            #
            #     uv1 = torch.einsum('bij, hwj -> bhwi', P, XYZ_1)  # shape = [B, H, W, 3]
            #     # only need view in front of camera ,Epsilon = 1e-6
            #     uv1_last = torch.maximum(uv1[:, :, :, 2:], torch.ones_like(uv1[:, :, :, 2:]) * 1e-6)
            #     uv = uv1[:, :, :, :2] / uv1_last  # shape = [B, H, W, 2]
            #     return uv
            #
            # auto_jac = torch.autograd.functional.jacobian(seq_warp_real2camera, (shift, heading, camera_k))
            # auto_jac_shift = torch.where(mask.unsqueeze(dim=0), auto_jac[0][:, :, :, :, 0, :].permute(4, 0, 1, 2, 3),
            #                              torch.zeros_like(jac_shift))
            # # auto_jac_shift = auto_jac[0][:, :, :, :, 0, :].permute(4, 0, 1, 2, 3)
            # diff = torch.abs(auto_jac_shift - jac_shift)
            # auto_jac_heading = torch.where(mask.unsqueeze(dim=0), auto_jac[1][:, :, :, :, 0, :].permute(4, 0, 1, 2, 3),
            #                                torch.zeros_like(jac_heading))
            # # auto_jac_heading = auto_jac[1][:, :, :, :, 0, :].permute(4, 0, 1, 2, 3)
            # diff1 = torch.abs(auto_jac_heading - jac_heading)
            # heading_np = jac_heading[0, 0].data.cpu().numpy()
            # auto_heading_np = auto_jac_heading[0, 0].data.cpu().numpy()
            # diff1_np = diff1.data.cpu().numpy()
            # diff_np = diff.data.cpu().numpy()
            # mask_np = mask[0, ..., 0].float().data.cpu().numpy()
            # # --------------------------------------------------------------------------------------------------

        elif self.args.proj == 'nn':
            uv, jac_shiftu, jac_shiftv, jac_heading, mask = self.inplane_grd_to_map(shift_u, shift_v, heading, satmap_sidelength, require_jac=True)
            # # --------------------------------------------------------------------------------------------------
            # def inplane_grd_to_map(ori_shift_u, ori_shift_v, ori_heading):
            #
            #     meter_per_pixel = utils.get_meter_per_pixel()
            #     meter_per_pixel *= utils.get_process_satmap_sidelength() / satmap_sidelength
            #
            #     B = ori_heading.shape[0]
            #     shift_u_pixels = self.args.shift_range_lon * ori_shift_u / meter_per_pixel
            #     shift_v_pixels = self.args.shift_range_lat * ori_shift_v / meter_per_pixel
            #     T = torch.cat([-shift_u_pixels, shift_v_pixels], dim=-1)  # [B, 2]
            #
            #     heading = ori_heading * self.args.rotation_range / 180 * np.pi
            #     cos = torch.cos(heading)
            #     sin = torch.sin(heading)
            #     R = torch.cat([cos, -sin, sin, cos], dim=-1).view(B, 2, 2)
            #
            #     i = j = torch.arange(0, satmap_sidelength).cuda()  # to(self.device)
            #     v, u = torch.meshgrid(i, j)  # i:h,j:w
            #     uv_2 = torch.stack([u, v], dim=-1).unsqueeze(dim=0).repeat(B, 1, 1, 1).float()  # [B, H, W, 2]
            #     uv_2 = uv_2 - satmap_sidelength / 2
            #
            #     uv_1 = torch.einsum('bij, bhwj->bhwi', R, uv_2)
            #     uv_0 = uv_1 + T[:, None, None, :]  # [B, H, W, 2]
            #
            #     uv = uv_0 + satmap_sidelength / 2
            #
            #     return uv
            #
            # auto_jac = torch.autograd.functional.jacobian(inplane_grd_to_map, (shift_u, shift_v, heading))
            #
            # auto_jac_shiftu = auto_jac[0][:, :, :, :, 0, 0]
            # diff_u = torch.abs(auto_jac_shiftu - jac_shiftu)
            #
            # auto_jac_shiftv = auto_jac[1][:, :, :, :, 0, 0]
            # diff_v = torch.abs(auto_jac_shiftv - jac_shiftv)
            #
            # auto_jac_heading = auto_jac[2][:, :, :, :, 0, 0]
            # diff_h = torch.abs(auto_jac_heading - jac_heading)
            #
            # # diff1_np = diff1.data.cpu().numpy()
            # # diff_np = diff.data.cpu().numpy()
            # # mask_np = mask[0, ..., 0].float().data.cpu().numpy()
            # # --------------------------------------------------------------------------------------------------

        jac = torch.stack([jac_shiftu, jac_shiftv, jac_heading], dim=0) # [3, B, H, W, 2]

        grd_f_trans, new_jac = grid_sample(grd_f, uv, jac)
        # [B,C,sidelength,sidelength], [3, B, C, sidelength, sidelength]
        if grd_c is not None:
            grd_c_trans, _ = grid_sample(grd_c, uv)
        else:
            grd_c_trans = None

        return grd_f_trans, grd_c_trans, new_jac

    def inplane_grd_to_map(self, ori_shift_u, ori_shift_v, ori_heading, satmap_sidelength, require_jac=True):

        meter_per_pixel = utils.get_meter_per_pixel()
        meter_per_pixel *= utils.get_process_satmap_sidelength() / satmap_sidelength

        B = ori_heading.shape[0]
        shift_u_pixels = self.args.shift_range_lon * ori_shift_u / meter_per_pixel
        shift_v_pixels = self.args.shift_range_lat * ori_shift_v / meter_per_pixel
        T = torch.cat([-shift_u_pixels, shift_v_pixels], dim=-1)  # [B, 2]

        heading = ori_heading * self.args.rotation_range / 180 * np.pi
        cos = torch.cos(heading)
        sin = torch.sin(heading)
        R = torch.cat([cos, -sin, sin, cos], dim=-1).view(B, 2, 2)

        i = j = torch.arange(0, satmap_sidelength).cuda()  # to(self.device)
        v, u = torch.meshgrid(i, j)  # i:h,j:w
        uv_2 = torch.stack([u, v], dim=-1).unsqueeze(dim=0).repeat(B, 1, 1, 1).float()  # [B, H, W, 2]
        uv_2 = uv_2 - satmap_sidelength/2

        uv_1 = torch.einsum('bij, bhwj->bhwi', R, uv_2)
        uv_0 = uv_1 + T[:, None, None, :]   # [B, H, W, 2]

        uv = uv_0 + satmap_sidelength/2
        mask = torch.ones_like(uv[..., 0])

        if require_jac:
            dT_dshiftu = self.args.shift_range_lon / meter_per_pixel\
                         * torch.tensor([-1., 0], dtype=torch.float32, device=ori_shift_u.device,
                                        requires_grad=True).view(1, 2).repeat(B, 1)
            dT_dshiftv = self.args.shift_range_lat / meter_per_pixel\
                         * torch.tensor([0., 1], dtype=torch.float32, device=ori_shift_u.device,
                                        requires_grad=True).view(1, 2).repeat(B, 1)
            dR_dtheta = self.args.rotation_range / 180 * np.pi * torch.cat(
                [-sin, -cos, cos, -sin], dim=-1).view(B, 2, 2)

            duv_dshiftu = dT_dshiftu[:, None, None, :].repeat(1, satmap_sidelength, satmap_sidelength, 1)
            duv_dshiftv = dT_dshiftv[:, None, None, :].repeat(1, satmap_sidelength, satmap_sidelength, 1)
            duv_dtheta = torch.einsum('bij, bhwj->bhwi', dR_dtheta, uv_2)

            return uv, duv_dshiftu, duv_dshiftv, duv_dtheta, mask
        else:
            return uv, mask

    def LM_update(self, shift_u, shift_v, heading, grd_feat_proj, grd_conf_proj, sat_feat, sat_conf, dfeat_dpose):
        '''
        Args:
            shift_u: [B, 1]
            shift_v: [B, 1]
            heading: [B, 1]
            grd_feat_proj: [B, C, H, W]
            grd_conf_proj: [B, 1, H, W]
            sat_feat: [B, C, H, W]
            sat_conf: [B, 1, H, W]
            dfeat_dpose: [3, B, C, H, W]

        Returns:
            shift_u_new [B, 1]
            shift_v_new [B, 1]
            heading_new [B, 1]
        '''

        N, B, C, H, W = dfeat_dpose.shape

        # grd_feat_proj_norm = torch.norm(grd_feat_proj.reshape(B, -1), p=2, dim=-1)
        # grd_feat_proj = grd_feat_proj / grd_feat_proj_norm[:, None, None, None]
        # dfeat_dpose = dfeat_dpose / grd_feat_proj_norm[None, :, None, None, None]

        r = grd_feat_proj - sat_feat  # [B, C, H, W]

        if self.args.train_damping:
            damping = self.damping
        else:
            damping = (self.args.damping * torch.ones(size=(1, 3), dtype=torch.float32, requires_grad=True)).to(dfeat_dpose.device)

        if self.using_weight:
            weight = (grd_conf_proj).repeat(1, C, 1, 1).reshape(B, C * H * W)
        else:
            weight = torch.ones([B, C*H*W], dtype=torch.float32, device=sat_feat.device, requires_grad=True)

        J = dfeat_dpose.flatten(start_dim=2).permute(1, 2, 0)  # [B, C*H*W, #pose]
        temp = J.transpose(1, 2) * weight.unsqueeze(dim=1)
        Hessian = temp @ J  # [B, #pose, #pose]
        # diag_H = torch.diag_embed(torch.diagonal(Hessian, dim1=1, dim2=2))  # [B, 3, 3]
        diag_H = torch.eye(Hessian.shape[-1], requires_grad=True).unsqueeze(dim=0).repeat(B, 1, 1).to(
            Hessian.device)
        delta_pose = - torch.inverse(Hessian + damping * diag_H) \
                     @ temp @ r.reshape(B, C * H * W, 1)

        shift_u_new = shift_u + delta_pose[:, 0:1, 0]
        shift_v_new = shift_v + delta_pose[:, 1:2, 0]
        heading_new = heading + delta_pose[:, 2:, 0]

        return shift_u_new, shift_v_new, heading_new

    def forward(self, sat_map, grd_img_left, left_camera_k, gt_shift_u=None, gt_shift_v=None, gt_heading=None,
                mode='train', file_name=None, gt_depth=None):
        '''
        Args:
            sat_map: [B, C, A, A] A--> sidelength
            left_camera_k: [B, 3, 3]
            grd_img_left: [B, C, H, W]
            gt_shift_u: [B, 1] u->longitudinal
            gt_shift_v: [B, 1] v->lateral
            gt_heading: [B, 1] east as 0-degree
            mode:
            file_name:

        Returns:

        '''
        '''
        :param sat_map: [B, C, A, A] A--> sidelength
        :param left_camera_k: [B, 3, 3]
        :param grd_img_left: [B, C, H, W]
        :return:
        '''

        B, _, ori_grdH, ori_grdW = grd_img_left.shape

        # A = sat_map.shape[-1]
        # sat_align_cam_trans, _, dimg_dpose = self.project_grd_to_map(
        #     sat_align_cam, None, gt_shift_u, gt_shift_v, gt_heading, left_camera_k, A, ori_grdH, ori_grdW)
        # grd_img = transforms.ToPILImage()(sat_align_cam_trans[0])
        # grd_img.save('sat_align_cam_trans.png')
        # sat_align_cam = transforms.ToPILImage()(sat_align_cam[0])
        # sat_align_cam.save('sat_align_cam.png')
        # sat = transforms.ToPILImage()(sat_map[0])
        # sat.save('sat.png')

        sat_feat_list, sat_conf_list = self.SatFeatureNet(sat_map)

        grd_feat_list, grd_conf_list = self.GrdFeatureNet(grd_img_left)

        shift_u = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        shift_v = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        heading = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)

        pred_feat_dict = {}
        shift_us_all = []
        shift_vs_all = []
        headings_all = []
        for iter in range(self.N_iters):
            shift_us = []
            shift_vs = []
            headings = []
            for level in range(len(sat_feat_list)):
                sat_feat = sat_feat_list[level]
                sat_conf = sat_conf_list[level]
                grd_feat = grd_feat_list[level]
                grd_conf = grd_conf_list[level]

                A = sat_feat.shape[-1]
                grd_feat_proj, grd_conf_proj, dfeat_dpose = self.project_grd_to_map(
                    grd_feat, grd_conf, shift_u, shift_v, heading, left_camera_k, A, ori_grdH, ori_grdW)
          
                shift_u_new, shift_v_new, heading_new = self.LM_update(
                    shift_u, shift_v, heading, grd_feat_proj, grd_conf_proj, sat_feat, sat_conf, dfeat_dpose)

                shift_us.append(shift_u_new[:, 0])  # [B]
                shift_vs.append(shift_v_new[:, 0])  # [B]
                headings.append(heading_new[:, 0])

                shift_u = shift_u_new.clone()
                shift_v = shift_v_new.clone()
                heading = heading_new.clone()

                if level not in pred_feat_dict.keys():
                    pred_feat_dict[level] = [grd_feat_proj]
                else:
                    pred_feat_dict[level].append(grd_feat_proj)

            shift_us_all.append(torch.stack(shift_us, dim=1))  # [B, Level]
            shift_vs_all.append(torch.stack(shift_vs, dim=1))  # [B, Level]
            headings_all.append(torch.stack(headings, dim=1)) # [B, Level]

        shift_lats = torch.stack(shift_vs_all, dim=1)  # [B, N_iters, Level]
        shift_lons = torch.stack(shift_us_all, dim=1)  # [B, N_iters, Level]
        thetas = torch.stack(headings_all, dim=1)  # [B, N_iters, Level]

        if mode == 'train':
            loss, loss_decrease, shift_lat_decrease, shift_lon_decrease, thetas_decrease, loss_last, \
            shift_lat_last, shift_lon_last, theta_last, \
            L1_loss, L2_loss, L3_loss, L4_loss \
                = loss_func(self.args.loss_method, grd_feat_list, pred_feat_dict, None,
                            shift_lats, shift_lons, thetas, gt_shift_v[:, 0], gt_shift_u[:, 0], gt_heading[:, 0],
                            None, None,
                            self.args.coe_shift_lat, self.args.coe_shift_lon, self.args.coe_heading,
                            self.args.coe_L1, self.args.coe_L2, self.args.coe_L3, self.args.coe_L4)

            return loss, loss_decrease, shift_lat_decrease, shift_lon_decrease, thetas_decrease, loss_last, \
                    shift_lat_last, shift_lon_last, theta_last, \
                    L1_loss, L2_loss, L3_loss, L4_loss, grd_conf_list

        else:
            return shift_lats[:, -1, -1], shift_lons[:, -1, -1], thetas[:, -1, -1]

    def corr(self, sat_map, grd_img_left, left_camera_k, gt_shift_u=None, gt_shift_v=None, gt_heading=None,
                mode='train', file_name=None, gt_depth=None):
        '''
        Args:
            sat_map: [B, C, A, A] A--> sidelength
            left_camera_k: [B, 3, 3]
            grd_img_left: [B, C, H, W]
            gt_shift_u: [B, 1] u->longitudinal
            gt_shift_v: [B, 1] v->lateral
            gt_heading: [B, 1] east as 0-degree
            mode:
            file_name:

        Returns:

        '''
        '''
        :param sat_map: [B, C, A, A] A--> sidelength
        :param left_camera_k: [B, 3, 3]
        :param grd_img_left: [B, C, H, W]
        :return:
        '''

        B, _, ori_grdH, ori_grdW = grd_img_left.shape

        sat_feat_list, sat_conf_list = self.SatFeatureNet(sat_map)

        grd_feat_list, grd_conf_list = self.GrdFeatureNet(grd_img_left)

        shift_u = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        shift_v = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        heading = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)

        corr_maps = []

        for level in range(len(sat_feat_list)):
            meter_per_pixel = self.meters_per_pixel[level]

            sat_feat = sat_feat_list[level]
            grd_feat = grd_feat_list[level]

            A = sat_feat.shape[-1]
            grd_feat_proj, _, dfeat_dpose = self.project_grd_to_map(
                grd_feat, None, shift_u, shift_v, heading, left_camera_k, A, ori_grdH, ori_grdW)

            crop_H = int(A - self.args.shift_range_lat * 2 / meter_per_pixel)
            crop_W = int(A - self.args.shift_range_lon * 2 / meter_per_pixel)
            g2s_feat = TF.center_crop(grd_feat_proj, [crop_H, crop_W])
            g2s_feat = F.normalize(g2s_feat.reshape(B, -1)).reshape(B, -1, crop_H, crop_W)

            s_feat = sat_feat.reshape(1, -1, A, A) # [B, C, H, W]->[1, B*C, H, W]
            corr = F.conv2d(s_feat, g2s_feat, groups=B)[0]  #[B, H, W]

            denominator = F.avg_pool2d(sat_feat.pow(2), (crop_H, crop_W), stride=1, divisor_override=1)  # [B, 4W]
            denominator = torch.sum(denominator, dim=1)  # [B, H, W]
            denominator = torch.maximum(torch.sqrt(denominator), torch.ones_like(denominator) * 1e-6)
            corr = 2 - 2 * corr / denominator

            B, corr_H, corr_W = corr.shape

            corr_maps.append(corr)

            max_index = torch.argmin(corr.reshape(B, -1), dim=1)
            pred_u = (max_index % corr_W - corr_W / 2) * meter_per_pixel # / self.args.shift_range_lon
            pred_v = -(max_index // corr_W - corr_H/2) * meter_per_pixel # / self.args.shift_range_lat

            # corr0 = []
            # for b in range(B):
            #     corr0.append(F.conv2d(s_feat[b:b+1, :, :, :], g2s_feat[b:b+1, :, :, :]))  # [1, 1, H, W]
            # corr0 = torch.cat(corr0, dim=1)
            # print(torch.sum(torch.abs(corr0 - corr)))

        if mode == 'train':
            return self.triplet_loss(corr_maps, gt_shift_u, gt_shift_v)
        else:
            return pred_u, pred_v  # [B], [B]


    def triplet_loss(self, corr_maps, gt_shift_u, gt_shift_v):
        losses = []
        for level in range(len(corr_maps)):
            meter_per_pixel = self.meters_per_pixel[level]

            corr = corr_maps[level]
            B, corr_H, corr_W = corr.shape

            w = torch.round(corr_W / 2 + gt_shift_u[:, 0] * self.args.shift_range_lon / meter_per_pixel)
            h = torch.round(corr_H / 2 - gt_shift_v[:, 0] * self.args.shift_range_lat / meter_per_pixel)

            pos = corr[range(B), h.long(), w.long()]  # [B]
            pos_neg = pos.reshape(-1, 1, 1) - corr  # [B, H, W]
            loss = torch.sum(torch.log(1 + torch.exp(pos_neg * 10))) / (B * (corr_H * corr_W - 1))
            losses.append(loss)

        return torch.sum(torch.stack(losses, dim=0))



CONFIG_PATH = Path.cwd() / 'transformer/config'
CONFIG_NAME = 'config.yaml'


def setup_network(cfg, type):
    if type=='satellite':
        # print(f'Setup Backbone Network for {type}: {cfg.satellite_model}')        
        return instantiate(cfg.satellite_model)
    else:
        # print(f'Setup Backbone Network for {type}: {cfg.model}')
        return instantiate(cfg.model)


@hydra.main(config_path=CONFIG_PATH, config_name=CONFIG_NAME)
def setup_satellite_model_module(cfg) -> ModelModule:
    print(f'setup_satellite_model_module')
    backbone = setup_network(cfg, 'satellite')
    loss_func = MultipleLoss(instantiate(cfg.loss))
    metrics = MetricCollection({k: v for k, v in instantiate(cfg.metrics).items()})

    model_module = ModelModule(backbone, loss_func, metrics,
                               cfg.optimizer, cfg.scheduler,
                               cfg=cfg)
    return model_module

@hydra.main(config_path=CONFIG_PATH, config_name=CONFIG_NAME)
def setup_model_module(cfg) -> ModelModule:
    print(f'setup_ground_images_model_module')
    backbone = setup_network(cfg, 'ground')
    # print(f'Setup loss_func: {cfg.loss}')
    loss_func = MultipleLoss(instantiate(cfg.loss))
    # print(f'Setup Metrics: {cfg.metrics}')
    metrics = MetricCollection({k: v for k, v in instantiate(cfg.metrics).items()})

    model_module = ModelModule(backbone, loss_func, metrics,
                               cfg.optimizer, cfg.scheduler,
                               cfg=cfg)
    return model_module


class LM_S2GP(nn.Module):
    def __init__(self, args):  # device='cuda:0',
        super(LM_S2GP, self).__init__()
        '''
        loss_method: 0: direct R T loss 1: feat loss 2: noise aware feat loss
        '''
        self.args = args.highlyaccurate

        self.level = args.highlyaccurate.level
        self.N_iters = args.highlyaccurate.N_iters
        self.using_weight = args.highlyaccurate.using_weight
        self.loss_method = args.highlyaccurate.loss_method

        self.data_dict = args.data
        self.loss_file = "loss.txt"

        self.SatFeatureNet = VGGUnet(self.level)
        self.GrdFeatureNet = VGGUnet(self.level)

        self.counts = 0

        print("Debug msg")
        if args.highlyaccurate.use_transformer == True:
            print("For Ground-View images, Use Transformer as Feature Extractor!")
            self.SatFeatureNet = setup_satellite_model_module(args)
            self.GrdFeatureNet = setup_model_module(args)


        if args.highlyaccurate.rotation_range > 0:
            self.damping = nn.Parameter(
                torch.zeros(size=(1, 3), dtype=torch.float32, requires_grad=True))
        else:
            self.damping = nn.Parameter(
            torch.zeros(size=(), dtype=torch.float32, requires_grad=True))

        # ori_grdH, ori_grdW = 256, 1024
        ori_grdH = args.data.image.h
        ori_grdW = args.data.image.w
        # print(f'ori_grdH: {ori_grdH}')
        # print(f'ori_grdW: {ori_grdW}')        
        xyz_grds = []
        for level in range(4):
            grd_H, grd_W = ori_grdH/(2**(3-level)), ori_grdW/(2**(3-level))
            if self.args.proj == 'geo':
                xyz_grd, mask, xyz_w = self.grd_img2cam(grd_H, grd_W, ori_grdH,
                                                 ori_grdW)  # [1, grd_H, grd_W, 3] under the grd camera coordinates
                xyz_grds.append((xyz_grd, mask, xyz_w))

            else:
                xyz_grd, mask = self.grd_img2cam_polar(grd_H, grd_W, ori_grdH, ori_grdW)
                xyz_grds.append((xyz_grd, mask))

        self.xyz_grds = xyz_grds

        self.meters_per_pixel = []
        meter_per_pixel = utils.get_meter_per_pixel() # 0.2 meter / pixel (for satellitemap)
        for level in range(4):
            self.meters_per_pixel.append(meter_per_pixel * (2 ** (3 - level)))

        polar_grids = []
        for level in range(4):
            grids = self.polar_coordinates(level)
            polar_grids.append(grids)
        self.polar_grids = polar_grids

        if self.args.Optimizer=='NN':
            self.NNrefine = NNrefine()

        torch.autograd.set_detect_anomaly(True)
        # Running the forward pass with detection enabled will allow the backward pass to print the traceback of the forward operation that created the failing backward function.
        # Any backward computation that generate “nan” value will raise an error.

    def grd_img2cam(self, grd_H, grd_W, ori_grdH, ori_grdW):
        
        ori_camera_k = torch.tensor([[[582.9802,   0.0000, 496.2420],
                                      [0.0000, 482.7076, 125.0034],
                                      [0.0000,   0.0000,   1.0000]]], 
                                    dtype=torch.float32, requires_grad=True)  # [1, 3, 3]
        
        camera_height = utils.get_camera_height()

        camera_k = ori_camera_k.clone()
        camera_k[:, :1, :] = ori_camera_k[:, :1,
                             :] * grd_W / ori_grdW  # original size input into feature get network/ output of feature get network
        camera_k[:, 1:2, :] = ori_camera_k[:, 1:2, :] * grd_H / ori_grdH
        camera_k_inv = torch.inverse(camera_k)  # [B, 3, 3]

        v, u = torch.meshgrid(torch.arange(0, grd_H, dtype=torch.float32),
                              torch.arange(0, grd_W, dtype=torch.float32))
        uv1 = torch.stack([u, v, torch.ones_like(u)], dim=-1).unsqueeze(dim=0)  # [1, grd_H, grd_W, 3]
        xyz_w = torch.sum(camera_k_inv[:, None, None, :, :] * uv1[:, :, :, None, :], dim=-1)  # [1, grd_H, grd_W, 3]

        w = camera_height / torch.where(torch.abs(xyz_w[..., 1:2]) > utils.EPS, xyz_w[..., 1:2],
                                        utils.EPS * torch.ones_like(xyz_w[..., 1:2]))  # [BN, grd_H, grd_W, 1]
        xyz_grd = xyz_w * w  # [1, grd_H, grd_W, 3] under the grd camera coordinates
        # xyz_grd = xyz_grd.reshape(B, N, grd_H, grd_W, 3)

        mask = (xyz_grd[..., -1] > 0).float()  # # [1, grd_H, grd_W]

        return xyz_grd, mask, xyz_w

    def grd_img2cam_polar(self, grd_H, grd_W, ori_grdH, ori_grdW):

        v, u = torch.meshgrid(torch.arange(0, grd_H, dtype=torch.float32),
                              torch.arange(0, grd_W, dtype=torch.float32))
        theta = u/grd_W * np.pi/4
        radius = (1 - v / grd_H) * 30  # set radius as 30 meters

        z = radius * torch.cos(np.pi/4 - theta)
        x = -radius * torch.sin(np.pi/4 - theta)
        y = utils.get_camera_height() * torch.ones_like(z)
        xyz_grd = torch.stack([x, y, z], dim=-1).unsqueeze(dim=0) # [1, grd_H, grd_W, 3] under the grd camera coordinates

        mask = torch.ones_like(z).unsqueeze(dim=0)  # [1, grd_H, grd_W]

        return xyz_grd, mask

    def grd2cam2world2sat(self, ori_shift_u, ori_shift_v, ori_heading, level,
                          satmap_sidelength, require_jac=False, gt_depth=None):
        '''
        realword: X: south, Y:down, Z: east
        camera: u:south, v: down from center (when heading east, need to rotate heading angle)
        Args:
            ori_shift_u: [B, 1]
            ori_shift_v: [B, 1]
            heading: [B, 1]
            XYZ_1: [H,W,4]
            ori_camera_k: [B,3,3]
            grd_H:
            grd_W:
            ori_grdH:
            ori_grdW:

        Returns:
        '''
        B, _ = ori_heading.shape
        # heading = ori_heading * self.args.rotation_range / 180 * np.pi
        # shift_u = ori_shift_u * self.args.shift_range_lon
        # shift_v = ori_shift_v * self.args.shift_range_lat
        heading = ori_heading / 180 * np.pi
        shift_u = ori_shift_u
        shift_v = ori_shift_v

        cos = torch.cos(heading)
        sin = torch.sin(heading)
        zeros = torch.zeros_like(cos)
        ones = torch.ones_like(cos)
        R = torch.cat([cos, zeros, -sin, zeros, ones, zeros, sin, zeros, cos], dim=-1)  # shape = [B, 9]
        R = R.view(B, 3, 3)  # shape = [B, N, 3, 3]
        # this R is the inverse of the R in G2SP

        camera_height = utils.get_camera_height()
        # camera offset, shift[0]:east,Z, shift[1]:north,X
        height = camera_height * torch.ones_like(shift_u[:, :1])
        T0 = torch.cat([shift_v, height, -shift_u], dim=-1)  # shape = [B, 3]
        # T0 = torch.unsqueeze(T0, dim=-1)  # shape = [B, N, 3, 1]
        # T = torch.einsum('bnij, bnj -> bni', -R, T0) # [B, N, 3]
        T = torch.sum(-R * T0[:, None, :], dim=-1)   # [B, 3]

        # The above R, T define transformation from camera to world

        if self.args.use_gt_depth and gt_depth!=None:
            xyz_w = self.xyz_grds[level][2].detach().to(ori_shift_u.device).repeat(B, 1, 1, 1)
            H, W = xyz_w.shape[1:-1]
            depth = F.interpolate(gt_depth[:, None, :, :], (H, W))
            xyz_grd = xyz_w * depth.permute(0, 2, 3, 1)
            mask = (gt_depth != -1).float()
            mask = F.interpolate(mask[:, None, :, :], (H, W), mode='nearest')
            mask = mask[:, 0, :, :]
        else:
            xyz_grd = self.xyz_grds[level][0].detach().to(ori_shift_u.device).repeat(B, 1, 1, 1)
            mask = self.xyz_grds[level][1].detach().to(ori_shift_u.device).repeat(B, 1, 1)  # [B, grd_H, grd_W]
        grd_H, grd_W = xyz_grd.shape[1:3]

        xyz = torch.sum(R[:, None, None, :, :] * xyz_grd[:, :, :, None, :], dim=-1) + T[:, None, None, :]
        # [B, grd_H, grd_W, 3]
        # zx0 = torch.stack([xyz[..., 2], xyz[..., 0]], dim=-1)  # [B, N, grd_H, grd_W, 2]
        R_sat = torch.tensor([0, 0, 1, 1, 0, 0], dtype=torch.float32, device=ori_shift_u.device, requires_grad=True)\
            .reshape(2, 3)
        zx = torch.sum(R_sat[None, None, None, :, :] * xyz[:, :, :, None, :], dim=-1)
        # [B, grd_H, grd_W, 2]
        # assert zx == zx0

        meter_per_pixel = utils.get_meter_per_pixel() # Pass MAP_ORIGINS latitude
        meter_per_pixel *= utils.get_process_satmap_sidelength() / satmap_sidelength
        sat_uv = zx/meter_per_pixel + satmap_sidelength / 2  # [B, grd_H, grd_W, 2] sat map uv Fixed across all input grd_images

        if require_jac:
            dR_dtheta = self.args.rotation_range / 180 * np.pi * \
                        torch.cat([-sin, zeros, -cos, zeros, zeros, zeros, cos, zeros, -sin], dim=-1)  # shape = [B, N, 9]
            dR_dtheta = dR_dtheta.view(B, 3, 3)
            # R_zeros = torch.zeros_like(dR_dtheta)

            dT0_dshiftu = self.args.shift_range_lon * torch.tensor([0., 0., -1.], dtype=torch.float32, device=shift_u.device,
                                                         requires_grad=True).view(1, 3).repeat(B, 1)
            dT0_dshiftv = self.args.shift_range_lat * torch.tensor([1., 0., 0.], dtype=torch.float32, device=shift_u.device,
                                                         requires_grad=True).view(1, 3).repeat(B, 1)
            # T0_zeros = torch.zeros_like(dT0_dx)

            dxyz_dshiftu = torch.sum(-R * dT0_dshiftu[:, None, :], dim=-1)[:, None, None, :].\
                repeat([1, grd_H, grd_W, 1])   # [B, grd_H, grd_W, 3]
            dxyz_dshiftv = torch.sum(-R * dT0_dshiftv[:, None, :], dim=-1)[:, None, None, :].\
                repeat([1, grd_H, grd_W, 1])   # [B, grd_H, grd_W, 3]
            dxyz_dtheta = torch.sum(dR_dtheta[:, None, None, :, :] * xyz_grd[:, :, :, None, :], dim=-1) + \
                          torch.sum(-dR_dtheta * T0[:, None, :], dim=-1)[:, None, None, :]

            duv_dshiftu = 1 / meter_per_pixel * \
                     torch.sum(R_sat[None, None, None, :, :] * dxyz_dshiftu[:, :, :, None, :], dim=-1)
            # [B, grd_H, grd_W, 2]
            duv_dshiftv = 1 / meter_per_pixel * \
                     torch.sum(R_sat[None, None, None, :, :] * dxyz_dshiftv[:, :, :, None, :], dim=-1)
            # [B, grd_H, grd_W, 2]
            duv_dtheta = 1 / meter_per_pixel * \
                     torch.sum(R_sat[None, None, None, :, :] * dxyz_dtheta[:, :, :, None, :], dim=-1)
            # [B, grd_H, grd_W, 2]

            # duv_dshift = torch.stack([duv_dx, duv_dy], dim=0)
            # duv_dtheta = duv_dtheta.unsqueeze(dim=0)

            return sat_uv, mask, duv_dshiftu, duv_dshiftv, duv_dtheta

        return sat_uv, mask, None, None, None

    def project_map_to_grd(self, sat_f, sat_c, shift_u, shift_v, heading, level, require_jac=True, gt_depth=None):
        '''
        Args:
            sat_f: [B, C, H, W]
            sat_c: [B, 1, H, W]
            shift_u: [B, 2]
            shift_v: [B, 2]
            heading: [B, 1]
            camera_k: [B, 3, 3]

            ori_grdH:
            ori_grdW:

        Returns:

        '''
        B, C, satmap_sidelength, _ = sat_f.size()
        uv, mask, jac_shiftu, jac_shiftv, jac_heading = self.grd2cam2world2sat(shift_u, shift_v, heading, level,
                                    satmap_sidelength, require_jac, gt_depth)
        # [B, H, W, 2], [B, H, W], [B, H, W, 2], [B, H, W, 2], [B,H, W, 2]
        B, grd_H, grd_W, _ = uv.shape
        if require_jac:
            jac = torch.stack([jac_shiftu, jac_shiftv, jac_heading], dim=0)  # [3, B, H, W, 2]
        else:
            jac = None

        sat_f_trans, new_jac = grid_sample(sat_f,
                                           uv,
                                           jac)
        sat_f_trans = sat_f_trans * mask[:, None, :, :]
        if require_jac:
            new_jac = new_jac * mask[None, :, None, :, :]

        if sat_c is not None:
            sat_c_trans, _ = grid_sample(sat_c, uv)
            sat_c_trans = sat_c_trans * mask[:, None, :, :]
        else:
            sat_c_trans = None

                     # sat_c is None by default
        return sat_f_trans, sat_c_trans, new_jac, uv * mask[:, :, :, None], mask

    def clamp_pi_tensor(self, data: torch.tensor):
        data = torch.where(data > torch.pi, data - 2 * torch.pi, data)
        data = torch.where(data < -torch.pi, data + 2 * torch.pi, data)
        return data

    def cal_jacobian(self, data: torch.tensor):
        # --- coord notation --- #
        # u point to right, v point to down
        # x point to right, y point to up
        EPS = 1e-12
        DEVICE = data.device

        B, C, H, W = data.shape

        mask = (data < EPS) & (data > -EPS)

        shiftu = F.pad(data[:, :, :, :-1], (1, 0), "constant", 0)
        shiftv = F.pad(data[:, :, :-1], (0, 0, 1, 0), "constant", 0)
        # shiftu.requires_grad = True
        # shiftv.requires_grad = True
        print("shiftu.requires_grad = ", shiftu.requires_grad)

        mask_shiftu = (shiftu < EPS) & (shiftu > -EPS)
        mask_shiftv = (shiftv < EPS) & (shiftv > -EPS)

        jac_u = shiftu - data
        jac_u[:, :, :, 0] = 0
        jac_u[mask] = 0
        jac_u[mask_shiftu] = 0

        jac_v = shiftv - data
        jac_v[:, :, 0, :] = 0
        jac_v[mask] = 0
        jac_v[mask_shiftv] = 0

        h = torch.arange(start=(H - 1) / 2.0, end=-(H - 1) / 2.0 - 1, step=-1)
        w = torch.arange(start=-(W - 1) / 2.0, end=(W - 1) / 2.0 + 1, step=1)
        grid_h, grid_w = torch.meshgrid(h, w, indexing='ij')
        grid_h.to(device=DEVICE)
        grid_w.to(device=DEVICE)
        grid = torch.stack((grid_h, grid_w), dim=-1) # (10, 10, 2)
        grid = grid.view(1, 1, H * W, 2, 1).repeat(B, C, 1, 1, 1) # (B, C, (HW), 2, 1)
        grid_dist = torch.sqrt(torch.sum(torch.square(grid), dim=-2)).view(B, C, H, W).to(device=DEVICE) # (B, C, H, W)

        theta = torch.atan2(grid_h, grid_w).to(device=DEVICE) # (H, W)
        theta = theta.view(1, 1, H * W).repeat(B, C, 1) # (B, C, (HW))
        dR_dtheta = theta + torch.pi / 2
        dR_dtheta = dR_dtheta.view(B, C, H, W)
        dR_dtheta = self.clamp_pi_tensor(dR_dtheta)
        dR_dtheta_x = grid_dist * torch.cos(dR_dtheta)
        dR_dtheta_y = grid_dist * torch.sin(dR_dtheta) # (B, C, H, W)

        jac_theta = dR_dtheta_x * jac_u + dR_dtheta_y * (-jac_v)

        # Not sure (leekt)
        jac_theta = jac_theta * 2 * torch.pi / 180 # Account for unit

        jac = torch.stack((jac_u, jac_v, jac_theta), dim=0) # (3, B, C, H, W)

        return jac_u, jac_v, jac_theta, jac

    def LM_update_2D(self, learned_u, learned_v, learned_theta, F_bev, F_s2bev, jac):
        '''
        Args:
            learned_u: [B, 1]
            learned_v: [B, 1]
            learned_theta: [B, 1]
            F_bev: [B, C, H, W]
            F_s2bev: [B, C, H, W]
            jac: [3, B, C, H, W]
        '''
        DEVICE = F_bev.device
        DAMPING = 0.1

        # print("F_s2bev = ", F_s2bev)

        B, C, H, W = F_bev.shape

        F_bev = F_bev.reshape(B, -1) # (B, (CHW))
        F_s2bev = F_s2bev.reshape(B, -1) # (B, (CHW))

        # Default to be False
        NORMALIZE = False
        if NORMALIZE:
            F_bev_norm = torch.norm(F_bev, p=2, dim=-1) # (B, )
            F_bev_norm = torch.maximum(F_bev_norm, 1e-6 * torch.ones_like(F_bev_norm))
            F_bev = F_bev / F_bev_norm.view(B, 1)

            F_s2bev_norm = torch.norm(F_s2bev, p=2, dim=-1) # (B, )
            F_s2bev_norm = torch.maximum(F_s2bev_norm, 1e-6 * torch.ones_like(F_s2bev_norm))
            F_s2bev = F_s2bev / F_s2bev_norm.view(B, 1)

            # jac = jac / F_s2bev_norm.view(1, B, 1, 1, 1)

        e = F_bev - F_s2bev # (B, (CHW))

        J = jac.view(3, B, C * H * W) # (3, B, (CHW))
        J = J.permute(1, 2, 0) # (B, (CHW), 3)
        J_transpose = J.permute(0, 2, 1) # (B, 3, (CHW))
        Hessian = J_transpose @ J # (B, 3, 3)
        diag_Hessian = torch.eye(Hessian.shape[-1], requires_grad=True).to(device=DEVICE)
        delta_pose = torch.inverse(Hessian + diag_Hessian * DAMPING) @ J_transpose @ e.view(B, C * H * W, 1) # (B, 3, 1)

        # print("delta_pose = ", delta_pose)
        learned_u_new = learned_u + delta_pose[:, 0, 0] # (B, )
        learned_v_new = learned_v + delta_pose[:, 1, 0]
        learned_theta_new = learned_theta + delta_pose[:, 2, 0]

        # Debug (leekt): Avoid large value
        rand_u = torch.distributions.uniform.Uniform(-1, 1).sample([B, 1]).to(device=DEVICE)
        rand_v = torch.distributions.uniform.Uniform(-1, 1).sample([B, 1]).to(device=DEVICE)
        rand_u.requires_grad = True
        rand_v.requires_grad = True
        learned_u_new = torch.where((learned_u_new > -2.5) & (learned_u_new < 2.5), learned_u_new, rand_u)
        learned_v_new = torch.where((learned_v_new > -2.5) & (learned_v_new < 2.5), learned_v_new, rand_v)

        return learned_u_new, learned_v_new, learned_theta_new


    def LM_update(self, shift_u, shift_v, theta, sat_feat_proj, sat_conf_proj, grd_feat, grd_conf, dfeat_dpose):
        '''
        Args:
            shift_u: [B, 1]
            shift_v: [B, 1]
            theta: [B, 1]
            sat_feat_proj: [B, C, H, W]
            sat_conf_proj: [B, 1, H, W]
            grd_feat: [B, C, H, W]
            grd_conf: [B, 1, H, W]
            dfeat_dpose: [3, B, C, H, W]

        Returns:
            shift_u_new
            shift_v_new
            theta_new

        Note:
            grrd_conf: is used for calculating 'weight'
            if args.use_wight:
                weight = (grd_conf[:, None, :]).repeat(1, C, 1).reshape(B, -1)
            This weight is later used for J' calculation
            >> temp = J.transpose(1, 2) * weight.unsqueeze(dim=1)

            sat_conf_proj: not used. Only itself.

        '''
        if self.args.rotation_range == 0:
            dfeat_dpose = dfeat_dpose[:2, ...]
        elif self.args.shift_range_lat == 0 and self.args.shift_range_lon == 0:
            dfeat_dpose = dfeat_dpose[2:, ...]

        N, B, C, H, W = dfeat_dpose.shape
        if self.args.train_damping:
            # damping = self.damping
            min_, max_ = -6, 5
            damping = 10.**(min_ + self.damping.sigmoid()*(max_ - min_))
        else:
            damping = (self.args.damping * torch.ones(size=(1, N), dtype=torch.float32, requires_grad=True)).to(
                dfeat_dpose.device)

        if self.args.dropout > 0:
            inds = np.random.permutation(np.arange(H * W))[: H*W//2]
            dfeat_dpose = dfeat_dpose.reshape(N, B, C, -1)[:, :, :, inds].reshape(N, B, -1)
            sat_feat_proj = sat_feat_proj.reshape(B, C, -1)[:, :, inds].reshape(B, -1)
            grd_feat = grd_feat.reshape(B, C, -1)[:, :, inds].reshape(B, -1)
            sat_conf_proj = sat_conf_proj.reshape(B, -1)[:, inds]
            grd_conf = grd_conf.reshape(B, -1)[:, inds]
        else:
            dfeat_dpose = dfeat_dpose.reshape(N, B, -1)
            sat_feat_proj = sat_feat_proj.reshape(B, -1)
            grd_feat = grd_feat.reshape(B, -1)
            sat_conf_proj = sat_conf_proj.reshape(B, -1)
            grd_conf = grd_conf.reshape(B, -1)

        sat_feat_norm = torch.norm(sat_feat_proj, p=2, dim=-1)
        sat_feat_norm = torch.maximum(sat_feat_norm, 1e-6 * torch.ones_like(sat_feat_norm))
        sat_feat_proj = sat_feat_proj / sat_feat_norm[:, None]
        dfeat_dpose = dfeat_dpose / sat_feat_norm[None, :, None]  # [N, B, D]

        grd_feat_norm = torch.norm(grd_feat, p=2, dim=-1)
        grd_feat_norm = torch.maximum(grd_feat_norm, 1e-6 * torch.ones_like(grd_feat_norm))
        grd_feat = grd_feat / grd_feat_norm[:, None]

        # This is the error e (differences between the satellite and ground features) in eqn(5) of paper
        r = sat_feat_proj - grd_feat  # [B, D]

        if self.using_weight:
            # weight = (sat_conf_proj * grd_conf).repeat(1, C, 1, 1).reshape(B, C * H * W)
            weight = (grd_conf[:, None, :]).repeat(1, C, 1).reshape(B, -1)
        else:
            weight = torch.ones([B, grd_feat.shape[-1]], dtype=torch.float32, device=shift_u.device, requires_grad=True)

        J = dfeat_dpose.permute(1, 2, 0)  # [B, C*H*W, #pose]
        temp = J.transpose(1, 2) * weight.unsqueeze(dim=1)
        Hessian = temp @ J  # [B, #pose, #pose]
        # print('===================')
        # print('Hessian.shape', Hessian.shape)
        if self.args.use_hessian:
            diag_H = torch.diag_embed(torch.diagonal(Hessian, dim1=1, dim2=2))  # [B, 3, 3]
            # print('diag_H.shape', diag_H.shape)
        else:
            diag_H = torch.eye(Hessian.shape[-1], requires_grad=True).unsqueeze(dim=0).repeat(B, 1, 1).to(
                Hessian.device)
        # print('Hessian + damping * diag_H.shape ', (Hessian + damping * diag_H).shape)
        delta_pose = - torch.inverse(Hessian + damping * diag_H) \
                     @ temp @ r.reshape(B, -1, 1)

        if self.args.rotation_range == 0:
            shift_u_new = shift_u + delta_pose[:, 0:1, 0]
            shift_v_new = shift_v + delta_pose[:, 1:2, 0]
            theta_new = theta
        elif self.args.shift_range_lat == 0 and self.args.shift_range_lon == 0:
            theta_new = theta + delta_pose[:, 0:1, 0]
            shift_u_new = shift_u
            shift_v_new = shift_v
        else:
            shift_u_new = shift_u + delta_pose[:, 0:1, 0]
            shift_v_new = shift_v + delta_pose[:, 1:2, 0]
            theta_new = theta + delta_pose[:, 2:3, 0]

            rand_u = torch.distributions.uniform.Uniform(-1, 1).sample([B, 1]).to(shift_u.device)
            rand_v = torch.distributions.uniform.Uniform(-1, 1).sample([B, 1]).to(shift_u.device)
            rand_u.requires_grad = True
            rand_v.requires_grad = True
            shift_u_new = torch.where((shift_u_new > -2.5) & (shift_u_new < 2.5), shift_u_new, rand_u)
            shift_v_new = torch.where((shift_v_new > -2.5) & (shift_v_new < 2.5), shift_v_new, rand_v)
            # shift_u_new = torch.where((shift_u_new > -2) & (shift_u_new < 2), shift_u_new, rand_u)
            # shift_v_new = torch.where((shift_v_new > -2) & (shift_v_new < 2), shift_v_new, rand_v)

            if torch.any(torch.isnan(theta_new)):
                print('theta_new is nan')
                print(theta, delta_pose[:, 2:3, 0], Hessian)

        return shift_u_new, shift_v_new, theta_new

    def NN_update(self, shift_u, shift_v, theta, sat_feat_proj, sat_conf_proj, grd_feat, grd_conf, eat_dpose):

        delta = self.NNrefine(sat_feat_proj, grd_feat)  # [B, 3]
        # print('=======================')
        # print('delta.shape: ', delta.shape)
        # print('shift_u.shape', shift_u.shape)
        # print('=======================')

        shift_u_new = shift_u + delta[:, 0:1]
        shift_v_new = shift_v + delta[:, 1:2]
        theta_new = theta + delta[:, 2:3]
        return shift_u_new, shift_v_new, theta_new

    def SGD_update(self, shift_u, shift_v, theta, sat_feat_proj, sat_conf_proj, grd_feat, grd_conf, dfeat_dpose):
        '''
        Args:
            shift: [B, 2]
            heading: [B, 1]
            sat_feat_proj: [B, C, H, W]
            sat_conf_proj: [B, 1, H, W]
            grd_feat: [B, C, H, W]
            grd_conf: [B, 1, H, W]
            dfeat_dpose: [3, B, C, H, W]
        Returns:
        '''

        B, C, H, W = grd_feat.shape
        r = sat_feat_proj - grd_feat  # [B, C, H, W]

        # idx0 = torch.le(r, 0)
        # idx1 = torch.greater(r, 0)
        # mask = idx0 * (-1) + idx1
        # dr_dfeat = mask.float() / (C * H * W)  # [B, C, H, W]
        dr_dfeat = 2 * r #/ (C * H * W)  # this is grad for l2 loss, above is grad for l1 loss
        delta_pose = torch.sum(dr_dfeat[None, ...] * dfeat_dpose, dim=[2, 3, 4]).transpose(0, 1)  # [B, #pose]

        # print(delta_pose)

        shift_u_new = shift_u - 0.01 * delta_pose[:, 0:1]
        shift_v_new = shift_v - 0.01 * delta_pose[:, 1:2]
        theta_new = theta - 0.01 * delta_pose[:, 2:3]
        return shift_u_new, shift_v_new, theta_new

    def ADAM_update(self, shift_u, shift_v, theta, sat_feat_proj, sat_conf_proj, grd_feat, grd_conf, dfeat_dpose, m, v, t):
        '''
        Args:
            shift: [B, 2]
            heading: [B, 1]
            sat_feat_proj: [B, C, H, W]
            sat_conf_proj: [B, 1, H, W]
            grd_feat: [B, C, H, W]
            grd_conf: [B, 1, H, W]
            dfeat_dpose: [3, B, C, H, W]
            m: [B, #pose], accumulator in ADAM
            v: [B, #pose], accumulator in ADAM
            t: scalar, current iteration number
        Returns:
        '''

        B, C, H, W = grd_feat.shape
        r = sat_feat_proj - grd_feat  # [B, C, H, W]

        # idx0 = torch.le(r, 0)
        # idx1 = torch.greater(r, 0)
        # mask = idx0 * (-1) + idx1
        # dr_dfeat = mask.float() / (C * H * W)  # [B, C, H, W]
        dr_dfeat = 2 * r #/ (C * H * W)  # this is grad for l2 loss, above is grad for l1 loss
        delta_pose = torch.sum(dr_dfeat[None, ...] * dfeat_dpose, dim=[2, 3, 4]).transpose(0, 1)  # [B, #pose]

        # adam optimizer
        m = self.args.beta1 * m + (1- self.args.beta1) * delta_pose
        v = self.args.beta2 * v + (1- self.args.beta2) * (delta_pose * delta_pose)
        m_hat = m / (1 - self.args.beta1 ** (t+1))
        v_hat = v / (1 - self.args.beta2 ** (t+1))
        delta_final = m_hat / (v_hat ** 0.5 + 1e-8)

        # print(delta_pose)

        shift_u_new = shift_u - 0.01 * delta_final[:, 0:1]
        shift_v_new = shift_v - 0.01 * delta_final[:, 1:2]
        theta_new = theta - 0.01 * delta_final[:, 2:3]
        return shift_u_new, shift_v_new, theta_new, m, v

    def forward(self, sat_map, grd_imgs, intrinsics, extrinsics, gt_shiftu=None, gt_shiftv=None, gt_heading=None, meter_per_pixel=None, sample_name=None, mode='train',
                file_name=None, gt_depth=None, loop=0, level_first=0):
        '''
        :param sat_map: [B, C, A, A] A--> sidelength
        :param grd_img_left: [B, C, H, W]
        :return: 
                loss, 
                loss_decrease, shift_lat_decrease, shift_lon_decrease, thetas_decrease, \
                loss_last, shift_lat_last, shift_lon_last, theta_last, \
                L1_loss, L2_loss, L3_loss, L4_loss, grd_conf_list
        '''
        if level_first:
            return self.forward_level_first(sat_map, grd_imgs, intrinsics, extrinsics, gt_shiftu, gt_shiftv, gt_heading, meter_per_pixel, sample_name, \
                mode, file_name, gt_depth, loop)
        else:
            return self.forward_iter_first(sat_map, grd_imgs, intrinsics, extrinsics, gt_shiftu, gt_shiftv, gt_heading, meter_per_pixel, sample_name, \
                mode, file_name, gt_depth, loop)


    def forward_iter_first(self, sat_map, grd_imgs, intrinsics, extrinsics, gt_shiftu=None, gt_shiftv=None, gt_heading=None, meter_per_pixel=None, sample_name=None, mode='train',
                file_name=None, gt_depth=None, loop=0):
        '''
        :param sat_map: [B, C, A, A] A--> sidelength
        :param grd_img_left: [B, C, H, W]
        :return:


        sat_map:  [1, 1, 3, 512, 512] (config/data.satellite_image.h/w)
        grd_imgs: [1, 6, 3, 224, 480] (config/data.image.h/w)

        sat_feat: [1, 3, 200, 200] (200 is defined in config/data.bev.h/w)
        grd_feat: [1, 3, 200, 200]

        * If batch_size = 4: kernel attention has problem (satellite net)
      
        '''

        # ---------- Multi-level Transformer ----------#
        '''
            In order to generate features in multiple levels,
            In the last layer of the transformer, we should output multiple tensors representing features under different scale
            Refer to the structure in VGG.py in Highlyaccurate:
            If level==3: it returns a list of tensors (x15, x18, x21) where 

        '''

        # ---------- Satellite Network ------------- #
        # Note: I/E is not used in sat_feat generation
        satnet_input = {'image': sat_map.unsqueeze(1),  'intrinsics': torch.eye(3,device=sat_map.device), 'extrinsics': torch.eye(4, device=sat_map.device).reshape(1, 1, 4, 4)}
        sat_feat_dict= self.SatFeatureNet(satnet_input)
        sat_feat_list = []
        for _ in range(len(sat_feat_dict)):
            sat_feat_list.append(sat_feat_dict['bev'])
        B, C, H, W = sat_feat_list[0].shape
        
        # ---------- GroundImgs Network ------------- #
        grdnet_input = {'image': grd_imgs, 'intrinsics': intrinsics, 'extrinsics':extrinsics}
        grd_feat_dict = self.GrdFeatureNet(grdnet_input)
        grd_feat_list = []
        for _ in range(len(grd_feat_dict)):
            grd_feat_list.append(grd_feat_dict['bev']) # (1, 3, 64, 64) 
        
        # TODO: Modify grd_conf_list
        conf_tensor = torch.ones([B, 1, H, W])
        scale = 0.1
        sat_conf_list = [torch.ones_like(conf_tensor, device=sat_map.device)*scale ]       
        grd_conf_list = [torch.ones_like(conf_tensor, device=sat_map.device)*scale ]

        # ---------- shift_u, shif_v, heading initialization -------------------------------------- #
        shift_u = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        shift_v = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        heading = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)

        gt_uv_dict = {}
        gt_feat_dict = {}
        pred_uv_dict = {}
        pred_feat_dict = {}
        shift_us_all = []
        shift_vs_all = []
        headings_all = []

        self.counts += 1

        for iter in range(self.N_iters):
            shift_us = []
            shift_vs = []
            headings = []
            for level in range(len(sat_feat_list)):
                
                sat_conf = sat_conf_list[level]
                grd_feat = grd_feat_list[level] # (1, 10, 128, 128)
                _, _, H_grd, W_grd = grd_feat.shape
                grd_conf = grd_conf_list[level]       

                # print(f'models_nuscenes: sample_name: {sample_name}')
                # This is a PARAMETER: which sample are we visualizing within this batch of features?
                timestamp_idx = 0

                sample_name_idx = int(sample_name[timestamp_idx].split('-')[-1])
                SAVE_IMAGE = True

                sat_feat = sat_feat_list[level] # (1, 10, 320, 320)

                save_image(sat_feat[timestamp_idx, -3:, :, :], f'origin_sat_feat_iter_{self.counts}_{sample_name[timestamp_idx]}.png')
                
                B_sat, C_sat, H_sat, W_sat = sat_feat.shape
                # B, C, H, W = sat_feat.shape
                meter_per_pixel_sat_feat = meter_per_pixel[0].item() * (self.data_dict.satellite_image.h/H_sat)
                # Extract BEV meter_per_pixel
                meter_per_pixel_BEV = self.data_dict.bev.h_meters / H_grd # (100 / 128)
                H_sat_resize, W_sat_resize = math.floor(H_sat * meter_per_pixel_sat_feat / meter_per_pixel_BEV), math.floor(W_sat * meter_per_pixel_sat_feat / meter_per_pixel_BEV)

                # print("H_grd = ", H_grd) # (100)
                # print("H_sat = ", H_sat) # (320)
                # print("H_sat_resize = ", H_sat_resize) # (489)

                sat_feat_transform = transforms.Resize(size=(H_sat_resize, W_sat_resize))                
                sat_feat_resized = sat_feat_transform(sat_feat) 

                sat_feat_transformed = sat_feat_resized.clone()
                # sat_feat_transformed.requires_grad = True

                for b in range(B_sat):
                    sat_feat_transformed[b] = TF.affine(sat_feat_resized[b], angle=heading[b].item(), translate=(0, 0), scale=1.0, shear=0)
                    sat_feat_transformed[b] = TF.affine(sat_feat_resized[b], angle=0.0, translate=(shift_u[b].item() / meter_per_pixel_sat_feat, shift_v[b].item() / meter_per_pixel_sat_feat), scale=1.0, shear=0)

                sat_feat_crop = TF.center_crop(sat_feat_transformed, H_grd)


                # print("sat_feat.shape = ", sat_feat.shape)
                # print("----------------------------------------")
                # print(f'grd_feat.shape = {grd_feat.shape}')
                # print("----------------------------------")

                sat_feat = sat_feat_crop


                if iter == 0 and SAVE_IMAGE and self.counts % 10 == 1:
                    print("self.counts = ", self.counts)
                    # print(f'sat_feat.shape {sat_feat.shape}')
                    sat_feat_last_3_dim = sat_feat[timestamp_idx, -3:, :, :] # (3, 128, 128)
                    save_image(sat_feat_last_3_dim, f'sat_feat_iter_{self.counts}_{sample_name[timestamp_idx]}.png')

                    grd_feat_last_3_dim = grd_feat[timestamp_idx, -3:, :, :]
                    save_image(grd_feat_last_3_dim, f"grd_feat_iter_{self.counts}_{sample_name[timestamp_idx]}.png")

                sat_feat_proj = sat_feat      

                small_const = 0.1
                sat_uv        = torch.ones([1, 1, 1, 2], device=sat_map.device) * small_const
                # [B, C, H, W], [B, 1, H, W], [3, B, C, H, W], [B, H, W, 2]
 
                sat_feat_new = sat_feat
                sat_conf_new = sat_conf
                grd_feat_new = grd_feat
                grd_conf_new = grd_conf

                # ------------- Partial Derivative of Features w.r.t. Pose(shift_u, shift_v, theta) for LM Optimization ------------- #
                jac_u, jac_v, jac_theta, dfeat_dpose_new = self.cal_jacobian(sat_feat_new)

                # shift_u_new, shift_v_new, heading_new = self.LM_update_2D(shift_u, shift_v, heading, grd_feat_new, sat_feat_new, dfeat_dpose_new)

                # dfeat_dpose_new = torch.ones([3, B, C, self.data_dict.bev.h, self.data_dict.bev.h], device=shift_u.device) #dfeat_dpose               
                
                # sat_feat_shiftu = F.pad(sat_feat[:, :, :, 1:], (0,1), "constant", 0)
                # dfeat_dpose_new[0,...] = sat_feat_shiftu - sat_feat 
                # sat_feat_shiftv = F.pad(sat_feat[:, :, 1:, :], (0,0,0,1), "constant", 0)
                # dfeat_dpose_new[1,...] = sat_feat_shiftv - sat_feat                 
                
                # '''
                #     dR_dtheta       (B, 3, 3)
                #     xyz_bev:        (1, bev_h, bev_w, 3)
                #     Tbev2sat:       (B, 3)
                #     R_bev2sat:      (2, 3)
                #     dxyz_dtheta:    (B, bev_h, bev_w, 3)
                #     meter_per_pixel:(B)
                #     duv_dtheta:     (B, C, bev_h, bev_w)
                # '''                   
                # cos = torch.cos(heading)        # (B,) where B = 4(batch size)
                # sin = torch.sin(heading)        # (B,)
                # zeros = torch.zeros_like(cos)   # (B,)

                # # dR_dtheta = self.args.rotation_range / 180 * np.pi * torch.cat([-sin, -cos, cos, -sin], dim=-1)
                # # dR_dtheta = dR_dtheta.view(B, 1, 2, 2)
                # # print("dR_dtheta.shape = ", dR_dtheta.shape)
                # # print("sat_feat.shape = ", sat_feat.shape)
                # # dfeat_dpose_new[2] = dR_dtheta @ sat_feat
                # dR_dtheta = self.args.rotation_range / 180 * np.pi * \
                #             torch.cat([-sin, zeros, -cos, zeros, zeros, zeros, cos, zeros, -sin], dim=-1)  # shape = [B, 9]
                # dR_dtheta = dR_dtheta.view(B, 3, 3)      

                # # Need: xyz_bev, Tbev2sat, R_bev2sat(bev2sat)
                # _, _, bev_H, bev_W = grd_feat.shape
                # v, u = torch.meshgrid(torch.arange(0, bev_H, dtype=torch.float32, device=dR_dtheta.device),
                #                       torch.arange(0, bev_W, dtype=torch.float32, device=dR_dtheta.device))
                # xyz_bev = torch.stack([u, v, torch.ones_like(u)], dim=-1).unsqueeze(dim=0)  # [1, grd_H, grd_W, 3]                
                # camera_height = utils.get_camera_height()
                # # camera offset, shift[0]:east,Z, shift[1]:north,X
                # height = camera_height * torch.ones_like(shift_u[:, :1])                
                # Tbev2sat = torch.cat([shift_v, height, -shift_u], dim=-1)  # shape = [B, 3]
                # R_bev2sat = torch.tensor([0, 0, 1, 1, 0, 0], \
                #             dtype=torch.float32, device=dR_dtheta.device, requires_grad=True).reshape(2, 3)

                # dxyz_dtheta = torch.sum(dR_dtheta[:, None, None, :, :] * xyz_bev[:, :, :, None, :], dim=-1) + \
                #               torch.sum(-dR_dtheta * Tbev2sat[:, None, :], dim=-1)[:, None, None, :]
             
                # # Note: meter_per_pixel is shape (B) here we take only the first one!
                # # denom: (B, bev_h, bev_w, 2) (4, 128, 128, 2)
                # duv_dtheta = 1 / meter_per_pixel[0] * \
                #         torch.sum(R_bev2sat[None, None, None, :, :] * dxyz_dtheta[:, :, :, None, :], dim=-1)  
                # # (B, bev_h, bev_w, 2)  => (B, 1, bev_h, bev_w, 2) => (B, C, bev_h, bev_W)
                # duv_dtheta = torch.sum(duv_dtheta[:, None, :, :, :].expand(-1, C, -1, -1, -1), dim=-1)
                # dfeat_dpose_new[2,...] = duv_dtheta        


                if self.args.Optimizer == 'LM':
                    # Check devices                 
                    shift_u_new, shift_v_new, heading_new = self.LM_update_2D(shift_u, shift_v, heading, grd_feat_new, sat_feat_new, dfeat_dpose_new)
                    # shift_u_new, shift_v_new, heading_new = self.LM_update(shift_u, shift_v, heading,
                    #                                         sat_feat_new,
                    #                                         sat_conf_new,
                    #                                         grd_feat_new,
                    #                                         grd_conf_new,
                    #                                         dfeat_dpose_new)  # only need to compare bottom half
                    
                    print("shift_u_new = ", shift_u_new)
                    print("shift_v_new = ", shift_v_new)
                    print("heading_new = ", heading_new)

                elif self.args.Optimizer == 'SGD':
                    r = sat_feat_proj[:, :, grd_H // 2:, :] - grd_feat[:, :, grd_H // 2:, :]
                    p = torch.mean(torch.abs(r), dim=[1, 2, 3])  # *100 #* 256 * 256 * 3
                    dp_dshiftu = torch.autograd.grad(p, shift_u, retain_graph=True, create_graph=True,
                                                 only_inputs=True)[0]
                    dp_dshiftv = torch.autograd.grad(p, shift_v, retain_graph=True, create_graph=True,
                                                     only_inputs=True)[0]
                    dp_dheading = torch.autograd.grad(p, heading, retain_graph=True, create_graph=True,
                                                    only_inputs=True)[0]
                    # print(dp_dshiftu)
                    # print(dp_dshiftv)
                    # print(dp_dheading)

                    shift_u_new, shift_v_new, heading_new = self.SGD_update(shift_u, shift_v, heading,
                                                                           sat_feat_new,
                                                                           sat_conf_new,
                                                                           grd_feat_new,
                                                                           grd_conf_new,
                                                                           dfeat_dpose_new)
                elif self.args.Optimizer == 'NN':
                    shift_u_new, shift_v_new, heading_new = self.NN_update(shift_u, shift_v, heading,
                                                                         sat_feat_new,
                                                                         sat_conf_new,
                                                                         grd_feat_new,
                                                                         grd_conf_new,
                                                                         dfeat_dpose_new)
                elif self.args.Optimizer == 'ADAM':
                    t = iter * self.args.level + level
                    if t==0:
                        m = 0
                        v = 0
                    shift_u_new, shift_v_new, heading_new, m, v = self.ADAM_update(shift_u, shift_v, heading,
                                                                         sat_feat_new,
                                                                         sat_conf_new,
                                                                         grd_feat_new,
                                                                         grd_conf_new,
                                                                         dfeat_dpose_new,
                                                                         m, v, t)


                shift_us.append(shift_u_new[:, 0] * meter_per_pixel_BEV)  # [B]
                shift_vs.append(shift_v_new[:, 0] * meter_per_pixel_BEV)  # [B]
                headings.append(heading_new[:, 0])  # [B]

                shift_u = shift_u_new.clone()
                shift_v = shift_v_new.clone()
                heading = heading_new.clone()

                if level not in pred_feat_dict.keys():
                    pred_feat_dict[level] = [sat_feat_proj]
                    pred_uv_dict[level] = [sat_uv / torch.tensor([sat_feat.shape[-1], sat_feat.shape[-2]], dtype=torch.float32).reshape(1, 1, 1, 2).to(sat_feat.device)]
                else:
                    pred_feat_dict[level].append(sat_feat_proj)
                    pred_uv_dict[level].append(sat_uv / torch.tensor([sat_feat.shape[-1], sat_feat.shape[-2]], dtype=torch.float32).reshape(1, 1, 1, 2).to(sat_feat.device))

                # Debug
                # if level not in gt_uv_dict.keys() and mode == 'train':
                #     gt_sat_feat_proj, _, _, gt_uv, _ = self.project_map_to_grd(
                #         sat_feat, None, gt_shiftu, gt_shiftv, gt_heading, level, require_jac=False, gt_depth=gt_depth)
                #     # [B, C, H, W], [B, H, W, 2]
                #     gt_feat_dict[level] = gt_sat_feat_proj # [B, C, H, W]
                #     gt_uv_dict[level] = gt_uv / torch.tensor([sat_feat.shape[-1], sat_feat.shape[-2]], dtype=torch.float32).reshape(1, 1, 1, 2).to(sat_feat.device)
                #     # [B, H, W, 2]

            shift_us_all.append(torch.stack(shift_us, dim=1))  # [B, Level]
            shift_vs_all.append(torch.stack(shift_vs, dim=1))  # [B, Level]
            headings_all.append(torch.stack(headings, dim=1))  # [B, Level]


        # Aggregate shift_vs_all / shift_us_all / headings_all to tensors
        # for later loss_func
        shift_lats = torch.stack(shift_vs_all, dim=1)  # [B, N_iters, Level]
        shift_lons = torch.stack(shift_us_all, dim=1)  # [B, N_iters, Level]
        thetas = torch.stack(headings_all, dim=1)  # [B, N_iters, Level]

        # print(f'shift_lats.shape {shift_lats.shape}') # (1, 5, 1)
        # print(f'gt_shiftv[:, 0].shape {gt_shiftv[:, 0].shape}') # [1]

        if self.args.visualize:
            from visualize_utils import features_to_RGB, RGB_iterative_pose
            # save_dir = './visualize_rot' + str(self.args.rotation_range)
            save_dir = '.'
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            features_to_RGB(sat_feat_list, grd_feat_list, pred_feat_dict, gt_feat_dict, loop,
                            save_dir)
            RGB_iterative_pose(sat_map, grd_imgs, shift_lats, shift_lons, thetas, gt_shiftu, gt_shiftv, gt_heading,
                                self.meters_per_pixel[-1], self.args, loop, save_dir)


        if mode == 'train':

            if self.args.rotation_range == 0:
                coe_heading = 0
            else:
                coe_heading = self.args.coe_heading

            loss, loss_decrease, shift_lat_decrease, shift_lon_decrease, thetas_decrease, loss_last, \
            shift_lat_last, shift_lon_last, theta_last, \
            L1_loss, L2_loss, L3_loss, L4_loss \
                = loss_func(self.args.loss_method, grd_feat_list, pred_feat_dict, gt_feat_dict,
                            shift_lats, shift_lons, thetas, gt_shiftv[:, 0], gt_shiftu[:, 0], gt_heading[:, 0],
                            pred_uv_dict, gt_uv_dict,
                            self.args.coe_shift_lat, self.args.coe_shift_lon, coe_heading,
                            self.args.coe_L1, self.args.coe_L2, self.args.coe_L3, self.args.coe_L4)

            print("loss = ", loss)

            with open(self.loss_file, 'a') as f:
                f.write(f'{loss}\n')
            return loss, loss_decrease, shift_lat_decrease, shift_lon_decrease, thetas_decrease, loss_last, \
                       shift_lat_last, shift_lon_last, theta_last, \
                       L1_loss, L2_loss, L3_loss, L4_loss, grd_conf_list
        else:
            return shift_lats[:, -1, -1], shift_lons[:, -1, -1], thetas[:, -1, -1]

    def forward_level_first(self, sat_map, grd_imgs, intrinsics_dict, extrinsics, gt_shiftu=None, gt_shiftv=None, gt_heading=None, meter_per_pixel=None, sample_name=None, mode='train',
                file_name=None, gt_depth=None, loop=0):
        '''
        :param sat_map: [B, C, A, A] A--> sidelength
        :param grd_img_left: [B, C, H, W]
        :return:
        '''

        B, _, ori_grdH, ori_grdW = grd_imgs.shape

        # A = sat_map.shape[-1]
        # sat_img_proj, _, _, _, _ = self.project_map_to_grd(
        #     grd_img_left, None, gt_shiftu, gt_shiftv, gt_heading, level=3, require_jac=True, gt_depth=gt_depth)
        # sat_img = transforms.ToPILImage()(sat_img_proj[0])
        # sat_img.save('sat_proj.png')
        # grd = transforms.ToPILImage()(grd_img_left[0])
        # grd.save('grd.png')
        # sat = transforms.ToPILImage()(sat_map[0])
        # sat.save('sat.png')

        sat_feat_list, sat_conf_list = self.SatFeatureNet(sat_map)

        grd_feat_list, grd_conf_list = self.GrdFeatureNet(grd_imgs)

        shift_u = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        shift_v = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)
        heading = torch.zeros([B, 1], dtype=torch.float32, requires_grad=True, device=sat_map.device)

        # shift_u = shift_u * self.args.shift_range_lon / meter_per_pixel[0]
        # shift_v = shift_v * self.args.shift_range_lon / meter_per_pixel[0]

        gt_uv_dict = {}
        gt_feat_dict = {}
        pred_uv_dict = {}
        pred_feat_dict = {}
        shift_us_all = []
        shift_vs_all = []
        headings_all = []
        for level in range(len(sat_feat_list)):

            shift_us = []
            shift_vs = []
            headings = []
            for iter in range(self.N_iters):
                sat_feat = sat_feat_list[level]
                sat_conf = sat_conf_list[level]
                grd_feat = grd_feat_list[level]
                grd_conf = grd_conf_list[level]

                grd_H, grd_W = grd_feat.shape[-2:]
                sat_feat_proj, sat_conf_proj, dfeat_dpose, sat_uv, mask = self.project_map_to_grd(
                    sat_feat, sat_conf, shift_u, shift_v, heading, level, gt_depth=gt_depth)
                # [B, C, H, W], [B, 1, H, W], [3, B, C, H, W], [B, H, W, 2]

                grd_feat = grd_feat * mask[:, None, :, :]
                grd_conf = grd_conf * mask[:, None, :, :]

                if self.args.proj == 'geo':
                    sat_feat_new = sat_feat_proj[:, :, grd_H // 2:, :]
                    sat_conf_new = sat_conf_proj[:, :, grd_H // 2:, :]
                    grd_feat_new = grd_feat[:, :, grd_H // 2:, :]
                    grd_conf_new = grd_conf[:, :, grd_H // 2:, :]
                    dfeat_dpose_new = dfeat_dpose[:, :, :, grd_H // 2:, :]
                else:
                    sat_feat_new = sat_feat_proj
                    sat_conf_new = sat_conf_proj
                    grd_feat_new = grd_feat
                    grd_conf_new = grd_conf
                    dfeat_dpose_new = dfeat_dpose

                if self.args.Optimizer == 'LM':
                    shift_u_new, shift_v_new, heading_new = self.LM_update(shift_u, shift_v, heading,
                                                            sat_feat_new,
                                                            sat_conf_new,
                                                            grd_feat_new,
                                                            grd_conf_new,
                                                            dfeat_dpose_new)  # only need to compare bottom half
                elif self.args.Optimizer == 'SGD':
                    # r = sat_feat_proj[:, :, grd_H // 2:, :] - grd_feat[:, :, grd_H // 2:, :]
                    # p = torch.mean(torch.abs(r), dim=[1, 2, 3])  # *100 #* 256 * 256 * 3
                    # dp_dshiftu = torch.autograd.grad(p, shift_u, retain_graph=True, create_graph=True,
                    #                              only_inputs=True)[0]
                    # dp_dshiftv = torch.autograd.grad(p, shift_v, retain_graph=True, create_graph=True,
                    #                                  only_inputs=True)[0]
                    # dp_dheading = torch.autograd.grad(p, heading, retain_graph=True, create_graph=True,
                    #                                 only_inputs=True)[0]
                    # print(dp_dshiftu)
                    # print(dp_dshiftv)
                    # print(dp_dheading)

                    shift_u_new, shift_v_new, heading_new = self.SGD_update(shift_u, shift_v, heading,
                                                                           sat_feat_new,
                                                                           sat_conf_new,
                                                                           grd_feat_new,
                                                                           grd_conf_new,
                                                                           dfeat_dpose_new)

                elif self.args.Optimizer == 'NN':
                    shift_u_new, shift_v_new, heading_new = self.NN_update(shift_u, shift_v, heading,
                                                                         sat_feat_new,
                                                                         sat_conf_new,
                                                                         grd_feat_new,
                                                                         grd_conf_new,
                                                                         dfeat_dpose_new)
                elif self.args.Optimizer == 'ADAM':
                    t = iter * self.args.level + level
                    if t==0:
                        m = 0
                        v = 0
                    shift_u_new, shift_v_new, heading_new, m, v = self.ADAM_update(shift_u, shift_v, heading,
                                                                         sat_feat_new,
                                                                         sat_conf_new,
                                                                         grd_feat_new,
                                                                         grd_conf_new,
                                                                         dfeat_dpose_new,
                                                                         m, v, t)


                shift_us.append(shift_u_new[:, 0])  # [B]
                shift_vs.append(shift_v_new[:, 0])  # [B]
                headings.append(heading_new[:, 0])  # [B]

                shift_u = shift_u_new.clone()
                shift_v = shift_v_new.clone()
                heading = heading_new.clone()

                if level not in pred_feat_dict.keys():
                    pred_feat_dict[level] = [sat_feat_proj]
                    pred_uv_dict[level] = [sat_uv / torch.tensor([sat_feat.shape[-1], sat_feat.shape[-2]], dtype=torch.float32).reshape(1, 1, 1, 2).to(sat_feat.device)]
                else:
                    pred_feat_dict[level].append(sat_feat_proj)
                    pred_uv_dict[level].append(sat_uv / torch.tensor([sat_feat.shape[-1], sat_feat.shape[-2]], dtype=torch.float32).reshape(1, 1, 1, 2).to(sat_feat.device))

                if level not in gt_uv_dict.keys() and mode == 'train':
                    gt_sat_feat_proj, _, _, gt_uv, _ = self.project_map_to_grd(
                        sat_feat, None, gt_shiftu, gt_shiftv, gt_heading, level, require_jac=False, gt_depth=gt_depth)
                    # [B, C, H, W], [B, H, W, 2]
                    gt_feat_dict[level] = gt_sat_feat_proj # [B, C, H, W]
                    gt_uv_dict[level] = gt_uv / torch.tensor([sat_feat.shape[-1], sat_feat.shape[-2]], dtype=torch.float32).reshape(1, 1, 1, 2).to(sat_feat.device)
                    # [B, H, W, 2]

            shift_us_all.append(torch.stack(shift_us, dim=1))  # [B, N_iters]
            shift_vs_all.append(torch.stack(shift_vs, dim=1))  # [B, N_iters]
            headings_all.append(torch.stack(headings, dim=1))  # [B, N_iters]

        shift_lats = torch.stack(shift_vs_all, dim=2)  # [B, N_iters, Level]
        shift_lons = torch.stack(shift_us_all, dim=2)  # [B, N_iters, Level]
        thetas = torch.stack(headings_all, dim=2)  # [B, N_iters, Level]

        if self.args.visualize:
            from visualize_utils import features_to_RGB, RGB_iterative_pose
            features_to_RGB(sat_feat_list, grd_feat_list, pred_feat_dict, gt_feat_dict, loop,
                            save_dir='./visualize/')
            RGB_iterative_pose(sat_map, grd_imgs, shift_lats, shift_lons, thetas, gt_shiftu, gt_shiftv, gt_heading,
                               self.meters_per_pixel[-1], self.args, loop, save_dir='./visualize/')


        if mode == 'train':

            if self.args.rotation_range == 0:
                coe_heading = 0
            else:
                coe_heading = self.args.coe_heading

            loss, loss_decrease, shift_lat_decrease, shift_lon_decrease, thetas_decrease, loss_last, \
            shift_lat_last, shift_lon_last, theta_last, \
            L1_loss, L2_loss, L3_loss, L4_loss \
                = loss_func(self.args.loss_method, grd_feat_list, pred_feat_dict, gt_feat_dict,
                            shift_lats, shift_lons, thetas, gt_shiftv[:, 0], gt_shiftu[:, 0], gt_heading[:, 0],
                            pred_uv_dict, gt_uv_dict,
                            self.args.coe_shift_lat, self.args.coe_shift_lon, coe_heading,
                            self.args.coe_L1, self.args.coe_L2, self.args.coe_L3, self.args.coe_L4)

            return loss, loss_decrease, shift_lat_decrease, shift_lon_decrease, thetas_decrease, loss_last, \
                       shift_lat_last, shift_lon_last, theta_last, \
                       L1_loss, L2_loss, L3_loss, L4_loss, grd_conf_list
        else:
            return shift_lats[:, -1, -1], shift_lons[:, -1, -1], thetas[:, -1, -1]

    def polar_transform(self, sat_feat, level):
        meters_per_pixel = self.meters_per_pixel[level]

        B, C, A, _ = sat_feat.shape

        grd_H = A // 2
        grd_W = A * 2

        v, u = torch.meshgrid(torch.arange(0, grd_H, dtype=torch.float32),
                              torch.arange(0, 4 * grd_W, dtype=torch.float32))
        v = v.to(sat_feat.device)
        u = u.to(sat_feat.device)
        theta = u / grd_W * np.pi * 2
        radius = (1 - v / grd_H) * 40 / meters_per_pixel  # set radius as 40 meters

        us = A / 2 + radius * torch.cos(np.pi / 4 - theta)
        vs = A / 2 - radius * torch.sin(np.pi / 4 - theta)

        grids = torch.stack([us, vs], dim=-1).unsqueeze(dim=0).repeat(B, 1, 1, 1)  # [B, grd_H, grd_W, 2]

        polar_sat, _ = grid_sample(sat_feat, grids)

        return polar_sat

    def polar_coordinates(self, level):
        meters_per_pixel = self.meters_per_pixel[level]

        # B, C, A, _ = sat_feat.shape
        A = 512 / 2**(3-level)

        grd_H = A // 2
        grd_W = A * 2

        v, u = torch.meshgrid(torch.arange(0, grd_H, dtype=torch.float32),
                              torch.arange(0, 4 * grd_W, dtype=torch.float32))
        # v = v.to(sat_feat.device)
        # u = u.to(sat_feat.device)
        theta = u / grd_W * np.pi * 2
        radius = (1 - v / grd_H) * 40 / meters_per_pixel  # set radius as 40 meters

        us = A / 2 + radius * torch.cos(np.pi / 4 - theta)
        vs = A / 2 - radius * torch.sin(np.pi / 4 - theta)

        grids = torch.stack([us, vs], dim=-1).unsqueeze(dim=0)# .repeat(B, 1, 1, 1)  # [1, grd_H, grd_W, 2]

        # polar_sat, _ = grid_sample(sat_feat, grids)

        return grids

    def orien_corr(self, sat_map, grd_img_left, gt_shiftu=None, gt_shiftv=None, gt_heading=None, mode='train',
                file_name=None, gt_depth=None):
        '''
        :param sat_map: [B, C, A, A] A--> sidelength
        :param grd_img_left: [B, C, H, W]
        :return:
        '''

        B, _, ori_grdH, ori_grdW = grd_img_left.shape

        # A = sat_map.shape[-1]
        # sat_img_proj, _, _, _, _ = self.project_map_to_grd(
        #     grd_img_left, None, gt_shiftu, gt_shiftv, gt_heading, level=3, require_jac=True, gt_depth=gt_depth)
        # sat_img = transforms.ToPILImage()(sat_img_proj[0])
        # sat_img.save('sat_proj.png')
        # grd = transforms.ToPILImage()(grd_img_left[0])
        # grd.save('grd.png')
        # sat = transforms.ToPILImage()(sat_map[0])
        # sat.save('sat.png')

        sat_feat_list, sat_conf_list = self.SatFeatureNet(sat_map)

        grd_feat_list, grd_conf_list = self.GrdFeatureNet(grd_img_left)

        corr_list = []
        for level in range(len(sat_feat_list)):
            sat_feat = sat_feat_list[level]
            grd_feat = grd_feat_list[level]  # [B, C, H, W]
            B, C, H, W = grd_feat.shape
            grd_feat = F.normalize(grd_feat.reshape(B, -1)).reshape(B, -1, H, W)

            grids = self.polar_grids[level].detach().to(sat_feat.device).repeat(B, 1, 1, 1)  # [B, H, 4W, 2]
            polar_sat, _ = grid_sample(sat_feat, grids)
            # polar_sat = self.polar_transform(sat_feat, level)
            # [B, C, H, 4W]

            degree_per_pixel = 90 / W
            n = int(np.ceil(self.args.rotation_range / degree_per_pixel))
            sat_W = polar_sat.shape[-1]
            if sat_W - W < n:
                polar_sat1 = torch.cat([polar_sat[:, :, :, -n:], polar_sat, polar_sat[:, :, :, : (n - sat_W + W)]], dim=-1)
            else:
                polar_sat1 = torch.cat([polar_sat[:, :, :, -n:], polar_sat[:, :, :, : (W + n)]], dim=-1)

            # polar_sat1 = torch.cat([polar_sat, polar_sat[:, :, :, : (W-1)]], dim=-1)
            polar_sat2 = polar_sat1.reshape(1, B*C, H, -1)
            corr = F.conv2d(polar_sat2, grd_feat, groups=B)[0, :, 0, :]  # [B, 4W]

            denominator = F.avg_pool2d(polar_sat1.pow(2), (H, W), stride=1, divisor_override=1)[:, :, 0, :]  # [B, 4W]
            denominator = torch.sum(denominator, dim=1)  # [B, 4W]
            denominator = torch.maximum(torch.sqrt(denominator), torch.ones_like(denominator) * 1e-6)
            corr = 2 - 2 * corr / denominator

            orien = torch.argmin(corr, dim=-1)  # / (4 * W) * 360  # [B]
            orien = (orien - n) * degree_per_pixel

            corr_list.append((corr, degree_per_pixel))

        if mode == 'train':

            return self.triplet_loss(corr_list, gt_heading)
        else:
            return orien

    def triplet_loss(self, corr_list, gt_heading):
        gt = gt_heading * self.args.rotation_range #/ 360

        losses = []
        for level in range(len(corr_list)):
            corr = corr_list[level][0]
            degree_per_pixel = corr_list[level][1]
            B, W = corr.shape
            gt_idx = ((W - 1)/2 + torch.round(gt[:, 0]/degree_per_pixel)).long()

            # gt_idx = (torch.round(gt[:, 0] * (W-1)) % (W-1)).long()

            pos = corr[range(B), gt_idx]  # [B]
            pos_neg = pos[:, None] - corr  # [B, W]
            loss = torch.sum(torch.log(1 + torch.exp(pos_neg * 10))) / (B * (W - 1))
            losses.append(loss)

        return torch.sum(torch.stack(losses, dim=0))



