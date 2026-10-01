/* realtime_apply_pybind.cpp —— §5 契约 C 节的 pybind11 绑定
 *
 * 模块名 = `_realtime_apply`（私有扩展）。**不叫 `realtime_apply`**：§10.3 的交付模块
 * `realtime_apply.py` 是 Python 侧门面，二者同名会互相遮蔽（CPython 的 FileFinder
 * 先命中扩展后缀，`.py` 会被静默屏蔽）⇒ 扩展取 `_` 前缀，公开 API 由 `realtime_apply.py`
 * 暴露（签名仍逐字对齐 §5 契约 C 节）。
 *
 * 缓冲参数一律走 Python 缓冲协议（`bytes` / `bytearray` / `memoryview` / 任何可写缓冲），
 * **不依赖 numpy**（板上镜像自带 python3 无 numpy）。
 *
 * 版本与 ABI：见 `docs/build.md` §4（pybind11 锁定 2.11.x；模块后缀取自解释器）。
 */

#include <pybind11/pybind11.h>

#include <cstdint>
#include <cstring>
#include <string>

#include "apply_core.h"

namespace py = pybind11;

namespace {

cf_precision_t parse_precision(const std::string& s) {
    if (s == "fp16") return CF_PREC_FP16;
    if (s == "u8") return CF_PREC_U8;
    throw py::value_error("precision 只允许 'fp16' / 'u8'（§10.4 pipeline.curve_field_precision）");
}

cf_csc_t parse_csc(const std::string& s) {
    if (s == "unset") return CF_CSC_UNSET;
    if (s == "bt601") return CF_CSC_BT601;
    if (s == "bt709") return CF_CSC_BT709;
    throw py::value_error("csc 只允许 'unset' / 'bt601' / 'bt709'（§10.4 isp.csc_matrix）");
}

cf_range_t parse_range(const std::string& s) {
    if (s == "unset") return CF_RANGE_UNSET;
    if (s == "limited") return CF_RANGE_LIMITED;
    if (s == "full") return CF_RANGE_FULL;
    throw py::value_error("range 只允许 'unset' / 'limited' / 'full'（§10.4 isp.rgb_range）");
}

size_t curve_bytes(uint32_t cw, uint32_t ch, cf_precision_t prec) {
    const size_t px = (size_t)3 * cw * ch;
    return px * (prec == CF_PREC_FP16 ? 2 : 1);
}

void require_size(const py::buffer_info& info, size_t need, const char* what) {
    const size_t have = (size_t)info.size * (size_t)info.itemsize;
    if (have < need) {
        throw py::value_error(std::string(what) + " 缓冲过小：需要 " + std::to_string(need) +
                              " 字节，实得 " + std::to_string(have));
    }
}

int py_configure(uint32_t curve_w, uint32_t curve_h, uint32_t iters,
                 const std::string& precision, const std::string& csc,
                 const std::string& range, bool preserve_ratio) {
    cf_config_t cfg;
    std::memset(&cfg, 0, sizeof(cfg));
    cfg.curve_w = curve_w;
    cfg.curve_h = curve_h;
    cfg.iters = iters;
    cfg.precision = parse_precision(precision);
    cfg.csc = parse_csc(csc);
    cfg.range = parse_range(range);
    /* 施加语义开关（**非 §10.4 config 键**）：true = 保比语义（**默认**）；false = 旧逐通道语义（仅 A/B）。 */
    cfg.preserve_ratio = preserve_ratio ? 1 : 0;
    return cf_configure(&cfg);
}

/* §5 契约 C 节：apply_window(const uint8* rgb, const void* curve, uint8* dst, uint32 w, uint32 h) */
int py_apply_window(py::buffer rgb, py::buffer curve, py::buffer dst, uint32_t w, uint32_t h) {
    if (!cf_is_configured()) throw py::value_error("realtime_apply 未 configure（见 §5 GAP-1 注）");
    py::buffer_info ri = rgb.request(), ci = curve.request(), di = dst.request();
    if (di.readonly) throw py::value_error("dst 必须是可写缓冲");
    require_size(ri, (size_t)w * h * 3, "rgb");
    require_size(di, (size_t)w * h * 3, "dst");
    const uint8_t* src = static_cast<const uint8_t*>(ri.ptr);
    const void* cv = ci.ptr;
    uint8_t* out = static_cast<uint8_t*>(di.ptr);
    int rc;
    {
        py::gil_scoped_release release; /* 热路径不触 Python 对象 */
        rc = apply_window(src, cv, out, w, h);
    }
    return rc;
}

/* §5 契约 C 节：apply_full(const uint8* nv12, const void* curve, uint8* dst_rgb, uint32 w, uint32 h) */
int py_apply_full(py::buffer nv12, py::buffer curve, py::buffer dst_rgb, uint32_t w, uint32_t h) {
    if (!cf_is_configured()) throw py::value_error("realtime_apply 未 configure（见 §5 GAP-1 注）");
    py::buffer_info ni = nv12.request(), ci = curve.request(), di = dst_rgb.request();
    if (di.readonly) throw py::value_error("dst_rgb 必须是可写缓冲");
    require_size(ni, (size_t)w * h * 3 / 2, "nv12");
    require_size(di, (size_t)w * h * 3, "dst_rgb");
    const uint8_t* src = static_cast<const uint8_t*>(ni.ptr);
    const void* cv = ci.ptr;
    uint8_t* out = static_cast<uint8_t*>(di.ptr);
    int rc;
    {
        py::gil_scoped_release release;
        rc = apply_full(src, cv, out, w, h);
    }
    return rc;
}

/* 自检用：与 NEON 同形的标量参考（非契约入口） */
int py_apply_scalar(py::buffer rgb, py::buffer curve, py::buffer dst, uint32_t w, uint32_t h) {
    if (!cf_is_configured()) throw py::value_error("realtime_apply 未 configure");
    py::buffer_info ri = rgb.request(), ci = curve.request(), di = dst.request();
    if (di.readonly) throw py::value_error("dst 必须是可写缓冲");
    require_size(ri, (size_t)w * h * 3, "rgb");
    require_size(di, (size_t)w * h * 3, "dst");
    const uint8_t* src = static_cast<const uint8_t*>(ri.ptr);
    const void* cv = ci.ptr;
    uint8_t* out = static_cast<uint8_t*>(di.ptr);
    int rc;
    {
        py::gil_scoped_release release;
        rc = cf_apply_scalar(src, cv, out, w, h);
    }
    return rc;
}

size_t py_curve_bytes(uint32_t curve_w, uint32_t curve_h, const std::string& precision) {
    return curve_bytes(curve_w, curve_h, parse_precision(precision));
}

}  // namespace

PYBIND11_MODULE(_realtime_apply, m) {
    m.doc() = "曲线应用 C/NEON 内核（主基准 §5 契约 C 节的 realtime_apply 实现；"
              "公开门面见同目录 realtime_apply.py）";

    m.def("configure", &py_configure, py::arg("curve_w"), py::arg("curve_h"), py::arg("iters"),
          py::arg("precision"), py::arg("csc"), py::arg("range"),
          py::arg("preserve_ratio") = true,
          "一次性注入曲线场几何与色彩参数（启动时调用，之后只读）；"
          "preserve_ratio=true 启用保比语义（**默认**）；false = 旧逐通道语义（仅 A/B）");
    m.def("is_configured", &cf_is_configured, "是否已完成 configure");
    m.def("shutdown", &cf_shutdown, "释放配置（进程退出用）");
    m.def("impl_id", &cf_impl_id, "内核标识串");
    m.def("curve_bytes", &py_curve_bytes, py::arg("curve_w"), py::arg("curve_h"),
          py::arg("precision"), "曲线场字节数（3 通道平面）");

    m.def("apply_window", &py_apply_window, py::arg("rgb"), py::arg("curve"), py::arg("dst"),
          py::arg("w"), py::arg("h"), "窗分辨率曲线应用（§5 契约 C 节）");
    m.def("apply_full", &py_apply_full, py::arg("nv12"), py::arg("curve"), py::arg("dst_rgb"),
          py::arg("w"), py::arg("h"), "满幅曲线应用（§8.3 集合 B，仅触发存盘）");
    m.def("apply_scalar", &py_apply_scalar, py::arg("rgb"), py::arg("curve"), py::arg("dst"),
          py::arg("w"), py::arg("h"), "标量参考（自检用，非契约入口）");
}
