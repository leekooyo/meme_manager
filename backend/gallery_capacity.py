# -*- coding: utf-8 -*-
"""[patcher:gallery-capacity] meme_manager —— 图库容量上限（每包独立，超出删除最旧）

设计（2026-10-12 用户拍板）
--------------------------
- **粒度** (Q1=b)：**每个表情包各自限额**，不是全局合计。实际增长只发生在
  `stealer-migrated`，看护效果与全局等价，又不会误删闲置的社区包。
- **顺序** (Q2=a)：`st_mtime_ns` 升序（=进入文件夹时间，与前端 `emoji_mtimes`
  排序同口径）；同 mtime 用文件名升序做稳定 tiebreaker（整包导入/迁移来的
  图片 mtime 会被压成同一时刻，此时退化为文件名序）。
- **触发** (Q3=d)：① 每次落盘后检查（防抖：同一包 `DEBOUNCE_SECONDS` 秒内只
  统计一次）② 手动指令 `/表情管理 清理超额 [--dry]`。
- **删除** (Q4=c)：永久 `unlink()`（上限的意义就是回收磁盘），每次删除追加一行
  JSONL 日志到 `plugin_data/meme_manager/logs/capacity_prune.jsonl`，便于事后
  追溯「谁被删了」。手动指令支持 `--dry` 干跑出清单再删。
- **配置** (Q5=a)：独立分组 `gallery_capacity.enabled` / `gallery_capacity.max_images`
  （`0` = 不限）。本模块**直接读配置文件**，不需要插件实例，故三处落盘点
  （手动上传 / 自动收集 / 整包导入）都能直接调用。
- **越界分类** (Q10=a)：不跳过 `needs_review`，它同样占体积，该删就删。
- **联动** (Q11=a)：删完调 `invalidate_semantic_metadata`，其余缓存（dHash 索引 /
  预览缓存）按「文件是否存在」自愈，与「清空指定类型」口径一致。

配置读取
--------
优先读 `get_astrbot_data_path()/config/meme_manager_config.json`（UTF-8 BOM + CRLF
JSON）；缺键 / 非法值一律回退本模块默认值（`enabled=True` / `max_images=10000`）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..config import (
    DEFAULT_PLUGIN_NAME,
    PACKS_DIR,
    PLUGIN_DATA_DIR,
    get_astrbot_data_path,
)
from .packs.resolver import get_pack_paths

DEFAULT_ENABLED = True
DEFAULT_MAX_IMAGES = 10000
DEBOUNCE_SECONDS = 8.0

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

CAPACITY_LOG_DIR = PLUGIN_DATA_DIR / "logs"
CAPACITY_LOG_PATH = CAPACITY_LOG_DIR / "capacity_prune.jsonl"
CAPACITY_LOG_MAX_BYTES = 8 * 1024 * 1024

# 防抖：包 id -> 上次统计时刻（单调钟）。模块级，重载即重置（可接受）。
_LAST_CHECK: dict[str, float] = {}


# ------------------------------------------------------------------ 配置
def capacity_config_path(config_path: str | Path | None = None) -> Path:
    """返回 meme_manager 的配置值文件路径。"""
    if config_path:
        return Path(config_path)
    try:
        root = Path(get_astrbot_data_path())
    except Exception:
        root = PLUGIN_DATA_DIR.parents[1]
    return root / "config" / f"{DEFAULT_PLUGIN_NAME}_config.json"


def _to_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(fallback)


def _to_int(value: Any, fallback: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(fallback)


def read_capacity_settings(config_path: str | Path | None = None) -> dict:
    """读取 `gallery_capacity` 配置；缺键 / 非法值回退默认。"""
    data: Any = {}
    path = capacity_config_path(config_path)
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except Exception:
        data = {}
    node = data.get("gallery_capacity") if isinstance(data, dict) else None
    node = node if isinstance(node, dict) else {}
    limit = max(0, _to_int(node.get("max_images"), DEFAULT_MAX_IMAGES))
    return {
        "enabled": _to_bool(node.get("enabled"), DEFAULT_ENABLED),
        "max_images": limit,
        "path": str(path),
    }


# ------------------------------------------------------------------ 扫描
def _iter_images(memes_dir: Path) -> list[Path]:
    """列出 memes 目录下所有分类里的图片文件（不含隐藏 / 临时文件）。"""
    root = Path(memes_dir)
    if not root.is_dir():
        return []
    images: list[Path] = []
    try:
        categories = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for category_dir in categories:
        if not category_dir.is_dir() or category_dir.is_symlink():
            continue
        try:
            entries = list(category_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES:
                    images.append(entry)
            except OSError:
                continue
    return images


def _age_key(path: Path):
    """排序键：进入时间升序，同时间用文件名升序兜底。"""
    try:
        return (path.stat().st_mtime_ns, path.name.lower())
    except OSError:
        return (float("inf"), path.name.lower())


def pack_memes_dir(pack_id: str) -> Path:
    return Path(get_pack_paths(str(pack_id))["memes_dir"])


def list_pack_ids() -> list[str]:
    if not PACKS_DIR.is_dir():
        return []
    # 跳过隐藏目录（例如误落在 packs/ 下的 .workbuddy）——它们不是表情包，
    # 既不该被统计，也不该被「表情包」随机发图取到（2026-10-12 实测发现 packs/.workbuddy）。
    return sorted(
        p.name
        for p in PACKS_DIR.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
    )


# ------------------------------------------------------------------ 报告 / 计划
def pack_capacity_report(
    pack_id: str,
    *,
    max_images: int | None = None,
    config_path: str | Path | None = None,
) -> dict:
    """返回某包的 (张数, 上限, 超额)。"""
    settings = read_capacity_settings(config_path)
    limit = settings["max_images"] if max_images is None else max(0, int(max_images))
    memes_dir = pack_memes_dir(pack_id)
    count = len(_iter_images(memes_dir))
    excess = max(0, count - limit) if limit > 0 else 0
    return {
        "pack_id": str(pack_id),
        "memes_dir": str(memes_dir),
        "count": count,
        "limit": limit,
        "excess": excess,
    }


def plan_prune(
    pack_id: str,
    *,
    max_images: int | None = None,
    config_path: str | Path | None = None,
) -> dict:
    """算出「该删哪些」。返回 victims = 按最旧优先排好的路径列表。"""
    report = pack_capacity_report(
        pack_id, max_images=max_images, config_path=config_path
    )
    if report["limit"] <= 0 or report["excess"] <= 0:
        return {**report, "victims": []}
    ordered = sorted(_iter_images(Path(report["memes_dir"])), key=_age_key)
    return {**report, "victims": ordered[: report["excess"]]}


def _describe(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {"category": path.parent.name, "file": path.name, "size": 0, "mtime": 0.0}
    return {
        "category": path.parent.name,
        "file": path.name,
        "size": int(stat.st_size),
        "mtime": float(stat.st_mtime),
    }


def _append_prune_log(records: list[dict]) -> None:
    if not records:
        return
    try:
        CAPACITY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if (
                CAPACITY_LOG_PATH.is_file()
                and CAPACITY_LOG_PATH.stat().st_size > CAPACITY_LOG_MAX_BYTES
            ):
                CAPACITY_LOG_PATH.replace(CAPACITY_LOG_PATH.with_suffix(".jsonl.1"))
        except OSError:
            pass
        with CAPACITY_LOG_PATH.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # 日志失败绝不阻断清理
        logger.warning("[meme_manager] 容量清理日志写入失败：%s", exc)


def _invalidate_semantics(pack_id: str) -> None:
    try:
        from .semantic.storage import invalidate_semantic_metadata
    except Exception:
        return
    pack_dir = Path(get_pack_paths(str(pack_id))["pack_dir"])
    if not (pack_dir / "semantic_metadata.json").is_file():
        return
    try:
        invalidate_semantic_metadata(pack_dir)
    except Exception as exc:
        logger.warning("[meme_manager] 容量清理后刷新语义元数据失败：%s", exc)


# ------------------------------------------------------------------ 执行
def enforce_pack_capacity(
    pack_id: str,
    *,
    max_images: int | None = None,
    enabled: bool | None = None,
    dry_run: bool = False,
    force: bool = False,
    reason: str = "auto",
    config_path: str | Path | None = None,
) -> dict:
    """对单个包执行超额清理。

    Args:
        pack_id: 目标表情包 ID。
        max_images: 覆盖配置里的上限（0 = 不限）。
        enabled: 覆盖配置里的总开关。
        dry_run: 只出清单，不删。
        force: 跳过防抖（手动指令用）。
        reason: 写入日志的来源标记（auto / import / manual ...）。
        config_path: 覆盖配置文件路径（一般不用）。

    Returns:
        dict: skipped / count_before / limit / deleted / victims ...
    """
    settings = read_capacity_settings(config_path)
    if enabled is None:
        enabled = settings["enabled"]
    limit = settings["max_images"] if max_images is None else max(0, int(max_images))
    result: dict[str, Any] = {
        "pack_id": str(pack_id),
        "skipped": "",
        "count_before": 0,
        "limit": limit,
        "deleted": [],
        "victims": [],
        "dry_run": bool(dry_run),
        "reason": str(reason),
    }
    if not enabled:
        result["skipped"] = "disabled"
        return result
    if limit <= 0:
        result["skipped"] = "unlimited"
        return result

    now = time.monotonic()
    if not force and (now - _LAST_CHECK.get(str(pack_id), 0.0)) < DEBOUNCE_SECONDS:
        result["skipped"] = "debounced"
        return result
    _LAST_CHECK[str(pack_id)] = now

    plan = plan_prune(pack_id, max_images=limit, config_path=config_path)
    result["count_before"] = int(plan["count"])
    victims = plan["victims"]
    if not victims:
        result["skipped"] = "under_limit"
        return result

    result["victims"] = [str(p) for p in victims]
    if dry_run:
        result["deleted"] = [
            {"category": info["category"], "file": info["file"], "size": info["size"]}
            for info in (_describe(p) for p in victims)
        ]
        return result

    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    log_records: list[dict] = []
    for path in victims:
        info = _describe(path)
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("[meme_manager] 容量清理删除失败 %s：%s", path, exc)
            continue
        result["deleted"].append(str(path))
        log_records.append(
            {
                "ts": stamp,
                "pack_id": str(pack_id),
                "reason": str(reason),
                "limit": limit,
                "count_before": result["count_before"],
                **info,
            }
        )

    if log_records:
        _append_prune_log(log_records)
        _invalidate_semantics(pack_id)
        logger.info(
            "[meme_manager] 容量清理：包=%s 上限=%s 清理前=%s 删除=%s 来源=%s",
            pack_id,
            limit,
            result["count_before"],
            len(log_records),
            reason,
        )
    return result


def enforce_for_memes_dir(
    memes_dir: str | Path,
    *,
    reason: str = "auto",
    force: bool = False,
    config_path: str | Path | None = None,
) -> dict:
    """落盘点只知道 memes 目录时的入口（目录名的父目录名 = pack_id）。"""
    try:
        pack_id = Path(memes_dir).resolve().parent.name
    except Exception:
        return {"skipped": "unknown_pack", "deleted": []}
    if not pack_id:
        return {"skipped": "unknown_pack", "deleted": []}
    return enforce_pack_capacity(
        pack_id,
        reason=reason,
        force=force,
        config_path=config_path,
    )


def enforce_all_packs(
    *,
    dry_run: bool = False,
    force: bool = True,
    reason: str = "manual",
    config_path: str | Path | None = None,
) -> dict:
    """对全部表情包执行一次清理（手动指令用）。"""
    summary: dict[str, dict] = {}
    for pack_id in list_pack_ids():
        summary[pack_id] = enforce_pack_capacity(
            pack_id,
            dry_run=dry_run,
            force=force,
            reason=reason,
            config_path=config_path,
        )
    return summary
