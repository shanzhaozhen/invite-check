"""给每个账号一套固定但各不相同的"设备指纹"，降低同机多号被风控 ban 的概率。

核心思路：按账号名做**确定性**派生——同一个账号每次跑都是同一台"设备"（注册、签到、
复用登录态都一致，像个真实用户），只有不同账号之间才不一样。指纹乱跳反而更可疑。

只动那些**不会自相矛盾**的物理参数：屏幕分辨率 / 缩放 / CPU 核数 / 内存 / 显卡。
**故意不伪造 UA 和 Chrome 版本**——真实浏览器会带 Sec-CH-UA 客户端提示，硬改 UA 版本会
和提示对不上，反而更容易被识破；所以 UA 一律用本机真实浏览器的，保持一致。

⚠ 同机 20 个号最大的破绽其实是**同一个出口 IP**。指纹只能缓解，真要压风险请配合
``--proxy``（最好每个号一个出口）并把 ``--delay`` 留大一点、别集中在一小时内全跑完。
"""

from __future__ import annotations

import hashlib
import json

# 常见桌面分辨率 (宽, 高, 缩放)
_SCREENS = [
    (1920, 1080, 1.0), (1536, 864, 1.25), (1600, 900, 1.0), (1366, 768, 1.0),
    (2560, 1440, 1.0), (1440, 900, 1.0), (1680, 1050, 1.0), (1920, 1200, 1.0),
    (1280, 720, 1.0),
]
_CORES = [4, 6, 8, 12, 16]
_MEMORY = [4, 8, 16]
# 真实存在的显卡组合（Chrome 在 Windows 上通过 ANGLE 报告的字符串）
_GPUS = [
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]


def _seed(username: str) -> int:
    return int(hashlib.sha256((username or "default").encode("utf-8")).hexdigest(), 16)


def for_user(username: str) -> dict:
    """按账号名确定性地挑一套设备参数。同名永远得到同一套。"""
    n = _seed(username)
    w, h, dpr = _SCREENS[n % len(_SCREENS)]
    # 视口比屏幕矮一些（模拟任务栏 + 浏览器标签/地址栏），宽度取满
    vh = max(600, h - 120 - (n % 4) * 30)
    return {
        "viewport": {"width": w, "height": vh},
        "screen": {"width": w, "height": h},
        "device_scale_factor": dpr,
        "cores": _CORES[(n // 7) % len(_CORES)],
        "memory": _MEMORY[(n // 13) % len(_MEMORY)],
        "gpu": _GPUS[(n // 17) % len(_GPUS)],
    }


# 隐藏自动化特征——所有情况都要，Cloudflare 拦截页靠它放行。
# 注意报 false 而不是 undefined：真实 Chrome 就是 false，报 undefined 反而是个破绽。
_WEBDRIVER = "Object.defineProperty(navigator,'webdriver',{get:()=>false});"

# 后台模式把窗口挪到屏幕外（见 runner.BACKGROUND_ARGS），JS 里 screenX/screenY 会变成
# -32000 这种明显不正常的值，这里报回正常位置，免得被当成自动化特征。
_ONSCREEN = "\n".join([
    "(function(){",
    "  var fix = {screenX: 0, screenY: 0, screenLeft: 0, screenTop: 0};",
    "  for (var k in fix) {",
    "    try { Object.defineProperty(window, k, {get: (function(v){return function(){return v;};})(fix[k])}); }",
    "    catch (e) {}",
    "  }",
    "})();",
])


def stealth_script(fp: dict | None, background: bool = False) -> str:
    """要注入页面的脚本。

    ``fp`` 为 None 时只隐藏 webdriver；给了就再叠加设备参数。
    ``background`` 为真时补一段修正窗口坐标（窗口被挪到屏幕外了）。
    """
    parts = [_WEBDRIVER]
    if background:
        parts.append(_ONSCREEN)
    if fp is not None:
        vendor, renderer = fp["gpu"]
        parts += [
            f"Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>{fp['cores']}}});",
            f"Object.defineProperty(navigator,'deviceMemory',{{get:()=>{fp['memory']}}});",
            # WebGL 显卡型号；把改写后的 getParameter.toString 伪装成原生，避免被一眼看穿
            "(function(){",
            f"  var V={json.dumps(vendor)},R={json.dumps(renderer)};",
            "  function patch(proto){",
            "    if(!proto||!proto.getParameter)return;",
            "    var gp=proto.getParameter;",
            "    var f=function(p){if(p===37445)return V;if(p===37446)return R;return gp.call(this,p);};",
            "    try{f.toString=gp.toString.bind(gp);}catch(e){}",
            "    proto.getParameter=f;",
            "  }",
            "  patch(self.WebGLRenderingContext&&WebGLRenderingContext.prototype);",
            "  patch(self.WebGL2RenderingContext&&WebGL2RenderingContext.prototype);",
            "})();",
        ]
    return "\n".join(parts)
