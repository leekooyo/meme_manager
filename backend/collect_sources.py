"""按「来源」配置表情包自动收集目标的持久化存储。

落盘位置：``<plugin_data>/meme_manager/collect_sources.json``

.. code-block:: json

    {
      "schema_version": 1,
      "sources": {
        "group:123456789": {"enabled": true,  "packs": ["example-pack"]},
        "user:987654321":  {"enabled": false, "packs": ["builtin-default"]}
      }
    }

设计口径（对应 grilling Q14/Q15/Q18/Q19/Q20/Q32）
------------------------------------------------
- **主键 = 来源**：``group:<群号>`` / ``user:<QQ号>``。
- **未出现的来源 = 不收图**（显式白名单，Q15=乙）。
- 命中的来源对 ``packs`` 里**每个包各入库一份**（复制镜像，Q18=甲）。
- ``packs`` 为空/缺省 → 回落到「默认图库」集合（Q32=甲：复用
  ``selection_rules.json`` 的 default 规则，不另起第二个 default 真相源）。
- 与发送绑定（``selection_rules.json``）是**两套独立**配置（Q5）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..config import PLUGIN_DATA_DIR

COLLECT_SOURCES_PATH = PLUGIN_DATA_DIR / "collect_sources.json"
SCHEMA_VERSION = 1
MAX_PACKS_PER_SOURCE = 64

_LOCK = threading.RLock()


def source_key(source_kind: str, source_id: str) -> str:
    """把来源类型与 ID 拼成 ``group:<群号>`` / ``user:<QQ号>`` 形式的主键。"""
    kind = str(source_kind or "").strip().lower()
    sid = str(source_id or "").strip()
    if not kind or not sid:
        return ""
    return f"{kind}:{sid}"


def _default_document() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "sources": {}}


def _normalize_pack_list(value: Any) -> list[str]:
    packs: list[str] = []
    raw = value if isinstance(value, (list, tuple, set)) else []
    for item in raw:
        pack_id = str(item or "").strip()
        if pack_id and pack_id not in packs:
            packs.append(pack_id)
        if len(packs) >= MAX_PACKS_PER_SOURCE:
            break
    return packs


def _normalize_document(payload: Any) -> dict[str, Any]:
    document = _default_document()
    if not isinstance(payload, dict):
        return document
    document["schema_version"] = int(
        payload.get("schema_version") or SCHEMA_VERSION
    )
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return document
    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in sources.items():
        key = str(raw_key or "").strip()
        if not key or ":" not in key or not isinstance(raw_value, dict):
            continue
        normalized[key] = {
            "enabled": bool(raw_value.get("enabled", False)),
            "packs": _normalize_pack_list(raw_value.get("packs")),
        }
    document["sources"] = normalized
    return document


def load() -> dict[str, Any]:
    """读取并规范化 ``collect_sources.json``；文件缺失/损坏时返回空文档。"""
    with _LOCK:
        try:
            with COLLECT_SOURCES_PATH.open(encoding="utf-8-sig") as file_obj:
                payload = json.load(file_obj)
        except Exception:
            return _default_document()
        return _normalize_document(payload)


def save(document: dict[str, Any]) -> dict[str, Any]:
    """原子写盘（同目录临时文件 + ``os.replace``）。"""
    normalized = _normalize_document(document)
    with _LOCK:
        COLLECT_SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = COLLECT_SOURCES_PATH.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as file_obj:
            json.dump(normalized, file_obj, ensure_ascii=False, indent=2)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary, COLLECT_SOURCES_PATH)
    return normalized


def exists() -> bool:
    """``collect_sources.json`` 是否存在（用于判断是否已迁移到新白名单）。"""
    return COLLECT_SOURCES_PATH.is_file()


def list_sources() -> dict[str, dict[str, Any]]:
    """返回全部来源配置（``{source_key: {...}}``）。"""
    return dict(load().get("sources", {}))


def get_source(key: str) -> dict[str, Any] | None:
    """读取单个来源配置；不存在时返回 ``None``。"""
    value = load().get("sources", {}).get(str(key or "").strip())
    return dict(value) if isinstance(value, dict) else None


def _write_source(key: str, entry: dict[str, Any]) -> dict[str, Any]:
    document = load()
    document.setdefault("sources", {})[key] = {
        "enabled": bool(entry.get("enabled", False)),
        "packs": _normalize_pack_list(entry.get("packs")),
    }
    return save(document)


def set_enabled(key: str, enabled: bool) -> dict[str, Any]:
    """开关来源的总闸；来源不存在时按默认包创建。"""
    entry = get_source(key) or {
        "enabled": False,
        "packs": default_packs(),
    }
    entry["enabled"] = bool(enabled)
    _write_source(key, entry)
    return dict(entry)


def set_packs(key: str, packs: list[str]) -> dict[str, Any]:
    """设置来源的镜像目标包集合（保留其开关状态）。"""
    entry = get_source(key) or {"enabled": False, "packs": []}
    entry["packs"] = _normalize_pack_list(packs)
    _write_source(key, entry)
    return dict(entry)


def default_packs() -> list[str]:
    """默认图库集合：``selection_rules.json`` 的 default 规则（Q32=甲）。"""
    try:
        from .packs.resolver import resolve_pack_ids

        packs = resolve_pack_ids()
        if packs:
            return list(packs)
    except Exception:
        pass
    from ..config import DEFAULT_PACK_ID

    return [DEFAULT_PACK_ID]


def resolve_packs(key: str) -> list[str]:
    """解析某来源当前的**有效**收集目标包集合。

    返回空列表表示「不收图」：来源未配置、总闸关闭、或所有目标包都已不存在。
    """
    entry = get_source(key)
    if not entry or not bool(entry.get("enabled")):
        return []
    packs = _normalize_pack_list(entry.get("packs")) or default_packs()
    return [pack_id for pack_id in packs if _pack_exists(pack_id)]


def _pack_exists(pack_id: str) -> bool:
    from ..config import PACKS_DIR

    return bool(pack_id) and (PACKS_DIR / pack_id).is_dir()


def main_pack_id() -> str:
    """「主包」= 默认图库（Q30=乙）：视觉识别只按它的分类表跑一次。"""
    packs = default_packs()
    return packs[0] if packs else ""


def seed_from_legacy(scope: list[str] | None, target_pack_id: str = "") -> bool:
    """首次启用时，把旧的 ``auto_collect.scope`` + ``target_pack_id`` 迁成新配置。

    仅当 ``collect_sources.json`` **不存在** 时执行，避免覆盖用户已编辑的配置。
    返回是否真的播种了。
    """
    if COLLECT_SOURCES_PATH.is_file():
        return False
    entries = [str(item or "").strip() for item in (scope or []) if str(item or "").strip()]
    fallback = str(target_pack_id or "").strip()
    packs = [fallback] if fallback and _pack_exists(fallback) else default_packs()
    document = _default_document()
    for raw in entries:
        key = raw if ":" in raw else source_key("group", raw)
        if not key:
            continue
        document["sources"][key] = {"enabled": True, "packs": list(packs)}
    if not document["sources"]:
        return False
    save(document)
    return True
