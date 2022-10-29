# This file contains modules common to various models
import math

import torch
import torch.nn as nn
from utils.general import non_max_suppression
'''
feature map尺寸计算公式： out_size = (in_size + 2*Padding - kernel_size)/strides + 1
卷积计算时map尺寸向下取整
池化计算时map尺寸向上取整
'''

def autopad(k, p=None):  # kernel, padding
    '''
    自动填充
    返回padding值
        kernel_size 为 int类型时 ：padding = k // 2（整数除法进行一次）
                        否则    : padding = [x // 2 for x in k]
    '''
    # Pad to 'same'
    if p is None:  # k是否为int类型，是则返回True
        p = k // 2 if isinstance(k, int) else [x // 2 for x in k]  # auto-pad
    return p

def DWConv(c1, c2, k=1, s=1, act=True):
    '''
    深度分离卷积层 Depthwise convolution：
        是G（group）CONV的极端情况；
        分组数量等于输入通道数量，即每个通道作为一个小组分别进行卷积，结果联结作为输出，Cin = Cout = g，没有bias项。
        c1 : in_channels
        c2 : out_channels
        k : kernel_size
        s : stride
        act : 是否使用激活函数
        math.gcd() 返回的是最大公约数
    '''
    # Depthwise convolution
    return Conv(c1, c2, k, s, g=math.gcd(c1, c2), act=act)

class Conv(nn.Module):
    '''
    标准卷积层Conv
    包括Conv2d + BN + HardWish激活函数
    (self, in_channels, out_channels, kernel_size, stride, padding, groups, activation_flag)
    p=None时，out_size = in_size/strides
    '''
    # Standard convolution
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1, act=True):  # ch_in, ch_out, kernel, stride, padding, groups
        super(Conv, self).__init__()
        self.conv = nn.Conv2d(c1, c2, k, s, autopad(k, p), groups=g, bias=False)
        self.bn = nn.BatchNorm2d(c2)
        self.act = nn.Hardswish() if act else nn.Identity()

    def forward(self, x):  # 前向计算（有BN）
        return self.act(self.bn(self.conv(x)))

    def fuseforward(self, x):  # 前向融合计算（无BN）
        return self.act(self.conv(x))

class Bottleneck(nn.Module):
    '''
    标准Bottleneck层
        input : input
        output : input + Conv3×3（Conv1×1(input)）
    (self, in_channels, out_channels, shortcut_flag, group, expansion隐藏神经元的缩放因子)
    out_size = in_size
    '''
    # Standard bottleneck
    def __init__(self, c1, c2, shortcut=True, g=1, e=0.5):  # ch_in, ch_out, shortcut, groups, expansion
        super(Bottleneck, self).__init__()
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = Conv(c_, c2, 3, 1, g=g)
        self.add = shortcut and c1 == c2

    def forward(self, x):
        '''
        若 shortcut_flag为Ture 且 输入输出通道数相等，则返回跳接后的结构：
            x + Conv3×3（Conv1×1(x)）
        否则不进行跳接：
            Conv3×3（Conv1×1(x)）
        '''
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))

class BottleneckCSP(nn.Module):
    '''
    标准ottleneckCSP层
    (self, in_channels, out_channels, Bottleneck层重复次数, shortcut_flag, group, expansion隐藏神经元的缩放因子)
    out_size = in_size
    '''
    # CSP Bottleneck https://github.com/WongKinYiu/CrossStagePartialNetworks
    def __init__(self, c1, c2, n=1, shortcut=True, g=1, e=0.5):  # ch_in, ch_out, number, shortcut, groups, expansion
        super(BottleneckCSP, self).__init__()
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = nn.Conv2d(c1, c_, 1, 1, bias=False)
        self.cv3 = nn.Conv2d(c_, c_, 1, 1, bias=False)
        self.cv4 = Conv(2 * c_, c2, 1, 1)
        self.bn = nn.BatchNorm2d(2 * c_)  # applied to cat(cv2, cv3)
        self.act = nn.LeakyReLU(0.1, inplace=True)
        self.m = nn.Sequential(*[Bottleneck(c_, c_, shortcut, g, e=1.0) for _ in range(n)])

    def forward(self, x):
        y1 = self.cv3(self.m(self.cv1(x)))   # CONV + BottleNeck + Conv2d  out_channels = c_
        y2 = self.cv2(x)  # Conv2d   out_channels = c_
        return self.cv4(self.act(self.bn(torch.cat((y1, y2), dim=1))))  # concat(y1 + y2) + BN + LeakyReLU + Conv2d  out_channels = c2

class SPP(nn.Module):
    '''
    空间金字塔池化SPP：
    (self, in_channels, out_channels, 池化尺寸strides[3])
    '''
    # Spatial pyramid pooling layer used in YOLOv3-SPP
    def __init__(self, c1, c2, k=(5, 9, 13)):
        super(SPP, self).__init__()
        c_ = c1 // 2  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = Conv(c_ * (len(k) + 1), c2, 1, 1)
        # 建立5×5 9×9 13×13的最大池化处理过程的list
        self.m = nn.ModuleList([nn.MaxPool2d(kernel_size=x, stride=1, padding=x // 2) for x in k])

    def forward(self, x):
        x = self.cv1(x)
        return self.cv2(torch.cat([x] + [m(x) for m in self.m], 1))

class Focus(nn.Module):
    '''
    Focus : 把宽度w和高度h的信息整合到c空间中
    (self, in_channels, out_channels, kernel_size, stride, padding, group, activation_flag)
    '''
    # Focus wh information into c-space
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1, act=True):  # ch_in, ch_out, kernel, stride, padding, groups
        super(Focus, self).__init__()
        self.conv = Conv(c1 * 4, c2, k, s, p, g, act)

    def forward(self, x):
        '''
        x(batch_size, channels, height, width) -> y(batch_size, 4*channels, height/2, weight/2)
        '''
        # ::代表[start:end:step], 以2为步长取值
        return self.conv(torch.cat([x[..., ::2, ::2], x[..., 1::2, ::2], x[..., ::2, 1::2], x[..., 1::2, 1::2]], 1))

class Concat(nn.Module):
    '''
    (dimension)
    默认d=1按列拼接 ， d=0则按行拼接
    '''
    # Concatenate a list of tensors along dimension
    def __init__(self, dimension=1):
        super(Concat, self).__init__()
        self.d = dimension

    def forward(self, x):
        return torch.cat(x, self.d)


class NMS(nn.Module):
    # Non-Maximum Suppression (NMS) module
    conf = 0.3  # confidence threshold
    iou = 0.6  # IoU threshold
    classes = None  # (optional list) filter by class

    def __init__(self, dimension=1):
        super(NMS, self).__init__()

    def forward(self, x):
        return non_max_suppression(x[0], conf_thres=self.conf, iou_thres=self.iou, classes=self.classes)

class Flatten(nn.Module):
    '''
    在全局平均池化以后使用，去掉2个维度
    (batch_size, channels, size, size) -> (batch_size, channels*size*size)
    '''
    # Use after nn.AdaptiveAvgPool2d(1) to remove last 2 dimensions
    @staticmethod
    def forward(x):
        return x.view(x.size(0), -1)

class Classify(nn.Module):
    '''
    (self, in_channels, out_channels, kernel_size=1, stride=1, padding=None, groups=1)
    (batch_size, channels, size, size) -> (batch_size, channels*1*1)
    '''
    # Classification head, i.e. x(b,c1,20,20) to x(b,c2)
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1):  # ch_in, ch_out, kernel, stride, padding, groups
        super(Classify, self).__init__()
        # 给定输入数据和输出数据的大小，自适应算法能够自动帮助我们计算核的大小和每次移动的步长
        self.aap = nn.AdaptiveAvgPool2d(1)  # to x(batch_size,ch_in,1,1) 返回1×1的池化结果
        self.conv = nn.Conv2d(c1, c2, k, s, autopad(k, p), groups=g, bias=False)  # to x(batch_size,ch_out,1,1)
        self.flat = Flatten()

    def forward(self, x):
        #
        z = torch.cat([self.aap(y) for y in (x if isinstance(x, list) else [x])], 1)  # cat if x is list
        return self.flat(self.conv(z))  # flatten to x(batch_size, ch_out×1×1)

class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7):
        super(SpatialAttention, self).__init__()
        self.conv1 = nn.Conv2d(2, 1, kernel_size=kernel_size,
                               padding=kernel_size//2, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        avg_out = torch.mean(x, dim=1,keepdim=True)
        max_out,_ = torch.max(x, dim=1,keepdim=True)
        out = torch.cat([avg_out,max_out], dim=1)
        out = self.conv1(out)
        return self.sigmoid(out)

class ChannelAttention(nn.Module):
    def __init__(self, in_planes, ratio=16):
        super(ChannelAttention, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.max_pool = nn.AdaptiveMaxPool2d(1)
        self.fc = nn.Sequential(
            nn.Conv2d(in_planes,in_planes//ratio, 1,bias=False),
            nn.ReLU(),
            nn.Conv2d(in_planes//ratio, in_planes,1,bias=False)
        )
        self.sigmoid = nn.Sigmoid()

    def forward(self,x):
        avg_out = self.fc(self.avg_pool(x))
        max_out = self.fc(self.max_pool(x))
        out = avg_out + max_out
        return self.sigmoid(out)

class CBAM(nn.Module):
    def __init__(self, in_planes):
        super(CBAM, self).__init__()
        self.sptial_attn = SpatialAttention()
        self.channel_attn = ChannelAttention(in_planes=in_planes)

    def forward(self, x):
        x = self.channel_attn(x)*x
        y = self.sptial_attn(x)*x
        return y

class SAM(nn.Module):
    def __init__(self, in_channels, width, height):
        super(SAM, self).__init__()
        self.width  = width
        self.height = height

        self.wam = nn.Conv2d(width, in_channels,  1)
        self.ham = nn.Conv2d(height, in_channels, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        xh = x.transpose(-2, -3)
        xw = x.transpose(-1, -3)

        yh = self.ham(xh)
        yw = self.wam(xw)

        y = yw @ yh
        y = self.sigmoid(y)
        return y

class DAM(nn.Module):
    def __init__(self, in_planes, width, height):
        super(DAM, self).__init__()
        self.width = width
        self.height = height
        self.cam = ChannelAttention(in_planes=in_planes)
        self.sam = SAM(in_channels=in_planes, width=width, height=height)

    def forward(self, x):
        x = self.cam(x) * x
        y = self.sam(x) * x
        return y

def window_partition(x, window_size):
    x = x.permute(0, 2, 3, 1)
    B,H,W,C = x.shape
    x = x.view(B, H // window_size, window_size, W // window_size, window_size, C)
    windows = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, C)
    windows = windows.permute(0, 3, 1, 2)
    return windows

def window_reverse(windows, window_size, H, W):
    windows = windows.permute(0, 2, 3, 1)
    B = int(windows.shape[0] / (H * W / window_size / window_size))
    x = windows.view(B, H // window_size, W // window_size, window_size, window_size, -1)
    x = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, H, W, -1)
    x = x.permute(0, 3, 1, 2)
    return x


class BDAM(nn.Module):
    def __init__(self, in_planes, width, height):
        super(BDAM, self).__init__()
        self.width = width
        self.height= height
        self.window_size = min(self.width // 4, self.height //4)
        self.dam = DAM(in_planes, self.window_size, self.window_size)

    def forward(self, x):
        x = window_partition(x, self.window_size)
        x = self.dam(x)
        y  = window_reverse(x,self.window_size, H=self.height, W=self.width)
        return y

if __name__ == "__main__":
    x = torch.randn([4,256,64,64])
    m = BDAM(in_planes=256, width=64,height=64)
    y = m(x)
    print("ending")
