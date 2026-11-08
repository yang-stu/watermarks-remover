#!/usr/bin/env python3
"""watermarks-remover 本地 Web UI（纯标准库，非上游内容）。

上游的 server.py 是纯 API 服务：`GET /` 返回 404，也完全没有 CORS 头，
所以浏览器直接打开 8765 是"什么都看不到"，另起一个页面去 fetch 也会被同源策略拦掉。

这个脚本把两件事合在一起解决：
  1. 在 / 提供一个真正能用的网页界面（拖拽图片 -> 检测 -> 清理 -> 下载）
  2. 在 /api/* 把请求同源转发给上游服务，浏览器不会再遇到 CORS 问题

用法：
    python3 local-tools/ui.py                    # 界面 http://127.0.0.1:8780
    python3 local-tools/ui.py --port 9000
    python3 local-tools/ui.py --target http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 让 UI 进程能直接用引擎的解析器与本地视频写入模块（清理仍走上游 API）
sys.path.insert(0, _HERE := os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "service", "scripts")))

import video_meta  # noqa: E402

TARGET = "http://127.0.0.1:8765"
PROXY_TIMEOUT = 300

STUDIO_PATH = os.path.join(_HERE, "pixel-studio.html")

VIDEO_EXTS = (".mp4", ".mov", ".m4v")


BACK_LINK = (
    '<a href="/" style="position:fixed;left:18px;bottom:18px;z-index:99999;'
    'display:inline-flex;align-items:center;gap:6px;padding:9px 15px;'
    'border-radius:999px;background:rgba(20,22,28,.92);color:#e8e8ea;'
    "font:13px/1 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;"
    'text-decoration:none;border:1px solid rgba(255,255,255,.16);'
    'box-shadow:0 4px 18px rgba(0,0,0,.4)">&#8592; 返回溯源清理</a>'
).encode("utf-8")


def load_studio() -> bytes | None:
    """读取像素工作台页面并注入返回入口。

    每次请求现读，改源文件刷新即可生效；返回链接是注入的，
    不改动原始 HTML，避免副本与源文件分叉。
    """
    try:
        with open(STUDIO_PATH, "rb") as fh:
            body = fh.read()
    except OSError:
        return None
    marker = b"</body>"
    idx = body.rfind(marker)
    if idx == -1:
        return body + BACK_LINK
    return body[:idx] + BACK_LINK + body[idx:]


class Handler(BaseHTTPRequestHandler):
    server_version = "wr-ui/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- 基础工具 ----------

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    # ---------- 静态界面 ----------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if self.path in ("/studio", "/studio/"):
            body = load_studio()
            if body is None:
                self._json(404, {"ok": False, "error": "pixel-studio.html 不存在"})
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/"):
            self._proxy("GET")
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self.path == "/api/video-tag":
            self._video_tag()
            return
        if self.path.startswith("/api/"):
            self._proxy("POST")
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _video_tag(self):
        """本地端点：给 MP4/MOV 写入设备/GPS 元数据（不动上游 server.py）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            raw = base64.b64decode(body.get("data") or "")
            if not raw:
                raise ValueError("没有收到文件数据")
            gps = None
            if body.get("writeGps"):
                gps = (float(body["lat"]), float(body["lng"]))
            tagged, actions = video_meta.tag_video(
                raw,
                make=(body.get("make") or None),
                model=(body.get("model") or None),
                gps=gps,
                created=(body.get("created") or None),
            )
            self._json(200, {
                "ok": True,
                "data": base64.b64encode(tagged).decode("ascii"),
                "actions": actions,
                "size": len(tagged),
            })
        except ValueError as e:
            self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"视频写入失败：{e}"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 同源转发 ----------

    def _proxy(self, method: str):
        upstream_path = self.path[len("/api"):]          # /api/health -> /health
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(TARGET + upstream_path, data=body, method=method)
        req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))

        try:
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
                self._send(resp.status, payload, ctype)
        except urllib.error.HTTPError as exc:
            # 上游的 400/404 等要原样透传，界面才能显示真正的错误原因
            payload = exc.read()
            ctype = exc.headers.get("Content-Type", "application/json; charset=utf-8") if exc.headers else \
                "application/json; charset=utf-8"
            self._send(exc.code, payload, ctype)
        except Exception as exc:  # noqa: BLE001
            self._json(502, {
                "ok": False,
                "error": f"无法连接上游服务 {TARGET}：{exc}",
                "hint": "请先启动 watermarks-remover：./run.sh",
            })


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 溯源标记清理工具</title>
<style>
  :root{
    --bg:#f7f7f5; --card:#ffffff; --line:#e3e2dd; --line2:#d3d1c7;
    --tx:#1f1f1e; --tx2:#5f5e5a; --tx3:#888780;
    --teal:#0f6e56; --teal-bg:#e1f5ee;
    --blue:#185fa5; --blue-bg:#e6f1fb;
    --amber:#854f0b; --amber-bg:#faeeda;
    --red:#a32d2d; --red-bg:#fcebeb;
    --green:#3b6d11; --green-bg:#eaf3de;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:960px;margin:0 auto;padding:32px 20px 64px}
  h1{font-size:20px;font-weight:500;margin:0 0 6px}
  .sub{color:var(--tx2);margin:0 0 20px;font-size:13px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:18px;margin-bottom:16px}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .chip{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid transparent;
    white-space:nowrap}
  .c-ok{background:var(--green-bg);color:var(--green)}
  .c-no{background:var(--red-bg);color:var(--red)}
  .c-info{background:var(--blue-bg);color:var(--blue)}
  #drop{border:1.5px dashed var(--line2);border-radius:12px;padding:38px 20px;
    text-align:center;color:var(--tx2);cursor:pointer;background:var(--card);
    transition:border-color .15s,background .15s}
  #drop:hover{border-color:var(--teal);background:var(--teal-bg)}
  #drop.hot{border-color:var(--teal);background:var(--teal-bg)}
  #drop b{color:var(--teal);font-weight:500}
  button{font:inherit;font-size:13px;padding:7px 14px;border-radius:8px;
    border:1px solid var(--line2);background:#fff;color:var(--tx);cursor:pointer}
  button:hover:not(:disabled){border-color:var(--tx3)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:var(--teal);border-color:var(--teal);color:#fff}
  button.primary:hover:not(:disabled){filter:brightness(.94)}
  label.opt{font-size:13px;color:var(--tx2);display:flex;gap:6px;align-items:center;
    cursor:pointer;user-select:none}
  .file{border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px;
    background:var(--card)}
  .fhead{display:flex;justify-content:space-between;gap:12px;align-items:baseline;
    flex-wrap:wrap;margin-bottom:12px}
  .fname{font-weight:500;word-break:break-all}
  .fsize{color:var(--tx3);font-size:12px;white-space:nowrap}
  .verdict{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
  .v{padding:4px 10px;border-radius:8px;font-size:12px}
  .v-bad{background:var(--amber-bg);color:var(--amber)}
  .v-good{background:var(--green-bg);color:var(--green)}
  ul.find{margin:0 0 12px;padding-left:18px;color:var(--tx2);font-size:13px}
  ul.find li{margin:2px 0}
  ul.find code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  .err{background:var(--red-bg);color:var(--red);padding:9px 12px;border-radius:8px;
    font-size:13px;margin-bottom:12px}
  .thumb{max-width:100%;max-height:180px;border-radius:8px;border:1px solid var(--line);
    display:block;margin-bottom:12px}
  .stats{font-size:12px;color:var(--tx3);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .note{font-size:12px;color:var(--tx3);margin:14px 0 0}
  .tabs{display:flex;gap:4px;margin:0 0 16px;border-bottom:1px solid var(--line)}
  .tabs a{font-size:13px;padding:8px 14px;color:var(--tx2);text-decoration:none;
    border:1px solid transparent;border-bottom:none;border-radius:8px 8px 0 0;
    margin-bottom:-1px}
  .tabs a:hover{color:var(--tx);background:var(--card)}
  .tabs a.on{color:var(--teal);background:var(--card);border-color:var(--line)}
  .banner{display:none;background:var(--red-bg);color:var(--red);padding:12px 14px;
    border-radius:10px;margin-bottom:16px;font-size:13px}
  .banner.show{display:block}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  .studio{border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0 0;background:var(--bg)}
  .studio h4{font-size:13px;font-weight:500;margin:0 0 10px}
  .sgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:10px}
  .sgrid label{display:block;font-size:12px;color:var(--tx2)}
  .sgrid select{width:100%;margin-top:4px;font:inherit;font-size:13px;padding:6px 8px;
    border:1px solid var(--line2);border-radius:8px;background:#fff;color:var(--tx)}
  .svar{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:8px;margin:10px 0}
  .svar .v{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--tx2)}
  .svar input[type=range]{width:88px}
  .sdiff{font-size:12px;line-height:1.7;background:#fff;border:1px solid var(--line);
    border-radius:8px;padding:10px;margin-top:10px;color:var(--tx2)}
  .sdiff b{font-weight:500;color:var(--tx)}
</style>
</head>
<body>
<div class="wrap">
  <div class="tabs">
    <a class="on" href="/">溯源清理</a>
    <a href="/studio">像素工作台</a>
  </div>

  <h1>AI 溯源标记清理工具</h1>
  <p class="sub">剥离图片 / 视频里的 C2PA / EXIF / XMP / AI 生成器标识。元数据在容器层，像素与视频流不会被改动。</p>

  <div id="banner" class="banner"></div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="row" id="caps"><span class="chip c-info">正在读取服务能力…</span></div>
      <button id="refresh">刷新状态</button>
    </div>
  </div>

  <div id="drop">
    把图片或视频拖到这里，或<b>点击选择文件</b><br>
    <span style="font-size:12px">图片：JPEG / PNG / WebP / AVIF / HEIC / BMP / GIF / TIFF ｜ 视频：MP4 / MOV / M4V</span>
  </div>
  <input id="pick" type="file" accept="image/*,video/mp4,video/quicktime,.mp4,.mov,.m4v" multiple hidden>

  <div class="card" style="margin-top:16px">
    <label class="opt">
      <input type="checkbox" id="keepmeta">
      <span>保留非 AI 元数据</span>
    </label>
    <p class="note" style="margin:8px 0 0 22px">
      勾选后可保住 <span class="mono">ICC 色彩配置</span>（避免严格色彩管理环境下偏色），
      <b>但实测也会放过未被识别的 APP1 段</b>（例如厂商自带的生成器标识）。
      要彻底清除痕迹，<b>请保持不勾选</b>。
    </p>
  </div>

  <div id="list"></div>

  <p class="note">
    清理只在本地进行，图片不会上传到任何外部服务。文件直接从本机发往 127.0.0.1 上的
    watermarks-remover 服务。
  </p>
</div>

<script>
const API = "/api";
const list = document.getElementById("list");
const banner = document.getElementById("banner");

function showBanner(msg){
  banner.textContent = msg;
  banner.classList.add("show");
}

async function api(path, payload){
  const opt = payload
    ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)}
    : {method:"GET"};
  const res = await fetch(API + path, opt);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch(e){ data = {ok:false, error:"上游返回了非 JSON 内容", raw:text.slice(0,300)}; }
  if(!res.ok && data.ok !== false){ data = {ok:false, error:"HTTP " + res.status}; }
  return data;
}

function b64(file){
  return new Promise((resolve,reject)=>{
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1]);
    r.onerror = () => reject(new Error("读取文件失败"));
    r.readAsDataURL(file);
  });
}

function human(bytes){
  if(bytes < 1024) return bytes + " B";
  if(bytes < 1024*1024) return (bytes/1024).toFixed(1) + " KB";
  return (bytes/1024/1024).toFixed(2) + " MB";
}

function downloadBytes(bytes, name, type){
  const blob = new Blob([bytes], {type: type || "application/octet-stream"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function b64ToBlob(b, type){
  const bin = atob(b);
  const arr = new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], {type: type || "application/octet-stream"});
}

async function loadCaps(){
  const el = document.getElementById("caps");
  try{
    const d = await api("/capabilities");
    if(!d.ok){ el.innerHTML = '<span class="chip c-no">服务不可用</span>'; showBanner("无法连接 watermarks-remover 服务：" + (d.error||"") + " " + (d.hint||"")); return; }
    banner.classList.remove("show");
    const tools = d.tools || {};
    const on = Object.keys(tools).filter(k=>tools[k]);
    const html = ['<span class="chip c-ok">服务在线 v' + (d.version||"?") + '</span>'];
    html.push('<span class="chip ' + (on.length ? 'c-ok"></span>' : 'c-no">系统工具：未安装（图片清理不需要）</span>'));
    if(on.length) html[1] = '<span class="chip c-ok">系统工具：' + on.join(", ") + '</span>';
    if(d.scorers && d.scorers.stylometry) html.push('<span class="chip c-info">风格计量打分可用</span>');
    el.innerHTML = html.join("");
  }catch(e){
    el.innerHTML = '<span class="chip c-no">服务不可用</span>';
    showBanner("请求失败：" + e.message);
  }
}

/* ================= 像素工作台引擎 =================
   工作台是纯浏览器的，把它装进同源 iframe，直接取它的 PS_* 库来用。
   好处：不复制任何一行处理逻辑，工作台升级这里自动跟着升。 */
let studioWin = null;

function ensureStudio(){
  return new Promise((resolve, reject) => {
    if(studioWin && studioWin.PS_META) return resolve(studioWin);
    let f = document.getElementById("studioFrame");
    if(!f){
      f = document.createElement("iframe");
      f.id = "studioFrame"; f.src = "/studio"; f.style.display = "none";
      document.body.appendChild(f);
    }
    const t0 = Date.now();
    const iv = setInterval(() => {
      try{
        const w = f.contentWindow;
        if(w && w.PS_META && w.PS_PIXELS && w.PS_DATA && w.PS_ANALYZE){
          clearInterval(iv); studioWin = w; resolve(w);
        }
      }catch(e){}
      if(Date.now() - t0 > 25000){
        clearInterval(iv);
        reject(new Error("像素工作台引擎加载超时（/studio 没起来？）"));
      }
    }, 120);
  });
}

function bytesFromB64(b){
  const bin = atob(b), n = bin.length, u = new Uint8Array(n);
  for(let i = 0; i < n; i++) u[i] = bin.charCodeAt(i);
  return u;
}

const STUDIO_MAX_SIDE = 2600;   // 解码上限：超大照片会把 WebKit 的 canvas 撑爆（面积上限），先降下来

async function decodeImg(u8, maxSide){
  maxSide = maxSide || STUDIO_MAX_SIDE;
  const blob = new Blob([u8], {type:"image/jpeg"});
  if(!window.createImageBitmap)
    throw new Error("浏览器不支持 createImageBitmap，请换 Chrome / Edge");
  let bmp;
  try{ bmp = await createImageBitmap(blob, {imageOrientation:"from-image"}); }
  catch(e){ throw new Error("图片解码失败：" + e.message); }
  const w = bmp.width, h = bmp.height;
  const long = Math.max(w, h);
  const tw = long > maxSide ? Math.round(w * maxSide / long) : w;
  const th = long > maxSide ? Math.round(h * maxSide / long) : h;
  const c = document.createElement("canvas");
  c.width = tw; c.height = th;
  const ctx = c.getContext("2d", {willReadFrequently:true});
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bmp, 0, 0, w, h, 0, 0, tw, th);
  if(bmp.close) bmp.close();
  const d = ctx.getImageData(0, 0, tw, th);
  if(!d || !d.data || d.data.length !== tw * th * 4){
    throw new Error("解码后像素长度异常（data=" + (d && d.data ? d.data.length : "null")
      + "，应为 " + (tw * th * 4) + "，尺寸 " + tw + "x" + th + "）");
  }
  return {data:d.data, width:tw, height:th};
}

/* 关键：不再用 new ImageData(跨环境的数组, w, h) —— 报错 "Length is not a
   multiple of sw" 就是它抛的。改用 createImageData(w,h) 造一块本环境、
   尺寸严格正确的缓冲区，再把像素 set 进去，跨 iframe 也稳。 */
function imgToCanvas(img){
  const w = img.width|0, h = img.height|0, need = w*h*4;
  const len = img.data ? img.data.length : 0;
  if(!len || len !== need){
    throw new Error("像素数据与尺寸对不上（data=" + len + "，应为 " + need
      + "，尺寸 " + w + "x" + h + "）");
  }
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const ctx = c.getContext("2d", {willReadFrequently:true});
  const id = ctx.createImageData(w, h);
  id.data.set(img.data.subarray ? img.data.subarray(0, need) : img.data);
  ctx.putImageData(id, 0, 0);
  return c;
}

function encodeJpeg(img, q){
  return new Promise((res, rej) => {
    imgToCanvas(img).toBlob((b) => {
      if(!b) return rej(new Error("JPEG 编码失败"));
      b.arrayBuffer().then((ab) => res(new Uint8Array(ab)));
    }, "image/jpeg", q);
  });
}

function resizeImg(img, w, h){
  const src = imgToCanvas(img);
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const x = c.getContext("2d", {willReadFrequently:true});
  x.imageSmoothingQuality = "high";
  x.drawImage(src, 0, 0, w, h);
  const d = x.getImageData(0, 0, w, h);
  return {data:d.data, width:w, height:h};
}

function jpegDims(M, u8){
  try{
    const p = M.parseJpeg(u8);
    for(const seg of p.segs){
      if(seg.marker >= 0xc0 && seg.marker <= 0xcf &&
         seg.marker !== 0xc4 && seg.marker !== 0xc8 && seg.marker !== 0xcc){
        return {height:(u8[seg.start+5]<<8)|u8[seg.start+6],
                width:(u8[seg.start+7]<<8)|u8[seg.start+8]};
      }
    }
  }catch(e){}
  return {width:0, height:0};
}

const scanHits = (s) => (s && s.hits && s.hits.length) ? s.hits.map(x => x.label).join("、") : "无";
const scanBoxes = (s) => (s && s.boxes && s.boxes.length) ? s.boxes.join("、") : "无";

/* 在某张照片的卡片里展开工作台面板 */
/* 深圳热门区域：在引擎自带的 55 个地点之外补充 */
const SZ_SPOTS = [
  ["深圳 · 人才公园",   "南山区科苑南路",           22.5155, 113.9410],
  ["深圳 · 莲花山公园", "福田区红荔路 6030 号",     22.5598, 114.0564],
  ["深圳 · 深圳湾公园", "南山区滨海大道",           22.5174, 113.9367],
  ["深圳 · 世界之窗",   "南山区深南大道 9037 号",   22.5364, 113.9736],
  ["深圳 · 欢乐谷",     "南山区华侨城",             22.5380, 113.9770],
  ["深圳 · 欢乐海岸",   "南山区白石路东 8 号",      22.5250, 113.9900],
  ["深圳 · 海上世界",   "南山区蛇口望海路",         22.4900, 113.9200],
  ["深圳 · 东门老街",   "罗湖区东门中路",           22.5470, 114.1180],
  ["深圳 · 华强北",     "福田区华强北路",           22.5470, 114.0880],
  ["深圳 · 市民中心",   "福田区福中三路",           22.5460, 114.0540],
  ["深圳 · 科技园",     "南山区科技南一路",         22.5400, 113.9500],
  ["深圳 · 大梅沙",     "盐田区盐梅路",             22.5960, 114.3080],
  ["深圳 · 较场尾",     "大鹏新区银滩路",           22.5920, 114.4700],
  ["深圳 · 甘坑客家小镇","龙岗区甘李路 18 号",      22.6280, 114.1280],
  ["深圳 · 大芬油画村", "龙岗区布吉大芬村",         22.5950, 114.1500],
  ["深圳 · 梧桐山",     "罗湖区梧桐山北麓",         22.5880, 114.1800]
];

function ensureShenzhen(D){
  if(!D || !D.LOCATIONS || D._szAdded) return;
  D._szAdded = true;
  SZ_SPOTS.forEach((r) => D.LOCATIONS.push({
    id: "sz-" + r[0], name: r[0], address: r[1],
    lat: r[2], lng: r[3], alt: Math.round((Math.random() * 30 + 5) * 10) / 10
  }));
}

/* 同一个地点每次跑坐标都略为偏移，避免每次写入的经纬度一模一样 */
function jitterLoc(base){
  const jLat = (Math.random() - 0.5) * 0.004;   // 约 ±220 m
  const jLng = (Math.random() - 0.5) * 0.005;
  return {id:base.id, name:base.name, address:base.address, alt:base.alt || 0,
          lat:+(base.lat + jLat).toFixed(6), lng:+(base.lng + jLng).toFixed(6)};
}

async function openStudioPanel(card, body, baseName){
  const old = body.querySelector(".studio");
  if(old){ old.remove(); return; }

  const panel = document.createElement("div");
  panel.className = "studio";
  panel.innerHTML = '<h4>像素工作台 · 正在加载引擎…</h4>';
  body.appendChild(panel);

  let w;
  try{ w = await ensureStudio(); }
  catch(e){ panel.innerHTML = '<div class="err">' + escapeHtml(e.message) + '</div>'; return; }

  const D = w.PS_DATA, P = w.PS_PIXELS;
  ensureShenzhen(D);

  // 默认：Apple iPhone 14 Pro / 深圳
  let defDev = D.DEVICES.findIndex(d => d.make === "Apple" && d.model === "iPhone 14 Pro");
  if(defDev < 0) defDev = D.DEVICES.findIndex(d => d.make === "Apple");
  if(defDev < 0) defDev = 0;
  let defLoc = D.LOCATIONS.findIndex(l => (l.name || "").indexOf("深圳") === 0);
  if(defLoc < 0) defLoc = D.LOCATIONS.findIndex(l => (l.name || "").indexOf("深圳") >= 0);
  if(defLoc < 0) defLoc = 0;

  const devOpts  = D.DEVICES.map((d,i) =>
    '<option value="'+i+'"'+(i===defDev?' selected':'')+'>'+escapeHtml(d.make+" "+d.model)+'</option>').join("");
  const locOpts  = D.LOCATIONS.map((l,i) =>
    '<option value="'+i+'"'+(i===defLoc?' selected':'')+'>'+escapeHtml(l.name)+'</option>').join("");
  const sizeOpts = P.SIZE_PRESETS.map((s,i) =>
    '<option value="'+i+'"'+(i===0?' selected':'')+'>'+escapeHtml(s.label+"（"+s.note+"）")+'</option>').join("");
  const varHtml  = P.VARIANTS.map(v =>
    '<div class="v"><input type="checkbox" data-k="'+v.key+'">' +
    '<span title="'+escapeHtml(v.desc)+'">'+escapeHtml(v.name)+'</span>' +
    '<input type="range" min="5" max="100" value="'+v.level+'" data-l="'+v.key+'"></div>').join("");

  panel.innerHTML =
    '<h4>像素工作台 · 写入相机参数 / 生成像素变体</h4>' +
    '<div class="sgrid">' +
      '<label>处理模式<select id="sMode">' +
        '<option value="reencode">重编码（可调变体）</option>' +
        '<option value="keep">保留原像素（只改元数据）</option></select></label>' +
      '<label>拍摄设备<select id="sDev">'+devOpts+'</select></label>' +
      '<label>拍摄地点 GPS<select id="sLoc">'+locOpts+'</select></label>' +
      '<label>导出尺寸<select id="sSize">'+sizeOpts+'</select></label>' +
    '</div>' +
    '<div class="svar">'+varHtml+'</div>' +
    '<div class="row"><button class="primary" id="sRun">执行处理</button>' +
    '<button id="sDown" disabled>下载处理结果</button>' +
    '<span class="stats" id="sNote"></span></div>' +
    '<div class="sdiff" id="sDiff" hidden></div>';

  const note = panel.querySelector("#sNote");
  const diff = panel.querySelector("#sDiff");
  const down = panel.querySelector("#sDown");
  const srcLabel = () => card._studioBytes ? "上次工作台输出"
    : (card._cleanBytes ? "清理后的输出" : "原始文件");
  note.textContent = "数据源：" + srcLabel();

  panel.querySelector("#sRun").addEventListener("click", async () => {
    const btn = panel.querySelector("#sRun");
    btn.disabled = true; btn.textContent = "处理中…";
    down.disabled = true;
    try{
      const M = w.PS_META;
      const bytes = card._bytes;
      const before = M.scanBinary(bytes);
      const mode = panel.querySelector("#sMode").value;
      const dev  = D.DEVICES[+panel.querySelector("#sDev").value];
      const loc  = jitterLoc(D.LOCATIONS[+panel.querySelector("#sLoc").value]);
      const sp   = P.SIZE_PRESETS[+panel.querySelector("#sSize").value];

      let workBytes, outImg = null;
      if(mode === "keep"){
        if(!M.isJpeg(bytes)) throw new Error("保留原像素模式只支持 JPEG 输入");
        workBytes = M.stripJpeg(bytes, {keepIcc:true});
      } else {
        let img = await decodeImg(bytes);
        const active = {};
        P.VARIANTS.forEach(v => {
          const cb = panel.querySelector('input[data-k="'+v.key+'"]');
          const rg = panel.querySelector('input[data-l="'+v.key+'"]');
          if(cb && cb.checked && v.key !== "jpeg2"){
            active[v.key] = {on:true, level:+(rg ? rg.value : v.level)};
          }
        });
        img = P.applyVariants(img, active, {seed:P.seedFrom(baseName + bytes.length)});
        const t = P.targetSize(img.width, img.height, sp.maxSide);
        if(t.scaled) img = resizeImg(img, t.width, t.height);
        outImg = img;
        workBytes = await encodeJpeg(img, 0.92);
      }

      const dims = outImg ? {width:outImg.width, height:outImg.height} : jpegDims(M, workBytes);
      const exposure = D.generateExposure(dev, 0.5, Math.random);
      const dict = M.buildExifDict({
        device:dev, exposure, location:loc, when:new Date(),
        width:dims.width, height:dims.height, author:"", copyright:""
      });
      const out = M.writeMetadata(workBytes, dict, {
        author:null, copyright:null,
        creatorTool:"watermarks-remover /studio", writeXmp:false
      });
      const after = M.scanBinary(out);

      // 只处理不下载；结果存起来，由「下载处理结果」按钮负责落盘
      card._studioBytes = out;
      card._bytes = out;
      down.disabled = false;
      note.textContent = "数据源：上次工作台输出";

      diff.hidden = false;
      diff.innerHTML =
        '<b>已写入</b>　' + escapeHtml(dev.make + " " + dev.model) + ' ｜ ' + escapeHtml(loc.name) +
        ' ｜ ' + loc.lat.toFixed(5) + ', ' + loc.lng.toFixed(5) +
        ' ｜ f/' + exposure.fno + '　ISO' + exposure.iso + '　1/' + exposure.shutterDenominator + 's<br>' +
        '<b>AI 标记扫描</b>　处理前 ' + escapeHtml(scanHits(before)) +
        '　→　处理后 ' + escapeHtml(scanHits(after)) + '<br>' +
        '<b>容器特征</b>　处理前 ' + escapeHtml(scanBoxes(before)) +
        '　→　处理后 ' + escapeHtml(scanBoxes(after)) + '<br>' +
        '<b>体积</b>　' + human(bytes.length) + ' → ' + human(out.length);

      btn.textContent = "已处理 ✓";
      btn.disabled = false;
    }catch(e){
      btn.disabled = false; btn.textContent = "执行处理";
      const box = document.createElement("div");
      box.className = "err";
      box.textContent = "处理失败：" + e.message;
      panel.appendChild(box);
    }
  });

  down.addEventListener("click", () => {
    if(!card._studioBytes) return;
    const blob = new Blob([card._studioBytes], {type:"image/jpeg"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = baseName + "-studio.jpg";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });
}

function u8ToB64(u8){
  let s = "";
  const CH = 0x8000;
  for(let i = 0; i < u8.length; i += CH)
    s += String.fromCharCode.apply(null, u8.subarray(i, Math.min(i + CH, u8.length)));
  return btoa(s);
}

/* 视频写入拍摄信息：容器层写 QuickTime ©mak/©mod/©xyz/©day，不重编码 */
async function openVideoPanel(card, body, baseName, fileName){
  const old = body.querySelector(".studio");
  if(old){ old.remove(); return; }

  const panel = document.createElement("div");
  panel.className = "studio";
  panel.innerHTML = '<h4>视频写入拍摄信息 · 准备中…</h4>';
  body.appendChild(panel);

  const IPHONES = ["iPhone 14 Pro","iPhone 14 Pro Max","iPhone 14","iPhone 15","iPhone 15 Pro",
                   "iPhone 15 Pro Max","iPhone 16","iPhone 16 Pro","iPhone 16 Pro Max"];
  const devOpts = IPHONES.map((m,i) =>
    '<option value="'+i+'"'+(i===0?' selected':'')+'>'+escapeHtml("Apple "+m)+'</option>').join("") +
    '<option value="-1">自定义机型…</option>';
  const spotOpts = '<option value="-1">不写入 GPS</option>' + SZ_SPOTS.map((r,i) =>
    '<option value="'+i+'"'+(i===0?' selected':'')+'>'+escapeHtml(r[0])+'</option>').join("");

  panel.innerHTML =
    '<h4>视频写入拍摄信息（容器层写入，不重新编码，画质无损）</h4>' +
    '<div class="sgrid">' +
      '<label>拍摄设备<select id="vDev">'+devOpts+'</select></label>' +
      '<label>自定义机型（选"自定义"时生效）<input id="vDevText" type="text" placeholder="如 iPhone 17 Pro" ' +
        'style="margin-top:4px;width:100%;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--line2);border-radius:8px"></label>' +
      '<label>拍摄地点 GPS<select id="vLoc">'+spotOpts+'</select></label>' +
      '<label>拍摄时间（留空不写）<input id="vDay" type="datetime-local" ' +
        'style="margin-top:4px;width:100%;font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--line2);border-radius:8px"></label>' +
    '</div>' +
    '<div class="row"><button class="primary" id="vRun">写入</button>' +
    '<button id="vDown" disabled>下载写入结果</button>' +
    '<span class="stats" id="vNote"></span></div>' +
    '<div class="sdiff" id="vDiff" hidden></div>';

  const note = panel.querySelector("#vNote");
  const diff = panel.querySelector("#vDiff");
  const down = panel.querySelector("#vDown");
  const devSel = panel.querySelector("#vDev");
  const devText = panel.querySelector("#vDevText");
  const syncDevText = () => { devText.disabled = devSel.value !== "-1"; };
  devSel.addEventListener("change", syncDevText);
  syncDevText();
  note.textContent = "数据源：" + (card._cleanBytes ? "清理后的输出" : "原始文件");

  panel.querySelector("#vRun").addEventListener("click", async () => {
    const btn = panel.querySelector("#vRun");
    btn.disabled = true; btn.textContent = "写入中…";
    down.disabled = true;
    try{
      if(!card._bytes || !card._bytes.length) throw new Error("没有可用的文件数据");
      const model = devSel.value === "-1"
        ? devText.value.trim()
        : IPHONES[+devSel.value];
      if(!model) throw new Error("机型不能为空");
      const locIdx = +panel.querySelector("#vLoc").value;
      let lat = 0, lng = 0, locName = "";
      if(locIdx >= 0){
        const loc = jitterLoc({id:"v", name:SZ_SPOTS[locIdx][0], lat:SZ_SPOTS[locIdx][2],
                               lng:SZ_SPOTS[locIdx][3], alt:0});
        lat = loc.lat; lng = loc.lng; locName = loc.name;
      }
      const dayVal = panel.querySelector("#vDay").value;   // "2026-11-08T15:30"
      const created = dayVal ? dayVal + ":00+0800" : null;

      const res = await api("/video-tag", {
        name: fileName,
        data: u8ToB64(card._bytes),
        make: "Apple",
        model: model,
        writeGps: locIdx >= 0,
        lat: lat, lng: lng,
        created: created
      });
      if(!res.ok) throw new Error(res.error || "未知原因");

      card._tagBytes = bytesFromB64(res.data);
      card._bytes = card._tagBytes;
      down.disabled = false;
      note.textContent = "数据源：上次写入结果";

      diff.hidden = false;
      diff.innerHTML =
        '<b>已写入</b>　Apple ' + escapeHtml(model) +
        (locName ? ' ｜ ' + escapeHtml(locName) + ' ｜ ' + lat.toFixed(5) + ', ' + lng.toFixed(5) : ' ｜ 不写 GPS') +
        (created ? ' ｜ ' + escapeHtml(created) : '') + '<br>' +
        '<b>动作</b>　' + escapeHtml((res.actions || []).join(" ｜ ")) + '<br>' +
        '<b>体积</b>　' + human(card._bytes.length) + ' → ' + human(res.size);
      btn.textContent = "已写入 ✓";
      btn.disabled = false;
    }catch(e){
      btn.disabled = false; btn.textContent = "写入";
      const box = document.createElement("div");
      box.className = "err";
      box.textContent = "写入失败：" + e.message;
      panel.appendChild(box);
    }
  });

  down.addEventListener("click", () => {
    if(!card._tagBytes) return;
    const dot = fileName.lastIndexOf(".");
    const ext = dot > 0 ? fileName.slice(dot) : ".mp4";
    downloadBytes(card._tagBytes, baseName + ".tagged" + ext, "video/mp4");
  });
}

async function handleFile(file){
  const isVideo = /\.(mp4|m4v|mov)$/i.test(file.name);
  const card = document.createElement("div");
  card.className = "file";
  card.innerHTML =
    '<div class="fhead"><span class="fname"></span><span class="fsize"></span></div>' +
    '<div class="body"><span class="stats">正在检测…</span></div>';
  card.querySelector(".fname").textContent = file.name;
  card.querySelector(".fsize").textContent = human(file.size);
  list.prepend(card);
  const body = card.querySelector(".body");

  let preview = "";
  try { preview = URL.createObjectURL(file); } catch(e){}

  let data64;
  try { data64 = await b64(file); }
  catch(e){ body.innerHTML = '<div class="err">读取文件失败：' + e.message + '</div>'; return; }

  // 工作台要吃原始字节；清理成功后会把这里换成清理结果，形成「先清后写」的流水线
  card._bytes = bytesFromB64(data64);
  card._cleaned = false;
  const baseName = (file.name.lastIndexOf(".") > 0)
    ? file.name.slice(0, file.name.lastIndexOf(".")) : file.name;

  let ins;
  try { ins = await api("/inspect", {file: data64, name: file.name}); }
  catch(e){ body.innerHTML = '<div class="err">检测请求失败：' + e.message + '</div>'; return; }

  const r = ins.report || {};
  const findings = r.findings || [];
  const dirty = ins.ok && (r.has_c2pa || r.has_ai_metadata || findings.length);

  let h = "";
  if(preview) h += isVideo
    ? '<video class="thumb" src="' + preview + '" controls muted preload="metadata"></video>'
    : '<img class="thumb" src="' + preview + '" alt="">';
  h += '<div class="verdict">';
  if(!ins.ok){
    h += '<span class="v v-bad">检测失败：' + (ins.error || "未知原因") + '</span>';
  } else if(dirty){
    h += '<span class="v v-bad">发现溯源标记</span>';
    h += '<span class="v ' + (r.has_c2pa ? 'v-bad' : 'v-good') + '">C2PA：' + (r.has_c2pa ? "有" : "无") + '</span>';
    h += '<span class="v ' + (r.has_ai_metadata ? 'v-bad' : 'v-good') + '">AI 元数据：' + (r.has_ai_metadata ? "有" : "无") + '</span>';
  } else {
    h += '<span class="v v-good">未发现可剥离的溯源标记</span>';
    h += '<span class="v v-good">C2PA：无</span><span class="v v-good">AI 元数据：无</span>';
  }
  h += '</div>';

  if(findings.length){
    h += '<ul class="find">';
    for(const f of findings.slice(0,12)) h += '<li>' + escapeHtml(f) + '</li>';
    if(findings.length > 12) h += '<li>…还有 ' + (findings.length-12) + ' 条</li>';
    h += '</ul>';
  }

  if(ins.ok){
    h += '<div class="row">';
    h += '<button class="primary" id="go">清理</button>';
    h += '<button id="dl" disabled>下载清理结果</button>';
    if(isVideo){
      h += '<button id="vt">写入拍摄信息</button>';
    } else {
      h += '<button id="st">像素工作台处理</button>';
    }
    h += '<span class="stats">原文件 ' + human(file.size) + '</span>';
    h += '</div>';
  }
  body.innerHTML = h;

  const stBtn = body.querySelector("#st");
  if(stBtn) stBtn.addEventListener("click", () => openStudioPanel(card, body, baseName));

  const vtBtn = body.querySelector("#vt");
  if(vtBtn) vtBtn.addEventListener("click", () => openVideoPanel(card, body, baseName, file.name));

  // 下载与清理分离：先清理，再决定要不要下载
  const dl = body.querySelector("#dl");
  dl.addEventListener("click", () => {
    if(!card._cleanBytes) return;
    const dot = file.name.lastIndexOf(".");
    const base = dot > 0 ? file.name.slice(0, dot) : file.name;
    const ext  = dot > 0 ? file.name.slice(dot) : "";
    downloadBytes(card._cleanBytes, base + ".cleaned" + ext, file.type);
  });

  const go = body.querySelector("#go");
  if(go){
    go.addEventListener("click", async () => {
      go.disabled = true;
      const old = go.textContent;
      go.textContent = "清理中…";
      try{
        const options = {keep_non_ai_metadata: document.getElementById("keepmeta").checked};
        const res = await api("/clean", {file: data64, name: file.name, options});
        if(!res.ok){
          go.disabled = false; go.textContent = old;
          const box = document.createElement("div");
          box.className = "err";
          box.textContent = "清理失败：" + (res.error || "未知原因");
          body.insertBefore(box, body.querySelector(".row"));
          return;
        }
        // 清理结果单独存，下载交给「下载清理结果」按钮；
        // 同时它也成为工作台的输入，点「像素工作台处理」就接着处理这一版
        card._cleanBytes = bytesFromB64(res.cleaned);
        card._bytes = card._cleanBytes;
        card._cleaned = true;
        dl.disabled = false;

        go.textContent = "已清理 ✓";
        const acts = (res.report && res.report.actions) || [];
        const stats = document.createElement("p");
        stats.className = "stats";
        stats.style.margin = "10px 0 0";
        stats.textContent = acts.length
          ? human(file.size) + " -> " + human(card._cleanBytes.length) + " ｜ " + acts.join(" ｜ ")
          : human(file.size) + " -> " + human(card._cleanBytes.length);
        body.appendChild(stats);
      }catch(e){
        go.disabled = false; go.textContent = old;
        const box = document.createElement("div");
        box.className = "err";
        box.textContent = "清理请求失败：" + e.message;
        body.insertBefore(box, body.querySelector(".row"));
      }
    });
  }
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

const drop = document.getElementById("drop");
const pick = document.getElementById("pick");

drop.addEventListener("click", () => pick.click());
pick.addEventListener("change", () => {
  for(const f of pick.files) handleFile(f);
  pick.value = "";
});

["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove("hot");
}));
drop.addEventListener("drop", e => {
  for(const f of (e.dataTransfer.files || [])) handleFile(f);
});

document.getElementById("refresh").addEventListener("click", loadCaps);
loadCaps();
</script>
</body>
</html>
"""


def main() -> int:
    global TARGET
    ap = argparse.ArgumentParser(description="watermarks-remover 本地 Web UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--target", default=TARGET, help="上游服务地址")
    args = ap.parse_args()

    TARGET = args.target.rstrip("/")

    try:
        with urllib.request.urlopen(TARGET + "/health", timeout=5) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        print(f"上游服务正常：{TARGET} -> {info}")
    except Exception as exc:  # noqa: BLE001
        print(f"警告：连不上上游 {TARGET}（{exc}）", file=sys.stderr)
        print("      界面仍会启动，但检测/清理会失败。请先运行 ./run.sh", file=sys.stderr)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"界面已启动：http://{args.host}:{args.port}")
    print("按 Ctrl-C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
