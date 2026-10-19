#!/usr/bin/env python3
"""透明读取 JSONL(含 .jsonl.zst 压缩)。

优先 python zstandard,其次 Node 的 zlib.zstdDecompress,再次 `zstd` CLI;
三者都没有时返回 None 让调用方明确报告"压缩文件未解码",绝不静默当成空文件。

同一压缩文件在同一次运行里会被多个阶段扫描(时间分布、标记计数、独立复核),
所以解压结果按 (路径, mtime, 大小) 缓存;普通 .jsonl 逐行流式读取,不占内存。
"""

import collections
import json
import os
import shutil
import subprocess

_NODE_SNIPPET = (
    "const z=require('zlib'),f=require('fs');"
    "const b=z.zstdDecompressSync(f.readFileSync(process.argv[1]));"
    "process.stdout.write(b);"
)
CHUNK = 1 << 20
FIRST_CHUNK = 1 << 16
CACHE_BUDGET_BYTES = 96 * 1024 * 1024


def zstd_backend():
    try:
        import zstandard  # noqa: F401

        return "python-zstandard"
    except ImportError:
        pass
    if shutil.which("node"):
        return "node-zlib"
    if shutil.which("zstd"):
        return "zstd-cli"
    return None


def _decompress(path, backend):
    if backend == "python-zstandard":
        import zstandard

        chunks = []
        with open(path, "rb") as fh, zstandard.ZstdDecompressor().stream_reader(fh) as reader:
            while True:
                block = reader.read(CHUNK)
                if not block:
                    break
                chunks.append(block)
        return b"".join(chunks).decode("utf-8", "replace")
    if backend == "node-zlib":
        proc = subprocess.run(["node", "-e", _NODE_SNIPPET, path], capture_output=True, timeout=180)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
            raise RuntimeError(detail or "node 解压失败")
        return proc.stdout.decode("utf-8", "replace")
    if backend == "zstd-cli":
        proc = subprocess.run(["zstd", "-dc", path], capture_output=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError("zstd 解压失败")
        return proc.stdout.decode("utf-8", "replace")
    raise RuntimeError("没有可用的 zstd 后端")


# (path, mtime_ns, size, backend) -> 解压文本;按字节预算做 LRU 淘汰
_TEXT_CACHE = collections.OrderedDict()


def _cache_put(key, text):
    if len(text) > CACHE_BUDGET_BYTES:
        return  # 单条就超预算时不要塞进来:否则刚存就把自己挤掉,等于白占一次拷贝
    _TEXT_CACHE[key] = text
    _TEXT_CACHE.move_to_end(key)
    while _TEXT_CACHE and sum(len(v) for v in _TEXT_CACHE.values()) > CACHE_BUDGET_BYTES:
        _TEXT_CACHE.popitem(last=False)


def _decompress_prefix(path, backend, limit=FIRST_CHUNK):
    """只解压文件开头,用于取首行 session_meta。

    codex 的索引阶段要对每个 rollout 读第一行,而本机一半以上是 .zst:
    全量解压 170MB 只为拿几十个首行是最贵的做法。只有能安全截断的后端才走这条路,
    其余后端返回 None 让调用方回退到完整解压(慢但正确)。
    """
    if backend != "python-zstandard":
        return None
    try:
        import zstandard

        with open(path, "rb") as fh, zstandard.ZstdDecompressor().stream_reader(fh) as reader:
            return reader.read(limit)
    except Exception:
        return None  # 预读失败就回退到完整解压,让损坏文件在正常路径上给出 DecodeError


def cache_stats():
    return {"entries": len(_TEXT_CACHE), "bytes": sum(len(v) for v in _TEXT_CACHE.values()),
            "budget_bytes": CACHE_BUDGET_BYTES}


class DecodeError(RuntimeError):
    """压缩文件解码失败 —— 调用方必须把它计入覆盖度告警,不能当成"没有数据"。"""


class LineSource:
    """按行产出文本;对 .zst 解压一次并按缓存复用。

    backend 显式传入时(包括 None,表示确实没有可用后端)不再自动探测;
    只有省略该参数才现场探测。
    """

    _UNSET = object()

    def __init__(self, path, backend=_UNSET):
        self.path = os.fspath(path)
        self.compressed = self.path.endswith(".zst")
        self.backend = zstd_backend() if backend is LineSource._UNSET else backend
        self.error = None

    def _text(self):
        if not self.backend:
            raise DecodeError(f"{os.path.basename(self.path)} 是 .zst 但没有可用的 zstd 后端")
        try:
            st = os.stat(self.path)
            key = (self.path, st.st_mtime_ns, st.st_size, self.backend)
        except OSError as exc:
            raise DecodeError(f"无法读取 {self.path}: {exc}")
        if key in _TEXT_CACHE:
            _TEXT_CACHE.move_to_end(key)
            return _TEXT_CACHE[key]
        try:
            text = _decompress(self.path, self.backend)
        except DecodeError:
            raise
        except Exception as exc:
            # zstandard 的 ZstdError 既不是 OSError 也不是 ValueError(它直接继承 Exception):
            # 损坏/截断的 .zst 必须在这里收敛成 DecodeError,否则会逃到顶层,
            # 让整份 fact pack 不落盘 —— 一个坏分片不该毁掉整个会话。
            raise DecodeError(f"{os.path.basename(self.path)} 解压失败: {type(exc).__name__}: {exc}")
        _cache_put(key, text)
        return text

    def lines(self):
        """逐行产出。解码失败会抛 DecodeError,由调用方显式上报而不是返回空。"""
        if self.compressed:
            return iter(self._text().splitlines())

        def stream():
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    yield line

        return stream()

    def first_line(self):
        if self.compressed:
            head = _decompress_prefix(self.path, self.backend)
            if head:
                text = head.decode("utf-8", "replace")
                cut = text.find("\n")
                if cut >= 0:
                    return text[:cut]
                # 首行比预读窗口还长:必须回退到完整解压,绝不能把截断的行交给调用方解析
        for line in self.lines():
            return line
        return None


if __name__ == "__main__":
    import sys

    # 诊断入口:不带参数只报告后端可用性(不碰任何用户数据),给了路径就检查那一个文件
    if len(sys.argv) < 2:
        print("backend =", zstd_backend() or "none")
        print("用法: python jsonl_reader.py [--backend] <jsonl 或 jsonl.zst 路径>")
        sys.exit(0)
    if sys.argv[1] == "--backend":
        print(zstd_backend() or "none")
        sys.exit(0)
    sample = sys.argv[1]
    src = LineSource(sample)
    try:
        first = src.first_line()
    except DecodeError as exc:
        print(f"解码失败: {exc}")
        sys.exit(1)
    print("file =", os.path.basename(sample), "size =", os.path.getsize(sample))
    print("first_line is None:", first is None)
    if first:
        rec = json.loads(first)
        pl = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        print("type =", rec.get("type"), "sid =", pl.get("session_id") or pl.get("id"), "ts =", rec.get("timestamp"))
        print("行数 =", sum(1 for _ in src.lines()), "| 缓存 =", cache_stats())
