# -*- coding: utf-8 -*-
"""[patcher:qq-config] meme_manager —— QQ 指令可调参数注册表

背景（2026-10-12 用户拍板 Q6 / Q7）
----------------------------------
把「改了立刻有意义 + 值类型简单（布尔 / 数字 / 短字符串）」的旋钮暴露给
QQ 指令（`/表情管理 参数`、`/表情管理 设置`），手机端即可调；长文本、
多值列表、以及牵动「模型 / 索引就绪状态」的项（语义检索、消息格式）
**只做只读展示，禁止指令修改** —— 一旦误配会静默搞坏整条链路。

- 键名同时接受**中文短别名**（手机好打，如 `发图概率`）与**完整配置路径**
  （精确，如 `generation.llm_free_random.probability`）。
- `默认` / `default` / `重置` → 写回该键的 schema 默认值。
- 边界（min/max）与面板 `plugin_settings.validate_settings_changes` 保持一致，
  避免指令写进面板之后拒绝的值。
"""

from __future__ import annotations

from typing import Any


def _p(
    alias: str,
    path: str,
    kind: str,
    default: Any,
    desc: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    unit: str = "",
    group: str = "",
) -> dict:
    return {
        "alias": alias,
        "path": path,
        "type": kind,
        "default": default,
        "desc": desc,
        "min": minimum,
        "max": maximum,
        "unit": unit,
        "group": group,
    }


# 可写参数（Q6 审核通过的清单）
PARAMS: list[dict] = [
    # ---- 免模型发图（generation.llm_free_random）----
    _p("发图开关", "generation.llm_free_random.enabled", "bool", True,
       "免模型随机发图总开关", group="免模型发图"),
    _p("主动发图", "generation.llm_free_random.active", "bool", True,
       "开=群里任意消息都可能触发；关=仅被@/私聊/指令前缀时触发", group="免模型发图"),
    _p("发图概率", "generation.llm_free_random.probability", "float", 3.0,
       "骰子1：本轮到底发不发图", minimum=0, maximum=100, unit="%", group="免模型发图"),
    _p("选图模型开关", "generation.llm_free_random.api_enabled", "bool", True,
       "命中后是否让视觉模型挑分类", group="免模型发图"),
    _p("选图概率", "generation.llm_free_random.api_probability", "float", 100.0,
       "骰子2：命中才报送模型，未命中纯随机抽图", minimum=0, maximum=100,
       unit="%", group="免模型发图"),
    _p("选图模型", "generation.llm_free_random.api_provider_id", "string",
       "zhipu/glm-4v-flash", "用哪个模型挑分类（provider_id）", group="免模型发图"),
    _p("选图日限额", "generation.llm_free_random.api_daily_limit", "int", 0,
       "每天最多调模型几次，0=不限", minimum=0, group="免模型发图"),
    _p("选图冷却", "generation.llm_free_random.api_cooldown", "float", 60.0,
       "同一会话两次调模型的最小间隔", minimum=0, maximum=600, unit="秒",
       group="免模型发图"),
    # ---- 智能配图（generation.emotion）----
    _p("附表情概率", "generation.emotion.probability", "int", 100,
       "走 LLM 回复时附赠表情图的概率", minimum=1, maximum=100, unit="%",
       group="智能配图"),
    _p("单条图片上限", "generation.emotion.max_memes_per_message", "int", -1,
       "一条回复最多几张图，-1=不限", group="智能配图"),
    # [patcher:group-cooldown] 群级配图冷却（per_group 映射表只走面板/配置文件）
    _p("群图冷却开关", "generation.rate_limit.enabled", "bool", False,
       "同群一定时间内最多自动发一张图", group="智能配图"),
    _p("群图冷却", "generation.rate_limit.interval_seconds", "int", 300,
       "同群两次自动配图的最小间隔，0=不限", minimum=0, maximum=86400,
       unit="秒", group="智能配图"),
    # ---- 自动收集（auto_collect）----
    _p("收图开关", "auto_collect.enabled", "bool", False,
       "群里图片自动识别入库", group="自动收集"),
    _p("收图概率", "auto_collect.sampling_probability", "int", 100,
       "每张群图片被送审的概率", minimum=0, maximum=100, unit="%", group="自动收集"),
    _p("收图冷却", "auto_collect.cooldown_seconds", "int", 20,
       "同一会话两次收集的最小间隔", minimum=0, maximum=600, unit="秒",
       group="自动收集"),
    _p("识别日限额", "auto_collect.daily_recognition_limit", "int", 100,
       "每天最多识别几次，0=不限", minimum=0, group="自动收集"),
    _p("待审模式", "auto_collect.manual_review", "bool", True,
       "开=先进待审箱；关=直接入库", group="自动收集"),
    _p("表情阈值", "auto_collect.min_meme_confidence", "float", 0.85,
       "判定「这是表情包」的最低置信", minimum=0, maximum=1, group="自动收集"),
    _p("分类阈值", "auto_collect.min_category_confidence", "float", 0.65,
       "判定分类的最低置信", minimum=0, maximum=1, group="自动收集"),
    _p("待审上限", "auto_collect.pending_limit", "int", 200,
       "待审箱最多囤多少张", minimum=1, maximum=2000, group="自动收集"),
    # ---- 图库容量（gallery_capacity）----
    _p("每包上限", "gallery_capacity.max_images", "int", 10000,
       "每个表情包最多多少张，超出删最旧；0=不限", minimum=0, group="图库容量"),
    _p("容量清理开关", "gallery_capacity.enabled", "bool", True,
       "临时停用超额自动清理", group="图库容量"),
    # ---- 图库绑定（selection）----
    # ⚠️ 这条不走通用「写配置」链路：set_config_param 会把它转成
    #    storage.set_default_pack()，直接改 selection_rules.json 的 default 规则
    #    （唯一真相源），改完立即生效。
    _p("默认图库", "selection.default_pack_id", "string", "stealer-migrated",
       "未绑定会话回落的默认表情包 pack_id（改完立即生效）", group="图库绑定"),
]

# 只读展示（Q6：涉及模型 / 索引 / 格式，误配会静默失效，禁止指令修改）
READONLY: list[dict] = [
    _p("语义检索开关", "semantic.enabled", "bool", False,
       "由回复模型检索表情（需模型 + 索引就绪）", group="语义检索"),
    _p("语义候选数", "semantic.top_k", "int", 5,
       "每次检索候选数", group="语义检索"),
    _p("语义最低分", "semantic.min_score", "float", 0.25,
       "最低相似度", group="语义检索"),
    _p("静态转GIF", "generation.message.convert_static_to_gif", "bool", False,
       "发送前把静态图转成 GIF", group="消息格式"),
    _p("图片转Base64", "generation.message.send_image_as_base64", "bool", False,
       "以 base64 形式发送图片", group="消息格式"),
]

BY_ALIAS: dict[str, dict] = {item["alias"]: item for item in PARAMS}
BY_PATH: dict[str, dict] = {item["path"]: item for item in PARAMS}

RESET_TOKENS = {"默认", "default", "重置", "reset", "还原"}
TRUE_TOKENS = {"开", "是", "真", "启用", "on", "true", "yes", "y", "1"}
FALSE_TOKENS = {"关", "否", "假", "停用", "off", "false", "no", "n", "0"}


def resolve(key: str) -> dict | None:
    """按别名或完整点路径查参数；未收录时返回 None。"""
    token = str(key or "").strip()
    if not token:
        return None
    return BY_ALIAS.get(token) or BY_PATH.get(token)


def is_readonly(key: str) -> dict | None:
    token = str(key or "").strip()
    for item in READONLY:
        if item["alias"] == token or item["path"] == token:
            return item
    return None


def format_value(spec: dict, value: Any) -> str:
    """把值渲染成人类可读文本。"""
    kind = spec["type"]
    if kind == "bool":
        return "开" if bool(value) else "关"
    if kind == "float":
        try:
            text = f"{float(value):g}"
        except (TypeError, ValueError):
            text = str(value)
        return text + (spec.get("unit") or "")
    if kind in ("int",):
        return f"{value}" + (spec.get("unit") or "")
    return str(value)


def get_by_path(config: Any, path: str) -> Any:
    """从（嵌套 dict 的）配置里取点路径的值；不存在返回 None。"""
    current: Any = config
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def parse_value(spec: dict, raw: str) -> tuple[bool, Any]:
    """把用户输入解析成该参数类型的值。

    Returns:
        (ok, value_or_error_message)。`默认` 等令牌返回该键的 schema 默认值。
    """
    token = str(raw or "").strip()
    if not token:
        return False, "值不能为空"
    if token.lower() in RESET_TOKENS or token in RESET_TOKENS:
        return True, spec["default"]

    kind = spec["type"]
    if kind == "bool":
        lowered = token.lower()
        if lowered in TRUE_TOKENS or token in TRUE_TOKENS:
            return True, True
        if lowered in FALSE_TOKENS or token in FALSE_TOKENS:
            return True, False
        return False, "布尔值请用 开/关（或 true/false、yes/no、1/0）"

    if kind in ("int", "float"):
        cleaned = token.rstrip("%").strip()
        try:
            number = float(cleaned)
        except ValueError:
            return False, "请输入数字" + ("（整数）" if kind == "int" else "")
        if kind == "int":
            if abs(number - round(number)) > 1e-9:
                return False, "请输入整数"
            value: Any = int(round(number))
        else:
            value = float(number)
        minimum, maximum = spec.get("min"), spec.get("max")
        if minimum is not None and value < minimum:
            return False, f"不能小于 {minimum}"
        if maximum is not None and value > maximum:
            return False, f"不能大于 {maximum}"
        return True, value

    if kind == "string":
        if len(token) > 200:
            return False, "内容过长（上限 200 字符）"
        return True, token

    return False, "不支持的参数类型"
