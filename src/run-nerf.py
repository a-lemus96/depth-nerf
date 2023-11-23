# stdlib imports
from datetime import date
import logging
import os
import random
from typing import List, Tuple, Union, Optional

# third-party imports
from lpips import LPIPS
import matplotlib.pyplot as plt
import nerfacc
from nerfacc.volrend import rendering
from nerfacc.estimators.occ_grid import OccGridEstimator
import numpy as np
import plotly.graph_objects as go
from skimage.metrics import structural_similarity as SSIM
import torch
from torch import nn
import torch.nn.functional as F
from torch.optim import Optimizer
from torch.utils.data import Dataset, DataLoader, Subset
from torch import Tensor
from tqdm import tqdm
import wandb

# local imports
import core.models as M
import core.loss as L
import core.scheduler as S
import data.dataset as D
import render.rendering as R
import utils.parser as P
import utils.plotting as PL
import utils.utilities as U

# GLOBAL VARIABLES
k = 0 # global step counter

# RANDOM SEED
seed = 42
torch.manual_seed(seed)
np.random.seed(seed)
random.seed(seed)

args = P.config_parser() # parse command line arguments

# MODEL INITIALIZATION

def init_models() -> Tuple[nn.Module, OccGridEstimator]:
    """
    Initialize NeRF-like model, occupancy grid estimator, and LPIPS net.
    ----------------------------------------------------------------------------
    Returns:
        Tuple[nn.Module, OccGridEstimator, LPIPS]: models
    """
    # keyword args for positional encoding
    kwargs = {
            'pos_fn': {
                'n_freqs': args.n_freqs,
                'log_space': args.log_space
            },
            'dir_fn': {
                'n_freqs': args.n_freqs_views,
                'log_space': args.log_space
            }
    }
    # instantiate model
    if args.model == 'nerf':
        model = M.NeRF(
                args.d_input,
                args.d_input,
                args.n_layers,
                args.d_filter, 
                args.skip,
                **kwargs
        )
    elif args.model == 'sinerf':
        model = M.SiNeRF(
                args.d_input,
                args.d_input,
                args.d_filter,
                [30., 1., 1., 1., 1., 1., 1., 1.]
        )
    # initialize occupancy estimator
    aabb = torch.tensor([-1.5, -1.5, -1.5, 1.5, 1.5, 1.5])
    # model parameters
    grid_resolution = 128
    grid_nlvl = 1
    # render parameters
    render_step_size = 5e-3
    estimator = OccGridEstimator(
            roi_aabb=aabb, 
            resolution=grid_resolution, 
            levels=grid_nlvl
    )
    # initialize LPIPS network
    lpips_net = LPIPS(net='vgg')
    
    return model, estimator, lpips_net

# TRAINING FUNCTIONS

def validation(
        hwf: Tensor,
        model: nn.Module,
        estimator: OccGridEstimator,
        lpips_net: LPIPS,
        val_loader: DataLoader,
        chunksize: int,
        device: torch.device,
        render_step_size: float = 5e-3,
) -> Tuple[float, float, float]:
    """
    Perform validation step
    ----------------------------------------------------------------------------
    Args:
        model (nn.Module): NeRF-like model
        estimator (OccGridEstimator): occupancy grid estimator
        lpips_net (LPIPS): LPIPS network
        val_loader (DataLoader): validation set loader
        chunksize (int): chunk size for frame rendering
        device (torch.device): device to train on
        render_step_size (float, optional): step size for rendering
    Returns:
        val_psnr (float): validation PSNR
        val_ssim (float): validation SSIM
        val_lpips (float): validation LPIPS
    """
    H, W, focal = hwf
    H, W = int(H), int(W)
    rgbs = []
    rgbs_gt = []
    for val_data in val_loader:
        rgb_gt, pose = val_data
        rgbs_gt.append(rgb_gt) # append ground truth rgb
        rgb, _ = R.render_frame(
                H, W, focal, pose[0],
                chunksize,
                estimator,
                device,
                model,
                train=False,
                white_bkgd=args.white_bkgd,
                render_step_size=render_step_size
        )
        rgbs.append(rgb) # append rendered rgb

    # compute PSNR
    rgbs = torch.permute(torch.stack(rgbs, dim=0), (0, 3, 1, 2))
    rgbs_gt = torch.permute(torch.cat(rgbs_gt, dim=0), (0, 3, 1, 2))
    rgbs_gt = rgbs_gt.to(device)
    val_psnr = -10. * torch.log10(F.mse_loss(rgbs, rgbs_gt))
    val_size = len(val_loader)

    # compute LPIPS
    if val_size < 25:
        val_lpips = lpips_net(rgbs, rgbs_gt).mean()
    else:
        # compute LPIPS in chunks
        n_chunks = 5
        chunk_size = val_size //  n_chunks
        chunk_idxs = [i for i in range(0, val_size, chunk_size)]
        chunks = [(rgbs[i:i+chunk_size], rgbs_gt[i:i+chunk_size]) 
                  for i in chunk_idxs]
        val_lpips = 0.
        for chunk, chunk_gt in chunks:
            val_lpips += lpips_net(chunk, chunk_gt).mean()
        val_lpips /= n_chunks

    # compute SSIM
    rgbs = torch.permute(rgbs, (0, 2, 3, 1)).cpu().numpy()
    rgbs_gt = torch.permute(rgbs_gt, (0, 2, 3, 1)).cpu().numpy()
    ssims = np.zeros((rgbs.shape[0],))
    val_ssim = 0.
    for rgb, rgb_gt in zip(rgbs, rgbs_gt):
        val_ssim += SSIM(
                rgb, 
                rgb_gt, 
                channel_axis=-1, 
                data_range=1.,
                gaussian_weights=True
        )
    val_ssim /= len(rgbs)

    return val_psnr, val_ssim, val_lpips

def train(
        model: nn.Module,
        estimator: OccGridEstimator,
        lpips_net: LPIPS,
        train_loader: Dataset,
        val_loader: Dataset,
        device: torch.device,
        render_step_size: float = 5e-3,
) -> Tuple[float, float]:
    """Train NeRF model.
    ----------------------------------------------------------------------------
    Args:
        model (nn.Module): NeRF model
        estimator (OccGridEstimator): occupancy grid estimator
        lpips_net (LPIPS): LPIPS network
        train_loader (Dataset): training set loader
        val_loader (Dataset): validation set loader
        device (torch.device): device to train on
    Returns:
        Tuple[float, float, float]: validation PSNR, SSIM, LPIPS
    ----------------------------------------------------------------------------
    """
    # retrieve camera intrinsics
    hwf = train_loader.dataset.hwf
    H, W, focal = hwf
    H, W = int(H), int(W)
    testpose = train_loader.dataset.testpose

    # set up optimizer and scheduler
    params = list(model.parameters())
    optimizer = torch.optim.Adam(params, lr=args.lro)
    sc_dict = {
            'const': (S.Constant, {}),
            'exp': (S.ExponentialDecay, {'r': args.decay_rate})
    }
    class_name, kwargs = sc_dict[args.scheduler]
    scheduler = class_name(
            optimizer,
            args.Td,
            args.lro,
            **kwargs
    )

    pbar = tqdm(range(args.n_iters), desc=f"[NeRF]") # set up progress bar
    iterator = iter(train_loader) # data iterator

    # regularizers
    if args.beta is not None:
        occ_reg = L.OcclusionRegularizer(args.beta, args.M)

    for k in pbar: # loop over the number of iterations
        model.train()
        estimator.train()
        # get next batch of data
        try:
            rays_o, rays_d, rgb_gt = next(iterator)
        except StopIteration:
            iterator = iter(train_loader)
            rays_o, rays_d, rgb_gt = next(iterator)

        # render rays
        (rgb, *_, extras), ray_indices = R.render_rays(
                rays_o=rays_o,
                rays_d=rays_d,
                estimator=estimator,
                device=device,
                model=model,
                train=True,
                white_bkgd=args.white_bkgd,
                render_step_size=render_step_size
        )
        
        # compute loss and PSNR
        rgb_gt = rgb_gt.to(device)
        loss = F.mse_loss(rgb, rgb_gt)
        with torch.no_grad():
            psnr = -10. * torch.log10(loss).item()

        # occlusion regularization
        if args.beta is not None:
            sigmas = extras['sigmas']
            if len(sigmas) > 0:
                loss += occ_reg(sigmas, ray_indices)

        # weight decay regularization
        if args.ao is not None:
            freq_reg = torch.tensor(0.).to(device)
            # linear decay schedule
            Ts = int(args.reg_ratio * args.Td)
            if k < Ts:
                for name, param in model.named_parameters():
                    if 'weight' in name and param.shape[0] > 3:
                        if args.reg == 'l1':
                            freq_reg += torch.abs(param).sum()
                        else:
                            freq_reg += torch.square(param).sum().sqrt()

                a = args.ao + (1. - args.ao) * (k / Ts)
                alpha = (args.ao / (1. - args.ao)) * (1. - min(1., a))
                loss += alpha * freq_reg

        # backpropagate loss
        loss.backward()
        optimizer.step()
        scheduler.step()
        optimizer.zero_grad()

        # define occupancy evaluation function
        def occ_eval_fn(x):
            density = model(x)
            return density * render_step_size

        # update occupancy grid
        estimator.update_every_n_steps(
                step=k,
                occ_eval_fn=occ_eval_fn,
                occ_thre=1e-2
        )

        # log metrics
        if not args.debug and k % args.val_rate != 0:
            wandb.log({
                'train_psnr': psnr,
                'lr': scheduler.lr,
                'alpha': alpha
            })

        # compute validation
        compute_val = k % args.val_rate == 0 and k > 0 and not args.no_val
        if compute_val:
            model.eval()
            estimator.eval()
            lpips_net.eval()
            with torch.no_grad():
                val_metrics = validation(
                        hwf,
                        model,
                        estimator,
                        lpips_net,
                        val_loader,
                        4*args.batch_size,
                        device
                )
                val_psnr, val_ssim, val_lpips = val_metrics
                # render test image
                rgb, depth = R.render_frame(
                        H, W, focal, 
                        testpose,
                        4*args.batch_size,
                        estimator,
                        device,
                        model,
                        train=False,
                        white_bkgd=args.white_bkgd,
                        render_step_size=render_step_size
                )

                # log data to wandb
                if not args.debug:
                    wandb.log({
                        'train_psnr': psnr,
                        'lr': scheduler.lr,
                        'alpha': alpha,
                        'val_psnr': val_psnr,
                        'val_ssim': val_ssim,
                        'val_lpips': val_lpips,
                        'rgb': wandb.Image(
                            rgb.cpu().numpy(),
                            caption='RGB'
                        ),
                        'depth': wandb.Image(
                            PL.apply_colormap(depth.cpu().numpy()),
                            caption='Depth'
                        )
                    })
    return

def main():
    # create llff dataset
    #llff = D.LLFF('fern')
    #exit()
    # select device
    device = torch.device(f'cuda' if torch.cuda.is_available() else 'cpu')

    # print device info or abort if no CUDA device available
    if device != 'cpu' :
        print(f"CUDA device: {torch.cuda.get_device_name(device)}")
    else:
        raise RuntimeError("CUDA device not available.")

    if not args.debug:
        wandb.login()
        # set up wandb run to track training
        name = f"{args.model}"
        name = name + f"-{args.reg}" if args.ao is not None else name
        name = name + f"-ao={args.ao:.2e}" if args.ao is not None else name
        run = wandb.init(
            project='depth-nerf',
            name=name,
            config=args
        )

    # training/validation datasets
    train_set = D.SyntheticRealistic(
            scene=args.scene,
            n_imgs=args.n_imgs,
            split='train',
            white_bkgd=args.white_bkgd,
            img_mode=args.img_mode
    )
    subset_size = int(args.val_ratio * 25) # % of val samples
    val_set = D.SyntheticRealistic(
            scene=args.scene,
            n_imgs=subset_size,
            split='val',
            white_bkgd=args.white_bkgd,
            img_mode=True
    )
    # data loader(s)
    train_loader = DataLoader(
            train_set,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=8
    )
    val_loader = DataLoader(
            val_set,
            batch_size=1,
            shuffle=True,
            num_workers=8
    )
    # log interactive 3D plot of camera positions
    fig = go.Figure(
            data=[go.Scatter3d(
                x=train_set.poses[:, 0, 3],
                y=train_set.poses[:, 1, 3],
                z=train_set.poses[:, 2, 3],
                mode='markers',
                marker=dict(size=7, opacity=0.8, color='red'),
            )],
            layout=go.Layout(
                margin=dict(l=20,r=20, t=20, b=20),
            )
    )
    # set fixed axis scales
    fig.update_layout(
        scene=dict(
            xaxis=dict(range=[-5, 5]),
            yaxis=dict(range=[-5, 5]),
            zaxis=dict(range=[0, 5]),
            xaxis_title='X',
            yaxis_title='Y',
            zaxis_title='Z'
        )
    )

    if not args.debug:
        wandb.log({
            'camera_positions': fig,
            'rgb_gt': wandb.Image(
                train_set.testimg.numpy(),
                caption='Ground Truth RGB'
            )
        })

    if not args.render_only:
        # initialize modules
        model, estimator, lpips_net = init_models()
        model.to(device)
        estimator.to(device)
        lpips_net.to(device)
        # train model
        train(
                model, 
                estimator,
                lpips_net,
                train_loader,
                val_loader,
                device=device
        )
        # final validation set and loader
        val_set = D.SyntheticRealistic(
                scene=args.scene,
                n_imgs=25,
                split='val',
                white_bkgd=args.white_bkgd,
                img_mode=True
        )
        val_loader = DataLoader(
                val_set,
                batch_size=1,
                shuffle=True,
                num_workers=8
        )
        # compute final validation metrics
        model.eval()
        estimator.eval()
        lpips_net.eval()
        with torch.no_grad():
            val_metrics = validation(
                    train_set.hwf,
                    model,
                    estimator,
                    lpips_net,
                    val_loader,
                    4*args.batch_size,
                    device
            )
        # log final metrics
        final_psnr, final_ssim, final_lpips = val_metrics
        if not args.debug:
            wandb.log({
                'final_psnr': final_psnr,
                'final_ssim': final_ssim,
                'final_lpips': final_lpips
            })
    else:
        model = init_models()
        # load model
        model.load_state_dict(torch.load(out_dir + '/model/nn.pt'))

    # build base path for output directories
    out_dir = os.path.normpath(
            os.path.join(
                args.out_dir, 
                args.model, 
                args.dataset,
                args.scene,
                f"n_imgs_{str(args.n_imgs)}",
                run.id
            )
    )

    # create output directories
    folders = ['video', 'model']
    [os.makedirs(os.path.join(out_dir, f), exist_ok=True) for f in folders]
    # save model
    if not args.render_only:
        torch.save(model.state_dict(), out_dir + '/model/nn.pt')

    # compute path poses for video output
    render_poses = R.sphere_path(theta=50, frames=90)
    render_poses = render_poses.to(device)
    # render frames for poses
    model.eval()
    H, W, focal = train_set.hwf
    H, W = int(H), int(W)
    output = R.render_path(
            render_poses=render_poses,
            hwf=[H, W, focal],
            chunksize=4*args.batch_size,
            device=device,
            model=model,
            estimator=estimator,
            white_bkgd=args.white_bkgd
    )
    frames, d_frames = output
    # put together frames and save result into .mp4 file
    R.render_video(
            basedir=f'{out_dir}/video/',
            frames=frames,
            d_frames=d_frames
    )
    # log final video renderings to wandb
    if not args.debug:
        wandb.log({
            'rgb_video': wandb.Video(f'{out_dir}/video/rgb.mp4', fps=30),
            'depth_video': wandb.Video(f'{out_dir}/video/depth.mp4', fps=30)
        })

if __name__ == '__main__':
    main()
