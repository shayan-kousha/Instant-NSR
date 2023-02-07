import os
import glob
import tqdm
import random
import warnings
import tensorboardX

import numpy as np
import pandas as pd

import time
from datetime import datetime

import cv2
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.distributed as dist
from torch.utils.data import Dataset, DataLoader
from torchmetrics import PeakSignalNoiseRatio

import trimesh
import mcubes
from rich.console import Console
from torch_ema import ExponentialMovingAverage
from nerfstudio.data.scene_box import SceneBox

def seed_everything(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    #torch.backends.cudnn.deterministic = True
    #torch.backends.cudnn.benchmark = True


def lift(x, y, z, intrinsics):
    # x, y, z: [B, N]
    # intrinsics: [B, 3, 3]
    
    fx = intrinsics[..., 0, 0].unsqueeze(-1)
    fy = intrinsics[..., 1, 1].unsqueeze(-1)
    cx = intrinsics[..., 0, 2].unsqueeze(-1)
    cy = intrinsics[..., 1, 2].unsqueeze(-1)
    sk = intrinsics[..., 0, 1].unsqueeze(-1)

    x_lift = (x - cx + cy * sk / fy - sk * y / fy) / fx * z
    y_lift = (y - cy) / fy * z

    # homogeneous
    return torch.stack((x_lift, y_lift, z, torch.ones_like(z)), dim=-1)

def get_rays(c2w, intrinsics, H, W, N_rays=-1):
    # c2w: [B, 4, 4]
    # intrinsics: [B, 3, 3]
    # return: rays_o, rays_d: [B, N_rays, 3]
    # return: select_inds: [B, N_rays]

    device = c2w.device
    rays_o = c2w[..., :3, 3] # [B, 3]
    prefix = c2w.shape[:-2]

    i, j = torch.meshgrid(torch.linspace(0, W-1, W, device=device), torch.linspace(0, H-1, H, device=device)) # for torch < 1.10, should remove indexing='ij'
    i = i.t().reshape([*[1]*len(prefix), H*W]).expand([*prefix, H*W])
    j = j.t().reshape([*[1]*len(prefix), H*W]).expand([*prefix, H*W])

    if N_rays > 0:
        N_rays = min(N_rays, H*W)
        select_hs = torch.randint(0, H, size=[N_rays], device=device)
        select_ws = torch.randint(0, W, size=[N_rays], device=device)
        select_inds = select_hs * W + select_ws
        select_inds = select_inds.expand([*prefix, N_rays])
        i = torch.gather(i, -1, select_inds)
        j = torch.gather(j, -1, select_inds)
    else:
        select_inds = torch.arange(H*W, device=device).expand([*prefix, H*W])

    pixel_points_cam = lift(i, j, torch.ones_like(i), intrinsics=intrinsics)
    pixel_points_cam = pixel_points_cam.transpose(-1, -2)

    world_coords = torch.bmm(c2w, pixel_points_cam).transpose(-1, -2)[..., :3]
    
    rays_d = world_coords - rays_o[..., None, :]
    rays_d = F.normalize(rays_d, dim=-1)

    rays_o = rays_o[..., None, :].expand_as(rays_d)

    return rays_o, rays_d, select_inds

# def get_rays(c2w, K, H, W, N_rays=-1):
#     K = K[0]
#     c2w = c2w[0]
#     device = c2w.device
#     i, j = torch.meshgrid(
#         torch.linspace(0, W-1, W, device=c2w.device),
#         torch.linspace(0, H-1, H, device=c2w.device))  # pytorch's meshgrid has indexing='ij'
#     i = i.t().float()
#     j = j.t().float()
#     # if mode == 'lefttop':
#     #     pass
#     # elif mode == 'center':
#     #     i, j = i+0.5, j+0.5
#     # elif mode == 'random':
#     #     i = i+torch.rand_like(i)
#     #     j = j+torch.rand_like(j)
#     # else:
#     #     raise NotImplementedError

#     # if flip_x:
#     #     i = i.flip((1,))
#     # if flip_y:
#     #     j = j.flip((0,))

#     dirs = torch.stack([(i-K[0][2])/K[0][0], (j-K[1][2])/K[1][1], torch.ones_like(i)], -1)
   
#     # Rotate ray directions from camera frame to the world frame
#     rays_d = torch.sum(dirs[..., np.newaxis, :] * c2w[:3,:3], -1)  # dot product, equals to: [c2w.dot(dir) for dir in dirs]

#     # Translate camera frame's origin to the world frame. It is the origin of all rays.
#     rays_o = c2w[:3,3].expand(rays_d.shape)

#     if N_rays > 0:
#         N_rays = min(N_rays, H*W)
#         select_hs = torch.randint(0, H, size=[N_rays], device=device)
#         select_ws = torch.randint(0, W, size=[N_rays], device=device)
#         select_inds = select_hs * W + select_ws
#         select_inds = select_inds.expand([1, N_rays])
#         rays_o = rays_o[select_hs, select_ws].unsqueeze(0)
#         rays_d = rays_d[select_hs, select_ws].unsqueeze(0)
#     else:
#         select_inds = torch.arange(H*W, device=device).expand([1, H*W])
#         rays_o = rays_o.reshape((1, H*W, 3))
#         rays_d = rays_d.reshape((1, H*W, 3))

#     return rays_o, rays_d, select_inds

def extract_fields(bound_min, bound_max, resolution, query_func):
    N = 64
    X = torch.linspace(bound_min[0], bound_max[0], resolution).split(N)
    Y = torch.linspace(bound_min[1], bound_max[1], resolution).split(N)
    Z = torch.linspace(bound_min[2], bound_max[2], resolution).split(N)

    u = np.zeros([resolution, resolution, resolution], dtype=np.float32)
    #with torch.no_grad():
    for xi, xs in enumerate(X):
        for yi, ys in enumerate(Y):
            for zi, zs in enumerate(Z):
                xx, yy, zz = torch.meshgrid(xs, ys, zs) # for torch < 1.10, should remove indexing='ij'
                pts = torch.cat([xx.reshape(-1, 1), yy.reshape(-1, 1), zz.reshape(-1, 1)], dim=-1) # [1, N, 3]
                val = query_func(pts).reshape(len(xs), len(ys), len(zs)) # [1, N, 1] --> [x, y, z]
                u[xi * N: xi * N + len(xs), yi * N: yi * N + len(ys), zi * N: zi * N + len(zs)] = val.detach().cpu().numpy()
                del val
    return u


def extract_geometry(bound_min, bound_max, resolution, threshold, query_func, use_sdf = False):
    #print('threshold: {}'.format(threshold))
    u = extract_fields(bound_min, bound_max, resolution, query_func)
    if use_sdf:
        u = - 1.0 *u

    #print(u.mean(), u.max(), u.min(), np.percentile(u, 50))
    
    vertices, triangles = mcubes.marching_cubes(u, threshold)

    b_max_np = bound_max.detach().cpu().numpy()
    b_min_np = bound_min.detach().cpu().numpy()

    vertices = vertices / (resolution - 1.0) * (b_max_np - b_min_np)[None, :] + b_min_np[None, :]
    return vertices, triangles



class Trainer(object):
    def __init__(self, 
                 name, # name of this experiment
                 conf, # extra conf
                 model, # network 
                 criterion=None, # loss function, if None, assume inline implementation in train_step
                 optimizer=None, # optimizer
                 ema_decay=None, # if use EMA, set the decay
                 lr_scheduler=None, # scheduler
                 metrics=[], # metrics for evaluation, if None, use val_loss to measure performance, else use the first metric.
                 local_rank=0, # which GPU am I
                 world_size=1, # total num of GPUs
                 device=None, # device to use, usually setting to None is OK. (auto choose device)
                 mute=False, # whether to mute all print
                 fp16=False, # amp optimize level
                 eval_interval=1, # eval once every $ epoch
                 max_keep_ckpt=2, # max num of saved ckpts in disk
                 workspace='workspace', # workspace to save logs & ckpts
                 best_mode='min', # the smaller/larger result, the better
                 use_loss_as_metric=True, # use loss as the first metirc
                 use_checkpoint="latest", # which ckpt to use at init time
                 use_tensorboardX=True, # whether to use tensorboard for logging
                 scheduler_update_every_step=False, # whether to call scheduler.step() after every train step
                 white_background = True,
                 ):
        
        self.name = name
        self.conf = conf
        self.mute = mute
        self.metrics = metrics
        self.local_rank = local_rank
        self.world_size = world_size
        self.workspace = workspace
        self.ema_decay = ema_decay
        self.fp16 = fp16
        self.best_mode = best_mode
        self.use_loss_as_metric = use_loss_as_metric
        self.max_keep_ckpt = max_keep_ckpt
        self.eval_interval = eval_interval
        self.use_checkpoint = use_checkpoint
        self.use_tensorboardX = use_tensorboardX
        self.time_stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        self.scheduler_update_every_step = scheduler_update_every_step
        self.white_background = white_background
        self.device = device if device is not None else torch.device(f'cuda:{local_rank}' if torch.cuda.is_available() else 'cpu')
        self.console = Console()

        self.psnr = PeakSignalNoiseRatio(data_range=1.0).to(self.device)

        model.to(self.device)

        if self.world_size > 1:
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank])
        self.model = model

        if isinstance(criterion, nn.Module):
            criterion.to(self.device)
        self.criterion = criterion

        if optimizer is None:
            self.optimizer = optim.Adam(self.model.parameters(), lr=0.001, weight_decay=5e-4) # naive adam
        else:
            self.optimizer = optimizer(self.model)

        if lr_scheduler is None:
            self.lr_scheduler = optim.lr_scheduler.LambdaLR(self.optimizer, lr_lambda=lambda epoch: 1) # fake scheduler
        else:
            self.lr_scheduler = lr_scheduler(self.optimizer)

        if ema_decay is not None:
            self.ema = ExponentialMovingAverage(self.model.parameters(), decay=ema_decay)
        else:
            self.ema = None

        self.scaler = torch.cuda.amp.GradScaler(enabled=self.fp16)

        # variable init
        self.epoch = 1
        self.global_step = 0
        self.local_step = 0
        self.stats = {
            "loss": [],
            "valid_loss": [],
            "results": [], # metrics[0], or valid_loss
            "checkpoints": [], # record path of saved ckpt, to automatically remove old ckpt
            "best_result": None,
            }

        # auto fix
        if len(metrics) == 0 or self.use_loss_as_metric:
            self.best_mode = 'min'

        # workspace prepare
        self.log_ptr = None
        
        if self.workspace is not None:
            os.makedirs(self.workspace, exist_ok=True)        
            self.log_path = os.path.join(workspace, f"log_{self.name}.txt")
            self.log_ptr = open(self.log_path, "a+")

            self.ckpt_path = os.path.join(self.workspace, 'checkpoints')
            self.best_path = f"{self.ckpt_path}/{self.name}.pth.tar"
            os.makedirs(self.ckpt_path, exist_ok=True)
            
        self.log(f'[INFO] Trainer: {self.name} | {self.time_stamp} | {self.device} | {"fp16" if self.fp16 else "fp32"} | {self.workspace}')
        self.log(f'[INFO] #parameters: {sum([p.numel() for p in model.parameters() if p.requires_grad])}')

        if self.workspace is not None:
            if self.use_checkpoint == "scratch":
                self.log("[INFO] Training from scratch ...")
            elif self.use_checkpoint == "latest":
                self.log("[INFO] Loading latest checkpoint ...")
                self.load_checkpoint()
            elif self.use_checkpoint == "best":
                if os.path.exists(self.best_path):
                    self.log("[INFO] Loading best checkpoint ...")
                    self.load_checkpoint(self.best_path)
                else:
                    self.log(f"[INFO] {self.best_path} not found, loading latest ...")
                    self.load_checkpoint()
            else: # path to ckpt
                self.log(f"[INFO] Loading {self.use_checkpoint} ...")
                self.load_checkpoint(self.use_checkpoint)

    def __del__(self):
        if self.log_ptr: 
            self.log_ptr.close()

    def log(self, *args, **kwargs):
        if self.local_rank == 0:
            if not self.mute: 
                #print(*args)
                self.console.print(*args, **kwargs)
            if self.log_ptr: 
                print(*args, file=self.log_ptr)
                self.log_ptr.flush() # write immediately to file

    ### ------------------------------	

    def train_step(self, data):

        images = data["image"] # [B, H, W, 3/4]
        poses = data["pose"] # [B, 4, 4]
        intrinsics = data["intrinsic"] # [B, 3, 3]

        # sample rays 
        B, H, W, C = images.shape
        rays_o, rays_d, inds = get_rays(poses, intrinsics, H, W, self.conf['num_rays'])
        images = torch.gather(images.reshape(B, -1, C), 1, torch.stack(C*[inds], -1)) # [B, N, 3/4]

        # train with random background color if using alpha mixing
        if self.white_background:
            bg_color = torch.ones(3, device=images.device) # [3], fixed white background
        else:
            # bg_color = torch.zeros(3, device=images.device) # [3], fixed black background.
            bg_color = torch.rand(3, device=images.device) # [3], frame-wise random.

        if C == 4:
            gt_rgb = images[..., :3] * images[..., 3:] + bg_color * (1 - images[..., 3:])
        else:
            gt_rgb = images

        outputs = self.model.render(rays_o, rays_d, staged=False, bg_color=bg_color, perturb=True, 
                                    cos_anneal_ratio = min(self.epoch / 200, 1.0), normal_epsilon_ratio= min(self.epoch / 200, 0.95),  
                                    **self.conf)
    
        pred_rgb = outputs['rgb']
        try:
            eikonal_loss = outputs['gradient_error']
        except:
            eikonal_loss = 0.0

        try:
            curvature_loss = outputs['curvature_error']
        except:
            curvature_loss = 0.0
        
        # gt_rgb = torch.load('gt_rgb.pt')
        loss_sparsity = outputs['loss_sparsity']
        loss_rgb_l1 = F.l1_loss(pred_rgb, gt_rgb)
        #loss = self.criterion(pred_rgb, gt_rgb) + 0.1 * eikonal_loss
        loss = self.criterion(pred_rgb, gt_rgb) + 0.1 * eikonal_loss + 0.1 * curvature_loss
        # loss = 10.0 * self.criterion(pred_rgb, gt_rgb) + 0.1 * eikonal_loss
        # loss = 10.0 * self.criterion(pred_rgb, gt_rgb) + 0.1 * eikonal_loss + 0.01 * loss_sparsity + loss_rgb_l1
        psnr = self.psnr(pred_rgb, gt_rgb)

        return pred_rgb, gt_rgb, loss, psnr

    def eval_step(self, data):
        images = data["image"] # [B, H, W, 3/4]
        poses = data["pose"] # [B, 4, 4]
        intrinsics = data["intrinsic"] # [B, 3, 3]

        # sample rays 
        B, H, W, C = images.shape
        rays_o, rays_d, _ = get_rays(poses, intrinsics, H, W, -1)

        bg_color = torch.ones(3, device=images.device) # [3]
        # eval with fixed background color
        if C == 4:
            gt_rgb = images[..., :3] * images[..., 3:] + bg_color * (1 - images[..., 3:])
        else:
            gt_rgb = images

        outputs = self.model.render(rays_o, rays_d, staged=True, bg_color=bg_color, perturb=False, 
                                    cos_anneal_ratio = min(self.epoch / 100, 1.0), normal_epsilon_ratio= min((self.epoch - 50) / 100, 0.99),  
                                    **self.conf)

        pred_rgb = outputs['rgb'].reshape(B, H, W, -1)
        if 'normal' in outputs.keys():
            pred_normal = outputs['normal'].reshape(B, H, W, -1)
            pred_normal = (pred_normal + 1.0) / 2.0
            if pred_normal.shape[-1] == 6:
                pred_normal = torch.cat([pred_normal[...,:3], pred_normal[...,3:]], dim = 2)
                
        else:
            pred_normal = torch.ones_like(pred_rgb)
        pred_depth = outputs['depth'].reshape(B, H, W)

        loss = self.criterion(pred_rgb, gt_rgb)
        psnr = self.psnr(pred_rgb, gt_rgb)

        return pred_rgb, pred_normal, pred_depth, gt_rgb, loss, psnr

    # moved out bg_color and perturb for more flexible control...
    def test_step(self, data, bg_color=None, perturb=False):  
        poses = data["pose"] # [B, 4, 4]
        intrinsics = data["intrinsic"] # [B, 3, 3]
        H, W = int(data['H'][0]), int(data['W'][0]) # get the target size...

        B = poses.shape[0]

        rays_o, rays_d, _ = get_rays(poses, intrinsics, H, W, -1)

        if bg_color is not None:
            bg_color = bg_color.to(rays_o.device)

        outputs = self.model.render(rays_o, rays_d, staged=True, bg_color=bg_color, perturb=False, 
                                    cos_anneal_ratio = min(self.epoch / 100, 1.0), normal_epsilon_ratio= min((self.epoch - 50) / 100, 0.99),  
                                    **self.conf)

        pred_rgb = outputs['rgb'].reshape(B, H, W, -1)

        if 'normal' in outputs.keys():
            pred_normal = outputs['normal'].reshape(B, H, W, -1)
            pred_normal = (pred_normal + 1.0) / 2.0

            if pred_normal.shape[-1] == 6:
                pred_normal = torch.cat([pred_normal[...,:3], pred_normal[...,3:]], dim = 2)

        else:
            pred_normal = torch.ones_like(pred_rgb)

        pred_depth = outputs['depth'].reshape(B, H, W)

        return pred_rgb, pred_normal, pred_depth


    def save_mesh(self, save_path=None, resolution= 256, aabb = None, bound=1, threshold=0.,  use_sdf = False):

        if save_path is None:
            save_path = os.path.join(self.workspace, 'meshes', f'{self.name}_{self.epoch}.obj')

        self.log(f"==> Saving mesh to {save_path}")

        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        def query_func(pts):
            with torch.no_grad():
                with torch.cuda.amp.autocast(enabled=self.fp16):
                    sdfs = self.model.density(pts.to(self.device), bound)
            return sdfs
        
        # scale = max(0.000001,
        #             max(max(abs(float(aabb[1][0])-float(aabb[0][0])),
        #                     abs(float(aabb[1][1])-float(aabb[0][1]))),
        #                     abs(float(aabb[1][2])-float(aabb[0][2]))))
                        
        # scale = 2.0 * bound / scale

        # offset =  torch.FloatTensor([
        #             ((float(aabb[1][0]) + float(aabb[0][0])) * 0.5) * -scale,
        #             ((float(aabb[1][1]) + float(aabb[0][1])) * 0.5) * -scale, 
        #             ((float(aabb[1][2]) + float(aabb[0][2])) * 0.5) * -scale])


        #demo setting w/o aabb
        # bounds_min = torch.FloatTensor([-0.4 * bound, -0.8 *bound, -0.3 *bound])
        # bounds_max = torch.FloatTensor([0.4 *bound, 0.4 * bound, 0.3 *bound])
        bounds_min = torch.FloatTensor([-1 * bound, -1 *bound, -1 *bound])
        bounds_max = torch.FloatTensor([1 *bound, 1 * bound, 1 *bound])
        
        #ficus setting w/o aabb
        # bounds_min = torch.FloatTensor([-0.35 * bound, -1.0 *bound, -0.35 *bound])
        # bounds_max = torch.FloatTensor([0.35 *bound, 0.4 * bound, 0.35 *bound])

        # w/ aabb
        # bounds_min = torch.FloatTensor([-bound] * 3)
        # bounds_max = torch.FloatTensor([bound] * 3)

        vertices, triangles = extract_geometry(bounds_min, bounds_max, resolution=resolution, threshold=threshold, query_func = query_func, use_sdf = use_sdf)
        
        # align camera
        vertices = np.concatenate([vertices[:,2:3], vertices[:,0:1], vertices[:,1:2]], axis=-1)
        # vertices = (vertices - offset.numpy()) / scale
        vertices = (vertices)

        print(vertices.shape)
        mesh = trimesh.Trimesh(vertices, triangles, process=False) # important, process=True leads to seg fault...
        mesh.export(save_path)

        self.log(f"==> Finished saving mesh.")
    ### ------------------------------

    def train(self, train_loader, valid_loader, max_epochs):
        if self.use_tensorboardX and self.local_rank == 0:
            self.writer = tensorboardX.SummaryWriter(os.path.join(self.workspace, "run", self.name))
        
        for epoch in range(self.epoch, max_epochs + 1):
            self.epoch = epoch

            self.train_one_epoch(train_loader)
            
            if self.epoch % self.eval_interval == 0:
                self.evaluate_one_epoch(valid_loader)
                self.save_checkpoint(full=False, best=True)

            if self.epoch % 10 == 0:
                if self.workspace is not None and self.local_rank == 0:
                        self.save_checkpoint(full=True, best=False)

        if self.use_tensorboardX and self.local_rank == 0:
            self.writer.close()

    def evaluate(self, loader):
        self.use_tensorboardX, use_tensorboardX = False, self.use_tensorboardX
        self.evaluate_one_epoch(loader)
        self.use_tensorboardX = use_tensorboardX

    def test(self, loader, save_path=None):

        if save_path is None:
            save_path = os.path.join(self.workspace, 'results')

        os.makedirs(save_path, exist_ok=True)
        
        self.log(f"==> Start Test, save results to {save_path}")

        pbar = tqdm.tqdm(total=len(loader) * loader.batch_size, bar_format='{percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')
        self.model.eval()
        #with torch.no_grad():
        for i, data in enumerate(loader):
            
            data = self.prepare_data(data)
            images = data["image"] # [B, H, W, 3/4]
            B, H, W, C = images.shape

            if self.white_background:
                bg_color = torch.ones(3, device=images.device) # [3], fixed white background
            else:
                # bg_color = torch.zeros(3, device=images.device) # [3], fixed black background.
                bg_color = torch.rand(3, device=images.device) # [3], frame-wise random.

            if C == 4:
                gt_rgb = images[..., :3] * images[..., 3:] + bg_color * (1 - images[..., 3:])
            else:
                gt_rgb = images
            with torch.cuda.amp.autocast(enabled=self.fp16):
                preds, preds_normal, preds_depth = self.test_step(data)   
            
            path = os.path.join(save_path, f'{i:04d}.png')
            path_normal = os.path.join(save_path, f'{i:04d}_normal.png')
            path_depth = os.path.join(save_path, f'{i:04d}_depth.png')
            psnr = self.psnr(preds, gt_rgb)

            self.log(f"[INFO] saving test image to {path}, psnr: {psnr}")

            cv2.imwrite(path, cv2.cvtColor((preds[0].detach().cpu().numpy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
            cv2.imwrite(path_normal, cv2.cvtColor((preds_normal[0].detach().cpu().numpy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
            # cv2.imwrite(path_depth, (preds_depth[0].detach().cpu().numpy() * 255).astype(np.uint8))

            to8b = lambda x : (255*np.clip(x,0,1)).astype(np.uint8)
            # cv2.imwrite(path_depth, to8b(1 - np.array(preds_depth[0].cpu()) / np.max(np.array(preds_depth[0].cpu()))))
            cv2.imwrite(path_depth, to8b(1 - (np.array(preds_depth[0].cpu()) / np.max(np.array(preds_depth[0].cpu())))))

            pbar.update(loader.batch_size)

        self.log(f"==> Finished Test.")
    
    # [GUI] just train for 16 steps, without any other overhead that may slow down rendering.
    def train_gui(self, train_loader, step=16):

        self.model.train()

        # update grid
        if self.model.cuda_ray:
            with torch.cuda.amp.autocast(enabled=self.fp16):
                self.model.update_extra_state(self.conf['bound'])

        total_loss = torch.tensor([0], dtype=torch.float32, device=self.device)
        
        loader = iter(train_loader)

        for _ in range(step):
            
            # mimic an infinite loop dataloader (in case the total dataset is smaller than step)
            try:
                data = next(loader)
            except StopIteration:
                loader = iter(train_loader)
                data = next(loader)
            
            self.global_step += 1
            
            data = self.prepare_data(data)

            self.optimizer.zero_grad()

            with torch.cuda.amp.autocast(enabled=self.fp16):
                preds, truths, loss = self.train_step(data)
         
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()
            
            if self.scheduler_update_every_step:
                self.lr_scheduler.step()

            total_loss += loss.detach()

        if self.ema is not None:
            self.ema.update()

        average_loss = total_loss.item() / step

        if not self.scheduler_update_every_step:
            if isinstance(self.lr_scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                self.lr_scheduler.step(average_loss)
            else:
                self.lr_scheduler.step()

        outputs = {
            'loss': average_loss,
            'lr': self.optimizer.param_groups[0]['lr'],
        }
        
        return outputs

    
    # [GUI] test on a single image
    def test_gui(self, pose, intrinsics, W, H, bg_color=None, spp=1):

        data = {
            'pose': pose[None, :],
            'intrinsic': intrinsics[None, :],
            'H': [str(H)],
            'W': [str(W)],
        }

        data = self.prepare_data(data)
        
        self.model.eval()

        if self.ema is not None:
            self.ema.store()
            self.ema.copy_to()

        with torch.no_grad():
            with torch.cuda.amp.autocast(enabled=self.fp16):
                # here spp is used as perturb random seed!
                preds, preds_depth = self.test_step(data, bg_color=bg_color, perturb=spp)

        if self.ema is not None:
            self.ema.restore()

        outputs = {
            'image': preds[0].detach().cpu().numpy(),
            'depth': preds_depth[0].detach().cpu().numpy(),
        }

        return outputs

    def prepare_data(self, data):
        if isinstance(data, list):
            for i, v in enumerate(data):
                if isinstance(v, np.ndarray):
                    data[i] = torch.from_numpy(v).to(self.device, non_blocking=True)
                if torch.is_tensor(v):
                    data[i] = v.to(self.device, non_blocking=True)
        elif isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, np.ndarray):
                    data[k] = torch.from_numpy(v).to(self.device, non_blocking=True)
                if torch.is_tensor(v):
                    data[k] = v.to(self.device, non_blocking=True)
        elif isinstance(data, np.ndarray):
            data = torch.from_numpy(data).to(self.device, non_blocking=True)
        else: # is_tensor, or other similar objects that has `to`
            data = data.to(self.device, non_blocking=True)

        return data

    def train_one_epoch(self, loader):
        self.log(f"==> Start Training Epoch {self.epoch}, lr={self.optimizer.param_groups[0]['lr']:.6f} ...")

        total_loss = 0
        if self.local_rank == 0:
            for metric in self.metrics:
                metric.clear()

        self.model.train()

        # update grid
        if self.model.cuda_ray:
            with torch.cuda.amp.autocast(enabled=self.fp16):
                self.model.update_extra_state(self.conf['bound'])

        # distributedSampler: must call set_epoch() to shuffle indices across multiple epochs
        # ref: https://pytorch.org/docs/stable/data.html
        if self.world_size > 1:
            loader.sampler.set_epoch(self.epoch)
        
        if self.local_rank == 0:
            pbar = tqdm.tqdm(total=len(loader), bar_format='{desc}: {percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')

        self.local_step = 0

        for data in loader:
            
            self.local_step += 1
            self.global_step += 1
            
            data = self.prepare_data(data)

            self.optimizer.zero_grad()

            with torch.cuda.amp.autocast(enabled=self.fp16):
                preds, truths, loss, psnr = self.train_step(data)
         
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()

            if self.scheduler_update_every_step:
                self.lr_scheduler.step()

            total_loss += loss.item()

            if self.local_rank == 0:
                for metric in self.metrics:
                    metric.update(preds, truths)
                        
                if self.use_tensorboardX:
                    self.writer.add_scalar("train/loss", loss.item(), self.global_step)
                    self.writer.add_scalar("train/inv_s", 1.0 / self.model.forward_variance()[0,0].detach(), self.global_step)
                    self.writer.add_scalar("train/lr", self.optimizer.param_groups[0]['lr'], self.global_step)

                if self.scheduler_update_every_step:
                    pbar.set_description(f"psnr={psnr.item():.4f}, loss={loss.item():.4f} ({total_loss/self.local_step:.4f}), s_val={self.model.forward_variance()[0,0].detach().cpu():.2f}, lr={self.optimizer.param_groups[0]['lr']:.6f}")
                else:
                    pbar.set_description(f"psnr={psnr.item():.4f}, loss={loss.item():.4f} ({total_loss/self.local_step:.4f}), s_val={self.model.forward_variance()[0,0].detach().cpu():.2f}, lr={self.optimizer.param_groups[0]['lr']:.6f}")
                pbar.update(1)

        if self.ema is not None:
            self.ema.update()

        average_loss = total_loss / self.local_step
        self.stats["loss"].append(average_loss)

        if self.local_rank == 0:
            pbar.close()
            for metric in self.metrics:
                self.log(metric.report(), style="red")
                if self.use_tensorboardX:
                    metric.write(self.writer, self.epoch, prefix="train")
                metric.clear()

        if not self.scheduler_update_every_step:
            if isinstance(self.lr_scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                self.lr_scheduler.step(average_loss)
            else:
                self.lr_scheduler.step()

        self.log(f"==> Finished Epoch {self.epoch}.")


    def evaluate_one_epoch(self, loader):
        self.log(f"++> Evaluate at epoch {self.epoch} ...")

        total_loss = 0
        if self.local_rank == 0:
            for metric in self.metrics:
                metric.clear()

        self.model.eval()

        if self.ema is not None:
            self.ema.store()
            self.ema.copy_to()

        if self.local_rank == 0:
            pbar = tqdm.tqdm(total=len(loader) * loader.batch_size, bar_format='{desc}: {percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')

        #with torch.no_grad():
        self.local_step = 0
        for data in loader:    
            self.local_step += 1
            
            data = self.prepare_data(data)


            with torch.cuda.amp.autocast(enabled=self.fp16):
                preds, preds_normal, preds_depth, truths, loss, psnr = self.eval_step(data)


            # all_gather/reduce the statistics (NCCL only support all_*)
            if self.world_size > 1:
                dist.all_reduce(loss, op=dist.ReduceOp.SUM)
                loss = loss / self.world_size
                
                preds_list = [torch.zeros_like(preds).to(self.device) for _ in range(self.world_size)] # [[B, ...], [B, ...], ...]
                dist.all_gather(preds_list, preds)
                preds = torch.cat(preds_list, dim=0)

                preds_depth_list = [torch.zeros_like(preds_depth).to(self.device) for _ in range(self.world_size)] # [[B, ...], [B, ...], ...]
                dist.all_gather(preds_depth_list, preds_depth)
                preds_depth = torch.cat(preds_depth_list, dim=0)

                truths_list = [torch.zeros_like(truths).to(self.device) for _ in range(self.world_size)] # [[B, ...], [B, ...], ...]
                dist.all_gather(truths_list, truths)
                truths = torch.cat(truths_list, dim=0)

                truths_list = [torch.zeros_like(truths).to(self.device) for _ in range(self.world_size)] # [[B, ...], [B, ...], ...]
                dist.all_gather(truths_list, truths)
                truths = torch.cat(truths_list, dim=0)

        total_loss += loss.item()

        # only rank = 0 will perform evaluation.
        if self.local_rank == 0:

            for metric in self.metrics:
                metric.update(preds, truths)


            if self.use_tensorboardX:
                self.writer.add_image('val/GT', truths[0].permute(2, 0, 1), self.global_step)
                self.writer.add_image('val/color', preds[0].permute(2, 0, 1), self.global_step)
                self.writer.add_image('val/normal', preds_normal[0].permute(2, 0, 1), self.global_step)
                self.writer.add_image('val/depth', preds_depth, self.global_step)
            else:
                # save image
                save_path = os.path.join(self.workspace, 'validation', f'{self.name}_{self.epoch:04d}_{self.local_step:04d}.png')
                save_path_depth = os.path.join(self.workspace, 'validation', f'{self.name}_{self.epoch:04d}_{self.local_step:04d}_depth.png')
                save_path_gt = os.path.join(self.workspace, 'validation', f'{self.name}_{self.epoch:04d}_{self.local_step:04d}_gt.png')

                self.log(f"==> Saving validation image to {save_path}")
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                cv2.imwrite(save_path, cv2.cvtColor((preds[0].detach().cpu().numpy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
                cv2.imwrite(save_path_depth, (preds_depth[0].detach().cpu().numpy() * 255).astype(np.uint8))
                cv2.imwrite(save_path_gt, cv2.cvtColor((truths[0].detach().cpu().numpy() * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
                
            pbar.set_description(f"psnr={psnr.item():.4f}, loss={loss.item():.4f} ({total_loss/self.local_step:.4f})")
            pbar.update(loader.batch_size)


        average_loss = total_loss / self.local_step
        self.stats["valid_loss"].append(average_loss)

        if self.local_rank == 0:
            pbar.close()
            if not self.use_loss_as_metric and len(self.metrics) > 0:
                result = self.metrics[0].measure()
                self.stats["results"].append(result if self.best_mode == 'min' else - result) # if max mode, use -result
            else:
                self.stats["results"].append(average_loss) # if no metric, choose best by min loss

            for metric in self.metrics:
                self.log(metric.report(), style="blue")
                if self.use_tensorboardX:
                    metric.write(self.writer, self.epoch, prefix="evaluate")
                metric.clear()

        if self.ema is not None:
            self.ema.restore()

        self.log(f"++> Evaluate epoch {self.epoch} Finished.")

    def save_checkpoint(self, full=False, best=False):

        state = {
            'epoch': self.epoch,
            'stats': self.stats,
        }

        if self.model.cuda_ray:
            state['mean_count'] = self.model.mean_count
            state['mean_density'] = self.model.mean_density

        if full:
            state['optimizer'] = self.optimizer.state_dict()
            state['lr_scheduler'] = self.lr_scheduler.state_dict()
            state['scaler'] = self.scaler.state_dict()
            if self.ema is not None:
                state['ema'] = self.ema.state_dict()
        
        if not best:

            state['model'] = self.model.state_dict()

            file_path = f"{self.ckpt_path}/{self.name}_ep{self.epoch:04d}.pth.tar"

            self.stats["checkpoints"].append(file_path)

            if len(self.stats["checkpoints"]) > self.max_keep_ckpt:
                old_ckpt = self.stats["checkpoints"].pop(0)
                if os.path.exists(old_ckpt):
                    os.remove(old_ckpt)

            torch.save(state, file_path)

        else:    
            if len(self.stats["results"]) > 0:
                if self.stats["best_result"] is None or self.stats["results"][-1] < self.stats["best_result"]:
                    self.log(f"[INFO] New best result: {self.stats['best_result']} --> {self.stats['results'][-1]}")
                    self.stats["best_result"] = self.stats["results"][-1]

                    # save ema results 
                    if self.ema is not None:
                        self.ema.store()
                        self.ema.copy_to()

                    state['model'] = self.model.state_dict()

                    if self.ema is not None:
                        self.ema.restore()
                    
                    torch.save(state, self.best_path)
            else:
                self.log(f"[WARN] no evaluated results found, skip saving best checkpoint.")
            
    def load_checkpoint(self, checkpoint=None):
        if checkpoint is None:
            checkpoint_list = sorted(glob.glob(f'{self.ckpt_path}/{self.name}_ep*.pth.tar'))
            if checkpoint_list:
                checkpoint = checkpoint_list[-1]
                self.log(f"[INFO] Latest checkpoint is {checkpoint}")
            else:
                self.log("[WARN] No checkpoint found, model randomly initialized.")
                return

        checkpoint_dict = torch.load(checkpoint, map_location=self.device)
        
        if 'model' not in checkpoint_dict:
            self.model.load_state_dict(checkpoint_dict)
            self.log("[INFO] loaded model.")
            return

        missing_keys, unexpected_keys = self.model.load_state_dict(checkpoint_dict['model'], strict=False)
        self.log("[INFO] loaded model.")
        if len(missing_keys) > 0:
            self.log(f"[WARN] missing keys: {missing_keys}")
        if len(unexpected_keys) > 0:
            self.log(f"[WARN] unexpected keys: {unexpected_keys}")   

        if self.ema is not None and 'ema' in checkpoint_dict:
            self.ema.load_state_dict(checkpoint_dict['ema'])

        self.stats = checkpoint_dict['stats']
        self.epoch = checkpoint_dict['epoch']

        if self.model.cuda_ray:
            if 'mean_count' in checkpoint_dict:
                self.model.mean_count = checkpoint_dict['mean_count']
            if 'mean_density' in checkpoint_dict:
                self.model.mean_density = checkpoint_dict['mean_density']
        
        if self.optimizer and  'optimizer' in checkpoint_dict:
            try:
                self.optimizer.load_state_dict(checkpoint_dict['optimizer'])
                self.log("[INFO] loaded optimizer.")
            except:
                self.log("[WARN] Failed to load optimizer, use default.")
        
        # strange bug: keyerror 'lr_lambdas'
        if self.lr_scheduler and 'lr_scheduler' in checkpoint_dict:
            try:
                self.lr_scheduler.load_state_dict(checkpoint_dict['lr_scheduler'])
                self.log("[INFO] loaded scheduler.")
            except:
                self.log("[WARN] Failed to load scheduler, use default.")
        
        if 'scaler' in checkpoint_dict:
            try:
                self.scaler.load_state_dict(checkpoint_dict['scaler'])
                self.log("[INFO] loaded scaler.")
            except:
                self.log("[WARN] Failed to load scaler, use default.")