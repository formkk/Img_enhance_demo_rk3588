"""Zero-DCE++ 网络定义（仅转换期使用）。

来源：官方 Zero-DCE_extension-main/Zero-DCE++/model.py（作者项目页 li-chongyi.github.io），
      本文件数学结构与官方逐行一致，仅去除官方文件中未使用的 import。

用途：
  - `enhance_net_nopool`：完整模型（curve estimator + 8 次迭代），用于权重加载与 PyTorch 基线
  - `CurveEstimator`：只含曲线估计部分，是导出 ONNX / 转 RKNN 的唯一入口；
    8 次迭代曲线应用在板端后处理完成（主基准 §4.1「推理方式」、§3.3「单遍融合」）
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class CSDN_Tem(nn.Module):
    """深度可分离卷积块：3x3 depthwise + 1x1 pointwise"""

    def __init__(self, in_ch, out_ch):
        super(CSDN_Tem, self).__init__()
        self.depth_conv = nn.Conv2d(
            in_channels=in_ch,
            out_channels=in_ch,
            kernel_size=3,
            stride=1,
            padding=1,
            groups=in_ch,
        )
        self.point_conv = nn.Conv2d(
            in_channels=in_ch,
            out_channels=out_ch,
            kernel_size=1,
            stride=1,
            padding=0,
            groups=1,
        )

    def forward(self, input):
        out = self.depth_conv(input)
        out = self.point_conv(out)
        return out


class enhance_net_nopool(nn.Module):
    """官方 Zero-DCE++（scale_factor=1 时为全分辨率曲线估计）"""

    def __init__(self, scale_factor):
        super(enhance_net_nopool, self).__init__()
        self.relu = nn.ReLU(inplace=True)
        self.scale_factor = scale_factor
        self.upsample = nn.UpsamplingBilinear2d(scale_factor=self.scale_factor)
        number_f = 32

        self.e_conv1 = CSDN_Tem(3, number_f)
        self.e_conv2 = CSDN_Tem(number_f, number_f)
        self.e_conv3 = CSDN_Tem(number_f, number_f)
        self.e_conv4 = CSDN_Tem(number_f, number_f)
        self.e_conv5 = CSDN_Tem(number_f * 2, number_f)
        self.e_conv6 = CSDN_Tem(number_f * 2, number_f)
        self.e_conv7 = CSDN_Tem(number_f * 2, 3)

    def enhance(self, x, x_r):
        """8 次迭代增强（4+4 分组）。板端等价实现见主基准 §3.3：必须单遍融合。"""
        x = x + x_r * (torch.pow(x, 2) - x)
        x = x + x_r * (torch.pow(x, 2) - x)
        x = x + x_r * (torch.pow(x, 2) - x)
        enhance_image_1 = x + x_r * (torch.pow(x, 2) - x)
        x = enhance_image_1 + x_r * (torch.pow(enhance_image_1, 2) - enhance_image_1)
        x = x + x_r * (torch.pow(x, 2) - x)
        x = x + x_r * (torch.pow(x, 2) - x)
        enhance_image = x + x_r * (torch.pow(x, 2) - x)
        return enhance_image

    def forward(self, x):
        if self.scale_factor == 1:
            x_down = x
        else:
            x_down = F.interpolate(x, scale_factor=1 / self.scale_factor, mode='bilinear')

        x1 = self.relu(self.e_conv1(x_down))
        x2 = self.relu(self.e_conv2(x1))
        x3 = self.relu(self.e_conv3(x2))
        x4 = self.relu(self.e_conv4(x3))
        x5 = self.relu(self.e_conv5(torch.cat([x3, x4], 1)))
        x6 = self.relu(self.e_conv6(torch.cat([x2, x5], 1)))
        x_r = F.tanh(self.e_conv7(torch.cat([x1, x6], 1)))
        if self.scale_factor == 1:
            x_r = x_r
        else:
            x_r = self.upsample(x_r)
        enhance_image = self.enhance(x, x_r)
        return enhance_image, x_r


class CurveEstimator(nn.Module):
    """只导出曲线估计部分（输出 3ch x_r，不做 8 次迭代）。"""

    def __init__(self, base_model):
        super().__init__()
        self.relu = base_model.relu
        self.e_conv1 = base_model.e_conv1
        self.e_conv2 = base_model.e_conv2
        self.e_conv3 = base_model.e_conv3
        self.e_conv4 = base_model.e_conv4
        self.e_conv5 = base_model.e_conv5
        self.e_conv6 = base_model.e_conv6
        self.e_conv7 = base_model.e_conv7

    def forward(self, x):
        x1 = self.relu(self.e_conv1(x))
        x2 = self.relu(self.e_conv2(x1))
        x3 = self.relu(self.e_conv3(x2))
        x4 = self.relu(self.e_conv4(x3))
        x5 = self.relu(self.e_conv5(torch.cat([x3, x4], 1)))
        x6 = self.relu(self.e_conv6(torch.cat([x2, x5], 1)))
        x_r = torch.tanh(self.e_conv7(torch.cat([x1, x6], 1)))
        return x_r