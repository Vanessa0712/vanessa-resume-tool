"""桌面窗口壳：把本地 Flask 服务包成一个独立的原生窗口.

设计约束（按优先级排列）：

1. **绝不出现「双击没反应」**。WebView2 缺失、初始化失败、页面 20 秒没加载出来，
   都必须退回浏览器打开同一条地址，让功能可用性不依赖窗口壳是否正常。
2. **后端零改动**。窗口只是前端渲染外壳，业务逻辑仍然全部走 HTTP，与浏览器模式完全一致。
3. **关窗前必须确认没有筛选任务在跑**，否则用户误点 X 会把跑了一半的筛选掐断。
4. **只有一个实例**。两个进程同时写同一个 SQLite 是真实的数据风险，用命名互斥量挡住。

只在 Windows + 已安装 pywebview 时可用；其余情况由调用方决定回退策略。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time

log = logging.getLogger("resume_tool.desktop")

WINDOW_TITLE = "简历筛选工具"
MUTEX_NAME = "Local\\ResumeTool.SingleInstance"
ERROR_ALREADY_EXISTS = 183
# WebView2 Evergreen Runtime 的三个注册位置（含 32/64 位视图差异）
WEBVIEW2_REG_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)
WEBVIEW2_REG_KEYS_HKCU = (
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)

# kernel32/user32 只有 Windows 才有。这里必须条件绑定而不是直接调用：
# 模块里的关窗决策函数是纯逻辑，任何平台都该能导入（Linux CI 就是靠它在跑测试）。
# 直接在模块级调 ctypes.WinDLL，非 Windows 上连 import 都会 AttributeError。
IS_WINDOWS = os.name == "nt"
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None
_user32 = ctypes.WinDLL("user32", use_last_error=True) if IS_WINDOWS else None

# 互斥量句柄必须活到进程结束，否则关闭即释放，锁形同虚设
_mutex_handle = None


def _read_registry_version(root, path: str) -> str:
    """读取注册表里的 WebView2 版本号；不存在或不是有效版本时返回空串."""
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            version, _ = winreg.QueryValueEx(key, "pv")
    except OSError:
        return ""
    return version if isinstance(version, str) and version.count(".") >= 2 else ""


def webview2_runtime_installed() -> bool:
    """WebView2 运行时是否可用（任一注册位置命中即认为可用）."""
    if not IS_WINDOWS:
        return False
    import winreg

    for path in WEBVIEW2_REG_KEYS:
        if _read_registry_version(winreg.HKEY_LOCAL_MACHINE, path):
            return True
    for path in WEBVIEW2_REG_KEYS_HKCU:
        if _read_registry_version(winreg.HKEY_CURRENT_USER, path):
            return True
    return False


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """尝试取得单实例锁；已被占用时返回 False.

    拿不到锁说明同一会话里已经有实例在跑，调用方应激活原窗口而不是继续启动。
    """
    global _mutex_handle
    if _kernel32 is None:
        # 非 Windows 没有互斥量，直接放行（不影响 Windows 上的单实例语义）
        return True
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        # 无法创建互斥量（权限等异常）时不阻断启动，宁可允许多开也不能打不开
        log.warning("创建单实例互斥量失败，跳过单实例检查：%s", ctypes.get_last_error())
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def activate_existing_window(title: str = WINDOW_TITLE, timeout_s: float = 3.0) -> bool:
    """把已有实例的窗口拉到前台；找不到时返回 False.

    给一点重试窗口：另一实例可能刚拿到锁、还没来得及建出窗口。
    """
    deadline = time.time() + timeout_s
    sw_restore = 9
    if _user32 is None:
        return False
    while time.time() < deadline:
        hwnd = _user32.FindWindowW(None, title)
        if hwnd:
            _user32.ShowWindow(hwnd, sw_restore)
            _user32.SetForegroundWindow(hwnd)
            return True
        time.sleep(0.2)
    return False


def should_allow_close(is_busy, confirm) -> bool:
    """关窗决策：返回 True 表示允许关闭.

    confirm 只在有任务运行时调用，返回 True 表示用户同意关闭。
    单独抽出来是因为 pywebview 的返回值语义反直觉，必须有测试锁住。
    """
    if not is_busy():
        return True
    return bool(confirm())


def _confirm_busy_close(window, is_busy) -> bool:
    """任务运行中时问一次再决定；返回 True 表示允许关闭."""
    try:
        return bool(window.create_confirmation_dialog(
            "筛选任务仍在运行",
            "现在关闭会中断正在进行的筛选任务。\n\n确定要退出吗？",
        ))
    except Exception:  # 对话框本身出错时保守处理：不关，避免误伤任务
        log.exception("弹确认框失败，保持窗口不关闭")
        return False


def open_window(url: str, is_busy, register_shutdown_hook=None,
                title: str = WINDOW_TITLE, load_timeout_s: float = 20.0) -> bool:
    """打开桌面窗口并阻塞到窗口关闭.

    返回 True 表示窗口正常打开过（可以认为桌面模式成立）；
    返回 False 表示窗口不可用，调用方应退回浏览器。
    """
    import webview

    # pywebview 默认 ALLOW_DOWNLOADS=False：导出 CSV / xlsx 会被静默取消，
    # 用户点「导出」什么都不会发生。必须显式打开，否则导出功能在窗口模式下等于废掉。
    webview.settings["ALLOW_DOWNLOADS"] = True

    # 仅调试用：设置 RESUME_TOOL_DEBUG_PORT 才开远程调试端口，便于自动化验证
    # 上传、导出等只能在真实浏览器内核里复现的路径。生产默认关闭。
    debug_port = os.environ.get("RESUME_TOOL_DEBUG_PORT", "").strip()
    if debug_port.isdigit():
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(debug_port)
        log.info("窗口已开启远程调试端口 %s（仅调试用）", debug_port)

    loaded = threading.Event()
    window = webview.create_window(
        title, url=url, width=1280, height=880, min_size=(1024, 700),
        confirm_close=False,  # 用我们自己的确认逻辑，pywebview 默认那个不带任务状态
    )
    if window is None:
        log.error("创建窗口失败，回退浏览器")
        return False

    if register_shutdown_hook is not None:
        # 设置页的「退出程序」走这条回调，语义从「杀进程」改为「关闭窗口」
        register_shutdown_hook(window.destroy)

    def on_loaded() -> None:
        loaded.set()

    def on_closing() -> bool:
        """关窗拦截。返回值语义与直觉相反：pywebview 的 closing 事件是

            should_cancel = closing.set()   # = 「有 handler 返回了 False」
            if should_cancel: args.Cancel = True

        也就是说 **返回 False 才是「阻止关闭」**。这里必须显式返回 True 放行，
        否则窗口会关不掉（第一版就踩了这个坑：空闲时 return False 导致点 X 无效）。
        """
        allowed = should_allow_close(
            is_busy, lambda: _confirm_busy_close(window, is_busy))
        if allowed and is_busy():
            log.warning("用户确认在任务运行中关闭窗口")
        return allowed

    window.events.loaded += on_loaded
    window.events.closing += on_closing

    stop_watchdog = threading.Event()

    def watchdog() -> None:
        if loaded.wait(load_timeout_s):
            return
        if stop_watchdog.is_set():
            return
        log.error("窗口 %.0f 秒内未完成加载，关闭窗口并回退浏览器", load_timeout_s)
        try:
            window.destroy()
        except Exception:
            log.exception("销毁无响应的窗口失败")

    threading.Thread(target=watchdog, name="window-watchdog", daemon=True).start()
    try:
        webview.start()
    finally:
        stop_watchdog.set()
    return loaded.is_set()
