from pathlib import Path

from .protocol import load_json_file
from ...config import (
    DEFAULT_CATEGORY_DESCRIPTIONS,
    DEFAULT_PACK_ID,
    PACKS_DIR,
    REGISTRY_PATH,
    SELECTION_RULES_PATH,
)
from ..semantic.models import runtime_category_mapping


def _load_json(path: Path, default):
    # [patcher:orphan-cleanup] 收敛到 packs.protocol.load_json_file（BOM 兼容）
    return load_json_file(path, default)


def _is_pack_enabled(pack_id: str, registry_data: dict) -> bool:
    installed = registry_data.get("installed_packs", [])
    if not isinstance(installed, list):
        return False

    for pack in installed:
        if not isinstance(pack, dict):
            continue
        if str(pack.get("id") or "").strip() != pack_id:
            continue
        return bool(pack.get("enabled", True))

    return False


def _pack_exists(pack_id: str) -> bool:
    return (PACKS_DIR / pack_id).is_dir()


def _normalize_rule_pack_ids(rule: dict) -> list[str]:
    """读取一条规则声明的包集合，向后兼容旧的单值 ``pack_id`` 字段。"""
    pack_ids: list[str] = []
    raw = rule.get("pack_ids")
    if isinstance(raw, list):
        for item in raw:
            pack_id = str(item or "").strip()
            if pack_id and pack_id not in pack_ids:
                pack_ids.append(pack_id)
    if not pack_ids:
        single = str(rule.get("pack_id") or "").strip()
        if single:
            pack_ids = [single]
    return pack_ids


def _rule_matches(
    rule: dict, session_id: str | None, persona_id: str | None
) -> bool:
    """判断一条 session / persona 规则是否命中当前会话。"""
    scope = str(rule.get("scope") or "").strip().lower()
    target = str(rule.get("target") or "").strip()
    if not target:
        return False
    if scope == "session":
        return bool(session_id) and target == session_id
    if scope == "persona":
        if not persona_id:
            return False
        # Webchat 对内置默认人格使用不同的名称。
        return target == persona_id or (
            target == "default" and persona_id == "_chatui_default_"
        )
    return False


def resolve_pack_id(
    session_id: str | None = None, persona_id: str | None = None
) -> str:
    """按照有序选择规则和回退策略解析表情包 ID（单包，取绑定集合首个有效包）。"""
    selection_data = _load_json(SELECTION_RULES_PATH, {})
    registry_data = _load_json(REGISTRY_PATH, {})
    rules = selection_data.get("rules", []) if isinstance(selection_data, dict) else []

    resolved_default = ""
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            scope = str(rule.get("scope") or "").strip().lower()
            rule_pack_ids = _normalize_rule_pack_ids(rule)
            if not rule_pack_ids:
                continue

            if scope == "default":
                resolved_default = rule_pack_ids[0]
                continue

            if not _rule_matches(rule, session_id, persona_id):
                continue
            for candidate in rule_pack_ids:
                if _pack_exists(candidate) and _is_pack_enabled(
                    candidate, registry_data
                ):
                    return candidate

    for fallback_pack_id in (
        resolved_default,
        DEFAULT_PACK_ID,
    ):
        if not fallback_pack_id:
            continue
        if _pack_exists(fallback_pack_id) and (
            _is_pack_enabled(fallback_pack_id, registry_data)
            or fallback_pack_id == DEFAULT_PACK_ID
        ):
            return fallback_pack_id

    installed = (
        registry_data.get("installed_packs", [])
        if isinstance(registry_data, dict)
        else []
    )
    if isinstance(installed, list):
        for pack in installed:
            if not isinstance(pack, dict):
                continue
            pack_id = str(pack.get("id") or "").strip()
            if not pack_id:
                continue
            if not bool(pack.get("enabled", True)):
                continue
            if _pack_exists(pack_id):
                return pack_id

    return DEFAULT_PACK_ID


def resolve_pack_ids(
    session_id: str | None = None, persona_id: str | None = None
) -> list[str]:
    """解析「该会话绑定的一组表情包」。

    命中 session / persona 规则时返回其全部**有效**包；未命中则回退默认包集合
    （default 规则的包集合 → 单个兜底包）。返回值至少含一个元素。
    """
    selection_data = _load_json(SELECTION_RULES_PATH, {})
    registry_data = _load_json(REGISTRY_PATH, {})
    rules = selection_data.get("rules", []) if isinstance(selection_data, dict) else []

    resolved_default_ids: list[str] = []
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            scope = str(rule.get("scope") or "").strip().lower()
            rule_pack_ids = _normalize_rule_pack_ids(rule)
            if not rule_pack_ids:
                continue

            if scope == "default":
                resolved_default_ids = rule_pack_ids
                continue

            if not _rule_matches(rule, session_id, persona_id):
                continue

            valid = [
                pack_id
                for pack_id in rule_pack_ids
                if _pack_exists(pack_id) and _is_pack_enabled(pack_id, registry_data)
            ]
            if valid:
                return valid

    fallback = [
        pack_id
        for pack_id in resolved_default_ids
        if _pack_exists(pack_id)
        and (
            _is_pack_enabled(pack_id, registry_data) or pack_id == DEFAULT_PACK_ID
        )
    ]
    if fallback:
        return fallback

    return [resolve_pack_id(session_id=session_id, persona_id=persona_id)]


def get_pack_paths(pack_id: str) -> dict[str, Path]:
    pack_dir = PACKS_DIR / pack_id
    return {
        "pack_dir": pack_dir,
        "memes_dir": pack_dir / "memes",
        "metadata_path": pack_dir / "memes_data.json",
        "manifest_path": pack_dir / "manifest.json",
    }


def load_pack_category_mapping(pack_id: str) -> dict[str, str]:
    paths = get_pack_paths(pack_id)
    mapping = _load_json(paths["metadata_path"], {})

    if isinstance(mapping, dict) and mapping:
        return runtime_category_mapping(mapping)

    manifest = _load_json(paths["manifest_path"], {})
    categories = manifest.get("categories", {}) if isinstance(manifest, dict) else {}
    if isinstance(categories, dict) and categories:
        resolved = {}
        for category, metadata in categories.items():
            if not str(category).strip():
                continue
            if isinstance(metadata, dict):
                description = str(metadata.get("description") or "请添加描述")
            else:
                description = str(metadata)
            resolved[str(category)] = description
        if resolved:
            return runtime_category_mapping(resolved)

    if pack_id == DEFAULT_PACK_ID:
        return runtime_category_mapping(DEFAULT_CATEGORY_DESCRIPTIONS)

    return {}


def _active_pack_id() -> str:
    """读「管理当前包」（selection_rules.json 顶层字段）。"""
    data = _load_json(SELECTION_RULES_PATH, {})
    if not isinstance(data, dict):
        return ""
    return str(data.get("active_pack_id") or "").strip()


def resolve_pack_context(
    session_id: str | None = None,
    persona_id: str | None = None,
) -> dict[str, object]:
    if session_id is None and persona_id is None:
        # 无会话上下文 = 管理类调用：优先落到「管理当前包」，未设置再走默认。
        admin_pack_id = _active_pack_id()
        if admin_pack_id and _pack_exists(admin_pack_id):
            paths = get_pack_paths(admin_pack_id)
            return {
                "pack_id": admin_pack_id,
                "pack_ids": [admin_pack_id],
                "pack_dir": paths["pack_dir"],
                "memes_dir": paths["memes_dir"],
                "metadata_path": paths["metadata_path"],
                "manifest_path": paths["manifest_path"],
                "category_mapping": load_pack_category_mapping(admin_pack_id),
            }

    pack_ids = resolve_pack_ids(session_id=session_id, persona_id=persona_id)
    pack_id = pack_ids[0]
    paths = get_pack_paths(pack_id)
    return {
        "pack_id": pack_id,
        "pack_ids": list(pack_ids),
        "pack_dir": paths["pack_dir"],
        "memes_dir": paths["memes_dir"],
        "metadata_path": paths["metadata_path"],
        "manifest_path": paths["manifest_path"],
        "category_mapping": load_pack_category_mapping(pack_id),
    }
