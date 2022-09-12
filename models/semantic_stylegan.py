# Copyright (C) 2022 ByteDance Inc.
# All rights reserved.
# Licensed under the CC BY-NC-SA 4.0 license (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).

# The software is made available under Creative Commons BY-NC-SA 4.0 license
# by ByteDance Inc. You can use, redistribute, and adapt it
# for non-commercial purposes, as long as you (a) give appropriate credit
# by citing our paper, (b) indicate any changes that you've made,
# and (c) distribute any derivative works under the same license.

# THE AUTHORS DISCLAIM ALL WARRANTIES WITH REGARD TO THIS SOFTWARE, INCLUDING ALL
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR ANY PARTICULAR PURPOSE.
# IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, INDIRECT OR CONSEQUENTIAL
# DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
# WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING
# OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import math
import random
import torch.nn.utils.parametrizations as parametrizations
import torch
from torch import nn
import torch.nn.functional as F

from .utils import (
    StyledConv,
    FixedStyledConv,
    ToRGB,
    PixelNorm,
    EqualLinear,
    ConvLayer,
    ResBlock,
    PositionEmbedding,
    Blur,
)


class LocalGenerator(nn.Module):
    def __init__(
        self,
        in_channel,
        out_channel,
        hidden_channel,
        style_dim,
        n_layers=8,
        depth_layers=8,
        use_depth=False,
        detach_texture=False,
    ):
        super().__init__()
        self.n_layers = n_layers
        self.depth_layers = depth_layers
        self.use_depth = use_depth
        self.detach_texture = detach_texture
        self.linears = nn.ModuleList()
        for _ in range(n_layers):
            self.linears.append(
                StyledConv(in_channel, hidden_channel, 1, style_dim, inject_noise=False)
            )
            in_channel = hidden_channel
        self.to_feat = ToRGB(hidden_channel, out_channel, style_dim)
        if self.use_depth:
            self.to_depth = ToRGB(hidden_channel, 1, style_dim)

    def forward(self, x, latent):
        depth = torch.zeros(x.size(0), 1, x.size(2), x.size(3)).to(x.device)
        for i, linear in enumerate(self.linears):
            x = linear(x, latent[:, i])
            if self.use_depth and i == self.depth_layers - 1:
                depth = self.to_depth(x, None)
                if self.detach_texture and i < self.n_layers - 1:
                    x = x.detach()
        feat = self.to_feat(x, None)
        return feat, depth


class RenderNet(nn.Module):
    def __init__(
        self,
        min_size,
        out_size,
        coarse_size,
        in_channel,
        img_dim,
        seg_dim,
        style_dim,
        channel_multiplier=2,
        blur_kernel=[1, 3, 3, 1],
    ):
        super().__init__()
        self.channels = {
            4: 512,
            8: 512,
            16: 512,
            32: 512,
            64: 256 * channel_multiplier,
            128: 128 * channel_multiplier,
            256: 64 * channel_multiplier,
            512: 32 * channel_multiplier,
            1024: 16 * channel_multiplier,
        }
        self.out_size = out_size
        self.min_size = min_size
        self.fade_iters = 0

        self.alpha = 1
        self.log_out_size = int(math.log(out_size, 2))
        self.log_min_size = int(math.log(min_size, 2))
        self.depth = self.log_min_size + 1
        # J-TODO: Check that the depth start from 1 not from 2 or 0.
        self.coarse_size = coarse_size
        self.n_layers = (self.log_out_size - self.log_min_size) * 2

        feat_channel = in_channel
        self.convs = nn.ModuleList()
        self.noises = nn.Module()
        self.to_rgbs = nn.ModuleList()
        self.to_segs = nn.ModuleList()
        # Upsampling
        # factor=2
        # kernel_size=3
        # p = (len(blur_kernel) - factor) - (kernel_size - 1)
        # pad0 = (p + 1) // 2 + factor - 1
        # pad1 = p // 2 + 1
        # self.blur = Blur(blur_kernel, pad=(pad0, pad1), upsample_factor=factor)
        self.upsample = nn.Upsample(scale_factor=2, mode="nearest")
        ##End Upsampling
        for i in range(self.log_out_size - self.log_min_size):
            cur_size = self.min_size * (2 ** (i + 1))
            out_channel = self.channels[cur_size]
            if cur_size // 2 == coarse_size:
                in_channel = in_channel + feat_channel
            self.convs.append(
                FixedStyledConv(
                    in_channel,
                    out_channel,
                    3,
                    style_dim,
                    upsample=True,
                    blur_kernel=blur_kernel,
                )
            )
            self.convs.append(
                FixedStyledConv(
                    out_channel,
                    out_channel,
                    3,
                    style_dim,
                    upsample=False,
                    blur_kernel=blur_kernel,
                )
            )
            self.noises.register_buffer(
                f"noise_{2*i}", torch.randn(1, 1, cur_size, cur_size)
            )
            self.noises.register_buffer(
                f"noise_{2*i+1}", torch.randn(1, 1, cur_size, cur_size)
            )
            self.to_rgbs.append(ToRGB(out_channel, img_dim, style_dim, upsample=True))
            self.to_segs.append(ToRGB(out_channel, seg_dim, style_dim, upsample=True))
            in_channel = out_channel

    def get_noise(self, noise, randomize_noise):
        if noise is None:
            if randomize_noise:
                noise = [None] * self.n_layers
            else:
                noise = [
                    getattr(self.noises, f"noise_{i}") for i in range(self.n_layers)
                ]
        return noise

    def forward(
        self, x, noise=None, randomize_noise=False, skip_rgb=None, skip_seg=None
    ):
        noise = self.get_noise(noise, randomize_noise)
        x_orig, x = x, F.adaptive_avg_pool2d(x, (self.min_size, self.min_size))
        rgb, seg, x_old, old_rgb, old_seg = None, None, None, None, None

        for i in range(self.depth - self.log_min_size):
            if x.size(2) == self.coarse_size:
                x = torch.cat((x, x_orig), 1)
            x_old = x
            old_rgb = rgb
            old_seg = seg
            x = self.convs[2 * i](x, None, noise=noise[2 * i])
            x = self.convs[2 * i + 1](x, None, noise=noise[2 * i + 1])
            rgb = self.to_rgbs[i](x, None, rgb)
            if skip_rgb is not None and rgb.size(2) == skip_rgb.size(2):
                rgb += skip_rgb
            seg = self.to_segs[i](x, None, seg)
            if skip_seg is not None and seg.size(2) == skip_seg.size(2):
                seg += skip_seg
        if self.alpha < 1:
            ##Upsampling

            # out = F.conv_transpose2d(input, weight.transpose(0, 1), padding=0, stride=2)
            # x_old = self.blur(out)
            # J-TODO: This is wrong implementation, u r upsamling the last outmost val
            x_old = self.upsample(
                x_old
            )  # Is this the correct way of sampling compared to Sampling in the conv block?
            ## End Upsampling
            print(f"X old shape is {x_old.shape}")
            print(f"X new shape is {x.shape}")
            print(f"alpha is = {self.alpha}")
            old_rgb = self.to_rgbs[self.depth - self.log_min_size - 2](
                x_old, None, old_rgb
            )  ##J-TODO -1? and rgb as third Option?
            old_seg = self.to_segs[self.depth - self.log_min_size - 2](
                x_old, None, old_seg
            )
            rgb = (1 - self.alpha) * old_rgb + self.alpha * rgb
            seg = (1 - self.alpha) * old_seg + self.alpha * seg
            self.alpha += self.fade_iters

        return rgb, seg

    def grow_network(self, num_iterations):
        self.fade_iters = 1 / num_iterations
        self.alpha = 1 / num_iterations
        self.depth += 1


class SemanticGenerator(nn.Module):
    def __init__(
        self,
        size=256,
        style_dim=512,
        n_mlp=8,
        channel_multiplier=2,
        blur_kernel=[1, 3, 3, 1],
        lr_mlp=0.01,
        seg_dim=2,
        coarse_size=64,
        min_feat_size=8,
        local_layers=10,
        local_channel=64,
        coarse_channel=512,
        base_layers=2,
        depth_layers=6,
        residual_refine=True,
        detach_texture=False,
        transparent_dims=(),
        **kwargs,
    ):
        super().__init__()

        assert depth_layers <= local_layers
        assert coarse_size < size
        assert min_feat_size < coarse_size and coarse_size % min_feat_size == 0
        self.size = size
        self.style_dim = style_dim
        self.log_size = int(math.log(size, 2))
        self.n_local = self.seg_dim = seg_dim
        self.base_layers = base_layers
        self.local_layers = local_layers
        self.local_channel = local_channel
        self.depth_layers = depth_layers
        self.coarse_channel = coarse_channel
        self.coarse_size = coarse_size
        self.min_feat_size = min_feat_size
        self.residual_refine = residual_refine
        self.detach_texture = detach_texture
        self.transparent_dims = list(transparent_dims)
        self.n_latent = self.base_layers + self.n_local * 2  # Default latent space
        self.n_latent_expand = self.n_local * self.local_layers  # Expanded latent space
        print(f"n_latent: {self.n_latent}, n_latent_expand: {self.n_latent_expand}")

        self.pos_embed = PositionEmbedding(2, self.local_channel, N_freqs=self.log_size)
        self.local_nets = nn.ModuleList()
        for i in range(0, self.n_local):
            use_depth = i > 0  # disable pseudo-depth for background generator
            self.local_nets.append(
                LocalGenerator(
                    local_channel,
                    coarse_channel,
                    local_channel,
                    style_dim,
                    n_layers=local_layers,
                    depth_layers=depth_layers,
                    use_depth=use_depth,
                    detach_texture=detach_texture,
                )
            )

        self.render_net = RenderNet(
            min_feat_size,
            size,
            coarse_size,
            coarse_channel,
            3,
            seg_dim,
            style_dim,
            channel_multiplier=channel_multiplier,
            blur_kernel=blur_kernel,
        )

        layers = [PixelNorm()]
        for i in range(n_mlp):
            layers.append(
                EqualLinear(
                    style_dim, style_dim, lr_mul=lr_mlp, activation="fused_lrelu"
                )
            )
        self.style = nn.Sequential(*layers)

    def truncate_styles(self, styles, truncation, truncation_latent):
        if truncation < 1:
            style_t = []
            for style in styles:
                style_t.append(
                    truncation_latent + truncation * (style - truncation_latent)
                )
            styles = style_t
        return styles

    def expand_latents(self, latent):
        """Expand the default latent codes.
        Input:
            latent: tensor of N x (n_base + n_local x 2) x style_dim
        Output:
            latent_expanded: tensor of N x (n_local x local_layers) x style_dim
        """
        assert latent.ndim == 3
        if latent.size(1) == self.n_latent_expand:
            return latent

        assert latent.size(1) == self.n_latent
        latent_expanded = []
        for i in range(self.n_local):
            if i == 0:
                # Disable base code for background
                if self.depth_layers > 0:
                    latent_expanded.append(
                        latent[:, 2 * i + self.base_layers]
                        .unsqueeze(1)
                        .repeat(1, self.depth_layers, 1)
                    )
                if self.local_layers - self.depth_layers > 0:
                    latent_expanded.append(
                        latent[:, 2 * i + self.base_layers + 1]
                        .unsqueeze(1)
                        .repeat(1, self.local_layers - self.depth_layers, 1)
                    )
            else:
                if self.base_layers > 0:
                    latent_expanded.append(latent[:, : self.base_layers])
                if self.depth_layers - self.base_layers > 0:
                    latent_expanded.append(
                        latent[:, 2 * i + self.base_layers]
                        .unsqueeze(1)
                        .repeat(1, self.depth_layers - self.base_layers, 1)
                    )
                if self.local_layers - self.depth_layers > 0:
                    latent_expanded.append(
                        latent[:, 2 * i + self.base_layers + 1]
                        .unsqueeze(1)
                        .repeat(1, self.local_layers - self.depth_layers, 1)
                    )
        latent_expanded = torch.cat(latent_expanded, 1)
        return latent_expanded

    def mix_styles(self, styles):
        if len(styles) < 2:
            # Input is the latent code
            if styles[0].ndim < 3:
                latent = styles[0].unsqueeze(1).repeat(1, self.n_latent, 1)
            else:
                latent = styles[0]
        elif len(styles) > 2:
            # Input is the latent code (list)
            latent = torch.stack(styles, 1)
        else:
            # Input are two latent codes -> style mixing
            base_latent = styles[0].unsqueeze(1).repeat(1, self.base_layers, 1)
            latent = [base_latent]
            for i in range(self.n_local):
                N = styles[0].size(0)
                latent1 = []
                latent2 = []
                for j in range(N):
                    inject_index = random.randint(0, 2)
                    if inject_index == 0:
                        latent1_ = latent2_ = styles[0][j]
                    elif inject_index == 1:
                        latent1_, latent2_ = styles[0][j], styles[1][j]
                    else:
                        latent1_ = latent2_ = styles[1][j]
                    latent1.append(latent1_)
                    latent2.append(latent2_)
                latent1 = torch.stack(latent1)  # N x style_dim
                latent2 = torch.stack(latent2)  # N x style_dim
                latent.append(latent1.unsqueeze(1))
                latent.append(latent2.unsqueeze(1))
            latent = torch.cat(latent, 1)  # N x n_latent x style_dim
        latent = self.expand_latents(
            latent
        )  # N  x (n_local x local_layers) x style_dim
        return latent

    def composite(self, feats, depths, mask=None):
        seg = F.softmax(torch.cat(depths, dim=1), dim=1)
        if mask is not None:
            # If mask is given, ignore specified classes
            assert mask.size(0) == seg.size(0)
            assert mask.size(1) == seg.size(1)
            mask = mask.reshape(seg.size(0), seg.size(1), 1, 1)
            seg = seg * mask
            seg = seg / (seg.sum(1, keepdim=True) + 1e-8)
        if len(self.transparent_dims) > 0:
            coefs = (
                torch.tensor(
                    [
                        0.0 if i in self.transparent_dims else 1.0
                        for i in range(self.seg_dim)
                    ]
                )
                .view(1, -1, 1, 1)
                .to(seg.device)
            )
            seg_normal = seg * coefs  # zero out transparent classes
            seg_normal = seg_normal / (
                seg_normal.sum(1, keepdim=True) + 1e-8
            )  # re-normalize the feature map

            coefs = (
                torch.tensor(
                    [
                        1.0 if i in self.transparent_dims else 0.0
                        for i in range(self.seg_dim)
                    ]
                )
                .view(1, -1, 1, 1)
                .to(seg.device)
            )
            seg_trans = seg * coefs  # zero out non-transparent classes

            weights = seg_normal + seg_trans
        else:
            weights = seg
        feat = sum([feats[i] * weights[:, i : i + 1] for i in range(self.seg_dim)])
        return feat, seg

    def make_coords(self, b, h, w, device):
        x_channel = (
            torch.linspace(-1, 1, w, device=device).view(1, 1, 1, -1).repeat(b, 1, w, 1)
        )
        y_channel = (
            torch.linspace(-1, 1, h, device=device).view(1, 1, -1, 1).repeat(b, 1, 1, h)
        )
        return torch.cat((x_channel, y_channel), dim=1)

    def forward(
        self,
        latent,
        coords=None,
        truncation=1,
        truncation_latent=None,
        noise=None,
        randomize_noise=True,
        input_is_latent=False,
        composition_mask=None,
        return_latents=False,
        return_coarse=False,
        return_all=False,
    ):

        if not input_is_latent:
            latent = [self.style(s) for s in latent]

        latent = self.truncate_styles(latent, truncation, truncation_latent)
        latent = self.mix_styles(latent)  # expanded latent code

        # Position Embedding
        if coords is None:
            coords = self.make_coords(
                latent.shape[0], self.coarse_size, self.coarse_size, latent.device
            )
            coords = [coords.clone() for _ in range(self.n_local)]

        # Local Generators
        feats = []
        depths = []
        for i in range(self.n_local):
            x = self.pos_embed(coords[i])
            local_latent = latent[
                :, i * self.local_layers : (i + 1) * self.local_layers
            ]
            feat, depth = self.local_nets[i](x, local_latent)
            feats.append(feat)
            depths.append(depth)

        # Composition and render
        feat, seg_coarse = self.composite(feats, depths, mask=composition_mask)
        seg_coarse = 2 * seg_coarse - 1  # normalize to [-1,1]

        skip_seg = seg_coarse if self.residual_refine else None
        rgb, seg = self.render_net(
            feat,
            noise=noise,
            randomize_noise=randomize_noise,
            skip_rgb=None,
            skip_seg=skip_seg,
        )

        if return_latents:
            return rgb, latent
        elif return_coarse:
            return rgb, seg_coarse
        elif return_all:
            return rgb, seg, seg_coarse, depths, latent
        else:
            return rgb, seg


class DualBranchDiscriminator(nn.Module):
    def __init__(
        self,
        img_size,
        seg_size,
        img_dim,
        seg_dim,
        channel_multiplier=2,
        blur_kernel=[1, 3, 3, 1],
    ):
        super().__init__()

        self.channels = {
            4: 512,
            8: 512,
            16: 512,
            32: 512,
            64: 256 * channel_multiplier,
            128: 128 * channel_multiplier,
            256: 64 * channel_multiplier,
            512: 32 * channel_multiplier,
            1024: 16 * channel_multiplier,
        }
        self.depth = 1
        self.alpha = 1
        self.fade_iters = 0
        log_size = int(math.log(img_size, 2))
        if seg_size is None:
            seg_size = img_size
        self.downsample = nn.AvgPool2d(kernel_size=(2, 2), stride=(2, 2))
        # convs_img_list = [ConvLayer(img_dim, self.channels[img_size], 1, spectral_norm=True)]
        self.convs_initial_layer = []
        for i in range(log_size, 2, -1):
            self.convs_initial_layer.append(
                ConvLayer(img_dim, self.channels[2 ** (i)], 1, spectral_norm=True)
            )

        self.convs_img_list = []
        in_channel = self.channels[img_size]
        for i in range(log_size, 2, -1):
            out_channel = self.channels[2 ** (i - 1)]
            self.convs_img_list.append(
                ResBlock(in_channel, out_channel, blur_kernel, spectral_norm=True)
            )
            in_channel = out_channel

        # self.convs_img = nn.Sequential(*convs_img_list)

        # seg_size = img_size anyway.
        log_size = int(math.log(seg_size, 2))
        # convs_seg_list = [ConvLayer(seg_dim, self.channels[seg_size], 1, spectral_norm=True)]
        self.seg_initial_layer = []
        for i in range(log_size, 2, -1):
            self.seg_initial_layer.append(
                ConvLayer(seg_dim, self.channels[2 ** (i)], 1, spectral_norm=True)
            )

        self.convs_seg_list = []
        in_channel = self.channels[seg_size]
        for i in range(log_size, 2, -1):
            out_channel = self.channels[2 ** (i - 1)]
            self.convs_seg_list.append(
                ResBlock(in_channel, out_channel, blur_kernel, spectral_norm=True)
            )
            in_channel = out_channel
        # self.convs_seg = nn.Sequential(*convs_seg_list)

        self.stddev_group = 4
        self.stddev_feat = 1

        self.final_conv = ConvLayer(in_channel + 1, self.channels[4], 3)
        self.final_linear = nn.Sequential(
            EqualLinear(
                self.channels[4] * 4 * 4, self.channels[4], activation="fused_lrelu"
            ),
            EqualLinear(self.channels[4], 1),
        )

    def _cal_stddev(self, x):
        batch, channel, height, width = x.shape
        group = min(batch, self.stddev_group)
        stddev = x.view(
            group, -1, self.stddev_feat, channel // self.stddev_feat, height, width
        )
        stddev = torch.sqrt(stddev.var(0, unbiased=False) + 1e-8)
        stddev = stddev.mean([2, 3, 4], keepdims=True).squeeze(2)
        stddev = stddev.repeat(group, 1, height, width)
        x = torch.cat([x, stddev], 1)

        return x

    # def forward(self, img, seg=None):
    #     batch = img.shape[0]

    #     out = self.convs_img(img)
    #     if seg is not None:
    #         out = out + self.convs_seg(seg)

    #     out = self._cal_stddev(out)

    #     out = self.final_conv(out)

    #     out = out.view(batch, -1)
    #     out = self.final_linear(out)

    #     return out

    # J-TODO: convs_initial_layer should be changed to from_rgb.
    # J-TODO: Need to specify maximum depth
    # checking for self.alpha should be done after 1 block of discriminator
    def forward(self, img, seg=None):
        batch = img.shape[0]
        layers_length = len(self.convs_img_list)

        out_img = img.copy()
        out_img = self.convs_initial_layer[layers_length - self.depth](out_img)  # 16x16
        out_img = self.convs_img_list[layers_length - self.depth](out_img)

        out_seg = seg.copy()
        out_seg = self.seg_initial_layer[layers_length - self.depth](out_seg)
        out_seg = self.convs_seg_list[layers_length - self.depth](out_seg)

        if self.alpha < 1:
            out_img_old = self.downsample(img)
            out_img_old = self.convs_initial_layer[layers_length - self.depth + 1](
                out_img
            )  # 8x8

            out_seg_old = self.downsample(seg)
            out_seg_old = self.seg_initial_layer[layers_length - self.depth + 1](
                out_seg_old
            )

            out_img = (1 - self.alpha) * out_img_old + self.alpha * out_img
            out_seg = (1 - self.alpha) * out_seg_old + self.alpha * out_seg
            self.alpha += self.fade_iters

        for i in range(self.depth - 1, 0, -1):
            out_img = self.convs_img_list[layers_length - i](out_img)

        out = out_img

        if seg is not None:
            for i in range(self.depth - 1, 0, -1):
                out_seg = self.convs_seg_list[layers_length - i](out_seg)

            out = out_img + out_seg

        out = self._cal_stddev(out)

        out = self.final_conv(out)

        out = out.view(batch, -1)
        out = self.final_linear(out)

        return out

    def grow_network(self, num_iterations):
        self.fade_iters = 1 / num_iterations
        self.alpha = 1 / num_iterations
        self.depth += 1
