"""只读样例卷：确定性生成一个镜像文件 + 清单（含每段期望 SHA-256）。

- 卷：``<volume_dir>/volume.bin``，按固定段大小切分；
- 清单：``<volume_dir>/manifest.json``，记录每段 offset/size/sha256；
- 校验机进程用 :func:`hash_segment` 以 O_RDONLY 方式逐段实算。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_SEGMENT_COUNT = 8
DEFAULT_SEGMENT_SIZE = 256 * 1024  # 256 KiB，演练时实算足够快
VOLUME_FILENAME = "volume.bin"
MANIFEST_FILENAME = "manifest.json"


def _deterministic_block(seed: bytes, counter: int, size: int) -> bytes:
    """用 SHA-256 链构造确定性、不可压缩也无所谓的数据块。"""
    out = bytearray()
    block_seed = seed + counter.to_bytes(8, "big")
    prev = block_seed
    while len(out) < size:
        prev = hashlib.sha256(prev).digest()
        out.extend(prev)
    return bytes(out[:size])


def generate_sample_volume(
    volume_dir: Path | str,
    segment_count: int = DEFAULT_SEGMENT_COUNT,
    segment_size: int = DEFAULT_SEGMENT_SIZE,
    *,
    force: bool = False,
) -> dict[str, Any]:
    volume_dir = Path(volume_dir)
    volume_dir.mkdir(parents=True, exist_ok=True)
    volume_path = volume_dir / VOLUME_FILENAME
    manifest_path = volume_dir / MANIFEST_FILENAME

    if not force and volume_path.exists() and manifest_path.exists():
        return load_manifest(volume_dir)

    seed = b"gsb-backup-drill-sample-volume-v1"
    segments: list[dict[str, Any]] = []
    hasher_total = hashlib.sha256()

    # 先写临时文件再改名，避免别的进程读到半成品
    tmp_path = volume_dir / (VOLUME_FILENAME + ".tmp")
    with open(tmp_path, "wb") as f:
        for number in range(1, segment_count + 1):
            offset = (number - 1) * segment_size
            blob = _deterministic_block(seed, number, segment_size)
            f.write(blob)
            hasher_total.update(blob)
            segments.append(
                {
                    "number": number,
                    "offset": offset,
                    "size": segment_size,
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
            )
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, volume_path)
    os.chmod(volume_path, 0o444)

    manifest = {
        "volume_file": VOLUME_FILENAME,
        "read_only": True,
        "segment_size": segment_size,
        "segment_count": segment_count,
        "total_bytes": segment_count * segment_size,
        "volume_sha256": hasher_total.hexdigest(),
        "segments": segments,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.chmod(manifest_path, 0o444)
    try:
        os.chmod(volume_dir, 0o555)
    except PermissionError:
        # 某些文件系统/挂载不允许改目录权限，读时仍以 O_RDONLY 打开
        pass
    return manifest


def ensure_sample_volume(volume_dir: Path | str) -> dict[str, Any]:
    volume_dir = Path(volume_dir)
    manifest_path = volume_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return generate_sample_volume(volume_dir)
    return load_manifest(volume_dir)


def load_manifest(volume_dir: Path | str) -> dict[str, Any]:
    manifest_path = Path(volume_dir) / MANIFEST_FILENAME
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def volume_path_of(volume_dir: Path | str, manifest: dict[str, Any]) -> Path:
    return Path(volume_dir) / manifest.get("volume_file", VOLUME_FILENAME)


def hash_segment(
    volume_path: Path | str, offset: int, size: int, chunk_size: int = 64 * 1024
) -> tuple[str, int, float]:
    """以只读方式打开卷，读取 [offset, offset+size) 并实算 SHA-256。

    返回 (hexdigest, 实际读取字节数, 实算墙钟毫秒)。
    """
    path = Path(volume_path)
    fd = os.open(str(path), os.O_RDONLY)
    digest = hashlib.sha256()
    read_total = 0
    started = time.perf_counter()
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(chunk_size, remaining))
            if not chunk:
                break
            digest.update(chunk)
            got = len(chunk)
            read_total += got
            remaining -= got
    finally:
        os.close(fd)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return digest.hexdigest(), read_total, elapsed_ms


if __name__ == "__main__":
    # 允许 python -m app.volume --dir sample_volume [--force]
    import argparse

    parser = argparse.ArgumentParser(description="生成只读样例卷与清单")
    parser.add_argument("--dir", default=None, help="样例卷目录")
    parser.add_argument("--segments", type=int, default=DEFAULT_SEGMENT_COUNT)
    parser.add_argument("--segment-size", type=int, default=DEFAULT_SEGMENT_SIZE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    from . import config

    target = Path(args.dir) if args.dir else config.VOLUME_DIR
    m = generate_sample_volume(
        target, args.segments, args.segment_size, force=args.force
    )
    print(
        f"sample volume ready: {target / VOLUME_FILENAME} "
        f"segments={m['segment_count']} total_bytes={m['total_bytes']} "
        f"volume_sha256={m['volume_sha256'][:16]}..."
    )
