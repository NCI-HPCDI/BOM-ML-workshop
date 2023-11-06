#adpated from https://github.com/NVlabs/AFNO-transformer

import math
from functools import partial
from collections import OrderedDict
from copy import Error, deepcopy
from re import S
from numpy.lib.arraypad import pad
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from timm.models.layers import DropPath, trunc_normal_
import torch.fft
from torch.nn.modules.container import Sequential
from einops import rearrange, repeat
from einops.layers.torch import Rearrange

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.fc2(x)
        return x


class AFNO2D(nn.Module):
    def __init__(self, hidden_size, num_blocks=8, sparsity_threshold=0.01, hard_thresholding_fraction=1, hidden_size_factor=1):
        super().__init__()
        assert hidden_size % num_blocks == 0, f"hidden_size {hidden_size} should be divisble by num_blocks {num_blocks}"

        self.hidden_size = hidden_size
        self.sparsity_threshold = sparsity_threshold
        self.num_blocks = num_blocks
        self.block_size = self.hidden_size // self.num_blocks
        self.hard_thresholding_fraction = hard_thresholding_fraction
        self.hidden_size_factor = hidden_size_factor

        self.w1 = nn.Parameter(torch.empty(2, self.num_blocks, self.block_size, self.block_size * self.hidden_size_factor))
        trunc_normal_(self.w1, std=.02)

        self.b1 = nn.Parameter(torch.empty(2, self.num_blocks, self.block_size * self.hidden_size_factor))
        trunc_normal_(self.b1, std=.02)

        self.w2 = nn.Parameter(torch.empty(2, self.num_blocks, self.block_size * self.hidden_size_factor, self.block_size))
        trunc_normal_(self.w2, std=.02)

        self.b2 = nn.Parameter(torch.empty(2, self.num_blocks, self.block_size))
        trunc_normal_(self.b2, std=.02)

    def forward(self, x):
        bias = x

        dtype = x.dtype
        x = x.float()
        B, H, W, C = x.shape

        x = torch.fft.rfft2(x, dim=(1, 2), norm="ortho")
        x = x.reshape(B, H, W // 2 + 1, self.num_blocks, self.block_size)

        o1_real = torch.zeros([B, H, W // 2 + 1, self.num_blocks, self.block_size * self.hidden_size_factor], device=x.device)
        o1_imag = torch.zeros([B, H, W // 2 + 1, self.num_blocks, self.block_size * self.hidden_size_factor], device=x.device)
        o2_real = torch.zeros(x.shape, device=x.device)
        o2_imag = torch.zeros(x.shape, device=x.device)

        total_modes = H // 2 + 1
        kept_modes = int(total_modes * self.hard_thresholding_fraction)

        o1_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes] = F.relu(
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].real, self.w1[0]) - \
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].imag, self.w1[1]) + \
            self.b1[0]
        )

        o1_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes] = F.relu(
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].imag, self.w1[0]) + \
            torch.einsum('...bi,bio->...bo', x[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes].real, self.w1[1]) + \
            self.b1[1]
        )

        o2_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes]  = (
            torch.einsum('...bi,bio->...bo', o1_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes], self.w2[0]) - \
            torch.einsum('...bi,bio->...bo', o1_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes], self.w2[1]) + \
            self.b2[0]
        )

        o2_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes]  = (
            torch.einsum('...bi,bio->...bo', o1_imag[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes], self.w2[0]) + \
            torch.einsum('...bi,bio->...bo', o1_real[:, total_modes-kept_modes:total_modes+kept_modes, :kept_modes], self.w2[1]) + \
            self.b2[1]
        )

        x = torch.stack([o2_real, o2_imag], dim=-1)
        x = F.softshrink(x, lambd=self.sparsity_threshold)
        x = torch.view_as_complex(x)
        x = x.reshape(B, H, W // 2 + 1, C)
        x = torch.fft.irfft2(x, s=(H, W), dim=(1,2), norm="ortho")
        x = x.type(dtype)

        return x + bias


class Block(nn.Module):
    def __init__(
            self,
            dim,
            mlp_ratio=4.,
            drop_path=0.,
            act_layer=nn.GELU,
            norm_layer=nn.LayerNorm,
            layer_scale=1.0,
            double_skip=True,
            num_blocks=8,
            sparsity_threshold=0.01,
            hard_thresholding_fraction=1.0,
            is_last_block=False,
        ):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.filter = AFNO2D(dim, num_blocks, sparsity_threshold, hard_thresholding_fraction) 
        #self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.is_last_block = is_last_block
        if not is_last_block:
          self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer)
        self.double_skip = double_skip
        self.layer_scale = layer_scale

    def forward(self, x):
        residual = x
        x = self.filter(x)

        if self.double_skip:
            x = x + residual * self.layer_scale 
            residual = x

        x = self.norm1(x)
        x = self.mlp(x)
        #x = self.drop_path(x)
        x = x + residual * self.layer_scale 

        if not self.is_last_block:
          x = self.norm2(x)

        return x

#class PrecipNet(nn.Module):
#    def __init__(self, params, backbone):
#        super().__init__()
#        self.params = params
#        self.patch_size = (params.patch_size, params.patch_size)
#        self.in_chans = params.N_in_channels
#        self.out_chans = params.N_out_channels
#        self.backbone = backbone
#        self.ppad = PeriodicPad2d(1)
#        self.conv = nn.Conv2d(self.out_chans, self.out_chans, kernel_size=3, stride=1, padding=0, bias=True)
#        self.act = nn.ReLU()

#    def forward(self, x):
#        x = self.backbone(x)
#        x = self.ppad(x)
#        x = self.conv(x)
#        x = self.act(x)
#        return x

class AFNONet(nn.Module):
    def __init__(
            self,
            img_size=(360, 720),
            patch_size=(6, 6),
            in_chans=2,
            out_chans=1,
            embed_dim=512,
            depth=1,
            mlp_ratio=4.,
            drop_path_rate=0.,
            num_blocks=8,
            sparsity_threshold=0.0,
            hard_thresholding_fraction=1.0,
        ):
        super().__init__()
        self.img_size = img_size
        self.patch_size = patch_size
        self.in_chans = in_chans
        self.out_chans = out_chans
        self.num_features = self.embed_dim = embed_dim
        self.num_blocks = num_blocks 
        norm_layer = partial(nn.LayerNorm, eps=1e-6)

        ## deep norm: https://arxiv.org/pdf/2203.00555.pdf
        layer_scale = (2 * depth) ** 0.25
        self.init_gain = (8 * depth) ** -0.25 

        self.patch_embed = PatchEmbed(img_size=img_size, patch_size=self.patch_size, in_chans=self.in_chans, embed_dim=embed_dim)
        num_patches = self.patch_embed.num_patches

        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches, embed_dim))

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]

        self.h = img_size[0] // self.patch_size[0]
        self.w = img_size[1] // self.patch_size[1]

        self.blocks = nn.Sequential(*[
            Block(dim=embed_dim, mlp_ratio=mlp_ratio, drop_path=dpr[i], layer_scale=layer_scale, norm_layer=norm_layer,
            num_blocks=self.num_blocks, sparsity_threshold=sparsity_threshold, hard_thresholding_fraction=hard_thresholding_fraction, is_last_block=i == depth-1) 
        for i in range(depth)])

        self.value_head = nn.Linear(embed_dim, self.out_chans*self.patch_size[0]*self.patch_size[1], bias=False)

        self.flow_norm = norm_layer(embed_dim)
        self.flow_act = nn.GELU()
        self.flow_head = nn.Linear(embed_dim, self.out_chans*self.patch_size[0]*self.patch_size[1]*2, bias=False)

        self.grid = self.setup_grid()
        
        trunc_normal_(self.pos_embed, std=.02)
        self.apply(self._init_weights)

        with torch.no_grad():
            self.flow_head.weight *= 1e-3

    def _init_weights(self, m):
        if isinstance(m, nn.Linear) or isinstance(m, nn.Conv2d):
            trunc_normal_(m.weight, std=.02)
            with torch.no_grad():
                m.weight *= self.init_gain
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}
      
    def setup_grid(self):
        h, w = self.img_size
        xgrid = torch.arange(w)
        xgrid = 2 * xgrid / (w - 1) - 1

        ygrid = torch.arange(h)
        ygrid = 2 * ygrid / (h - 1) - 1
        coords = torch.meshgrid(ygrid, xgrid, indexing="ij")
        coords = torch.stack(coords[::-1], dim=0).float()
        return coords.permute(1, 2, 0)

    def forward_features(self, x):
        B = x.shape[0]
        x = self.patch_embed(x) + self.pos_embed
        
        x = x.reshape(B, self.h, self.w, self.embed_dim)
        embed = x
        x = self.blocks(x)
        #for blk in self.blocks:
        #    x = blk(x)

        return x, embed

    def forward(self, x):
        x0 = x
        x, embed = self.forward_features(x)
        val = self.value_head(x)
        val = rearrange(
            val,
            "b h w (p1 p2 c_out) -> b c_out (h p1) (w p2)",
            p1=self.patch_size[0],
            p2=self.patch_size[1],
            h=self.img_size[0] // self.patch_size[0],
            w=self.img_size[1] // self.patch_size[1],
        )

        flow = self.flow_norm(embed)
        flow = self.flow_act(flow)
        flow = self.flow_head(flow)
        flow = rearrange(
            flow,
            "b h w (p1 p2 c_out coord) -> b c_out (h p1) (w p2) coord",
            p1=self.patch_size[0],
            p2=self.patch_size[1],
            h=self.img_size[0] // self.patch_size[0],
            w=self.img_size[1] // self.patch_size[1],
            coord=2,
        )

        x = x0[:, -self.out_chans:] #B, [t-1, t], H, W
        B, C, H, W = x.shape
        warp_coords = self.grid.repeat(B*C, 1, 1, 1) + flow.view(B*C, H, W, 2)
        x = x.view(B*C, 1, H, W)
        warped_x = F.grid_sample(x, warp_coords, mode='bilinear', align_corners=True)
        warped_x = warped_x.view(B, C, H, W)
        return warped_x + val
        
        return val, flow 

class PatchEmbed(nn.Module):
    def __init__(self, img_size=(224, 224), patch_size=(16, 16), in_chans=3, embed_dim=768):
        super().__init__()
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0])
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x):
        B, C, H, W = x.shape
        assert H == self.img_size[0] and W == self.img_size[1], f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)
        return x


if __name__ == "__main__":
    model = AFNONet(img_size=(224, 224), patch_size=(4,4), in_chans=2, out_chans=1)
    sample = torch.randn(1, 2, 224, 224)
    result = model(sample)
    print(result.shape)
    print(torch.norm(result))

