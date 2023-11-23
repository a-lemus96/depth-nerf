# standard library modules
import json
import os
from typing import Tuple, List, Union, Callable

# third-party modules
import imageio as iio
import matplotlib.pyplot as plt
import numpy as np
import torch
from sklearn.cluster import KMeans
from torch import Tensor
from torch.utils.data import Dataset
from torchvision.transforms import GaussianBlur, Resize

# custom modules
from utils import utilities as U

class SyntheticRealistic(Dataset):
    """
    Synthetic realistic dataset. It is made up of N x H x W ray origins and
    directions in world coordinate frame paired with ground truth pixel values. 
    The dataset is stored in a directory named 'synthetic'. Here, N is the 
    number of training images of size H x W.
    ----------------------------------------------------------------------------
    """
    def __init__(
            self, 
            scene: str, 
            split: str,
            n_imgs: int = None,
            white_bkgd: bool = False,
            img_mode: bool = False
    ) -> None:
        """
        Initialize the dataset.
        ------------------------------------------------------------------------
        Args:
            scene (str): scene name
            split (str): train, val or test split
            n_imgs (int): number of training images
            white_bkgd (bool): whether to use white background
            img_mode (bool): wether to iterate over rays or images
        Returns:
            None
        """
        super(SyntheticRealistic).__init__()  # inherit from Dataset
        self.scene = scene
        self.split = split
        self.near = 2.0
        self.far = 8.0
        self.img_mode = img_mode

        # load the dataset
        imgs, poses, hwf = self.__load()
        self.hwf = hwf
        # compute background color
        if white_bkgd:
            imgs = imgs[..., :3] * imgs[..., -1:] + (1. - imgs[..., -1:])
        else:
            imgs = imgs[..., :3]

        # choose random index for visual comparisons
        idx = np.random.randint(0, imgs.shape[0])
        self.testimg = imgs[idx]
        self.testpose = poses[idx]

        # apply K-means to draw N views and ensure maximum scene coverage
        x = poses[:, :3, 3]
        kmeans = KMeans(n_clusters=n_imgs,  n_init=10).fit(x) # kmeans model
        labels = kmeans.labels_
        # compute distances to cluster centers
        dists = np.linalg.norm(x - kmeans.cluster_centers_[labels], axis=1)
        # choose the closest view for every cluster center
        idxs = np.empty((n_imgs,), dtype=int) # array for indices of views
        for i in range(n_imgs):
            cluster_dists = np.where(labels == i, dists, np.inf)
            idxs[i] = np.argmin(cluster_dists)

        # full resolution images
        self.imgs = imgs[idxs]
        self.poses = poses[idxs]

        if not self.img_mode:
            # split images into individual per-ray samples
            self.__build_data(self.imgs, self.poses, self.hwf)


    def __len__(self) -> int:
        """Compute the number of training samples.
        ------------------------------------------------------------------------
        Args:
            None
        Returns:
            N (int): number of training samples
        """
        if self.img_mode:
            return len(self.imgs)

        return len(self.rgb)

    def __getitem__(self, idx: int) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        """Get a training sample by index.
        ------------------------------------------------------------------------
        Args:
            idx (int): index of the training sample
        Returns:
            ray_o (Tensor): [3,]. Ray origin
            ray_d (Tensor): [3,]. Ray direction
            rgb (Tensor): [3,]. Pixel RGB color
        """
        if self.img_mode:
            return self.imgs[idx], self.poses[idx]

        return self.rays_o[idx], self.rays_d[idx], self.rgb[idx]

    def __build_data(
            self,
            imgs: Tensor,
            poses: Tensor,
            hwf: Tensor
    ) -> None:
        """
        Build set of rays in world coordinate frame and their corresponding 
        pixel RGB values.
        ------------------------------------------------------------------------
        Args:
            imgs (Tensor): [N, H, W, 4]. RGBa images
            poses (Tensor): [N, 4, 4]. Camera poses
            hwf (Tensor): [3,]. Camera intrinsics
        """
        # compute ray origins and directions
        H, W, f = hwf
        rays = torch.stack([torch.cat(U.get_rays(H, W, f, p), -1) 
                            for p in poses], 0)
        rays = rays.reshape(-1, 6)
        self.rays_o = rays[:, :3] # ray origins
        self.rays_d = rays[:, 3:] # ray directions
        self.rgb = imgs.reshape(-1, 3) # reshape to [N, 3]

    def __downsample(
            self, 
            imgs: Tensor, 
            hwf: Tensor,
            factor: int
    ) -> None:
        """
        Downsample images and apply resize factor to camera intrinsics.
        ------------------------------------------------------------------------
        Args:
            imgs (Tensor): [N, H, W, 4]. RGBa images
            hwf (Tensor): [3,]. Camera intrinsics
            factor (int): resize factor
        Returns:
            new_imgs (Tensor): [N, H // factor, W // factor, 4]. RGBa images
            new_hwf (Tensor): [3,]. Camera intrinsics
        """
        # apply factor to camera intrinsics
        H, W, f = hwf
        new_H, new_W = int(H) // factor, int(W) // factor
        new_focal = hwf[2] / float(factor)
        new_hwf = torch.Tensor((new_H, new_W, new_focal))
        # downsample images
        new_imgs = Resize((new_H, new_W))(imgs)

        return new_imgs, new_hwf

    def __load(self) -> Tuple[Tensor, Tensor, Tensor]:
        """
        Loads the dataset. It loads images, camera poses and intrinsics.
        ------------------------------------------------------------------------
        Args:
            None
        Returns:
            imgs (Tensor): [N, H, W, 4]. RGBa images
            poses (Tensor): [N, 4, 4]. Camera poses
            hwf (Tensor): [3,]. Camera intrinsics. It contains height, width and
                          focal length
        """
        scene = self.scene
        path = os.path.join('..', 'datasets', 'synthetic', scene)
        # load JSON file
        with open(os.path.join(path, f'transforms_{self.split}.json'), 'r') as f:
            meta = json.load(f) # metadata

        # load images and camera poses
        imgs = []
        poses = []
        for frame in meta['frames']:
            # camera pose
            poses.append(np.array(frame['transform_matrix']))
            # frame image
            fname = os.path.join(path, frame['file_path'] + '.png')
            imgs.append(iio.imread(fname)) # RGBa image

        # convert to numpy arrays
        poses = np.stack(poses, axis=0).astype(np.float32)
        imgs = (np.stack(imgs, axis=0) / 255.).astype(np.float32)

        # compute image height, width and camera's focal length
        H, W = imgs.shape[1:3]
        fov_x = meta['camera_angle_x'] # field of view along camera x-axis
        focal = 0.5 * W / np.tan(0.5 * fov_x)
        hwf = np.array([H, W, np.array(focal)])

        # create tensors
        poses = torch.from_numpy(poses)
        imgs = torch.from_numpy(imgs)
        hwf = torch.from_numpy(hwf)

        return imgs, poses, hwf


    def gaussian_downsample(self, t: int) -> None:
        """
        Applies Gaussian blur + downsampling to images.
        ------------------------------------------------------------------------
        Args:
            t (int): Gaussian blur standard deviation
        Returns:
            None
        """    
        t = int(t)
        if t > 0:
            # permute images to [N, C, H, W] format
            imgs = torch.permute(self.imgs, (0, 3, 1, 2)) # [N, 3, H, W]

            # apply Gaussian blur
            blur = GaussianBlur(6 * t + 1, sigma=float(t))
            imgs = blur(imgs) # [N, 3, H, W]

            # downsample images
            imgs, hwf = self.__downsample(imgs, self.hwf, 1)
            # permute images back to [N, H, W, C] format
            imgs = torch.permute(imgs, (0, 2, 3, 1)) # [N, H, W, 3]

            # re-build training samples
            self.__build_data(imgs, self.poses, hwf)

            return imgs, hwf

        return self.imgs, self.hwf


class LLFF(Dataset):
    """
    Local Light Field Fusion dataset.
    ----------------------------------------------------------------------------
    """
    def __init__(
            self,
            scene: str,
            factor: int = 4,
            bd_factor: float = 0.75,
            img_mode: bool = False,
            recenter: bool = True,
    ) -> None:
        """
        Initialize dataset.
        ------------------------------------------------------------------------
        Args:
            scene (str): scene name
            factor (int): resize factor
            bd_factor (float): bounding box factor
            img_mode (bool): if True, it returns images instead of rays
            recenter (bool): if True, it re-centers the poses
        """
        super(LLFF, self).__init__()
        self.img_mode = img_mode
        basedir = os.path.join('..', 'datasets', 'llff', scene)
        imgs, poses, bounds = self.__load(basedir, factor)

        # rescale bounds and poses
        scale = 1. if bound_factor is None else 1. / (bounds.min() * bd_factor)
        poses[..., :3, 3] *= scale
        bounds *= scale

        if recenter:
            poses = __recenter_poses(poses)


    def __normalize(v: Tensor) -> Tensor:
        """
        Normalizes a vector.
        ------------------------------------------------------------------------
        Args:
            v (Tensor): [N,]. Vector
        Returns:
            v (Tensor): [N,]. Normalized vector
        """
        return v / np.linalg.norm(v)

    def __viewmatrix(z: ndarray, up: ndarray, pos: ndarray) -> ndarray:
        """
        Computes the view matrix.
        ------------------------------------------------------------------------
        Args:
            z (ndarray): [3,]. View direction
            up (ndarray): [3,]. Up direction
            pos (ndarray): [3,]. Camera position
        Returns:
            view (ndarray): [3, 4]. View matrix without bottom row
        """
        z = __normalize(z)
        y = up
        x = __normalize(np.cross(y, z))
        y = __normalize(np.cross(z, x))
        matrix = np.stack([x, y, z, pos], axis=1)

        return matrix

    def __avg_pose(poses: Tensor) -> Tensor:
        """
        Computes camera to world matrix.
        ------------------------------------------------------------------------
        Args:
            poses (Tensor): [N, 3, 5]. Camera poses
        Returns:
            avg_pose (Tensor): [N, 3, 5]. Camera to world matrix
        """
        hwf = poses[0, :3, -1]
        center = poses[:, :3, 3].mean(0)
        viewdir = __normalize(poses[:, :3, 2].sum(0))
        up = poses[:, :3, 1].sum(0)
        c2w = np.concatenate([__viewmatrix(viewdir, up, center), hwf], axis=1)


    def __recenter_poses(poses: Tensor) -> Tensor:
        """
        Re-centers camera poses.
        ------------------------------------------------------------------------
        Args:
            poses (Tensor): [N, 3, 5]. Camera poses
        Returns:
            poses (Tensor): [N, 3, 5]. Re-centered camera poses
        """
        poses_ = poses.clone()
        bottom = np.reshape([0, 0, 0, 1.], [1, 4]) # last row of camera matrix
        c2w = __avg_pose(poses) # average pose



    def __downsample(
            self,
            basedir: str,
            factor: int
    ) -> None:
        """
        Downsample images and apply resize factor to camera intrinsics.
        ------------------------------------------------------------------------
        Args:
            basedir (str): base directory
            factor (int): resize factor
        Returns:
            None
        """
        load = False

        if not load:
            return
    
    def __imread(f: str) -> np.ndarray:
        """
        Reads an image.
        ------------------------------------------------------------------------
        Args:
            f (str): image filepath
        Returns:
            img (np.ndarray): [H, W, 3]. RGB image
        """
        if f.endswith('png'):
            return iio.imread(f, ignoregamma=True)
        else:
            return iio.imread(f)

    def __load(
            self,
            basedir,
            factor
    ) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        """
        Loads the dataset. It loads images, camera poses, camera bounds and 
        intrinsics.
        ------------------------------------------------------------------------
        Args:
            basedir (str): base directory
            factor (int): resize factor
        Returns:
            imgs (Tensor): [N, H, W, 3]. RGB images
            poses (Tensor): [N, 4, 4]. Camera poses
            bds (Tensor): [N, 2]. Camera bounds
            hwf (Tensor): [3,]. Camera intrinsics. It contains height, width and
                          focal length
        """
        # load camera poses and bounds
        data = np.load(os.path.join(basedir, 'poses_bounds.npy'))
        poses = data[:, :-2].reshape([-1, 3, 5]).transpose([1, 2, 0])
        bounds = data[:, -2:].transpose([1, 0])

        # search for downsampled images
        suffix = '' # path suffix
        if factor > 1:
            suffix = f'_{factor}'
        img_dir = os.path.join(basedir, 'images' + suffix)
        assert os.path.exists(img_dir), f"Images path '{img_dir}' does not exist"

        # load images
        paths = [os.path.join((img_dir, f))
                 for f in sorted(os.listdir(img_dir))
                 if f.endswith(('JPG', 'jpg', 'png'))]
        assert len(paths) == poses.shape[-1], \
                'Mismath between the number of images and poses'
        imgs = np.stack([__imread(p)[..., :3] / 255. for p in paths], axis=0)

        # modify camera poses
        H, W, _ = iio.imread(paths[0]).shape
        poses[:2, 4, :] = np.array([H, W]).reshape([2, 1])
        poses[2, 4, :] = poses[2, 4, :] * 1. / factor
        # correct poses ordering
        poses = np.concatenate(
                [poses[:, 1:2, :], -poses[:, 0:1, :], poses[:, 2:, :]],
                axis=1
        )
        poses = np.moveaxis(poses, -1, 0).astype(np.float32)
        bounds = np.moveaxis(bounds, -1, 0).astype(np.float32)
        
        return imgs, poses, bounds
