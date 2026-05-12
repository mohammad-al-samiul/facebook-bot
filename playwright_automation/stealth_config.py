"""
Stealth defaults using ``playwright_stealth`` plus extra init scripts for
WebRTC / canvas / WebGL fingerprint hardening.

``playwright_stealth`` covers navigator, webdriver, plugins, WebGL vendor
strings, and related signals. This module layers canvas noise, optional WebGL
readback jitter, and WebRTC policy hooks so automation surfaces are reduced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from playwright_stealth import Stealth

from playwright_automation.user_agent_rotation import RotatedProfile

# Playwright ``add_init_script`` expects a function expression when passing args.
_FINGERPRINT_INIT = r"""
(args) => {
  const seed = (args && args.seed) >>> 0;
  const webrtcRelay = Boolean(args && args.webrtcRelay);
  const webglNoise = Boolean(args && args.webglNoise);

  const rnd = (() => {
    let s = seed >>> 0;
    return () => {
      s = (Math.imul(1664525, s >>> 0) + 1013904223) >>> 0;
      return s / 4294967296;
    };
  })();

  const canvasNoise = (data) => {
    for (let i = 0; i < data.length; i += 4) {
      if (rnd() < 0.12) {
        const n = (rnd() * 6) | 0;
        data[i + 0] = Math.max(0, Math.min(255, data[i + 0] + n));
      }
    }
    return data;
  };

  try {
    const proto = CanvasRenderingContext2D.prototype;
    const origGet = proto.getImageData;
    proto.getImageData = function (...a) {
      const img = origGet.apply(this, a);
      canvasNoise(img.data);
      return img;
    };
    const HTMLProto = HTMLCanvasElement.prototype;
    const origToDataURL = HTMLProto.toDataURL;
    HTMLProto.toDataURL = function (...a) {
      const w = this.width;
      const h = this.height;
      if (w && h) {
        const ctx = this.getContext("2d");
        if (ctx) {
          const snap = origGet.call(ctx, 0, 0, w, h);
          canvasNoise(snap.data);
          ctx.putImageData(snap, 0, 0);
        }
      }
      return origToDataURL.apply(this, a);
    };
    const origToBlob = HTMLProto.toBlob;
    if (origToBlob) {
      HTMLProto.toBlob = function (...a) {
        const w = this.width;
        const h = this.height;
        if (w && h) {
          const ctx = this.getContext("2d");
          if (ctx) {
            const snap = origGet.call(ctx, 0, 0, w, h);
            canvasNoise(snap.data);
            ctx.putImageData(snap, 0, 0);
          }
        }
        return origToBlob.apply(this, a);
      };
    }
  } catch (_) {}

  if (webrtcRelay && window.RTCPeerConnection) {
    const Native = window.RTCPeerConnection;
    const Wrapped = function (cfg, ...rest) {
      const next = Object.assign({}, cfg || {});
      next.iceTransportPolicy = "relay";
      return new Native(next, ...rest);
    };
    Wrapped.prototype = Native.prototype;
    Object.defineProperty(Wrapped, "name", { value: "RTCPeerConnection" });
    window.RTCPeerConnection = Wrapped;
  }

  if (webglNoise) {
    try {
      const patch = (GLProto) => {
        if (!GLProto || !GLProto.readPixels) return;
        const orig = GLProto.readPixels;
        GLProto.readPixels = function (x, y, w, h, format, type, pixels) {
          const ret = orig.call(this, x, y, w, h, format, type, pixels);
          if (pixels && pixels.length) {
            for (let i = 0; i < pixels.length; i++) {
              if (rnd() < 0.04) pixels[i] = Math.max(0, Math.min(255, pixels[i] + ((rnd() * 3) | 0)));
            }
          }
          return ret;
        };
      };
      patch(WebGLRenderingContext && WebGLRenderingContext.prototype);
      patch(WebGL2RenderingContext && WebGL2RenderingContext.prototype);
    } catch (_) {}
  }
}
"""


def fingerprint_init_script() -> str:
    """Return the init-script source (function body) for ``BrowserContext.add_init_script``."""
    return _FINGERPRINT_INIT.strip()


@dataclass(slots=True)
class StealthBundle:
    """Holds a configured ``Stealth`` instance and supplemental fingerprint args."""

    stealth: Stealth
    fingerprint_args: dict[str, Any]


def build_stealth(
    profile: RotatedProfile,
    *,
    webgl_vendor: str = "Google Inc. (NVIDIA)",
    webgl_renderer: str = "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)",
    webrtc_relay_only: bool = False,
    webgl_readpixels_noise: bool = True,
    chrome_runtime: bool = False,
) -> StealthBundle:
    """
    Build ``playwright_stealth.Stealth`` aligned to the rotated profile.

    ``webrtc_relay_only`` forces relay candidates only (stronger IP masking,
    may break real-time media on some sites).
    """
    stealth = Stealth(
        chrome_runtime=chrome_runtime,
        navigator_languages_override=profile.languages,
        navigator_platform_override=profile.platform,
        navigator_user_agent_override=profile.user_agent,
        sec_ch_ua_override=profile.sec_ch_ua,
        navigator_vendor_override="Google Inc.",
        webgl_vendor_override=webgl_vendor,
        webgl_renderer_override=webgl_renderer,
        # Enable all packaged evasions that touch automation fingerprints.
        chrome_app=True,
        chrome_csi=True,
        chrome_load_times=True,
        hairline=True,
        iframe_content_window=True,
        media_codecs=True,
        navigator_hardware_concurrency=True,
        navigator_languages=True,
        navigator_permissions=True,
        navigator_platform=True,
        navigator_plugins=True,
        navigator_user_agent=True,
        navigator_user_agent_data=True,
        navigator_vendor=True,
        navigator_webdriver=True,
        error_prototype=True,
        sec_ch_ua=True,
        webgl_vendor=True,
    )
    fp_args: dict[str, Any] = {
        "webrtcRelay": webrtc_relay_only,
        "webglNoise": webgl_readpixels_noise,
    }
    return StealthBundle(stealth=stealth, fingerprint_args=fp_args)


async def apply_stealth_to_context(context, bundle: StealthBundle, fingerprint_seed: int) -> None:
    """Apply playwright-stealth to a context, then register supplemental fingerprint scripts."""
    await bundle.stealth.apply_stealth_async(context)
    args = dict(bundle.fingerprint_args)
    args["seed"] = fingerprint_seed & 0xFFFFFFFF
    # Build init script that invokes the fingerprint function with args
    func = fingerprint_init_script()
    full_script = f"({func})({json.dumps(args)})"
    await context.add_init_script(full_script)
