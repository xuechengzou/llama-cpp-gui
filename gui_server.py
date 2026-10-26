#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llama.cpp 服务管理器 —— 本地 Web GUI 后端

为什么用 Web UI 而不是 tkinter：
  本机自带的 Tk 8.6 编译时未启用 Xft，无法使用 fontconfig 字体，
  中文字形渲染不可靠。浏览器渲染则完全没有这个问题，且界面更好看。

启动后会打开 http://127.0.0.1:8099
"""

import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── 路径与常量 ───────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

LLAMA_BIN_DIR = Path.home() / "deepseek_workspace/deepseek_harness/llama.cpp/build-musa/bin"
LLAMA_SERVER = LLAMA_BIN_DIR / "llama-server"

MUSA_LIB = "/usr/local/musa/lib"
MUSA_INFO = "/usr/local/musa/bin/musaInfo"

MODEL_DIRS = [
    Path.home() / "models",
    Path.home() / ".cache" / "llama.cpp",
]

GUI_PORT = 8099

PRESETS = {
    "推荐": {
        "ctx": 16384, "parallel": 1, "fa": "on",
        "ctk": "q8_0", "ctv": "q8_0",
        "desc": "日常对话，显存充裕",
    },
    "长上下文": {
        "ctx": 65536, "parallel": 1, "fa": "on",
        "ctk": "q8_0", "ctv": "q8_0",
        "desc": "长文档处理",
    },
    "超长上下文": {
        "ctx": 131072, "parallel": 1, "fa": "on",
        "ctk": "q8_0", "ctv": "q8_0",
        "desc": "极限长度，显存余量很小",
    },
    "省显存": {
        "ctx": 8192, "parallel": 1, "fa": "on",
        "ctk": "q8_0", "ctv": "q8_0",
        "desc": "同时开浏览器 / QQ",
    },
    "高保真": {
        "ctx": 16384, "parallel": 1, "fa": "on",
        "ctk": "f16", "ctv": "f16",
        "desc": "KV 不量化，质量优先",
    },
}

KV_BYTES = {"f16": 2.0, "bf16": 2.0, "f32": 4.0, "q8_0": 1.06,
            "q4_0": 0.56, "q4_1": 0.62, "iq4_nl": 0.56,
            "q5_0": 0.69, "q5_1": 0.75}


# ── GGUF 解析 ────────────────────────────────────────────────

class GgufReader:
    _SCALAR = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4,
               7: 1, 10: 8, 11: 8, 12: 8}
    STRING, ARRAY = 8, 9

    def __init__(self, path):
        self.f = open(path, "rb")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        try:
            self.f.close()
        except Exception:
            pass

    def _u32(self):
        return int.from_bytes(self.f.read(4), "little")

    def _u64(self):
        return int.from_bytes(self.f.read(8), "little")

    def _str(self):
        return self.f.read(self._u64()).decode("utf-8", "replace")

    def _skip(self, t):
        if t in self._SCALAR:
            self.f.read(self._SCALAR[t])
        elif t == self.STRING:
            self.f.read(self._u64())
        elif t == self.ARRAY:
            it, n = self._u32(), self._u64()
            if it == self.STRING:
                for _ in range(n):
                    self.f.read(self._u64())
            elif it in self._SCALAR:
                self.f.read(self._SCALAR[it] * n)
            else:
                raise ValueError("nested")
        else:
            raise ValueError("type")

    def parse(self, limit=300):
        if self.f.read(4) != b"GGUF":
            raise ValueError("not gguf")
        self._u32()
        self._u64()
        nkv = self._u64()
        meta = {}
        for _ in range(min(nkv, limit)):
            key = self._str()
            t = self._u32()
            if t in (0, 2, 4, 10):
                meta[key] = int.from_bytes(self.f.read(self._SCALAR[t]), "little")
            elif t in (1, 3, 5, 11):
                meta[key] = int.from_bytes(self.f.read(self._SCALAR[t]),
                                           "little", signed=True)
            elif t == 6:
                meta[key] = struct.unpack("<f", self.f.read(4))[0]
            elif t == 12:
                meta[key] = struct.unpack("<d", self.f.read(8))[0]
            elif t == 7:
                meta[key] = bool(self.f.read(1)[0])
            elif t == 8:
                meta[key] = self._str()
            else:
                self._skip(t)
        return meta


def read_gguf_meta(path):
    try:
        with GgufReader(path) as r:
            return r.parse()
    except Exception:
        return {}


def model_params(path):
    """提取模型架构参数，用于显存估算。"""
    meta = read_gguf_meta(path)
    arch = meta.get("general.architecture", "")
    d = dict(
        arch=arch or "unknown",
        layers=meta.get(f"{arch}.block_count"),
        heads=meta.get(f"{arch}.attention.head_count"),
        kv_heads=meta.get(f"{arch}.attention.head_count_kv"),
        embd=meta.get(f"{arch}.embedding_length"),
        k_len=meta.get(f"{arch}.attention.key_length"),
        v_len=meta.get(f"{arch}.attention.value_length"),
        ctx_train=meta.get(f"{arch}.context_length"),
    )
    if d["kv_heads"] is None:
        d["kv_heads"] = d["heads"]
    if isinstance(d["kv_heads"], list):
        d["kv_heads"] = max(d["kv_heads"]) if d["kv_heads"] else None
    return d


# ── 系统信息 ─────────────────────────────────────────────────

def vram_info():
    try:
        out = subprocess.run([MUSA_INFO], capture_output=True, text=True,
                             timeout=4).stdout
        total = free = None
        for line in out.splitlines():
            m = re.search(r"memInfo\.total:\s*([\d.]+)\s*GB", line)
            if m:
                total = float(m.group(1))
            m = re.search(r"memInfo\.free:\s*([\d.]+)\s*GB", line)
            if m:
                free = float(m.group(1))
        if total and free is not None:
            return {"total": total, "free": free, "used": total - free}
    except Exception:
        pass
    return None


def port_in_use(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def find_models():
    out, seen = [], set()
    for d in MODEL_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.gguf")):
            try:
                rp = p.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                st = p.stat()
                out.append({
                    "path": str(p),
                    "name": p.name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
            except OSError:
                continue
    return out


# ── 服务管理 ─────────────────────────────────────────────────

class ServerManager:
    def __init__(self):
        self.proc = None
        self.lines = deque(maxlen=4000)     # [(seq, text, tag)]
        self.seq = 0
        self.lock = threading.Lock()
        self.started_at = None
        self.port = None
        self.status = "idle"                # idle | starting | ready | error
        self.message = ""
        self.running_cfg = None             # 正在运行的实际配置
        self._reader = None
        self._watcher = None

    # 日志
    def log(self, text, tag=""):
        with self.lock:
            for chunk in text.splitlines() or [""]:
                self.seq += 1
                self.lines.append((self.seq, chunk, tag))

    def logs_since(self, since):
        with self.lock:
            return [(s, t, g) for (s, t, g) in self.lines if s > since]

    # 状态
    def snapshot(self):
        running = self.proc is not None and self.proc.poll() is None
        return {
            "status": self.status if running else ("idle" if self.status == "idle" else self.status),
            "running": running,
            "message": self.message,
            "port": self.port,
            "uptime": int(time.time() - self.started_at) if self.started_at and running else 0,
            "seq": self.seq,
            "running_cfg": self.running_cfg if running else None,
        }

    # 启动
    def start(self, cfg):
        if self.proc is not None and self.proc.poll() is None:
            return {"ok": False, "error": "服务已在运行"}

        # 校验
        try:
            model = cfg["model"]
            ngl = int(cfg["ngl"])
            ctx = int(cfg["ctx"])
            parallel = int(cfg["parallel"])
            ub = int(cfg["ub"])
            port = int(cfg["port"])
        except (KeyError, ValueError) as e:
            return {"ok": False, "error": f"参数无效: {e}"}

        if not Path(model).exists():
            return {"ok": False, "error": f"模型不存在: {model}"}
        if ctx <= 0:
            return {"ok": False, "error": "上下文必须大于 0"}
        if port_in_use(port):
            return {"ok": False, "error": f"端口 {port} 已被占用"}

        fa = cfg.get("fa", "on")
        ctk = cfg.get("ctk", "q8_0")
        ctv = cfg.get("ctv", "q8_0")

        # 实测约束：S80 上 KV 量化必须配合 Flash Attention
        fa_adjusted = False
        if ((ctk != "f16") or (ctv != "f16")) and fa in ("auto", "off"):
            fa = "on"
            fa_adjusted = True

        args = [str(LLAMA_SERVER), "-m", model,
                "-ngl", str(ngl),
                "-c", str(ctx),
                "--parallel", str(parallel),
                "-ub", str(ub)]
        if fa != "auto":
            args += ["-fa", fa]
        args += ["-ctk", ctk, "-ctv", ctv,
                 "--host", "127.0.0.1", "--port", str(port)]

        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = f"{MUSA_LIB}:{LLAMA_BIN_DIR}:" + env.get("LD_LIBRARY_PATH", "")

        self.log("", "dim")
        self.log("$ " + " ".join([Path(args[0]).name] + args[1:]), "cmd")
        if fa_adjusted:
            self.log("[自动] KV 量化需要 Flash Attention，已把 -fa 改为 on", "warn")

        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, cwd=str(LLAMA_BIN_DIR), text=True, bufsize=1)
        except Exception as e:
            self.status = "error"
            self.message = str(e)
            return {"ok": False, "error": str(e)}

        self.started_at = time.time()
        self.port = port
        self.status = "starting"
        self.message = "启动中"
        self.running_cfg = {
            "model": model, "ngl": ngl, "ctx": ctx,
            "parallel": parallel, "ub": ub,
            "fa": fa, "ctk": ctk, "ctv": ctv, "port": port,
            "argv": args,
        }

        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._watcher = threading.Thread(target=self._wait_ready,
                                         args=(port,), daemon=True)
        self._watcher.start()
        return {"ok": True, "pid": self.proc.pid, "fa_adjusted": fa_adjusted}

    def _read(self):
        proc = self.proc
        if not proc or not proc.stdout:
            return
        keys = re.compile(r"error|warn|listening|loaded|musa|context|buffer|"
                          r"fatal|failed|quantiz|flash", re.I)
        for line in proc.stdout:
            low = line.lower()
            tag = ""
            if "error" in low or "fatal" in low or "failed" in low:
                tag = "err"
            elif "warn" in low or "quantiz" in low or "flash attention" in low:
                tag = "warn"
            elif "listening" in low or "model loaded" in low:
                tag = "ok"
            # 全量记录，前端可切换过滤
            self.log(line.rstrip(), tag)
        # 进程结束：等退出码确定
        try:
            code = proc.wait(timeout=3)
        except Exception:
            code = proc.poll()
        if self.status not in ("idle",):
            if code == 0:
                self.status = "idle"
                self.message = "已停止"
            else:
                self.status = "error"
                self.message = f"异常退出（代码 {code}）"
                self.log(f"[退出] 进程以代码 {code} 结束", "err")
                # 按日志内容给出针对性提示，而不是一律归因于显存
                hints = self._diagnose()
                for h in hints:
                    self.log(h, "warn")

    def _diagnose(self):
        """根据已捕获的日志给出失败原因提示。"""
        with self.lock:
            text = "\n".join(t for _, t, _ in self.lines).lower()

        if "unknown pre-tokenizer type" in text or "unknown model architecture" in text:
            import re as _re
            kind = "分词器" if "pre-tokenizer" in text else "模型架构"
            m = _re.search(r"unknown (?:pre-tokenizer type|model architecture): '([^']+)'", text)
            name = m.group(1) if m else "未知"
            return [
                f"[诊断] 当前 llama.cpp 版本不支持这个模型的{kind}：{name}",
                "[建议] 这不是显存问题。需要满足以下之一：",
                "  · 换一个架构受支持的模型（如 Qwen3 系列的 qwen3）",
                "  · 或重新编译更新版本的 llama.cpp 以支持该架构",
                "[参考] 在 GUI 里点模型的下拉框换其他模型即可。",
            ]

        if "quantized v cache was requested" in text:
            return [
                "[诊断] KV 量化需要 Flash Attention，但 FA 未启用。",
                "[建议] 把 FlashAttn 设为 on（GUI 通常会自动纠正，"
                "若手动改过后请复位）。",
            ]

        if "out of memory" in text or "invalid argument" in text \
                or "musafuncsetattribute" in text or "failed to allocate" in text:
            return [
                "[诊断] 显存不足或共享内存超限。",
                "[建议] 换「省显存」预设，或调小上下文 / 批大小。",
                "[注意] 本机单卡同时负责显示，显存耗尽会导致整个桌面崩溃。",
            ]

        if "port" in text and ("address already in use" in text or "bind" in text):
            return ["[诊断] 端口被占用，请换一个端口。"]

        return [
            "[诊断] 模型加载失败，请查看上方日志中的 error 行。",
            "[建议] 常见原因：模型架构不受支持、文件损坏、参数不兼容。",
        ]

    def _wait_ready(self, port):
        import urllib.request
        deadline = time.time() + 240
        url = f"http://127.0.0.1:{port}/health"
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                return
            try:
                with urllib.request.urlopen(url, timeout=1.2) as r:
                    if r.status == 200:
                        self.status = "ready"
                        self.message = f"运行中 · 端口 {port}"
                        self.log("[就绪] 服务已可访问", "ok")
                        return
            except Exception:
                pass
            time.sleep(0.7)
        self.status = "error"
        self.message = "启动超时"

    # 停止
    def stop(self):
        if self.proc is None or self.proc.poll() is not None:
            self.status = "idle"
            self.message = "已停止"
            return {"ok": True, "note": "未在运行"}

        self.log("[停止] 正在关闭…", "dim")
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.log("[停止] 优雅退出超时，强制终止", "warn")
                self.proc.kill()
                self.proc.wait(timeout=5)
        except Exception as e:
            self.log(f"[停止] 出错: {e}", "err")

        self.proc = None
        self.status = "idle"
        self.message = "已停止"
        self.started_at = None
        self.running_cfg = None
        self.log("[停止] 服务已关闭", "dim")
        return {"ok": True}


MANAGER = ServerManager()


# ── HTTP 处理 ────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静音，避免刷屏

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        if path in ("/", "/index.html"):
            f = STATIC / "index.html"
            if not f.exists():
                return self._send(500, {"error": "static/index.html 缺失"})
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

        if path.startswith("/static/"):
            name = path[len("/static/"):]
            f = (STATIC / name).resolve()
            if not str(f).startswith(str(STATIC.resolve())) or not f.exists():
                return self._send(404, {"error": "not found"})
            ctype = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }.get(f.suffix, "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype)

        if path == "/api/state":
            since = int(qs.get("since", ["0"])[0])
            return self._send(200, {
                "models": find_models(),
                "vram": vram_info(),
                "presets": PRESETS,
                "server": MANAGER.snapshot(),
                "logs": MANAGER.logs_since(since),
                "llama": {
                    "server_exists": LLAMA_SERVER.exists(),
                    "server_path": str(LLAMA_SERVER),
                },
            })

        if path == "/api/vram":
            return self._send(200, {"vram": vram_info()})

        if path == "/api/model-info":
            mp = qs.get("path", [""])[0]
            if not mp or not Path(mp).exists():
                return self._send(400, {"error": "invalid path"})
            return self._send(200, {"params": model_params(Path(mp))})

        if path == "/api/port-check":
            try:
                p = int(qs.get("port", ["8080"])[0])
            except ValueError:
                return self._send(400, {"error": "bad port"})
            return self._send(200, {"port": p, "in_use": port_in_use(p)})

        if path == "/api/health":
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/api/start":
            return self._send(200, MANAGER.start(body))

        if path == "/api/stop":
            return self._send(200, MANAGER.stop())

        if path == "/api/restart":
            MANAGER.stop()
            time.sleep(0.8)
            return self._send(200, MANAGER.start(body))

        if path == "/api/clear-log":
            with MANAGER.lock:
                MANAGER.lines.clear()
            return self._send(200, {"ok": True})

        if path == "/api/estimate":
            try:
                mp = body["model"]
                ctx = int(body["ctx"])
                ctk = body.get("ctk", "f16")
                ctv = body.get("ctv", "f16")
            except (KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})

            p = Path(mp)
            if not p.exists():
                return self._send(400, {"error": "model not found"})

            prm = model_params(p)
            layers = prm["layers"] or 36
            kvh = prm["kv_heads"] or 8
            if prm["k_len"] and prm["v_len"]:
                dk, dv = prm["k_len"], prm["v_len"]
                src = "GGUF 精确值"
            elif prm["heads"] and prm["embd"]:
                dk = dv = prm["embd"] // prm["heads"]
                src = "按 head 推算"
            else:
                dk = dv = 128
                src = "经验兜底"

            per_tok = layers * (kvh * dk * KV_BYTES.get(ctk, 2.0)
                                + kvh * dv * KV_BYTES.get(ctv, 2.0))
            model_gb = p.stat().st_size / (1024 ** 3)
            kv_gb = per_tok * ctx / (1024 ** 3)
            compute = model_gb * 0.15 + 0.6
            return self._send(200, {
                "model": round(model_gb, 2),
                "kv": round(kv_gb, 2),
                "compute": round(compute, 2),
                "total": round(model_gb + kv_gb + compute, 2),
                "layers": layers,
                "kv_heads": kvh,
                "dim": f"{dk}/{dv}",
                "per_tok_kib": round(per_tok / 1024, 1),
                "source": src,
                "arch": prm["arch"],
            })

        if path == "/api/open":
            port = body.get("port", MANAGER.port or 8080)
            webbrowser.open(f"http://127.0.0.1:{port}")
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def main():
    if not LLAMA_SERVER.exists():
        print(f"错误：找不到 {LLAMA_SERVER}", file=sys.stderr)
        sys.exit(1)

    # GUI 端口被占用时自动往后找
    global GUI_PORT
    while port_in_use(GUI_PORT) and GUI_PORT < 8110:
        GUI_PORT += 1

    srv = ThreadingHTTPServer(("127.0.0.1", GUI_PORT), Handler)
    srv.daemon_threads = True

    url = f"http://127.0.0.1:{GUI_PORT}"
    print(f"llama.cpp 服务管理器")
    print(f"  界面地址: {url}")
    print(f"  按 Ctrl+C 退出")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
        MANAGER.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
