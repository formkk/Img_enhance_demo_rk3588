# CMakeLists.txt —— 交付链路原生构建（规格见附件规范 docs/build.md）
#
# 目标产物：
#   · `_realtime_apply`  —— pybind11 扩展（§5 契约 C 节的 realtime_apply 实现）
#     **不叫 `realtime_apply`**：§10.3 的交付模块 `realtime_apply.py` 是同名 Python 门面，
#     二者同名会互相遮蔽（CPython FileFinder 先命中扩展后缀）⇒ 扩展取 `_` 前缀。
#     该命名偏离 `docs/build.md` §2 的字面（"目标产物 realtime_apply"），须随实施回写该节。
#
# 依赖（docs/build.md §2）：librga / librknnrt / libdrm / pybind11
# 选项（docs/build.md §3）：-O3、-march=armv8.2-a+fp16 -mcpu=cortex-a76.cortex-a55、
#                          -Wall -Wextra 零告警、-ffast-math **仅热路径**。
# ABI（docs/build.md §4）：模块后缀取自解释器（sysconfig EXT_SUFFIX）——**不得手工命名**。

cmake_minimum_required(VERSION 3.16)
project(starlight_demo_native LANGUAGES C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# ── 板上 Python（docs/build.md §4：后缀取自解释器，不得手工命名）──────────
# 板上无 `python3-config`，故用与其等价的 sysconfig 取值（`python3-config --extension-suffix`
# 内部即 sysconfig.get_config_var('EXT_SUFFIX')）。
find_package(Python3 REQUIRED COMPONENTS Interpreter)
execute_process(
  COMMAND ${Python3_EXECUTABLE} -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))"
  OUTPUT_VARIABLE PY_EXT_SUFFIX
  OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT PY_EXT_SUFFIX)
  message(FATAL_ERROR "无法从解释器取得 EXT_SUFFIX")
endif()
message(STATUS "Python EXT_SUFFIX = ${PY_EXT_SUFFIX}")

# Python 头：板上默认未装 python3-dev，允许以 `-DPython3_INCLUDE_DIR=...` 指向本地解包的头目录。
# 注意：`<prefix>/usr/include/python3.9/pyconfig.h` 是多架构**转发头**（内含
# `#include <aarch64-linux-gnu/python3.9/pyconfig.h>`），故还须把 `<prefix>/usr/include`
# 一并加入 include 路径 —— 由 `-DSTARLIGHT_PY_HEADERS_ROOT=<prefix>/usr/include` 提供。
find_package(Python3 REQUIRED COMPONENTS Development.Module)

set(STARLIGHT_PY_HEADERS_ROOT "" CACHE PATH
    "本地解包的 Python 头根（含 aarch64-linux-gnu/python3.9/pyconfig.h）；留空则用系统头")

# ── pybind11（docs/build.md §4：基线 2.11.x，必须锁定并写入构建日志）──────
find_package(pybind11 CONFIG REQUIRED)
message(STATUS "pybind11 version = ${pybind11_VERSION}")

# ── 架构与 NEON（docs/build.md §3）─────────────────────────────────────
set(ARCH_FLAGS -march=armv8.2-a+fp16 -mcpu=cortex-a76.cortex-a55)
add_compile_options(-O3 -Wall -Wextra ${ARCH_FLAGS})

# ── 扩展模块 ──────────────────────────────────────────────────────────
add_library(_realtime_apply MODULE
  native/apply_core.c
  native/realtime_apply_pybind.cpp)

target_include_directories(_realtime_apply PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/native
  ${pybind11_INCLUDE_DIRS}
  ${Python3_INCLUDE_DIRS}
  ${STARLIGHT_PY_HEADERS_ROOT})

# pybind11 扩展不链接 libpython（Linux 惯例：符号由解释器在加载时提供）
target_link_libraries(_realtime_apply PRIVATE pybind11::headers)

set_target_properties(_realtime_apply PROPERTIES
  PREFIX ""
  SUFFIX "${PY_EXT_SUFFIX}"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")

# 浮点收缩纪律：**必须 `-ffp-contract=off`**。NEON intrinsic（`vmulq_f32` / `vsubq_f32`）是两次
# 独立舍入，而标量写法 `t*t - t` 在 GCC 默认（`-ffp-contract=fast`）下会被收缩成单次 FMA ⇒ 两路
# 差 ≤1 ULP，实测在 U8 档产生 1 字节差异。关闭收缩后两路同形，逐位一致（U8 与 FP16 双档验证）。
# 显式的 `fmaf()` 调用不受本选项影响，故 NEON 的 `vfmaq_f32` 仍在标量侧有对应。
set_source_files_properties(native/apply_core.c PROPERTIES COMPILE_OPTIONS "-ffp-contract=off")

# ── 构建期版本记录（docs/build.md §2）─────────────────────────────────
find_program(RKNNRT_LIB librknnrt.so PATHS /usr/lib /usr/lib/aarch64-linux-gnu NO_DEFAULT_PATH)
add_custom_command(TARGET _realtime_apply POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E echo
          "build-info: cmake=${CMAKE_VERSION} cxx=${CMAKE_CXX_COMPILER} pybind11=${pybind11_VERSION} python=${Python3_VERSION} suffix=${PY_EXT_SUFFIX} rknnrt=${RKNNRT_LIB}"
  COMMENT "记录构建版本三元组（§10.4 runtime.*）")

# ── 设备内核扩展：capture(TH1) / rga / rknn / drm ─────────────────────
add_library(_starlight_native MODULE
  native/v4l2_core.c
  native/rga_core.c
  native/rknn_core.c
  native/drm_core.c
  native/osd_core.c
  native/starlight_native_pybind.cpp)

# 设备头（厂商/系统路径；与 docs/build.md §2 依赖一致）
find_path(RGA_INCLUDE_DIR im2d.h HINTS /usr/include/rga)
# libdrm：`xf86drm.h` 会 `#include <drm.h>`，故 **/usr/include/libdrm 必须也在搜索路径里**
# （只加 /usr/include 时能找到 <libdrm/drm.h> 但找不到 <drm.h>，实测编译失败）。
find_path(LIBDRM_INCLUDE_DIR libdrm/drm.h HINTS /usr/include)
find_path(LIBDRM_DRM_INCLUDE_DIR drm.h HINTS /usr/include/libdrm)

target_include_directories(_starlight_native PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/native
  ${pybind11_INCLUDE_DIRS}
  ${Python3_INCLUDE_DIRS}
  ${STARLIGHT_PY_HEADERS_ROOT}
  ${RGA_INCLUDE_DIR}
  ${LIBDRM_INCLUDE_DIR}
  ${LIBDRM_DRM_INCLUDE_DIR})

target_link_libraries(_starlight_native PRIVATE
  pybind11::headers rga drm rknnrt pthread)

set_target_properties(_starlight_native PROPERTIES
  PREFIX ""
  SUFFIX "${PY_EXT_SUFFIX}"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")

# 同一浮点收缩纪律（与 apply_core.c 一致；标量参考与 NEON 须逐位一致）
set_source_files_properties(native/rga_core.c native/rknn_core.c native/v4l2_core.c native/drm_core.c native/osd_core.c
  PROPERTIES COMPILE_OPTIONS "-ffp-contract=off")
