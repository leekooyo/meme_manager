# -*- coding: utf-8 -*-
"""图库创建与重命名操作。

新建图库时初始化标准分类，可设置为当前管理图库或默认图库。重命名时，
同步迁移图库目录及注册表、发送规则、自动收集来源和相关索引等持久引用。

重命名会先校验状态，再迁移文件与配置；失败时尽可能回滚。派生索引属于缓存，
若缓存文件被占用而无法迁移，会返回提示，后续可重新生成。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ...config import (
    DEFAULT_CATEGORY_DESCRIPTIONS,
    PACKS_DIR,
    PLUGIN_DATA_DIR,
    REGISTRY_PATH,
    SELECTION_RULES_PATH,
)
from .protocol import validate_pack_id
from .storage import (
    _load_registry,
    _save_json,
    _save_registry,
    _load_selection_rules,
    _save_selection_rules,
    set_active_pack_id,
    set_default_pack,
)

_IDLE_BLOCKING_STATES = {"running", "paused"}
_DERIVED_INDEX_DIRS = ("auto_collect_index", "content_hash_index")


# ------------------------------------------------------------------ 工具
def _plugin_config_path() -> Path | None:
    """定位插件配置文件 `<data>/config/<插件目录名>_config.json`；找不到返回 None。"""
    candidate = (
        PLUGIN_DATA_DIR.parent.parent / "config" / f"{PLUGIN_DATA_DIR.name}_config.json"
    )
    return candidate if candidate.is_file() else None


def _read_json_any(path: Path, default):
    try:
        text = path.read_bytes().decode("utf-8-sig")
        return json.loads(text)
    except (OSError, ValueError):
        return default


def _write_config_json(path: Path, data: dict) -> None:
    """按 AstrBot 配置格式回写：**BOM + CRLF + indent 2**（区别于包内 JSON 的 LF/无 BOM）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))


def _pack_config_json_path(pack_dir: Path, name: str) -> Path:
    return pack_dir / name


def _semantic_index_dir(pack_id: str) -> Path:
    return PLUGIN_DATA_DIR / "semantic_indexes" / pack_id


def assert_pack_idle(pack_id: str) -> None:
    """id 迁移前置校验：该包不得有进行中的语义任务。"""
    state_path = _semantic_index_dir(pack_id) / "task_state.json"
    if not state_path.is_file():
        return
    state = _read_json_any(state_path, {})
    status = str(state.get("task_status") or "").strip().lower()
    if status in _IDLE_BLOCKING_STATES:
        raise RuntimeError(
            f"图库 {pack_id} 有进行中的语义任务（{status}），请先暂停/结束再重命名 id"
        )


def list_pack_ids() -> list[str]:
    """返回当前物理存在的包 id（不含隐藏目录）。"""
    if not PACKS_DIR.is_dir():
        return []
    return sorted(
        path.name
        for path in PACKS_DIR.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


# ------------------------------------------------------------------ 新建
def create_pack(
    pack_id: str,
    name: str = "",
    *,
    set_default: bool = False,
    set_active: bool = True,
) -> dict:
    """新建一个空图库（标准分类表），并登记进注册表。

    Args:
        pack_id: 新图库的稳定 ID（目录名），`[a-z0-9._-]`、2–64 位。
        name: 显示名称；留空则用 ID。
        set_default: 是否同时设为默认图库（影响未绑定会话的发图回落）。
        set_active: 是否同时设为「管理当前包」（WebUI 立即可编辑）。

    Returns:
        {"pack_id", "name", "category_count", "set_active", "set_default"}

    Raises:
        ValueError: ID 非法 / 已被占用。
    """
    token = validate_pack_id(str(pack_id or "").strip(), "新图库")
    if (PACKS_DIR / token).exists():
        raise ValueError(f"图库 ID「{token}」已存在，请换一个")

    registry = _load_registry()
    if any(str(item.get("id") or "") == token for item in registry["installed_packs"]):
        raise ValueError(f"图库 ID「{token}」已在注册表中，请换一个")

    display = str(name or "").strip() or token
    if len(display) > 64:
        raise ValueError("显示名称过长（上限 64 字符）")

    descriptions = dict(DEFAULT_CATEGORY_DESCRIPTIONS)
    pack_dir = PACKS_DIR / token
    memes_dir = pack_dir / "memes"
    try:
        for category in descriptions:
            (memes_dir / category).mkdir(parents=True, exist_ok=True)
        _save_json(pack_dir / "memes_data.json", descriptions)
        _save_json(
            pack_dir / "manifest.json",
            {
                "schema_version": 1,
                "id": token,
                "name": display,
                "version": "1.0.0",
                "description": "由 WebUI/指令新建的本地图库",
                "tags": ["runtime", "user-created"],
                "categories": {
                    category: {"description": desc}
                    for category, desc in descriptions.items()
                },
            },
        )
    except Exception:
        shutil.rmtree(pack_dir, ignore_errors=True)
        raise

    registry["installed_packs"].append(
        {
            "id": token,
            "name": display,
            "version": "1.0.0",
            "enabled": True,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _save_registry(registry)

    active_after = ""
    if set_active:
        active_after = str(set_active_pack_id(token).get("active_pack_id") or "")
    default_after = ""
    if set_default:
        default_after = str(set_default_pack(token).get("pack_id") or "")

    return {
        "pack_id": token,
        "name": display,
        "category_count": len(descriptions),
        "set_active": bool(set_active),
        "active_pack_id": active_after,
        "set_default": bool(set_default),
        "default_pack_id": default_after,
    }


# ------------------------------------------------------------------ 重命名
def _replace_in_list(values, old: str, new: str) -> tuple[list, bool]:
    changed = False
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text == old:
            text = new
            changed = True
        if text and text not in result:
            result.append(text)
    return result, changed


def rename_pack(
    old_pack_id: str,
    *,
    new_pack_id: str = "",
    new_name: str = "",
) -> dict:
    """重命名图库：可单独改显示名，也可同时改内部 ID（迁移全部持久引用）。

    Args:
        old_pack_id: 原图库 ID。
        new_pack_id: 新 ID（留空或与原 ID 相同则不改目录）。
        new_name: 新显示名（留空则保持原显示名）。

    Returns:
        {"pack_id", "old_pack_id", "name", "renamed_id": bool,
         "migrated": {...}, "warnings": [...]}

    Raises:
        FileNotFoundError / ValueError / RuntimeError: 前置校验失败。
    """
    old = str(old_pack_id or "").strip()
    if not old:
        raise ValueError("缺少原图库 ID")
    old_dir = PACKS_DIR / old
    if not old_dir.is_dir():
        raise FileNotFoundError(f"图库 {old} 不存在")

    raw_new_id = str(new_pack_id or "").strip()
    id_change = bool(raw_new_id) and raw_new_id != old
    if id_change:
        new_id = validate_pack_id(raw_new_id, "新图库 ID")
        if (PACKS_DIR / new_id).exists():
            raise ValueError(f"图库 ID「{new_id}」已被占用，请换一个")
        registry = _load_registry()
        if any(
            str(item.get("id") or "") == new_id for item in registry["installed_packs"]
        ):
            raise ValueError(f"图库 ID「{new_id}」已在注册表中，请换一个")
        assert_pack_idle(old)
    else:
        new_id = old

    display = str(new_name or "").strip()
    if len(display) > 64:
        raise ValueError("显示名称过长（上限 64 字符）")

    manifest_path = _pack_config_json_path(old_dir, "manifest.json")
    manifest = _read_json_any(manifest_path, {})
    final_name = display or str(manifest.get("name") or "").strip() or old
    name_change = final_name != (str(manifest.get("name") or "").strip() or old)

    if not id_change and not name_change:
        raise ValueError("没有需要修改的内容")

    target_dir = PACKS_DIR / new_id
    warnings: list[str] = []
    migrated: dict[str, int] = {}

    # ---------- 备份（回滚用）----------
    json_targets = [REGISTRY_PATH, SELECTION_RULES_PATH]
    # 包内清单也必须备份：目录改名后它们会被改写，而回滚靠「目录搬回 + 字节复原」
    json_targets.append(old_dir / "manifest.json")
    semantic_metadata_path = old_dir / "semantic_metadata.json"
    if semantic_metadata_path.is_file():
        json_targets.append(semantic_metadata_path)
    collect_sources_path = PLUGIN_DATA_DIR / "collect_sources.json"
    if collect_sources_path.is_file():
        json_targets.append(collect_sources_path)
    config_path = _plugin_config_path()
    if config_path is not None:
        json_targets.append(config_path)

    backups: dict[Path, bytes | None] = {}
    for path in json_targets:
        try:
            backups[path] = path.read_bytes()
        except OSError:
            backups[path] = None

    moved_dirs: list[tuple[Path, Path]] = []

    def _rollback() -> None:
        for src, dst in reversed(moved_dirs):
            try:
                if dst.exists() and not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
            except OSError:
                pass
        for path, content in backups.items():
            try:
                if content is None:
                    continue
                path.write_bytes(content)
            except OSError:
                pass

    try:
        # ---------- 1) 目录迁移 ----------
        if id_change:
            shutil.move(str(old_dir), str(target_dir))
            moved_dirs.append((old_dir, target_dir))

            sem_src = _semantic_index_dir(old)
            if sem_src.is_dir():
                sem_dst = _semantic_index_dir(new_id)
                shutil.move(str(sem_src), str(sem_dst))
                moved_dirs.append((sem_src, sem_dst))

            for index_dir_name in _DERIVED_INDEX_DIRS:
                index_dir = PLUGIN_DATA_DIR / index_dir_name
                for suffix in (".sqlite3", ".sqlite3-wal", ".sqlite3-shm"):
                    src = index_dir / f"{old}{suffix}"
                    if not src.is_file():
                        continue
                    dst = index_dir / f"{new_id}{suffix}"
                    try:
                        shutil.move(str(src), str(dst))
                        moved_dirs.append((src, dst))
                    except OSError:
                        warnings.append(
                            f"{index_dir_name}/{old}{suffix} 被占用，未改名"
                            f"（纯缓存，新 ID 会自行重建）"
                        )

        # ---------- 2) 包内清单 ----------
        target_manifest = target_dir / "manifest.json"
        if target_manifest.is_file():
            data = _read_json_any(target_manifest, {})
            data["id"] = new_id
            data["name"] = final_name
            _save_json(target_manifest, data)
            migrated["manifest"] = 1

        target_semantic = target_dir / "semantic_metadata.json"
        if target_semantic.is_file() and id_change:
            data = _read_json_any(target_semantic, {})
            if data.get("pack_id") != new_id:
                data["pack_id"] = new_id
                _save_json(target_semantic, data)
                migrated["semantic_metadata"] = 1

        # ---------- 3) 注册表 ----------
        registry = _load_registry()
        registry_hits = 0
        for item in registry["installed_packs"]:
            if str(item.get("id") or "") == old:
                item["id"] = new_id
                item["name"] = final_name
                registry_hits += 1
        if registry_hits:
            _save_registry(registry)
            migrated["registry"] = registry_hits
        else:
            warnings.append(f"注册表中未找到图库 {old}（已跳过）")

        # ---------- 4) 选择规则（pack_id / pack_ids / active_pack_id）----------
        rules_data = _load_selection_rules()
        rule_hits = 0
        for rule in rules_data.get("rules", []):
            if not isinstance(rule, dict):
                continue
            if str(rule.get("pack_id") or "").strip() == old:
                rule["pack_id"] = new_id
                rule_hits += 1
            if isinstance(rule.get("pack_ids"), list):
                replaced, changed = _replace_in_list(rule.get("pack_ids"), old, new_id)
                if changed:
                    rule["pack_ids"] = replaced
                    rule_hits += 1
        active_changed = 0
        if str(rules_data.get("active_pack_id") or "").strip() == old:
            rules_data["active_pack_id"] = new_id
            active_changed = 1
        if rule_hits or active_changed:
            _save_selection_rules(rules_data)
            migrated["selection_rules"] = rule_hits
            migrated["active_pack_id"] = active_changed

        # ---------- 5) 收集来源白名单 ----------
        if collect_sources_path.is_file():
            sources_data = _read_json_any(collect_sources_path, {})
            source_hits = 0
            sources = sources_data.get("sources")
            if isinstance(sources, dict):
                for entry in sources.values():
                    if not isinstance(entry, dict):
                        continue
                    packs = entry.get("packs")
                    if isinstance(packs, list):
                        replaced, changed = _replace_in_list(packs, old, new_id)
                        if changed:
                            entry["packs"] = replaced
                            source_hits += 1
            if source_hits:
                _save_json(collect_sources_path, sources_data)
                migrated["collect_sources"] = source_hits

        # ---------- 6) 插件配置（legacy auto_collect.target_pack_id）----------
        if config_path is not None:
            config_data = _read_json_any(config_path, {})
            auto_collect = config_data.get("auto_collect")
            if (
                isinstance(auto_collect, dict)
                and str(auto_collect.get("target_pack_id") or "").strip() == old
            ):
                auto_collect["target_pack_id"] = new_id
                _write_config_json(config_path, config_data)
                migrated["plugin_config"] = 1
                warnings.append(
                    "插件配置 auto_collect.target_pack_id 已改为新 ID，"
                    "需重载插件后生效（该字段为旧版回退值，实际收集目标以「收集来源」为准）"
                )

    except Exception:
        _rollback()
        raise

    return {
        "old_pack_id": old,
        "pack_id": new_id,
        "name": final_name,
        "renamed_id": bool(id_change),
        "renamed_name": bool(name_change),
        "migrated": migrated,
        "warnings": warnings,
    }
