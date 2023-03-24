import torch
import torch.nn as nn
import torch.nn.functional as F

from einops import rearrange, repeat
from torchvision.models.resnet import Bottleneck
from typing import List

from IPython import embed
import pdb

def ResNetBottleNeck(c): return Bottleneck(c, c // 4)


def generate_grid(height: int, width: int):
    xs = torch.linspace(0, 1, width)
    ys = torch.linspace(0, 1, height)

    indices = torch.stack(torch.meshgrid(
        (xs, ys), indexing='xy'), 0)       # 2 h w
    indices = F.pad(indices, (0, 0, 0, 0, 0, 1),
                    value=1)                   # 3 h w
    # 1 3 h w
    indices = indices[None]

    return indices


def get_view_matrix(h=200, w=200, h_meters=100.0, w_meters=100.0, offset=0.0):
    """
    copied from ..data.common but want to keep models standalone
    """
    sh = h / h_meters
    sw = w / w_meters

    return [
        [0., -sw,          w/2.],
        [-sh,  0., h*offset+h/2.],
        [0.,  0.,            1.]
    ]


class Normalize(nn.Module):
    def __init__(self, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]):
        super().__init__()

        self.register_buffer('mean', torch.tensor(
            mean)[None, :, None, None], persistent=False)
        self.register_buffer('std', torch.tensor(
            std)[None, :, None, None], persistent=False)

    def forward(self, x):
        return (x - self.mean) / self.std


class RandomCos(nn.Module):
    def __init__(self, *args, stride=1, padding=0, **kwargs):
        super().__init__()

        linear = nn.Conv2d(*args, **kwargs)

        self.register_buffer('weight', linear.weight)
        self.register_buffer('bias', linear.bias)
        self.kwargs = {
            'stride': stride,
            'padding': padding,
        }

    def forward(self, x):
        return torch.cos(F.conv2d(x, self.weight, self.bias, **self.kwargs))


class BEVEmbedding(nn.Module):
    def __init__(
        self,
        dim: int,
        sigma: int,
        bev_height: int,
        bev_width: int,
        h_meters: int,
        w_meters: int,
        offset: int,
        decoder_blocks: list,
    ):
        """
        Only real arguments are:

        dim: embedding size
        sigma: scale for initializing embedding

        The rest of the arguments are used for constructing the view matrix.

        In hindsight we should have just specified the view matrix in config
        and passed in the view matrix...
        """
        super().__init__()

        # each decoder block upsamples the bev embedding by a factor of 2
        h = bev_height // (2 ** len(decoder_blocks))
        w = bev_width // (2 ** len(decoder_blocks))

        # bev coordinates
        grid = generate_grid(h, w).squeeze(0)
        grid[0] = bev_width * grid[0]
        grid[1] = bev_height * grid[1]

        # map from bev coordinates to ego frame
        V = get_view_matrix(bev_height, bev_width,
                            h_meters, w_meters, offset)  # 3 3
        V_inv = torch.FloatTensor(V).inverse(
        )                                  # 3 3
        # pdb.set_trace()
        # 3 (h w)
        grid = V_inv @ rearrange(grid, 'd h w -> d (h w)')
        grid = rearrange(grid, 'd (h w) -> d h w', h=h,
                         w=w)                    # 3 h w

        # egocentric frame
        self.register_buffer(
            'grid', grid, persistent=False)                    # 3 h w
        self.learned_features = nn.Parameter(
            sigma * torch.randn(dim, h, w))    # d h w

    def get_prior(self):
        return self.learned_features


class CrossAttention(nn.Module):
    def __init__(self, dim, heads, dim_head, qkv_bias, norm=nn.LayerNorm):
        super().__init__()

        self.scale = dim_head ** -0.5

        self.heads = heads
        self.dim_head = dim_head

        self.to_q = nn.Sequential(norm(dim), nn.Linear(
            dim, heads * dim_head, bias=qkv_bias))
        self.to_k = nn.Sequential(norm(dim), nn.Linear(
            dim, heads * dim_head, bias=qkv_bias))
        self.to_v = nn.Sequential(norm(dim), nn.Linear(
            dim, heads * dim_head, bias=qkv_bias))

        self.proj = nn.Linear(heads * dim_head, dim)
        self.prenorm = norm(dim)
        self.mlp = nn.Sequential(
            nn.Linear(dim, 2 * dim), nn.GELU(), nn.Linear(2 * dim, dim))
        self.postnorm = norm(dim)

    def forward(self, q, k, v, skip=None):
        """
        q: (b n d H W)
        k: (b n d h w)
        v: (b n d h w)
        """
        _, _, _, H, W = q.shape

        # Move feature dim to last for multi-head proj
        q = rearrange(q, 'b n d H W -> b n (H W) d')
        k = rearrange(k, 'b n d h w -> b n (h w) d')
        v = rearrange(v, 'b n d h w -> b (n h w) d')

        # Project with multiple heads
        # b (n H W) (heads dim_head)
        q = self.to_q(q)
        # b (n h w) (heads dim_head)
        k = self.to_k(k)
        # b (n h w) (heads dim_head)
        v = self.to_v(v)

        # Group the head dim with batch dim
        q = rearrange(q, 'b ... (m d) -> (b m) ... d',
                      m=self.heads, d=self.dim_head)
        k = rearrange(k, 'b ... (m d) -> (b m) ... d',
                      m=self.heads, d=self.dim_head)
        v = rearrange(v, 'b ... (m d) -> (b m) ... d',
                      m=self.heads, d=self.dim_head)

        # Dot product attention along cameras
        dot = self.scale * torch.einsum('b n Q d, b n K d -> b n Q K', q, k)
        dot = rearrange(dot, 'b n Q K -> b Q (n K)')
        att = dot.softmax(dim=-1)

        # Combine values (image level features).
        a = torch.einsum('b Q K, b K d -> b Q d', att, v)
        a = rearrange(a, '(b m) ... d -> b ... (m d)',
                      m=self.heads, d=self.dim_head)

        # Combine multiple heads
        z = self.proj(a)

        # Optional skip connection
        if skip is not None:
            z = z + rearrange(skip, 'b d H W -> b (H W) d')

        z = self.prenorm(z)
        z = z + self.mlp(z)
        z = self.postnorm(z)
        z = rearrange(z, 'b (H W) d -> b d H W', H=H, W=W)

        return z


class CrossViewAttention(nn.Module):
    def __init__(
        self,
        feat_height: int,
        feat_width: int,
        feat_dim: int,
        dim: int,
        image_height: int,
        image_width: int,
        qkv_bias: bool,
        heads: int = 4,
        dim_head: int = 32,
        no_image_features: bool = False,
        skip: bool = True,
    ):
        super().__init__()

        # 1 1 3 h w
        image_plane = generate_grid(feat_height, feat_width)[None]
        image_plane[:, :, 0] *= image_width
        image_plane[:, :, 1] *= image_height

        self.register_buffer('image_plane', image_plane, persistent=False)

        self.feature_linear = nn.Sequential(
            nn.BatchNorm2d(feat_dim),
            nn.ReLU(),
            nn.Conv2d(feat_dim, dim, 1, bias=False))

        if no_image_features:
            self.feature_proj = None
        else:
            self.feature_proj = nn.Sequential(
                nn.BatchNorm2d(feat_dim),
                nn.ReLU(),
                nn.Conv2d(feat_dim, dim, 1, bias=False))

        self.bev_embed = nn.Conv2d(2, dim, 1)
        self.img_embed = nn.Conv2d(4, dim, 1, bias=False)
        self.cam_embed = nn.Conv2d(4, dim, 1, bias=False)

        self.cross_attend = CrossAttention(dim, heads, dim_head, qkv_bias)
        self.skip = skip

    def forward(
        self,
        x: torch.FloatTensor,
        bev: BEVEmbedding,
        feature: torch.FloatTensor,
        I_inv: torch.FloatTensor,
        E_inv: torch.FloatTensor,
    ):
        """
        x: (b, c, H, W)
        feature: (b, n, dim_in, h, w)
        I_inv: (b, n, 3, 3)
        E_inv: (b, n, 4, 4)

        Returns: (b, d, H, W)
        """
        b, n, _, _, _ = feature.shape

        # b n 3 h w
        pixel = self.image_plane
        _, _, _, h, w = pixel.shape

        # b n 4 1
        c = E_inv[..., -1:]
        
        # print(E_inv)
        # embed()
        # (b n) 4 1 1
        c_flat = rearrange(c, 'b n ... -> (b n) ...')[..., None]
        # (b n) d 1 1
        c_embed = self.cam_embed(c_flat)

        # 1 1 3 (h w)
        pixel_flat = rearrange(pixel, '... h w -> ... (h w)')
        # b n 3 (h w)
        cam = I_inv @ pixel_flat
        cam = F.pad(cam, (0, 0, 0, 1, 0, 0, 0, 0),
                    value=1)                     # b n 4 (h w)
        # b n 4 (h w)
        d = E_inv @ cam
        d_flat = rearrange(d, 'b n d (h w) -> (b n) d h w',
                           h=h, w=w)           # (b n) 4 h w
        # (b n) d h w
        d_embed = self.img_embed(d_flat)

        # (b n) d h w
        img_embed = d_embed - c_embed
        img_embed = img_embed / \
            (img_embed.norm(dim=1, keepdim=True) + 1e-7)    # (b n) d h w

        # 2 H W
        world = bev.grid[:2]
        print(f'bev.grid.shape {bev.grid.shape} bev.grid[:,2].shape {bev.grid[:2].shape}')
        # 1 d H W

        print(f'world.shape {world.shape} world[None] {world[None].shape}')
        w_embed = self.bev_embed(world[None])
        

        print(f'w_embed {w_embed.shape}  c_embed {c_embed.shape}')
        # (b n) d H W
        bev_embed = w_embed - c_embed
        bev_embed = bev_embed / \
            (bev_embed.norm(dim=1, keepdim=True) + 1e-7)    # (b n) d H W
        query_pos = rearrange(
            bev_embed, '(b n) ... -> b n ...', b=b, n=n)      # b n d H W 

        feature_flat = rearrange(
            feature, 'b n ... -> (b n) ...')               # (b n) d h w

        if self.feature_proj is not None:
            key_flat = img_embed + \
                self.feature_proj(feature_flat)              # (b n) d h w
        else:
            # (b n) d h w
            key_flat = img_embed

        val_flat = self.feature_linear(
            feature_flat)                            # (b n) d h w

        # Expand + refine the BEV embedding
        # b n d H W
        query = query_pos + x[:, None]
        key = rearrange(key_flat, '(b n) ... -> b n ...',
                        b=b, n=n)             # b n d h w
        val = rearrange(val_flat, '(b n) ... -> b n ...',
                        b=b, n=n)             # b n d h w

        return self.cross_attend(query, key, val, skip=x if self.skip else None)


class Encoder(nn.Module):
    def __init__(
            self,
            backbone,
            cross_view: dict,
            bev_embedding: dict,
            dim: int = 128,
            middle: List[int] = [2, 2],
            scale: float = 1.0,
    ):
        super().__init__()

        self.norm = Normalize()
        self.backbone = backbone

        if scale < 1.0:
            self.down = lambda x: F.interpolate(
                x, scale_factor=scale, recompute_scale_factor=False)
        else:
            self.down = lambda x: x

        assert len(self.backbone.output_shapes) == len(middle)

        cross_views = list()
        layers = list()

        for feat_shape, num_layers in zip(self.backbone.output_shapes, middle):
            _, feat_dim, feat_height, feat_width = self.down(
                torch.zeros(feat_shape)).shape

            cva = CrossViewAttention(
                feat_height, feat_width, feat_dim, dim, **cross_view)
            cross_views.append(cva)

            layer = nn.Sequential(*[ResNetBottleNeck(dim)
                                  for _ in range(num_layers)])
            layers.append(layer)

        self.bev_embedding = BEVEmbedding(dim, **bev_embedding)
        self.cross_views = nn.ModuleList(cross_views)
        self.layers = nn.ModuleList(layers)

    def forward(self, batch):
        b, n, _, _, _ = batch['image'].shape

        print(f'satellite map.shape {batch["image"].shape}') # 4, 1, 3, 512, 512

        image = batch['image'].flatten(0, 1)            # b n c h w
        I_inv = batch['intrinsics'].inverse()           # b n 3 3
        E_inv = batch['extrinsics'].inverse()           # b n 4 4

        features = [self.down(y) for y in self.backbone(self.norm(image))]

        # TODO: Pass the sat_map tp backbone only...
        # print(f'[Satellite net] features[0].shape: {features[0].shape}')
        # return features 

        print(f'features[0].shape {features[0].shape}')
        print(f'features[1].shape {features[1].shape}')        

        # Goal: return a tensor of shape ()

        x = self.bev_embedding.get_prior()              # d H W
        print(f' self.bev_embedding.get_prior().shape {self.bev_embedding.get_prior().shape}')
        x = repeat(x, '... -> b ...', b=b)              # b d H W
        print(f'x.shape {x.shape}')

        for cross_view, feature, layer in zip(self.cross_views, features, self.layers):
            feature = rearrange(feature, '(b n) ... -> b n ...', b=b, n=n)  # Now, feature = 1, 128, 25, 25
            # print(f'self.bev_embedding.shape {self.bev_embedding.shape}')
            x = cross_view(x, self.bev_embedding, feature, I_inv, E_inv)
            x = layer(x)

        return x
