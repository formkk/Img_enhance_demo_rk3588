#!/usr/bin/env python3
"""O35 逐算子归因：把 HQ（Retinexformer）中的**单类算子**做成微型模型，用**真实中间张量**喂入，
在板上以与正式模型完全相同的 FP16 配置编译/推理，逐算子量出"板上 RKNN vs PyTorch FP32"的偏差，
并与"纯 FP16 舍入对照"相比得到**超出倍率**。

契约来源：
  - §11.2 M0-①b 板上输出 vs PyTorch 的按精度档阈值（FP16 张量 ≤1e-3 + 图像域 PSNR ≥70 dB）
  - `docs/evidence/M0-4-hq-r9-gate.md` §8.3 的三条待查方向（算子近似实现 / 中间累加精度 / 编译期融合）
  - 规则 `10-evidence.md` §1：结论三要素（报告路径 + 测量条件随行 + 时钟口径）

与 `tools/convert_models.py` 的差异（**刻意**）：`convert_models.py` 的 mean/std 固定 3 值，只适用于
3 通道输入；本工具按 RKNN 读取的通道数（`dims[1]`）生成**恒等归一化**（mean=0 / std=1），
从而可编译 40 通道中间特征。恒等归一化数值上是 no-op，不改变被测算子的输入值域。

三个子命令：
  dump     跑 PT FP32 正式模型，抓取**真实中间张量**（每个探针的输入 + PT 参考输出）
  export   用抓到的真实输入 + 原算子（真模块或同码包装）导出 ONNX，并编译为 RK3588 FP16
  compare  板上输出 .f32 vs PT 参考 → max_abs / mean_abs / rel_max，并与 FP16 舍入对照比倍率

用法：
  python3 tools/opattr.py dump    --out-dir docs/evidence/reports/M0-4/opattr2
  python3 tools/opattr.py export  --dir docs/evidence/reports/M0-4/opattr2 --build
  python3 tools/opattr.py compare --dir docs/evidence/reports/M0-4/opattr2 \
      --board-dir <板端回传目录> --out docs/evidence/reports/M0-4/opattr2/compare.json
"""

import argparse
import hashlib
import importlib.util
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HQ_ARCH_DEFAULT = os.path.join(ROOT, 'refs', 'pre-research', 'basicsr',
                               'models', 'archs', 'RetinexFormer_arch.py')
HQ_WEIGHTS_DEFAULT = os.path.join(ROOT, 'refs', 'pre-research', 'pretrain_model', 'LOL_v1.pth')

# 真模块探针：名字 → (取模块的 lambda 路径说明, 说明)
MODULE_PROBES = {
    'norm40':  ('PreNorm 的 nn.LayerNorm(40)（encoder_layers.0.0/blocks.0.1/norm），归约长度 40 → exNorm'),
    'conv3x3': ('denoiser.embedding = Conv2d(3,40,3,1,1)，归约长度 27 → Conv'),
    'convg':   ('IG_MSA.pos_emb = dwconv3x3 + GELU + dwconv3x3 → ConvExGelu'),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_arch(arch_path):
    spec = importlib.util.spec_from_file_location('RetinexFormer_arch', arch_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_model(arch_path, weights, n_feat, stage, num_blocks):
    import torch
    mod = load_arch(arch_path)
    model = mod.RetinexFormer(in_channels=3, out_channels=3, n_feat=n_feat,
                              stage=stage, num_blocks=list(num_blocks))
    sd = torch.load(weights, map_location='cpu')
    if isinstance(sd, dict):
        for key in ('params', 'state_dict'):
            if isinstance(sd.get(key), dict):
                sd = sd[key]
                break
    sd = {k.replace('module.', ''): v for k, v in sd.items()}
    missing, unexpected = model.load_state_dict(sd, strict=False)
    assert not missing and not unexpected, f'missing={len(missing)} unexpected={len(unexpected)}'
    model.eval()
    return model


def module_probes(model):
    den = model.body[0].denoiser
    return {
        'norm40': den.encoder_layers[0][0].blocks[0][1].norm,
        'conv3x3': den.embedding,
        'convg': den.encoder_layers[0][0].blocks[0][0].pos_emb,
    }


def l2norm_wrapper():
    """F.normalize(x, dim=-1, p=2)：与 arch 中 IG_MSA 对 q/k 的调用逐字一致。"""
    import torch.nn as nn

    class _W(nn.Module):
        def forward(self, x):
            import torch.nn.functional as F
            return F.normalize(x, dim=-1, p=2)

    return _W()


def softmax_wrapper():
    import torch.nn as nn

    class _W(nn.Module):
        def forward(self, x):
            return x.softmax(dim=-1)

    return _W()


def matmul_const_wrapper(v_const):
    """y = x @ V（V 为真实 attn@v 的右操作数，常量入图）：只测 exMatMul 的归约（长度 40）。"""
    import torch
    import torch.nn as nn

    class _W(nn.Module):
        def __init__(self):
            super().__init__()
            self.register_buffer('v', torch.from_numpy(v_const))

        def forward(self, x):
            return x @ self.v

    return _W()


def matmul_split_wrapper(d):
    """y = z[..., :d] @ z[..., d:]（**两个操作数都是运行时张量**）⇒ 让 RKNN 生成真正的 `exMatMul`。

    单输入 + 图内 Slice 是为了绕开 C 探针的"双输入仅支持 rawu8"限制；Slice 不改变被测算子的数值路径。
    """
    import torch.nn as nn

    class _W(nn.Module):
        def forward(self, z):
            return z[..., :d] @ z[..., d:]

    return _W()


def matmul_mm_wrapper():
    """y = x @ y2（**两个输入都是运行时张量**）：直接生成 `exMatMul`，与 `matmul_const_wrapper` 只差右操作数是输入还是常量。"""
    import torch.nn as nn

    class _W(nn.Module):
        def forward(self, a, b):
            return a @ b

    return _W()


# ---------------------------------------------------------------- dump

def cmd_dump(args):
    import cv2
    import torch
    import torch.nn.functional as F

    model = build_model(args.arch_path, args.weights, args.n_feat, args.stage, args.num_blocks)
    mods = module_probes(model)

    captured = {}
    for name, m in mods.items():
        def mk(nm):
            def hook(mod, inp, out):
                captured[nm] = (inp[0].detach().clone(), out.detach().clone())
            return hook
        m.register_forward_hook(mk(name))

    # 3) 注意力核心探针：hook 第一个 IG_MSA 的输入与 to_v 输出，用于重建 softmax 输入与 attn@v 的右操作数
    igmsa = model.body[0].denoiser.encoder_layers[0][0].blocks[0][0]
    ig_in = {}

    def ig_hook(m, args, kwargs, out):
        # IGAB 以关键字传入 illu_fea_trans（见 arch IGAB.forward）
        ig_in['x_in'] = args[0].detach().clone()
        ig_in['illu'] = kwargs['illu_fea_trans'].detach().clone()

    igmsa.register_forward_hook(ig_hook, with_kwargs=True)
    v_inp = {}
    igmsa.to_v.register_forward_hook(lambda m, i, o: v_inp.update(v=o.detach().clone()))

    norm_calls, norm_outs = [], []
    orig_norm = F.normalize

    def patched(x, p=2, dim=1, eps=1e-12, out=None):
        norm_calls.append(x.detach().clone())
        y = orig_norm(x, p=p, dim=dim, eps=eps, out=out)
        norm_outs.append(y.detach().clone())
        return y

    F.normalize = patched
    try:
        img = cv2.cvtColor(cv2.imread(args.input_png), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        assert img.shape[:2] == (args.h, args.w), f'输入尺寸 {img.shape[:2]} != ({args.h},{args.w})'
        x = torch.from_numpy(img).permute(2, 0, 1)[None]
        with torch.no_grad():
            out = model(x)
    finally:
        F.normalize = orig_norm

    os.makedirs(args.out_dir, exist_ok=True)
    manifest = {
        'mode': 'opattr-dump',
        'arch_path': os.path.abspath(args.arch_path),
        'arch_sha256': sha256(args.arch_path),
        'weights': os.path.abspath(args.weights),
        'weights_sha256': sha256(args.weights),
        'input_png': os.path.abspath(args.input_png),
        'input_hw': [args.h, args.w],
        'pt_model_out_absmax': float(out.abs().max()),
        'n_normalize_calls': len(norm_calls),
        'normalize_call_shapes': [list(t.shape) for t in norm_calls],
        'probes': {},
    }

    # 注意力核心：按 IG_MSA.forward 的算式重建（float64），softmax 输入与 attn@v 右操作数
    heads = igmsa.num_heads
    rescale = igmsa.rescale.detach().numpy().astype(np.float64)
    x_in = ig_in['x_in'].numpy().astype(np.float64)          # [b,h,w,c]
    illu = ig_in['illu'].numpy().astype(np.float64)          # [b,c,h,w]
    v_raw = v_inp['v'].numpy().astype(np.float64)            # [b, n, heads*d]
    b_, h_, w_, c_ = x_in.shape
    n_ = h_ * w_

    def to_heads(t):                                          # [b,n,heads*d] -> [b,heads,n,d]
        return t.reshape(b_, n_, heads, -1).transpose(0, 2, 1, 3)

    def flat_illu(t):                                         # [b,c,h,w] -> [b,heads,n,d]
        return t.reshape(b_, c_, n_).transpose(0, 2, 1).reshape(b_, n_, heads, -1).transpose(0, 2, 1, 3)

    qn = norm_outs[0].numpy().astype(np.float64)              # [b,heads,d,n]（已 L2 归一，对应 arch 中 q.transpose 之后）
    kn = norm_outs[1].numpy().astype(np.float64)
    # arch：v = v * illu_attn 后 v = v.transpose(-2,-1) ⇒ [b,heads,d,n]
    v_attn = (to_heads(v_raw) * flat_illu(illu)).transpose(0, 1, 3, 2)
    attn_logits = (kn @ qn.transpose(0, 1, 3, 2)) * rescale.reshape(1, heads, 1, 1)
    with torch.no_grad():
        attn_sm = torch.from_numpy(attn_logits).softmax(dim=-1).numpy()
        y_av = torch.from_numpy(attn_sm @ v_attn).numpy()

    def dump_probe(name, xa, ya, note):
        xa = np.ascontiguousarray(xa.astype(np.float32))
        ya = np.ascontiguousarray(ya.astype(np.float32))
        xp = os.path.join(args.out_dir, f'{name}_in.npy')
        yp = os.path.join(args.out_dir, f'{name}_ref.npy')
        np.save(xp, xa)
        np.save(yp, ya)
        ctl = ya.astype(np.float16).astype(np.float32)
        manifest['probes'][name] = {
            'note': note,
            'in_npy': os.path.abspath(xp),
            'ref_npy': os.path.abspath(yp),
            'in_shape': list(xa.shape),
            'out_shape': list(ya.shape),
            'in_range': [float(xa.min()), float(xa.max())],
            'out_range': [float(ya.min()), float(ya.max())],
            'pt_fp16_control_max_abs': float(np.abs(ya - ctl).max()),
            'pt_fp16_control_mean_abs': float(np.abs(ya - ctl).mean()),
        }
        print(f'{name:20s} in {xa.shape} out {ya.shape}  '
              f'ctl max {np.abs(ya - ctl).max():.3e} mean {np.abs(ya - ctl).mean():.3e}')

    def dump_probe2(name, xa, xb, ya, note):
        """双输入探针：input0 = xa、input1 = xb（板端用 `rknn_c_probe` 的第 12 参喂第二个文件）。"""
        xa = np.ascontiguousarray(xa.astype(np.float32))
        xb = np.ascontiguousarray(xb.astype(np.float32))
        ya = np.ascontiguousarray(ya.astype(np.float32))
        xp = os.path.join(args.out_dir, f'{name}_in.npy')
        xp2 = os.path.join(args.out_dir, f'{name}_in2.npy')
        yp = os.path.join(args.out_dir, f'{name}_ref.npy')
        np.save(xp, xa)
        np.save(xp2, xb)
        np.save(yp, ya)
        ctl = ya.astype(np.float16).astype(np.float32)
        manifest['probes'][name] = {
            'note': note,
            'in_npy': os.path.abspath(xp),
            'in2_npy': os.path.abspath(xp2),
            'ref_npy': os.path.abspath(yp),
            'in_shape': list(xa.shape),
            'in2_shape': list(xb.shape),
            'out_shape': list(ya.shape),
            'in_range': [float(xa.min()), float(xa.max())],
            'in2_range': [float(xb.min()), float(xb.max())],
            'out_range': [float(ya.min()), float(ya.max())],
            'pt_fp16_control_max_abs': float(np.abs(ya - ctl).max()),
            'pt_fp16_control_mean_abs': float(np.abs(ya - ctl).mean()),
        }
        print(f'{name:20s} in {xa.shape} + in2 {xb.shape} out {ya.shape}  '
              f'ctl max {np.abs(ya - ctl).max():.3e} mean {np.abs(ya - ctl).mean():.3e}')

    for name in MODULE_PROBES:
        x_t, y_t = captured[name]
        dump_probe(name, x_t.numpy(), y_t.numpy(), MODULE_PROBES[name][1])

    # l2norm：同一张真实 q 张量上，按归约长度 N 做**受控截断**，N 取该模型内自然出现的三个长度 + 上限
    assert norm_calls, '未抓到 F.normalize 调用'
    q = norm_calls[0]
    n_full = int(q.shape[-1])
    print('l2norm captured q:', tuple(q.shape))
    manifest['l2norm_source_shape'] = list(q.shape)
    for n in args.n_sweep:
        if n > n_full:
            print(f'l2norm_n{n}: 超出真实长度 {n_full}，跳过')
            continue
        sub = q[..., :n].contiguous()
        with torch.no_grad():
            y = l2norm_wrapper()(sub)
        dump_probe(f'l2norm_n{n}', sub.numpy(), y.numpy(),
                   f'F.normalize(dim=-1,p=2) 归约长度 N={n}（真实 q 张量前 {n} 列）')

    dump_probe('attn_softmax', attn_logits, attn_sm,
               f'注意力 softmax(dim=-1)：输入 = 真实 (k_n @ q_n^T) * rescale，形状 [1,{heads},40,40]')
    dump_probe('attn_av', attn_sm, y_av,
               f'注意力 attn @ v：左操作数 = 真实 softmax 输出（作为模型输入），右操作数 = 真实 v*illu（常量入图），归约长度 40')
    # attn_av2：**两个操作数都是运行时张量**的双输入 MatMul（input0 = attn_sm，input1 = v_attn）。
    # 上一版"单输入 + 图内 Slice→Split"在大 N 下被 RKNN 编译成 CPU Transpose 旁路 ⇒ 作废（见 §6-3 注）。
    # 双输入直接生成真正的 `exMatMul` 层，且与 attn_av 只差"右操作数是输入还是常量"这一唯一变量。
    d_ = int(attn_sm.shape[-2])
    n_ = int(v_attn.shape[-1])
    dump_probe2('attn_av2', attn_sm, v_attn, y_av,
                f'注意力 attn @ v（**双运行时操作数**：input0 = 真实 softmax 输出 [1,{heads},{d_},{d_}]，'
                f'input1 = 真实 v*illu [1,{heads},{d_},{n_}]，MatMul 归约长度 {d_}）⇒ 预期映射为 exMatMul')
    vp = os.path.join(args.out_dir, 'attn_av_vconst.npy')
    np.save(vp, np.ascontiguousarray(v_attn.astype(np.float32)))
    manifest['attn_av_vconst_npy'] = os.path.abspath(vp)
    manifest['attn_av2_v_shape'] = list(v_attn.shape)

    write_json(manifest, args.out or os.path.join(args.out_dir, 'dump.json'))
    return manifest


# ---------------------------------------------------------------- export + build

def cmd_export(args):
    import onnx
    import onnxruntime as ort
    import torch
    from onnxsim import simplify

    manifest = json.load(open(os.path.join(args.dir, 'dump.json')))
    model = build_model(manifest['arch_path'], manifest['weights'], args.n_feat, args.stage, args.num_blocks)
    mods = module_probes(model)

    report = {'mode': 'opattr-export', 'dir': os.path.abspath(args.dir), 'probes': {}}
    v_const = np.load(manifest['attn_av_vconst_npy']).astype(np.float32)
    for name, spec in manifest['probes'].items():
        x = np.load(spec['in_npy']).astype(np.float32)
        is_two_input = 'in2_npy' in spec
        if is_two_input:
            x2 = np.load(spec['in2_npy']).astype(np.float32)
            module = matmul_mm_wrapper()
            feed = (torch.from_numpy(x), torch.from_numpy(x2))
            input_names = ['input0', 'input1']
        elif name.startswith('l2norm'):
            module = l2norm_wrapper(); feed = (torch.from_numpy(x),); input_names = ['input']
        elif name == 'attn_softmax':
            module = softmax_wrapper(); feed = (torch.from_numpy(x),); input_names = ['input']
        elif name == 'attn_av':
            module = matmul_const_wrapper(v_const); feed = (torch.from_numpy(x),); input_names = ['input']
        else:
            module = mods[name]; feed = (torch.from_numpy(x),); input_names = ['input']
        onnx_path = os.path.join(args.dir, f'{name}.onnx')
        torch.onnx.export(module, feed, onnx_path, opset_version=12,
                          input_names=input_names, output_names=['output'],
                          do_constant_folding=True)
        onnx.save(simplify(onnx.load(onnx_path))[0], onnx_path)

        ref = np.load(spec['ref_npy']).astype(np.float32)
        sess = ort.InferenceSession(onnx_path, providers=['CPUExecutionProvider'])
        onnx_in = {'input0': x, 'input1': x2} if is_two_input else {'input': x}
        align = float(np.abs(sess.run(None, onnx_in)[0] - ref).max())
        entry = {'onnx': os.path.abspath(onnx_path), 'onnx_sha256': sha256(onnx_path),
                 'onnx_vs_pt_max_abs': align, 'in_shape': list(x.shape)}
        print(f'{name:20s} ONNX↔PT max_abs {align:.3e}')
        if args.build:
            rknn_path = os.path.join(args.dir, f'{name}.fp16.rknn')
            entry.update(build_rknn(onnx_path, rknn_path, args.target))
            entry['rknn_sha256'] = sha256(rknn_path)
        report['probes'][name] = entry
    write_json(report, args.out or os.path.join(args.dir, 'export.json'))
    return report


def build_rknn(onnx_path, rknn_path, target):
    """与 convert_models.py 同一配置（target / FP16 / 不量化），但 mean/std 按 RKNN 读取的 dims[1] 生成恒等归一化。"""
    import onnx
    from rknn.api import RKNN

    m = onnx.load(onnx_path)
    cs = [int(i.type.tensor_type.shape.dim[1].dim_value) for i in m.graph.input]
    r = RKNN(verbose=True)
    r.config(mean_values=[[0.0] * c for c in cs], std_values=[[1.0] * c for c in cs],
             target_platform=target, quant_img_RGB2BGR=False)
    assert r.load_onnx(model=onnx_path) == 0, 'load_onnx failed'
    assert r.build(do_quantization=False, rknn_batch_size=1) == 0, 'build failed'
    assert r.export_rknn(rknn_path) == 0, 'export_rknn failed'
    r.release()
    return {'rknn': os.path.abspath(rknn_path),
            'mean_std_policy': f'identity(mean=0,std=1) x {cs}（RKNN 读取各输入的 dims[1]）'}


# ---------------------------------------------------------------- compare

def cmd_compare(args):
    manifest = json.load(open(os.path.join(args.dir, 'dump.json')))
    report = {'mode': 'opattr-compare', 'dir': os.path.abspath(args.dir),
              'board_dir': os.path.abspath(args.board_dir), 'probes': {}}
    for name, spec in manifest['probes'].items():
        bp = os.path.join(args.board_dir, f'{name}_out.f32')
        if not os.path.exists(bp):
            print(f'{name:20s} 缺板上输出 {bp}，跳过')
            continue
        ref = np.load(spec['ref_npy']).astype(np.float32)
        cand = np.fromfile(bp, dtype=np.float32).reshape(ref.shape)
        d = np.abs(ref - cand)
        ctl_max = spec['pt_fp16_control_max_abs']
        ctl_mean = spec['pt_fp16_control_mean_abs']
        ref_absmax = float(np.abs(ref).max())
        report['probes'][name] = {
            'in_shape': spec['in_shape'],
            'out_shape': spec['out_shape'],
            'ref_absmax': ref_absmax,
            'board_max_abs': float(d.max()),
            'board_mean_abs': float(d.mean()),
            'board_rel_max': float(d.max() / ref_absmax) if ref_absmax else None,
            'pt_fp16_control_max_abs': ctl_max,
            'pt_fp16_control_mean_abs': ctl_mean,
            'excess_max_x': float(d.max() / ctl_max) if ctl_max else None,
            'excess_mean_x': float(d.mean() / ctl_mean) if ctl_mean else None,
            'pass_1e-3': bool(d.max() <= 1e-3),
        }
        e = report['probes'][name]
        print(f"{name:20s} board max {e['board_max_abs']:.3e} mean {e['board_mean_abs']:.3e} | "
              f"ctl max {ctl_max:.3e} mean {ctl_mean:.3e} | excess max {e['excess_max_x']:.1f}x "
              f"mean {e['excess_mean_x']:.1f}x | {'PASS' if e['pass_1e-3'] else 'FAIL'} 1e-3")
    write_json(report, args.out or os.path.join(args.dir, 'compare.json'))
    return report


def write_json(obj, path):
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    with open(path, 'w') as f:
        f.write(text + '\n')
    print(f'[report] {path}')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='mode', required=True)

    def add_common(sp):
        sp.add_argument('--arch-path', default=HQ_ARCH_DEFAULT)
        sp.add_argument('--weights', default=HQ_WEIGHTS_DEFAULT)
        sp.add_argument('--n-feat', type=int, default=40)
        sp.add_argument('--stage', type=int, default=1)
        sp.add_argument('--num-blocks', type=int, nargs=3, default=[1, 2, 2])

    pd = sub.add_parser('dump')
    pd.add_argument('--h', type=int, default=288)
    pd.add_argument('--w', type=int, default=512)
    pd.add_argument('--input-png', default=os.path.join(
        ROOT, 'docs/evidence/reports/M0-4/gate_input_512x288.png'))
    pd.add_argument('--n-sweep', type=int, nargs='+', default=[9216, 36864, 147456])
    pd.add_argument('--out-dir', required=True)
    pd.add_argument('--out', default='')
    add_common(pd)

    pe = sub.add_parser('export')
    pe.add_argument('--dir', required=True)
    pe.add_argument('--build', action='store_true', help='同时编译 RK3588 FP16（需 rknn-toolkit2 环境）')
    pe.add_argument('--target', default='rk3588')
    pe.add_argument('--out', default='')
    add_common(pe)

    pc = sub.add_parser('compare')
    pc.add_argument('--dir', required=True)
    pc.add_argument('--board-dir', required=True)
    pc.add_argument('--out', default='')

    args = p.parse_args()
    {'dump': cmd_dump, 'export': cmd_export, 'compare': cmd_compare}[args.mode](args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
