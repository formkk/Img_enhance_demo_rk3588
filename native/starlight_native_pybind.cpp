/* starlight_native_pybind.cpp —— capture / rga / rknn / drm 四个内核的 pybind11 绑定
 *
 * 模块名 `_starlight_native`，含子模块 `capture` / `rga` / `rknn` / `drm`。
 * Python 侧只做**编排与状态机**（§10.3 的 capture.py / curve_estim.py / display_mipi.py），
 * 计算与设备 I/O 全部在本扩展内完成。
 *
 * `Buffer` 类实现 Python 缓冲协议（零拷贝包装 C 侧 dma-buf 映射），可直接传给
 * `_realtime_apply.apply_window(rgb, curve, dst, w, h)`。
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#include "drm_core.h"
#include "osd_core.h"
#include "rga_core.h"
#include "rknn_core.h"
#include "v4l2_core.h"

namespace py = pybind11;

namespace {

/* 零拷贝包装一块外部内存（dma-buf 映射或 V4L2 mmap），实现缓冲协议。 */
class Buffer {
public:
    Buffer(void* ptr, size_t len, bool readonly)
        : ptr_(ptr), len_(len), readonly_(readonly) {}

    void* ptr() const { return ptr_; }
    size_t len() const { return len_; }
    bool readonly() const { return readonly_; }

    py::bytes to_bytes() const {
        return py::bytes(static_cast<const char*>(ptr_), len_);
    }
    void write(py::buffer src) {
        if (readonly_) throw py::value_error("只读缓冲不可写");
        py::buffer_info info = src.request();
        const size_t n = static_cast<size_t>(info.size) * static_cast<size_t>(info.itemsize);
        if (n > len_) throw py::value_error("源数据大于目标缓冲");
        std::memcpy(ptr_, info.ptr, n);
    }

private:
    void*  ptr_;
    size_t len_;
    bool   readonly_;
};

py::buffer_info buffer_info_of(Buffer& b) {
    return py::buffer_info(b.ptr(), 1, py::format_descriptor<uint8_t>::format(), 1,
                           {static_cast<py::ssize_t>(b.len())},
                           {static_cast<py::ssize_t>(1)}, b.readonly());
}

Buffer wrap_va(void* p, size_t len, bool ro) {
    if (!p) throw py::value_error("缓冲未就绪（未 configure 或分配失败）");
    return Buffer(p, len, ro);
}

/* ── capture ─────────────────────────────────────────────────────────── */
py::dict frame_to_dict(const frame_desc_t& f) {
    py::dict d;
    d["seq"] = f.seq;
    d["t_capture_ns"] = f.t_capture_ns;
    d["timestamp_source"] = (f.timestamp_source == TS_SRC_EOF) ? "EOF" : "SOE";
    d["buffer_index"] = f.buffer_index;
    d["generation"] = f.generation;
    d["dmabuf_fd"] = f.dmabuf_fd;
    d["w"] = f.w;
    d["h"] = f.h;
    d["format"] = "NV12";
    d["stride"] = f.stride;
    d["isp_preset_id"] = f.isp_preset_id;
    d["lease_id"] = f.lease_id;
    return d;
}

py::object capture_acquire(int n_consumers, int timeout_ms) {
    frame_desc_t f;
    std::memset(&f, 0, sizeof(f));
    const int rc = v4l2_acquire(n_consumers, &f, timeout_ms);
    if (rc != V4L2_OK) return py::none();   /* 超时/失败一律 None，调用方据 counters 判定 */
    return frame_to_dict(f);
}

py::dict capture_counters() {
    uint64_t frames = 0, dropped = 0, timeouts = 0;
    uint32_t outstanding = 0;
    v4l2_counters(&frames, &dropped, &timeouts, &outstanding);
    py::dict d;
    d["frames"] = frames;
    d["dropped_stale"] = dropped;
    d["lease_timeouts"] = timeouts;
    d["outstanding_leases"] = outstanding;
    d["stream_on_count"] = v4l2_stream_on_count();
    d["ts_source_changes"] = v4l2_ts_source_changes();
    d["ts_source"] = (v4l2_current_ts_source() == TS_SRC_EOF) ? "EOF" : "SOE";
    return d;
}

/* ── rga ─────────────────────────────────────────────────────────────── */
py::dict rga_info() {
    py::dict d;
    d["configured"] = (bool)rga_is_configured();
    d["last_blit_ns"] = rga_last_blit_ns();
    d["last_canvas_ns"] = rga_last_canvas_ns();
    return d;
}

/* ── drm ─────────────────────────────────────────────────────────────── */
py::dict drm_info() {
    py::dict d;
    d["configured"] = (bool)drm_is_configured();
    d["connector_id"] = drm_connector_id();
    d["crtc_id"] = drm_crtc_id();
    d["mode_name"] = std::string(drm_mode_name());
    d["hdisplay"] = drm_mode_hdisplay();
    d["vdisplay"] = drm_mode_vdisplay();
    d["vrefresh"] = drm_mode_vrefresh();
    d["canvas_stride"] = drm_canvas_stride();
    d["canvas_count"] = drm_canvas_count();
    d["flip_count"] = drm_flip_count();
    d["flip_skipped"] = drm_flip_skipped();
    d["last_submit_ns"] = drm_last_submit_ns();
    d["last_flip_ns"] = drm_last_flip_ns();
    d["last_flip_seq"] = drm_last_flip_seq();
    d["last_error"] = std::string(drm_last_error());
    d["last_errno"] = drm_last_errno();
    return d;
}

}  // namespace

PYBIND11_MODULE(_starlight_native, m) {
    m.doc() = "交付链路原生内核：capture(TH1) / rga(TH2a 几何 + TH4 画布) / "
              "rknn(TH2a NPU) / drm(TH4 显示)";

    py::class_<Buffer>(m, "Buffer", py::buffer_protocol())
        .def_buffer(&buffer_info_of)
        .def_property_readonly("nbytes", &Buffer::len)
        .def_property_readonly("readonly", &Buffer::readonly)
        .def("to_bytes", &Buffer::to_bytes, "复制为 bytes")
        .def("write", &Buffer::write, py::arg("src"), "从缓冲协议对象拷入");

    /* 通用 dma-buf 分配（system-dma32）。用于曲线场等**按精度定尺寸**的发布缓冲。 */
    m.def("alloc", [](size_t nbytes) {
        void* va = nullptr;
        const int fd = dma_heap_alloc_system(nbytes, &va);
        if (fd < 0 || !va) throw std::runtime_error("dma_heap 分配失败");
        /* 映射随进程存活（dma-buf fd 不 close，§5 硬契约）；Buffer 按值返回，仅持裸指针 */
        return Buffer(va, nbytes, false);
    }, py::arg("nbytes"), "分配 system-dma32 缓冲并返回 Buffer（映射随进程存活）");

    /* ── capture（TH1）────────────────────────────────────────────── */
    auto cap = m.def_submodule("capture", "TH1：V4L2 mainpath + DMABUF 池 + lease");
    cap.def("init", [](const std::string& dev, int nbuf, uint32_t w, uint32_t h) {
        return v4l2_init(dev.c_str(), nbuf, w, h);
    }, py::arg("dev"), py::arg("nbuf"), py::arg("w"), py::arg("h"));
    cap.def("stream_on_once", &v4l2_stream_on_once, "全生命周期仅一次（§5 硬契约）");
    cap.def("acquire", &capture_acquire, py::arg("n_consumers"), py::arg("timeout_ms") = 1000,
            "取一帧并签发 lease；失败返回 None");
    cap.def("release", &v4l2_release, py::arg("lease_id"));
    cap.def("stream_off", &v4l2_stream_off, "仅进程终止路径（软复位不得调用）");
    cap.def("deinit", &v4l2_deinit);
    cap.def("counters", &capture_counters);
    cap.def("buffer_count", &v4l2_buffer_count);
    cap.def("buffer", [](int i) { return wrap_va(v4l2_buffer_va(i), v4l2_buffer_len(i), true); },
            py::arg("index"), "V4L2 缓冲（只读，NV12）");
    cap.def("dmabuf_fd", &v4l2_dmabuf_fd, py::arg("index"));
    cap.def("width", &v4l2_width);
    cap.def("height", &v4l2_height);
    cap.def("stride", &v4l2_stride);
    cap.def("sizeimage", &v4l2_sizeimage);
    cap.def("stream_on_count", &v4l2_stream_on_count);

    /* ── rga（TH2a 几何 / TH4 画布）───────────────────────────────── */
    auto rga = m.def_submodule("rga", "RGA 几何链路与画布合成");
    rga.def("configure", [](py::dict d) {
        rga_cfg_t c;
        std::memset(&c, 0, sizeof(c));
        c.cap_w = d["cap_w"].cast<uint32_t>();
        c.cap_h = d["cap_h"].cast<uint32_t>();
        c.eff_w = d["eff_w"].cast<uint32_t>();
        c.eff_h = d["eff_h"].cast<uint32_t>();
        c.pad_w = d["pad_w"].cast<uint32_t>();
        c.pad_h = d["pad_h"].cast<uint32_t>();
        c.pad_top = d["pad_top"].cast<uint32_t>();
        c.win_w = d["win_w"].cast<uint32_t>();
        c.win_h = d["win_h"].cast<uint32_t>();
        c.canvas_w = d["canvas_w"].cast<uint32_t>();
        c.canvas_h = d["canvas_h"].cast<uint32_t>();
        c.core_mask = d["core_mask"].cast<uint32_t>();
        return rga_configure(&c);
    }, py::arg("cfg"));
    rga.def("is_configured", &rga_is_configured);
    rga.def("blit_nv12_fd", [](int fd, int dst) { return rga_blit_nv12_fd(fd, (rga_dst_t)dst); },
            py::arg("src_fd"), py::arg("dst"), "dst: 0=EST 1=WIN");
    rga.def("blit_nv12_va", [](Buffer& src, int dst) {
        return rga_blit_nv12_va(src.ptr(), (rga_dst_t)dst);
    }, py::arg("src"), py::arg("dst"), "staging 兜底路径");
    rga.def("canvas_compose_to", [](Buffer& canvas, uint32_t w, uint32_t h) {
        return rga_canvas_compose_to(canvas.ptr(), w, h);
    }, py::arg("canvas"), py::arg("w"), py::arg("h"));
    rga.def("shutdown", &rga_shutdown);
    rga.def("info", &rga_info);
    rga.def("est", [](py::dict cfg) {
        return wrap_va(rga_est_va(), cfg["pad_w"].cast<size_t>() * cfg["pad_h"].cast<size_t>() * 3, false);
    }, py::arg("cfg"));
    rga.def("est_eff", [](py::dict cfg) {
        return wrap_va(rga_est_eff_va(), cfg["eff_w"].cast<size_t>() * cfg["eff_h"].cast<size_t>() * 3, false);
    }, py::arg("cfg"));
    rga.def("win", [](py::dict cfg) {
        return wrap_va(rga_win_va(), cfg["win_w"].cast<size_t>() * cfg["win_h"].cast<size_t>() * 3, false);
    }, py::arg("cfg"));
    rga.def("enh", [](py::dict cfg) {
        return wrap_va(rga_enh_va(), cfg["win_w"].cast<size_t>() * cfg["win_h"].cast<size_t>() * 3, false);
    }, py::arg("cfg"));
    rga.def("stage", [](py::dict cfg) {
        return wrap_va(rga_stage_va(), cfg["cap_w"].cast<size_t>() * cfg["cap_h"].cast<size_t>() * 3 / 2, false);
    }, py::arg("cfg"));
    rga.def("win_fd", &rga_win_fd);
    rga.def("enh_fd", &rga_enh_fd);
    rga.def("est_fd", &rga_est_fd);

    /* ── rknn（TH2a NPU）─────────────────────────────────────────── */
    auto rk = m.def_submodule("rknn", "ZDE++ 2 条带空间切分 + EMA");
    rk.def("init", [](const std::string& model, py::dict d) {
        rknn_cfg_t c;
        std::memset(&c, 0, sizeof(c));
        c.est_w = d["est_w"].cast<uint32_t>();
        c.est_h = d["est_h"].cast<uint32_t>();
        c.eff_h = d["eff_h"].cast<uint32_t>();
        c.pad_top = d["pad_top"].cast<uint32_t>();
        c.n_slices = d["n_slices"].cast<uint32_t>();
        c.slice_h = d["slice_h"].cast<uint32_t>();
        c.slice_w_in = d["slice_w_in"].cast<uint32_t>();
        c.slice_w_out = d["slice_w_out"].cast<uint32_t>();
        py::list masks = d["core_mask"];
        for (size_t i = 0; i < masks.size() && i < RKNN_CORE_MAX_SLICES; i++)
            c.core_mask[i] = masks[i].cast<int32_t>();
        return rknn_estimate_init(model.c_str(), &c);
    }, py::arg("model"), py::arg("cfg"));
    rk.def("deinit", &rknn_estimate_deinit);
    rk.def("ready", &rknn_estimate_ready);
    rk.def("estimate", [](Buffer& est_rgb, Buffer& dst, int dst_prec,
                          float alpha, bool use_ema, bool first) {
        return rknn_estimate((const uint8_t*)est_rgb.ptr(), dst.ptr(), dst_prec,
                             alpha, use_ema ? 1 : 0, first ? 1 : 0);
    }, py::arg("est_rgb"), py::arg("curve_out"), py::arg("dst_prec"),
       py::arg("alpha"), py::arg("use_ema") = true, py::arg("first") = false);
    rk.def("slice_mask_ret", &rknn_slice_mask_ret, py::arg("idx"));
    rk.def("last_estimate_ns", &rknn_last_estimate_ns);
    rk.def("slice_count", &rknn_slice_count);

    /* ── drm（TH4）────────────────────────────────────────────────── */
    auto drm = m.def_submodule("drm", "DRM 单 CRTC（DSI）+ 画布双缓冲 + pageflip");
    drm.def("configure", [](py::dict d) {
        drm_cfg_t c;
        std::memset(&c, 0, sizeof(c));
        const std::string card = d.contains("card") ? d["card"].cast<std::string>() : "/dev/dri/card0";
        std::snprintf(c.card, sizeof(c.card), "%s", card.c_str());
        c.conn_id = d["conn_id"].cast<uint32_t>();
        c.canvas_w = d["canvas_w"].cast<uint32_t>();
        c.canvas_h = d["canvas_h"].cast<uint32_t>();
        c.nbuf = d["nbuf"].cast<uint32_t>();
        return drm_configure(&c);
    }, py::arg("cfg"));
    drm.def("is_configured", &drm_is_configured);
    drm.def("compose_flip", &drm_compose_flip, py::arg("buf_idx"), py::arg("seq") = 0);
    drm.def("wait_flip", &drm_wait_flip);
    drm.def("shutdown", &drm_shutdown);
    drm.def("info", &drm_info);
    drm.def("canvas", [](int idx, py::dict cfg) {
        return wrap_va(drm_canvas_va(idx),
                       cfg["canvas_w"].cast<size_t>() * cfg["canvas_h"].cast<size_t>() * 3, false);
    }, py::arg("index"), py::arg("cfg"));
    drm.def("canvas_fd", &drm_canvas_fd, py::arg("index"));
    /* §6.5：显示时间戳取 pageflip 完成的 vblank，不取提交时刻 —— 供 TH4 上报 */
    drm.def("last_submit_ns", &drm_last_submit_ns);
    drm.def("last_flip_ns", &drm_last_flip_ns);
    /* §6.5：回调取回的 seq，与提交值比对即为「按 seq 回填配对」 */
    drm.def("last_flip_seq", &drm_last_flip_seq);

    /* ── osd（TH4 的像素原语；布局由 osd.py 决定）────────────────────── */
    auto osd = m.def_submodule("osd", "OSD 绘制原语：矩形 / 线段 / 字形 blit");
    osd.def("fill_rect", [](Buffer& canvas, uint32_t cw, uint32_t ch, int32_t x, int32_t y,
                            uint32_t w, uint32_t h, uint32_t rgb) {
        return osd_fill_rect(canvas.ptr(), cw, ch, x, y, w, h, rgb);
    }, py::arg("canvas"), py::arg("cw"), py::arg("ch"), py::arg("x"), py::arg("y"),
       py::arg("w"), py::arg("h"), py::arg("rgb"));
    osd.def("draw_line", [](Buffer& canvas, uint32_t cw, uint32_t ch, int32_t x0, int32_t y0,
                            int32_t x1, int32_t y1, uint32_t thickness, uint32_t rgb) {
        return osd_draw_line(canvas.ptr(), cw, ch, x0, y0, x1, y1, thickness, rgb);
    }, py::arg("canvas"), py::arg("cw"), py::arg("ch"), py::arg("x0"), py::arg("y0"),
       py::arg("x1"), py::arg("y1"), py::arg("thickness"), py::arg("rgb"));
    osd.def("blit_glyph", [](Buffer& canvas, uint32_t cw, uint32_t ch, int32_t x, int32_t y,
                             uint32_t scale, py::bytes glyph, uint32_t gw, uint32_t gh,
                             uint32_t rgb) {
        const std::string s = glyph;
        return osd_blit_glyph(canvas.ptr(), cw, ch, x, y, scale,
                              reinterpret_cast<const uint8_t*>(s.data()), gw, gh, rgb);
    }, py::arg("canvas"), py::arg("cw"), py::arg("ch"), py::arg("x"), py::arg("y"),
       py::arg("scale"), py::arg("glyph"), py::arg("gw"), py::arg("gh"), py::arg("rgb"));
}
