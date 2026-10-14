import asyncio
import copy
import os
import random
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter

from ..backend.packs.images import (
    clear_all_emojis,
    clear_category_emojis,
    get_emoji_by_category,
    IMAGE_EXTENSIONS,
)
from ..backend.packs.categories import resolve_safe_category_directory
from ..backend.packs.storage import install_first_official_pack_from_index
from ..backend.semantic.storage import invalidate_semantic_metadata
from ..backend import qq_config as meme_qq
from ..backend.gallery_capacity import (
    enforce_all_packs as enforce_gallery_capacity,
    read_capacity_settings as read_gallery_capacity_settings,
)
from ..config import COMMUNITY_INDEX_URL

# [patcher:llm-free-random] 两级骰子发图（随机 / 报送 API），全程不调用聊天模型
# ⭐ 下列常量现为「兜底默认值」；实际参数读插件配置 generation.llm_free_random.*
#    （面板 / 配置 JSON / 配置 API 均可调，见 _llm_free_settings）
LLM_FREE_RANDOM_ENABLED = True
LLM_FREE_RANDOM_ACTIVE = True  # True=群里任意消息都可触发；False=仅被@/私聊/前缀
LLM_FREE_RANDOM_PROBABILITY = 3  # 骰子1：命中才发图（兜底值）
LLM_FREE_RANDOM_EXCLUDE_CATEGORIES = {"needs_review"}

# 骰子2：命中骰子1后，是否报送 API 让模型选分类；未命中则纯随机、不调 API
LLM_FREE_API_ENABLED = True
LLM_FREE_API_PROBABILITY = 100  # 骰子2：命中才报送 API（兜底值）
LLM_FREE_API_PROVIDER_ID = "zhipu/glm-4v-flash"
LLM_FREE_API_TIMEOUT = 15
LLM_FREE_API_DAILY_LIMIT = 0
LLM_FREE_API_COOLDOWN = 60

# [patcher:qq-config] QQ 指令可调参数 + 「表情包」随机发图（2026-10-12 用户拍板）
PLUGIN_NAME = "meme_manager"
RANDOM_MEME_COOLDOWN_SECONDS = 3.0  # 同一用户两次「表情包」的最小间隔（Q13=b）
RANDOM_MEME_EXCLUDE_CATEGORIES = {"needs_review"}  # Q12=c：全包随机但跳过待审分类
_HOT_RELOAD_DELAY_SECONDS = 1.5  # 先回复、再延迟后台热重载（Q8=a）
_RANDOM_MEME_LAST_HIT: dict = {}  # {umo|sender_id: 上次命中时刻}


class CommandMixin:
    """表情包管理命令组及所有管理命令"""

    @filter.command_group("表情管理")
    def meme_manager(self):
        """表情包管理命令组:
        查看图库
        添加分类
        添加表情
        恢复默认表情包
        清空指定类型
        清空全部
        删除类型本身
        同步状态
        同步到云端
        从云端同步
        图库统计
        参数
        设置
        清理超额
        绑定图库
        解除图库
        图库绑定
        切换图库
        收集开关
        收集目标
        收集状态

        （另有独立指令「表情包」：在本会话绑定的图库里随机发一张）
        """
        pass

    # ---------- 辅助方法 ----------
    def _extract_category_description_from_command(
        self, event: AstrMessageEvent, category: str
    ) -> str:
        command_prefix = "表情管理 添加分类"
        message = re.sub(r"\s+", " ", event.get_message_str().strip())
        if not message.startswith(command_prefix):
            return ""
        remaining = message[len(command_prefix) :].strip()
        if remaining == category:
            return ""
        if not remaining.startswith(f"{category} "):
            return ""
        return remaining[len(category) :].strip()

    async def _wait_for_category_description(
        self, event: AstrMessageEvent, category: str, timeout: int = 60
    ) -> str:
        description = ""

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def description_waiter(
            controller, description_event: AstrMessageEvent
        ) -> None:
            nonlocal description
            reply = (description_event.message_str or "").strip()
            if reply == "返回":
                await description_event.send(
                    description_event.plain_result("已取消创建分类。")
                )
                controller.stop(CategoryCreationCancelled())
                return
            if not reply:
                await description_event.send(
                    description_event.plain_result(
                        f"请发送分类「{category}」的描述，或发送“返回”取消创建。"
                    )
                )
                controller.keep(timeout=timeout, reset_timeout=True)
                return
            description = reply
            controller.stop()

        await description_waiter(event, SenderScopedSessionFilter())
        return description

    async def _wait_for_command_confirmation(
        self, event: AstrMessageEvent, timeout: int = 30
    ) -> bool:
        @session_waiter(timeout=timeout, record_history_chains=False)
        async def confirmation_waiter(
            controller, confirm_event: AstrMessageEvent
        ) -> None:
            reply = (confirm_event.message_str or "").strip()
            if reply in {"确认", "确定"}:
                controller.stop()
                return
            if reply in {"取消", "退出"}:
                await confirm_event.send(confirm_event.plain_result("已取消本次操作。"))
                controller.stop(ConfirmationCancelled())
                return
            await confirm_event.send(
                confirm_event.plain_result(
                    "请回复“确认”继续执行，或回复“取消”终止本次操作。"
                )
            )
            controller.keep(timeout=timeout, reset_timeout=True)

        try:
            await confirmation_waiter(event, SenderScopedSessionFilter())
            return True
        except TimeoutError:
            await event.send(event.plain_result("⌛ 等待确认超时，操作已取消。"))
            return False
        except ConfirmationCancelled:
            return False

    def _format_category_counts(
        self, category_counts: dict[str, int], limit: int = 8
    ) -> str:
        non_empty_items = [
            (c, cnt) for c, cnt in sorted(category_counts.items()) if cnt > 0
        ]
        if not non_empty_items:
            return "无可删除的表情包文件。"
        lines = [f"- {c}: {cnt} 个" for c, cnt in non_empty_items[:limit]]
        if len(non_empty_items) > limit:
            lines.append(f"- 其余 {len(non_empty_items) - limit} 个类型已省略")
        return "\n".join(lines)

    # ---------- 命令实现 ----------
    @meme_manager.command("查看图库")
    async def list_emotions(self, event: AstrMessageEvent):
        pack_context = self._resolve_runtime_pack_context(event=event)
        descriptions = pack_context.get("category_mapping") or self.category_mapping
        categories = "\n".join(
            [f"- {tag}: {desc}" for tag, desc in descriptions.items()]
        )
        yield event.plain_result(f"🖼️ 当前图库：\n{categories}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("添加分类")
    async def add_category_command(self, event: AstrMessageEvent, category: str = None):
        if not category:
            yield event.plain_result(
                "📌 若要添加分类，请按照此格式操作：\n"
                "/表情管理 添加分类 [类别名称] [描述]\n"
                "也可以只发送类别名称，随后按提示补充描述。"
            )
            return
        category = category.strip()
        available_categories = await asyncio.to_thread(self._get_manageable_categories)
        if category in available_categories:
            yield event.plain_result(f"ℹ️ 分类「{category}」已存在，无需重复创建。")
            return
        description = self._extract_category_description_from_command(event, category)
        if not description:
            yield event.plain_result(
                f"请发送分类「{category}」的描述，或发送“返回”取消创建。"
            )
            try:
                description = await self._wait_for_category_description(event, category)
            except TimeoutError:
                yield event.plain_result("⌛ 等待描述超时，已取消创建分类。")
                return
            except CategoryCreationCancelled:
                return
        try:
            created = await self._run_default_pack_mutation(
                "创建表情分类",
                lambda: self.category_manager.create_category(category, description),
            )
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        if not created:
            yield event.plain_result(f"❌ 创建分类「{category}」失败，请稍后重试。")
            return
        self._reload_personas()
        yield event.plain_result(f"✅ 已创建分类「{category}」：{description}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("添加表情")
    async def upload_meme(self, event: AstrMessageEvent, category: str = None):
        if not category:
            yield event.plain_result(
                "📌 若要添加表情，请按照此格式操作：\n/表情管理 添加表情 [类别名称]\n（输入/表情管理 查看图库 可获取类别列表）"
            )
            return
        descriptions = await asyncio.to_thread(self.category_manager.get_descriptions)
        if category not in descriptions:
            yield event.plain_result(
                f"您输入的表情包类别「{category}」是无效的哦。\n可以使用/表情管理 查看图库来查看可用的类别。"
            )
            return
        user_key = f"{event.session_id}_{event.get_sender_id()}"
        pack_context = self._resolve_runtime_pack_context(event=event)
        self.upload_states[user_key] = {
            "category": category,
            "pack_id": str(pack_context.get("pack_id") or ""),
            "memes_dir": str(pack_context.get("memes_dir") or ""),
            "expire_time": time.time() + 30,
        }
        yield event.plain_result(
            f"请在30秒内发送要添加到【{category}】类别的图片（可发送多张图片）。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("恢复默认表情包")
    async def restore_default_memes_command(
        self, event: AstrMessageEvent, category: str = None
    ):
        """从社区索引安装首个官方表情包并设为默认。"""
        if category:
            yield event.plain_result(
                "ℹ️ 该命令已改为从官方仓库安装默认包，不再支持按类别恢复。"
            )

        try:
            result = await self._run_guarded_runtime_file_operation(
                "安装官方资源包",
                install_first_official_pack_from_index,
                index_url=COMMUNITY_INDEX_URL,
                overwrite=False,
                set_as_default=True,
            )
            selected_name = str(
                result.get("selected_pack_name")
                or result.get("name")
                or result.get("pack_id")
                or ""
            )
            selected_pack_id = str(
                result.get("pack_id") or result.get("selected_pack_id") or ""
            )
            self._reload_personas()
            yield event.plain_result(
                f"✅ 已从官方仓库安装默认表情包：{selected_name} ({selected_pack_id})。"
            )
        except FileExistsError:
            yield event.plain_result(
                "⚠️ 目标表情包已存在。请先在广场或管理页卸载同名包后重试。"
            )
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
        except Exception as exc:
            logger.error("从官方仓库安装默认表情包失败: %s", exc, exc_info=True)
            yield event.plain_result(f"❌ 安装默认表情包失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("清空指定类型")
    async def clear_category_command(
        self, event: AstrMessageEvent, category: str = None
    ):
        """清空指定类型下的所有表情包，但保留类型本身。"""
        if not category:
            yield event.plain_result(
                "📌 若要清空指定类型，请按照此格式操作：\n/表情管理 清空指定类型 [类别名称]"
            )
            return

        category = category.strip()
        available_categories = await asyncio.to_thread(self._get_manageable_categories)
        if category not in available_categories:
            yield event.plain_result(
                f"⚠️ 未找到类型「{category}」。\n可先使用 /表情管理 查看图库 查看当前类型。"
            )
            return

        emoji_count = len(await asyncio.to_thread(get_emoji_by_category, category))
        if emoji_count == 0:
            yield event.plain_result(f"📭 类型「{category}」当前没有可清空的表情包。")
            return

        yield event.plain_result(
            f"⚠️ 即将清空类型「{category}」下的 {emoji_count} 个表情包，但会保留类型本身。\n"
            "请在 30 秒内回复“确认”继续执行，或回复“取消”终止本次操作。"
        )
        if not await self._wait_for_command_confirmation(event):
            return

        def mutate():
            clear_result = clear_category_emojis(category)
            if clear_result.get("deleted_files"):
                self._invalidate_default_pack_semantics()
            return clear_result

        try:
            result = await self._run_default_pack_mutation("清空表情分类", mutate)
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        deleted_count = len(result["deleted_files"])
        yield event.plain_result(
            f"✅ 已清空类型「{category}」，共删除 {deleted_count} 个表情包。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("清空全部")
    async def clear_all_emojis_command(self, event: AstrMessageEvent):
        """清空所有类型下的表情包，但保留类型和描述配置。"""
        available_categories = sorted(
            await asyncio.to_thread(self._get_manageable_categories)
        )
        category_counts = await asyncio.to_thread(
            lambda: {
                category: len(get_emoji_by_category(category))
                for category in available_categories
            }
        )
        total_count = sum(category_counts.values())

        if total_count == 0:
            yield event.plain_result("📭 当前没有可清空的表情包文件。")
            return

        category_count = sum(1 for count in category_counts.values() if count > 0)
        summary = self._format_category_counts(category_counts)
        yield event.plain_result(
            f"⚠️ 即将清空全部表情包，共 {total_count} 个文件，涉及 {category_count} 个类型。\n"
            "该操作会保留所有类型名称和描述配置。\n"
            f"{summary}\n"
            "请在 30 秒内回复“确认”继续执行，或回复“取消”终止本次操作。"
        )
        if not await self._wait_for_command_confirmation(event):
            return

        def mutate():
            clear_result = clear_all_emojis()
            if any(clear_result.get("deleted_by_category", {}).values()):
                self._invalidate_default_pack_semantics()
            return clear_result

        try:
            result = await self._run_default_pack_mutation("清空全部表情图片", mutate)
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        deleted_total = sum(result["deleted_by_category"].values())
        yield event.plain_result(
            f"✅ 已清空全部表情包，共删除 {deleted_total} 个文件，类型配置已保留。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("删除类型本身")
    async def delete_category_command(
        self, event: AstrMessageEvent, category: str = None
    ):
        """删除指定类型本身，同时移除其描述配置和本地文件夹。"""
        if not category:
            yield event.plain_result(
                "📌 若要删除类型本身，请按照此格式操作：\n/表情管理 删除类型本身 [类别名称]"
            )
            return

        category = category.strip()
        available_categories = await asyncio.to_thread(self._get_manageable_categories)
        if category not in available_categories:
            yield event.plain_result(
                f"⚠️ 未找到类型「{category}」。\n可先使用 /表情管理 查看图库 查看当前类型。"
            )
            return

        emoji_count = len(await asyncio.to_thread(get_emoji_by_category, category))
        yield event.plain_result(
            f"⚠️ 即将删除类型「{category}」本身，并移除其描述配置"
            f"{f'，同时删除其中的 {emoji_count} 个表情包' if emoji_count > 0 else ''}。\n"
            "该操作不可恢复。\n"
            "请在 30 秒内回复“确认”继续执行，或回复“取消”终止本次操作。"
        )
        if not await self._wait_for_command_confirmation(event):
            return

        try:
            deleted = await self._run_default_pack_mutation(
                "删除表情分类",
                lambda: self.category_manager.delete_category(category),
            )
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        if not deleted:
            yield event.plain_result(f"❌ 删除类型「{category}」失败，请稍后重试。")
            return

        self._reload_personas()
        yield event.plain_result(
            f"✅ 已删除类型「{category}」"
            f"{f'，并移除 {emoji_count} 个表情包。' if emoji_count > 0 else '。'}"
        )

    # 同步相关命令
    @meme_manager.command("同步状态")
    async def check_sync_status(self, event: AstrMessageEvent, detail: str = None):
        """检查表情包与图床的同步状态"""
        sync_client = self._ensure_img_sync_for_pack()
        if not sync_client:
            yield event.plain_result(
                "图床服务尚未配置，请先在插件页面的配置中完成图床配置哦。"
            )
            return

        try:
            # 获取图床配置信息
            provider_name = sync_client.provider.__class__.__name__
            if hasattr(sync_client.provider, "bucket_name"):
                storage_info = f"存储桶: {sync_client.provider.bucket_name}"
            elif hasattr(sync_client.provider, "album_id"):
                storage_info = f"相册ID: {sync_client.provider.album_id}"
            else:
                storage_info = "未知存储类型"

            # 获取同步状态
            status = await asyncio.to_thread(sync_client.check_status)
            to_upload = status.get("to_upload", [])
            to_download = status.get("to_download", [])

            # 统计信息
            result = [
                "📊 图床同步状态报告",
                "",
                f"🔧 图床服务: {provider_name}",
                f"📁 {storage_info}",
                "",
                "📈 文件统计:",
                f"  • 需要上传: {len(to_upload)} 个文件",
                f"  • 需要下载: {len(to_download)} 个文件",
                f"  • 同名冲突或待确认: {status.get('conflict_count', 0)} 个文件",
                "",
            ]

            # 分类统计
            upload_categories = {}
            download_categories = {}

            for file in to_upload:
                cat = file.get("category", "未分类")
                upload_categories[cat] = upload_categories.get(cat, 0) + 1

            for file in to_download:
                cat = file.get("category", "未分类")
                download_categories[cat] = download_categories.get(cat, 0) + 1

            # 显示上传分类统计
            if upload_categories:
                result.append("📤 待上传文件分类:")
                for cat, count in sorted(
                    upload_categories.items(), key=lambda x: x[1], reverse=True
                ):
                    result.append(f"  • {cat}: {count} 个")
                result.append("")

            # 显示下载分类统计
            if download_categories:
                result.append("📥 待下载文件分类:")
                for cat, count in sorted(
                    download_categories.items(), key=lambda x: x[1], reverse=True
                ):
                    result.append(f"  • {cat}: {count} 个")
                result.append("")

            # 显示文件详情（最多各显示5个）
            if to_upload:
                result.append("📤 待上传文件示例（前5个）:")
                for file in to_upload[:5]:
                    result.append(
                        f"  • {file.get('category', '未分类')}/{file['filename']}"
                    )
                if len(to_upload) > 5:
                    result.append(f"  • ...还有 {len(to_upload) - 5} 个文件")
                result.append("")

            if to_download:
                result.append("📥 待下载文件示例（前5个）:")
                for file in to_download[:5]:
                    result.append(
                        f"  • {file.get('category', '未分类')}/{file['filename']}"
                    )
                if len(to_download) > 5:
                    result.append(f"  • ...还有 {len(to_download) - 5} 个文件")
                result.append("")

            # 同步状态总结
            if status.get("is_synced"):
                result.append("✅ 云端与本地图库已经完全同步啦！")

                # 如果用户要求详细信息，显示更多内容
                if detail and detail.strip() == "详细":
                    result.append("")
                    result.append("📋 详细信息:")

                    # 显示所有文件类别的统计
                    try:
                        if hasattr(sync_client.provider, "get_image_list"):
                            remote_images = await asyncio.to_thread(
                                sync_client.provider.get_image_list
                            )
                            remote_stats = {}
                            for img in remote_images:
                                cat = img.get("category", "未分类")
                                remote_stats[cat] = remote_stats.get(cat, 0) + 1

                            if remote_stats:
                                result.append("📂 云端文件分类详情:")
                                for cat, count in sorted(
                                    remote_stats.items(),
                                    key=lambda x: x[1],
                                    reverse=True,
                                ):
                                    result.append(f"  • {cat}: {count} 个")

                                # 显示文件总数
                                result.append(
                                    f"📊 云端总计: {len(remote_images)} 个文件"
                                )
                            else:
                                result.append("📂 云端无文件")
                    except Exception as e:
                        result.append(f"⚠️ 获取云端详情失败: {str(e)}")

                    # 显示本地图库统计
                    local_stats = {}
                    local_total = 0
                    local_memes_dir = str(
                        getattr(
                            sync_client,
                            "local_dir",
                            self._resolve_runtime_pack_context().get("memes_dir"),
                        )
                    )
                    if os.path.exists(local_memes_dir):
                        for category in os.listdir(local_memes_dir):
                            category_path = os.path.join(local_memes_dir, category)
                            if os.path.isdir(category_path):
                                files = [
                                    f
                                    for f in os.listdir(category_path)
                                    if f.endswith(
                                        (".jpg", ".jpeg", ".png", ".gif", ".webp")
                                    )
                                ]
                                count = len(files)
                                local_stats[category] = count
                                local_total += count

                    if local_stats:
                        result.append("")
                        result.append("📂 本地文件分类详情:")
                        for cat, count in sorted(
                            local_stats.items(), key=lambda x: x[1], reverse=True
                        ):
                            result.append(f"  • {cat}: {count} 个")
                        result.append(f"📊 本地总计: {local_total} 个文件")
                    else:
                        result.append("")
                        result.append("📂 本地无文件")
            else:
                result.append("⏳ 需要同步以保持云端与本地图库一致")
                if status.get("conflicts"):
                    result.append(
                        "⚠️ 同名冲突会保留，请检查后选择“覆盖到云端”或“从云端覆盖”。"
                    )
                    for item in status["conflicts"][:5]:
                        result.append(f"  • {item['relative_path']}")
                result.append(
                    "💡 使用 '/表情管理 同步到云端' 或 '/表情管理 从云端同步' 进行同步"
                )

            # 上传记录统计（如果有的话）
            if (
                hasattr(sync_client.sync_manager, "upload_tracker")
                and sync_client.sync_manager.upload_tracker
            ):
                try:
                    # 获取上传记录总数
                    if hasattr(
                        sync_client.sync_manager.upload_tracker, "get_uploaded_files"
                    ):
                        uploaded_files = (
                            sync_client.sync_manager.upload_tracker.get_uploaded_files()
                        )
                        result.append("")
                        result.append(
                            f"📝 上传记录: 已记录 {len(uploaded_files)} 个文件"
                        )
                except Exception:
                    pass  # 忽略获取上传记录时的错误

            yield event.plain_result("\n".join(result))
        except Exception as e:
            logger.error(f"检查同步状态失败: {str(e)}")
            yield event.plain_result(f"检查同步状态失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("同步到云端")
    async def sync_to_remote(self, event: AstrMessageEvent):
        """将本地表情包同步到云端"""
        sync_client = self._ensure_img_sync_for_pack()
        if not sync_client:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result("⚡ 正在开启云端同步任务...")
            success = await sync_client.start_sync("upload")
            if success:
                yield event.plain_result("云端同步已完成！")
            else:
                yield event.plain_result("云端同步失败，请查看日志哦。")
        except Exception as e:
            logger.error(f"同步到云端失败: {str(e)}")
            yield event.plain_result(f"同步到云端失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("从云端同步")
    async def sync_from_remote(self, event: AstrMessageEvent):
        """从云端同步表情包到本地"""
        sync_client = self._ensure_img_sync_for_pack()
        if not sync_client:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        pack_id = str(
            getattr(self, "_img_sync_pack_id", "")
            or self._resolve_runtime_pack_context().get("pack_id")
        ).strip()
        try:
            self.semantic_task_manager.begin_external_pack_operation(
                pack_id, "从图床下载表情包"
            )
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        try:
            yield event.plain_result("开始从云端进行同步...")
            success = await sync_client.start_sync("download")
            if success:
                yield event.plain_result("从云端同步已完成！")
                # 重新加载表情配置
                await self.reload_emotions()
            else:
                yield event.plain_result("从云端同步失败，请查看日志哦。")
        except Exception as e:
            logger.error(f"从云端同步失败: {str(e)}")
            yield event.plain_result(f"从云端同步失败: {str(e)}")
        finally:
            try:
                pack_dir = Path(sync_client.local_dir).parent
                if (pack_dir / "semantic_metadata.json").is_file():
                    await asyncio.to_thread(invalidate_semantic_metadata, pack_dir)
            except Exception as exc:
                logger.warning("图片同步后无法使语义元数据失效：%s", exc)
            finally:
                self.semantic_task_manager.end_external_pack_operation(pack_id)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("覆盖到云端")
    async def overwrite_to_remote(self, event: AstrMessageEvent):
        """让云端完全和本地一致（会删除云端多出的图）"""
        sync_client = self._ensure_img_sync_for_pack()
        if not sync_client:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result(
                "⚠️ 正在执行覆盖到云端任务（将清理云端多余文件）..."
            )
            success = await sync_client.start_sync("overwrite_to_remote")
            if success:
                yield event.plain_result(
                    "覆盖到云端任务已完成！云端现在与本地完全一致。"
                )
            else:
                yield event.plain_result("任务失败，请查看日志。")
        except Exception as e:
            logger.error(f"覆盖到云端失败: {str(e)}")
            yield event.plain_result(f"覆盖到云端失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("从云端覆盖")
    async def overwrite_from_remote(self, event: AstrMessageEvent):
        """让本地完全和云端一致（会删除本地多出的图）"""
        sync_client = self._ensure_img_sync_for_pack()
        if not sync_client:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        pack_id = str(
            getattr(self, "_img_sync_pack_id", "")
            or self._resolve_runtime_pack_context().get("pack_id")
        ).strip()
        try:
            self.semantic_task_manager.begin_external_pack_operation(
                pack_id, "从远端覆盖本地表情包"
            )
        except RuntimeError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        try:
            yield event.plain_result(
                "⚠️ 正在执行从云端覆盖任务（将清理本地多余文件）..."
            )
            success = await sync_client.start_sync("overwrite_from_remote")
            if success:
                yield event.plain_result(
                    "从云端覆盖任务已完成！本地现在与云端完全一致。"
                )
            else:
                yield event.plain_result("任务失败，请查看日志。")
        except Exception as e:
            logger.error(f"从云端覆盖失败: {str(e)}")
            yield event.plain_result(f"从云端覆盖失败: {str(e)}")
        finally:
            try:
                pack_dir = Path(sync_client.local_dir).parent
                if (pack_dir / "semantic_metadata.json").is_file():
                    await asyncio.to_thread(invalidate_semantic_metadata, pack_dir)
            except Exception as exc:
                logger.warning("图片同步后无法使语义元数据失效：%s", exc)
            finally:
                self.semantic_task_manager.end_external_pack_operation(pack_id)

    @meme_manager.command("图库统计")
    async def show_library_stats(self, event: AstrMessageEvent):
        """显示图库详细统计信息"""
        try:
            sync_client = self._ensure_img_sync_for_pack()
            result = ["📊 表情包图库统计报告", "", "📁 本地图库统计:"]

            # 统计本地文件
            local_stats = {}
            local_total = 0

            default_memes_dir = self._resolve_runtime_pack_context().get("memes_dir")
            local_memes_dir = str(
                getattr(sync_client, "local_dir", default_memes_dir)
                if sync_client
                else default_memes_dir
            )
            if os.path.exists(local_memes_dir):
                for category in os.listdir(local_memes_dir):
                    category_path = os.path.join(local_memes_dir, category)
                    if os.path.isdir(category_path):
                        files = [
                            f
                            for f in os.listdir(category_path)
                            if f.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                        ]
                        count = len(files)
                        local_stats[category] = count
                        local_total += count

            # 显示本地统计
            if local_stats:
                result.append(f"  • 总文件数: {local_total} 个")
                result.append(f"  • 分类数: {len(local_stats)} 个")
                result.append("")
                result.append("📂 本地分类详情:")
                for cat, count in sorted(
                    local_stats.items(), key=lambda x: x[1], reverse=True
                ):
                    result.append(f"  • {cat}: {count} 个")
            else:
                result.append("  • 本地图库为空")

            # 云端统计（如果配置了图床）
            if sync_client:
                result.append("")
                result.append("☁️ 云端图库统计:")

                try:
                    remote_images = await asyncio.to_thread(
                        sync_client.provider.get_image_list
                    )
                    remote_stats = {}
                    remote_total = len(remote_images)

                    for img in remote_images:
                        cat = img.get("category", "未分类")
                        remote_stats[cat] = remote_stats.get(cat, 0) + 1

                    result.append(f"  • 总文件数: {remote_total} 个")
                    result.append(f"  • 分类数: {len(remote_stats)} 个")
                    result.append("")
                    result.append("📂 云端分类详情:")
                    for cat, count in sorted(
                        remote_stats.items(), key=lambda x: x[1], reverse=True
                    ):
                        result.append(f"  • {cat}: {count} 个")

                    # 对比统计
                    result.append("")
                    result.append("📈 本地与云端对比:")
                    result.append(f"  • 本地文件: {local_total} 个")
                    result.append(f"  • 云端文件: {remote_total} 个")

                    if local_total > remote_total:
                        result.append(
                            f"  • 本地比云端多 {local_total - remote_total} 个文件"
                        )
                    elif remote_total > local_total:
                        result.append(
                            f"  • 云端比本地多 {remote_total - local_total} 个文件"
                        )
                    else:
                        result.append("  • 本地与云端文件数相同")

                    # 分类对比
                    local_categories = set(local_stats.keys())
                    remote_categories = set(remote_stats.keys())

                    only_local = local_categories - remote_categories
                    only_remote = remote_categories - local_categories
                    common_categories = local_categories & remote_categories

                    if only_local:
                        result.append(
                            f"  • 仅本地有的分类: {', '.join(sorted(only_local))}"
                        )
                    if only_remote:
                        result.append(
                            f"  • 仅云端有的分类: {', '.join(sorted(only_remote))}"
                        )
                    if common_categories:
                        result.append(f"  • 共同分类: {len(common_categories)} 个")

                except Exception as e:
                    result.append(f"  • 获取云端统计失败: {str(e)}")
            else:
                result.append("")
                result.append("☁️ 云端图库: 未配置")

            # 存储空间估算
            result.append("")
            result.append("💾 存储空间估算:")
            if local_total > 0:
                # 假设平均每个文件 500KB
                estimated_size = local_total * 500 / 1024  # 转换为MB
                result.append(f"  • 本地图库约: {estimated_size:.1f} MB")

            if sync_client and "remote_total" in locals():
                estimated_remote_size = remote_total * 500 / 1024
                result.append(f"  • 云端图库约: {estimated_remote_size:.1f} MB")

            yield event.plain_result("\n".join(result))

        except Exception as e:
            logger.error(f"获取图库统计失败: {str(e)}")
            yield event.plain_result(f"获取图库统计失败: {str(e)}")

    # ---------- [patcher:llm-free-random] 两级骰子发图 ----------
    def _llm_free_settings(self) -> dict:
        """读取插件配置 generation.llm_free_random.*，缺键/非法值时回退模块常量。

        把「概率」从硬编码提升为面板可调项（2026-10-11）。面板保存会触发
        plugin_manager.reload() → 本方法下次被调用即读到新值。
        """
        cfg = getattr(self, "config", None)
        node = None
        if isinstance(cfg, dict):
            gen = cfg.get("generation")
            if isinstance(gen, dict):
                candid = gen.get("llm_free_random")
                if isinstance(candid, dict):
                    node = candid

        def _get(key, fallback):
            if node is not None and key in node and node.get(key) is not None:
                return node.get(key)
            return fallback

        def _to_bool(value, fallback):
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(fallback)

        def _to_float(value, fallback):
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(fallback)

        def _to_int(value, fallback):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return int(fallback)

        raw_exclude = _get("exclude_categories", None)
        if isinstance(raw_exclude, (list, tuple, set)):
            exclude = {str(x).strip() for x in raw_exclude if str(x).strip()}
        elif isinstance(raw_exclude, str) and raw_exclude.strip():
            parts = [t.strip() for t in re.split(r"[,\s]+", raw_exclude)]
            exclude = {p for p in parts if p}
        else:
            exclude = set(LLM_FREE_RANDOM_EXCLUDE_CATEGORIES)

        return {
            "enabled": _to_bool(_get("enabled", None), LLM_FREE_RANDOM_ENABLED),
            "active": _to_bool(_get("active", None), LLM_FREE_RANDOM_ACTIVE),
            "probability": _to_float(
                _get("probability", None), LLM_FREE_RANDOM_PROBABILITY
            ),
            "exclude_categories": exclude,
            "api_enabled": _to_bool(_get("api_enabled", None), LLM_FREE_API_ENABLED),
            "api_probability": _to_float(
                _get("api_probability", None), LLM_FREE_API_PROBABILITY
            ),
            "api_provider_id": str(
                _get("api_provider_id", None) or LLM_FREE_API_PROVIDER_ID
            ),
            "api_timeout": _to_float(_get("api_timeout", None), LLM_FREE_API_TIMEOUT),
            "api_daily_limit": _to_int(
                _get("api_daily_limit", None), LLM_FREE_API_DAILY_LIMIT
            ),
            "api_cooldown": _to_float(
                _get("api_cooldown", None), LLM_FREE_API_COOLDOWN
            ),
        }

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100)
    async def _llm_free_random_meme(self, event: AstrMessageEvent):
        """不调用聊天模型，按两级概率决定是否发图、以及是否用模型选分类。

        ⚠️ 副作用修复（[patcher:qq-config]，2026-10-12）：本 handler 的 priority=-100
        最低，运行在「表情包」等显式指令之后。若不拦截，@bot 发「表情包」时本 handler
        仍有 `probability`% 概率再补一张 → **双发**。故显式指令会置 extra
        `meme_manager_skip_random=True`，此处直接早返回。其他插件已经回复时也不再附赠图片。
        """
        if getattr(event, "_has_send_oper", False):
            return
        try:
            if event.get_extra("meme_manager_skip_random"):
                return
        except Exception:
            pass
        settings = self._llm_free_settings()
        if not settings["enabled"]:
            return
        if settings["active"]:
            # 主动模式：群里任何消息都可触发（不要求 @ 或唤醒前缀）
            if not getattr(event, "is_wake", False):
                return
        elif not getattr(event, "is_at_or_wake_command", False):
            return

        # 显式命令（以唤醒前缀开头）不附赠表情
        try:
            text = (event.get_message_str() or "").strip()
        except Exception:
            text = ""
        for prefix in self._llm_free_wake_prefixes():
            if prefix and text.startswith(prefix):
                return

        # 骰子 1：这一轮到底发不发图（未命中即零开销返回）
        if not self._llm_free_probability_hit(settings["probability"]):
            return

        try:
            # Q25=甲：非模型路径合并「该会话绑定集合」的全部候选池。
            from ..backend.packs.resolver import get_pack_paths

            pack_ids = self._get_runtime_pack_ids_for_event(event)
            memes_roots = [
                get_pack_paths(pack_id)["memes_dir"] for pack_id in pack_ids
            ]
        except Exception as e:
            logger.error(f"[meme_manager] 免聊天模型发图：解析图库目录失败: {e}")
            return

        # 骰子 2：随机抽 还是 报送 API
        category = ""
        route = "random"
        if settings["api_enabled"] and self._llm_free_probability_hit(
            settings["api_probability"]
        ):
            if self._llm_free_api_allowed(event, settings):
                route = "api"
                category = await self._llm_free_pick_category_by_api(
                    event, text, settings
                )
                if not category:
                    route = "api_fallback"

        picked = await self._llm_free_pick_image(memes_roots, category, settings)
        if not picked:
            return

        logger.info(
            "[meme_manager] 免聊天模型发图：route=%s category=%s file=%s",
            route,
            category or "-",
            picked,
        )

        final_path = picked
        try:
            final_path = await asyncio.to_thread(self._convert_to_gif, picked)
            await self._send_meme_image(event, Image.fromFileSystem(final_path))
        except Exception as e:
            logger.error(f"[meme_manager] 免聊天模型发图：发送失败: {e}")
        finally:
            if final_path != picked and os.path.exists(final_path):
                try:
                    os.remove(final_path)
                except Exception:
                    pass

    def _llm_free_probability_hit(self, probability) -> bool:
        """掷一次骰子，probability 为命中百分比（0~100，**支持小数**如 0.5）。

        ⚠️ 必须用浮点：旧版 `int(probability)` 会把 0.5 截成 0 再被夹到 1，
        导致小数概率失效（2026-10-11 修）。
        """
        try:
            value = float(probability)
        except Exception:
            value = 100.0
        if value >= 100.0:
            return True
        if value <= 0.0:
            return False
        return (random.random() * 100.0) < value

    def _llm_free_api_allowed(self, event: AstrMessageEvent, settings: dict) -> bool:
        """日配额 + 每会话冷却，防止被刷消息推高 API 成本。"""
        state = getattr(self, "_llm_free_api_state", None)
        if not isinstance(state, dict):
            state = {"day": "", "used": 0, "last": {}}
            self._llm_free_api_state = state

        today = time.strftime("%Y%m%d")
        if state.get("day") != today:
            state["day"] = today
            state["used"] = 0
            state["last"] = {}

        try:
            limit = int(settings.get("api_daily_limit") or 0)
        except Exception:
            limit = 0
        if limit > 0 and int(state.get("used") or 0) >= limit:
            return False

        try:
            cooldown = float(settings.get("api_cooldown") or 0.0)
        except Exception:
            cooldown = 0.0
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        last = state.get("last") or {}
        if cooldown > 0 and (time.time() - float(last.get(umo) or 0)) < cooldown:
            return False

        last[umo] = time.time()
        state["last"] = last
        state["used"] = int(state.get("used") or 0) + 1
        return True

    async def _llm_free_pick_category_by_api(
        self, event: AstrMessageEvent, text: str, settings: dict
    ) -> str:
        """报送 API：让模型读用户消息，从可用分类里挑一个键。

        只接受与现存分类键**完全一致**的 token，杜绝模型幻觉。
        返回空字符串表示「交给调用方回落随机」。
        """
        mapping = getattr(self, "category_mapping", None) or {}
        if not isinstance(mapping, dict) or not mapping:
            return ""
        exclude = settings.get("exclude_categories") or set()
        allowed = {
            str(k).strip(): str(v)
            for k, v in mapping.items()
            if str(k).strip() and str(k).strip() not in exclude
        }
        if not allowed or not text:
            return ""

        options = "\n".join(f"{key} - {desc}" for key, desc in allowed.items())
        prompt = (
            "任务：为下面这条聊天消息挑一个最贴切的表情分类，仅用于发送一张表情图片。\n"
            "只输出一个分类键；没有任何分类贴切时输出 none。\n"
            "不要输出解释、标点、代码框或任何多余内容。\n\n"
            f"可选分类：\n{options}\n\n"
            f"聊天消息：{text[:500]}"
        )

        kwargs: dict = {"prompt": prompt}
        provider_id = str(settings.get("api_provider_id") or "").strip()
        if provider_id:
            kwargs["chat_provider_id"] = provider_id

        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(**kwargs),
                timeout=float(settings.get("api_timeout") or 0) or None,
            )
        except Exception as e:
            logger.warning(f"[meme_manager] 免聊天模型发图：API 选分类失败: {e}")
            return ""

        raw = str(getattr(response, "completion_text", "") or "").strip()
        if not raw:
            return ""
        for token in re.split(r"[^A-Za-z0-9_\-]+", raw):
            token = token.strip()
            if token and token in allowed:
                return token
        return ""

    async def _llm_free_pick_image(
        self, memes_root, category: str = "", settings: dict = None
    ) -> str:
        """按分类抽一张；无分类或该分类为空时回落全池随机。

        `memes_root` 可传单个路径或路径列表（Q25=甲：非模型路径把多个绑定包的
        同名分类合并成同一个候选池一起抽）。
        """
        if isinstance(memes_root, (list, tuple, set)):
            roots = [Path(item) for item in memes_root if item]
        else:
            roots = [Path(memes_root)]
        roots = [root for root in roots if root.is_dir()]
        if not roots:
            return ""

        exclude = set(LLM_FREE_RANDOM_EXCLUDE_CATEGORIES)
        if isinstance(settings, dict):
            exclude = settings.get("exclude_categories") or exclude

        if category:
            category_candidates: list[str] = []
            for root in roots:
                try:
                    category_dir = resolve_safe_category_directory(root, category)
                except Exception:
                    category_dir = None
                if category_dir and category_dir.is_dir():
                    category_candidates.extend(
                        str(category_dir / name)
                        for name in os.listdir(category_dir)
                        if name.lower().endswith(IMAGE_EXTENSIONS)
                    )
            if category_candidates:
                return random.choice(category_candidates)

        candidates: list[str] = []
        for root in roots:
            try:
                for category_dir in sorted(root.iterdir()):
                    if not category_dir.is_dir():
                        continue
                    if category_dir.name in exclude:
                        continue
                    for name in os.listdir(category_dir):
                        if name.lower().endswith(IMAGE_EXTENSIONS):
                            candidates.append(str(category_dir / name))
            except Exception as e:
                logger.error(f"[meme_manager] 免聊天模型发图：扫描图库失败: {e}")

        return random.choice(candidates) if candidates else ""

    def _llm_free_wake_prefixes(self) -> tuple:
        """当前配置的唤醒前缀，用于排除显式命令。"""
        try:
            cfg = self.context.get_config()
            prefixes = (cfg.get("wake_prefix") or []) if hasattr(cfg, "get") else []
        except Exception:
            prefixes = []
        return tuple(str(p).strip() for p in prefixes if str(p).strip())

    # ========== [patcher:qq-config] 「表情包」随机发图 ==========
    @filter.command("表情包")
    async def random_meme_command(self, event: AstrMessageEvent):
        """全图库随机发一张表情包。

        Q12=c：覆盖全部表情包，但跳过 `needs_review` 待审分类；
        Q13=b：1 张、纯图无文字、同一用户 3 秒防抖；
        Q14=a：任何人可用（管理类指令另有 ADMIN 限制兜底）。
        """
        # 副作用修复：抑制 llm_free_random 对本轮再补一张（否则会双发）
        try:
            event.set_extra("meme_manager_skip_random", True)
        except Exception:
            pass

        cooldown_key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
        now = time.time()
        last = _RANDOM_MEME_LAST_HIT.get(cooldown_key, 0.0)
        if now - last < RANDOM_MEME_COOLDOWN_SECONDS:
            remain = max(1, int(RANDOM_MEME_COOLDOWN_SECONDS - (now - last) + 0.999))
            yield event.plain_result(f"⌛ 慢一点，{remain} 秒后再试～")
            return
        _RANDOM_MEME_LAST_HIT[cooldown_key] = now

        try:
            picked = await asyncio.to_thread(
                self._random_meme_across_packs, event
            )
        except Exception as exc:
            logger.error("[meme_manager] 「表情包」随机取图失败：%s", exc)
            picked = ""
        if not picked:
            yield event.plain_result("📭 图库里还没有可发送的表情包。")
            return

        final_path = picked
        try:
            final_path = await asyncio.to_thread(self._convert_to_gif, picked)
        except Exception:
            final_path = picked
        if final_path != picked:
            # 临时 GIF 交给 after_message_sent 统一清理（本插件既有机制）
            existing = event.get_extra("meme_manager_temp_files") or []
            event.set_extra("meme_manager_temp_files", list(existing) + [final_path])
        logger.info("[meme_manager] 「表情包」指令命中：file=%s", picked)
        yield event.image_result(final_path)

    def _random_meme_across_packs(self, event: AstrMessageEvent | None = None) -> str:
        """在该会话绑定的表情包集合里随机抽一张（跳过 needs_review 待审分类）。

        Q11=甲：由「跨全部包」收窄为「跨该群绑定集合」；未绑定走默认包。
        """
        from ..backend.gallery_capacity import list_pack_ids, pack_memes_dir

        pack_ids = None
        if event is not None:
            try:
                pack_ids = self._get_runtime_pack_ids_for_event(event)
            except Exception:
                pack_ids = None
        if not pack_ids:
            pack_ids = list_pack_ids()

        candidates: list = []
        for pack_id in pack_ids:
            memes_dir = pack_memes_dir(pack_id)
            if not memes_dir.is_dir():
                continue
            try:
                category_dirs = sorted(memes_dir.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for category_dir in category_dirs:
                if not category_dir.is_dir() or category_dir.is_symlink():
                    continue
                if category_dir.name in RANDOM_MEME_EXCLUDE_CATEGORIES:
                    continue
                try:
                    names = os.listdir(category_dir)
                except OSError:
                    continue
                for name in names:
                    if name.lower().endswith(IMAGE_EXTENSIONS):
                        candidates.append(str(category_dir / name))
        if not candidates:
            return ""
        return random.choice(candidates)

    # ========== [patcher:qq-config] 参数查看 / 修改 ==========
    def _param_effective_value(self, spec: dict):
        """读取参数的「当前实际值」。

        [patcher:group-gallery] `selection.default_pack_id` 的真值**不在插件 config**
        里，而在 `selection_rules.json` 的 default 规则上（`set_default_pack` 写那里）。
        不特判的话，指令里会永远显示静态默认值，改过之后看着像没生效。
        """
        if spec.get("path") == "selection.default_pack_id":
            try:
                from ..backend.packs import storage as pack_storage

                rule = pack_storage.get_pack_rule("default", "")
            except Exception:
                rule = None
            if isinstance(rule, dict):
                ids = rule.get("pack_ids")
                if isinstance(ids, list) and ids:
                    return str(ids[0])
                single = str(rule.get("pack_id") or "").strip()
                if single:
                    return single
        value = meme_qq.get_by_path(self.config, spec["path"])
        return spec["default"] if value is None else value

    def _params_listing(self) -> str:
        """把可调 + 只读参数渲染成手机友好的清单。"""
        lines = ["🔧 表情包可调参数", "改法：/表情管理 设置 <参数名> <值>", ""]
        current_group = None
        for spec in meme_qq.PARAMS:
            if spec["group"] != current_group:
                current_group = spec["group"]
                lines.append(f"【{current_group}】")
            value = self._param_effective_value(spec)
            lines.append(f"  {spec['alias']} = {meme_qq.format_value(spec, value)}")
        lines.append("")
        lines.append("【只读（指令不可改，请用插件面板）】")
        for spec in meme_qq.READONLY:
            value = meme_qq.get_by_path(self.config, spec["path"])
            lines.append(
                f"  {spec['alias']} = {meme_qq.format_value(spec, value)}"
                f"（{spec['desc']}）"
            )
        lines.append("")
        lines.append("提示：值填「默认」可还原；参数名也接受完整配置路径。")
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("参数")
    async def show_config_params(self, event: AstrMessageEvent, key: str = ""):
        """查看可调参数：不填键名列出全部，填键名看单项。"""
        token = str(key or "").strip()
        if not token:
            yield event.plain_result(self._params_listing())
            return
        spec = meme_qq.resolve(token)
        if spec is None:
            readonly = meme_qq.is_readonly(token)
            if readonly is not None:
                value = meme_qq.get_by_path(self.config, readonly["path"])
                yield event.plain_result(
                    f"ℹ️「{readonly['alias']}」= "
                    f"{meme_qq.format_value(readonly, value)}（只读项，指令不可改）\n"
                    f"说明：{readonly['desc']}\n路径：{readonly['path']}"
                )
                return
            yield event.plain_result(
                f"⚠️ 未收录参数「{token}」。\n用 /表情管理 参数 查看全部可调项。"
            )
            return
        value = self._param_effective_value(spec)
        yield event.plain_result(
            f"🔧 {spec['alias']} = {meme_qq.format_value(spec, value)}\n"
            f"说明：{spec['desc']}\n"
            f"路径：{spec['path']}\n"
            f"改法：/表情管理 设置 {spec['alias']} <值>"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("设置")
    async def set_config_param(
        self, event: AstrMessageEvent, key: str = "", value: str = ""
    ):
        """修改可调参数：/表情管理 设置 <参数名> <值|默认>。"""
        token = str(key or "").strip()
        if not token:
            yield event.plain_result(
                "📌 用法：/表情管理 设置 <参数名> <值>\n"
                "用 /表情管理 参数 查看全部可调项。"
            )
            return
        spec = meme_qq.resolve(token)
        if spec is None:
            readonly = meme_qq.is_readonly(token)
            if readonly is not None:
                yield event.plain_result(
                    f"🚫 「{readonly['alias']}」是只读项（{readonly['desc']}），"
                    "不能在指令里修改，请到插件面板调整。"
                )
                return
            yield event.plain_result(
                f"⚠️ 未收录参数「{token}」。\n用 /表情管理 参数 查看全部可调项。"
            )
            return

        if not str(value or "").strip():
            current = self._param_effective_value(spec)
            yield event.plain_result(
                f"📌 用法：/表情管理 设置 {spec['alias']} <值>\n"
                f"当前：{meme_qq.format_value(spec, current)}（{spec['desc']}）\n"
                f"值填「默认」可还原为 "
                f"{meme_qq.format_value(spec, spec['default'])}。"
            )
            return

        ok, parsed = meme_qq.parse_value(spec, value)
        if not ok:
            yield event.plain_result(f"❌ 「{spec['alias']}」设置失败：{parsed}")
            return

        if spec["path"] == "selection.default_pack_id":
            # [patcher:group-gallery] 默认图库：直接改 selection_rules.json 的
            # default 规则（唯一真相源），改完立即生效，无需重载插件。
            from ..backend.packs import storage as pack_storage

            try:
                result = await asyncio.to_thread(
                    pack_storage.set_default_pack, parsed
                )
            except Exception as exc:
                yield event.plain_result(f"❌ 设置默认图库失败：{exc}")
                return
            yield event.plain_result(
                f"✅ 默认图库已设为 {result['pack_id']}（改完立即生效）。"
            )
            return

        previous = meme_qq.get_by_path(self.config, spec["path"])
        if previous is None:
            previous = spec["default"]
        try:
            self._write_config_path(spec["path"], parsed)
        except Exception as exc:
            logger.error("[meme_manager] 指令写配置失败：%s", exc, exc_info=True)
            yield event.plain_result(f"❌ 写入插件配置失败：{exc}")
            return

        if self._can_hot_reload():
            scheduled = self._schedule_plugin_reload()
            tail = (
                "已保存，1~2 秒后自动重载生效。"
                if scheduled
                else "已保存，但未能安排自动重载，请到面板点一次保存或重载插件。"
            )
        else:
            tail = (
                "已保存。当前有语义 / 图床 / 安装任务在跑，为安全起见未自动重载；"
                "任务结束后到面板点一次保存即可生效。"
            )
        logger.info(
            "[meme_manager] 指令改配置：%s %s -> %s",
            spec["path"],
            previous,
            parsed,
        )
        yield event.plain_result(
            f"✅ {spec['alias']}：{meme_qq.format_value(spec, previous)}"
            f" → {meme_qq.format_value(spec, parsed)}\n{tail}"
        )

    def _write_config_path(self, path: str, value) -> None:
        """按点路径写入 AstrBotConfig 并原子落盘（BOM+CRLF 由 save_config 保证）。"""
        if not hasattr(self.config, "save_config"):
            raise RuntimeError("当前未注册可编辑的 AstrBot 配置")
        keys = str(path).split(".")
        snapshot = copy.deepcopy(dict(self.config))
        updated = copy.deepcopy(snapshot)
        node = updated
        for key in keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        node[keys[-1]] = value
        try:
            self.config.clear()
            self.config.save_config(updated)
        except Exception:
            self.config.clear()
            self.config.update(snapshot)
            raise
        # 同步内存配置，未重载的窗口内也能读到新值
        try:
            self.config.clear()
            self.config.update(updated)
        except Exception:
            pass

    def _can_hot_reload(self) -> bool:
        """热重载安全前置检查：语义任务 / 图床同步 / 社区安装都不在跑。"""
        try:
            if any(
                not task.done()
                for task in getattr(self, "_community_install_tasks", set())
            ):
                return False
            manager = getattr(self, "semantic_task_manager", None)
            if manager is not None:
                if manager.active_pack_tasks():
                    return False
                if getattr(manager, "_external_pack_operations", None):
                    return False
            sync = getattr(self, "img_sync", None)
            process = getattr(sync, "sync_process", None)
            if process is not None and process.is_alive():
                return False
        except Exception as exc:
            logger.warning(
                "[meme_manager] 热重载前置检查异常，按不安全处理：%s", exc
            )
            return False
        return True

    def _schedule_plugin_reload(self) -> bool:
        """先回复、再延迟 1.5 秒后台热重载（Q8=a）。

        绝不在指令 handler 自身执行过程中重载插件 —— 那会在事件处理途中替换
        handler 注册表。调用方应先 yield 结果（pipeline 洋葱模型会在 yield 时
        立刻发送），本任务随后才真正重载。
        """
        manager = getattr(getattr(self, "context", None), "_star_manager", None)
        if manager is None:
            logger.warning("[meme_manager] 找不到 star_manager，无法自动重载。")
            return False

        async def _reload_later():
            await asyncio.sleep(_HOT_RELOAD_DELAY_SECONDS)
            try:
                success, message = await manager.reload(PLUGIN_NAME)
                if not success:
                    logger.warning(
                        "[meme_manager] 参数已写入，但插件重载失败：%s", message
                    )
            except Exception as exc:
                logger.error("[meme_manager] 参数已写入，但插件重载异常：%s", exc)

        try:
            asyncio.get_running_loop().create_task(_reload_later())
        except RuntimeError:
            return False
        return True

    # ========== [patcher:gallery-capacity] 图库容量手动清理 ==========
    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("清理超额")
    async def prune_over_capacity(self, event: AstrMessageEvent, mode: str = ""):
        """检查各包张数，超出 `gallery_capacity.max_images` 的按最旧优先清理。

        加 `--dry` 只出清单不删除（Q4=c 的干跑）。
        """
        dry = str(mode or "").strip().lower() in {"--dry", "dry", "-d", "干跑"}
        settings = read_gallery_capacity_settings()
        if not settings["enabled"] and not dry:
            yield event.plain_result(
                "ℹ️ 容量自动清理已关闭（gallery_capacity.enabled = 关），未做任何修改。\n"
                "如要临时跑一次：/表情管理 设置 容量清理开关 开；\n"
                "或加 --dry 只看清单。"
            )
            return
        if settings["max_images"] <= 0:
            yield event.plain_result(
                "ℹ️ 每包上限 = 0（不限），无需清理。\n"
                "如需设置：/表情管理 设置 每包上限 10000"
            )
            return
        yield event.plain_result("🔍 正在统计各表情包张数…")
        try:
            summary = await asyncio.to_thread(
                enforce_gallery_capacity,
                dry_run=dry,
                force=True,
                reason="manual-dry" if dry else "manual",
            )
        except Exception as exc:
            logger.error("[meme_manager] 容量清理失败：%s", exc, exc_info=True)
            yield event.plain_result(f"❌ 容量清理失败：{exc}")
            return

        lines = [
            f"🧹 图库容量{'（干跑，未删除）' if dry else ''}｜每包上限 {settings['max_images']} 张"
        ]
        touched = 0
        total = 0
        for pack_id, result in summary.items():
            if result.get("skipped") in ("under_limit", "unlimited", "disabled"):
                continue
            deleted = result.get("deleted") or []
            total += len(deleted)
            touched += 1
            lines.append(
                f"• {pack_id}：{result.get('count_before')} 张 → "
                f"{'将' if dry else '已'}清理 {len(deleted)} 张"
            )
            if dry and deleted:
                for item in deleted[:5]:
                    lines.append(
                        f"    - {item.get('category')}/{item.get('file')}"
                    )
                if len(deleted) > 5:
                    lines.append(f"    - …另有 {len(deleted) - 5} 张")
        if not touched:
            lines.append("✅ 所有表情包都在上限内，无需清理。")
        elif dry:
            lines.append(f"合计将删除 {total} 张（去掉 --dry 即执行）。")
        else:
            lines.append(f"合计删除 {total} 张，明细见 logs/capacity_prune.jsonl。")
        yield event.plain_result("\n".join(lines))

    # ========== [patcher:group-gallery] 按群绑定图库（2026-10-13）==========

    def _gallery_raw_args(self, event: AstrMessageEvent, fallback: str = "") -> str:
        """取「/表情管理 <子命令>」之后的原始参数字串。

        指令框架按空格切分会截断多余参数（`-g 123` 会丢），因此这里直接从
        原始消息里截取，保证 `绑定图库 a,b -g 123` 能完整解析。
        """
        text = ""
        try:
            text = str(event.get_message_str() or "")
        except Exception:
            text = ""
        text = re.sub(r"\s+", " ", text).strip()
        marker = "表情管理"
        idx = text.find(marker)
        if idx >= 0:
            tail = text[idx + len(marker):].strip()
            parts = tail.split(" ", 1)
            return parts[1].strip() if len(parts) > 1 else ""
        return str(fallback or "").strip()

    def _gallery_target_from_args(self, event: AstrMessageEvent, raw: str) -> tuple:
        """解析 [-g <群号>]；缺省时用当前会话（群聊即群号）。"""
        text = str(self._gallery_raw_args(event, raw) or "").strip()
        target = ""
        match = re.search(r"(?:^|\s)-g\s+(\S+)", text)
        if match:
            target = match.group(1).strip()
            text = (text[: match.start()] + " " + text[match.end():]).strip()
        if not target:
            target = str(getattr(event, "session_id", "") or "").strip()
        return text, target

    def _split_pack_tokens(self, raw: str) -> list:
        return [
            token.strip()
            for token in re.split(r"[,，;；\s]+", str(raw or ""))
            if token.strip()
        ]

    def _available_pack_ids(self) -> list:
        try:
            from ..backend.gallery_capacity import list_pack_ids

            return [str(pid) for pid in list_pack_ids()]
        except Exception:
            return []

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("绑定图库")
    async def bind_group_gallery(self, event: AstrMessageEvent, packs: str = ""):
        """把当前群绑定到 1~多个表情包。

        用法：/表情管理 绑定图库 <包>[,<包>…] [-g <群号>]
        """
        from ..backend.packs import storage as pack_storage

        raw, target = self._gallery_target_from_args(event, packs)
        if not target:
            yield event.plain_result("❌ 请在群里使用，或用 -g <群号> 指定目标群。")
            return
        pack_ids = self._split_pack_tokens(raw)
        if not pack_ids:
            yield event.plain_result(
                "📌 用法：/表情管理 绑定图库 <包>[,<包>…] [-g <群号>]\n"
                f"可用图库：{', '.join(self._available_pack_ids()) or '(无)'}"
            )
            return
        try:
            result = await asyncio.to_thread(
                pack_storage.upsert_pack_rule, "session", target, pack_ids
            )
        except Exception as exc:
            yield event.plain_result(f"❌ 绑定失败：{exc}")
            return
        yield event.plain_result(
            f"✅ 群 {target} 已绑定图库：{', '.join(result['pack_ids'])}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("解除图库")
    async def unbind_group_gallery(self, event: AstrMessageEvent, extra: str = ""):
        """清除当前群绑定，回落默认图库。

        用法：/表情管理 解除图库 [-g <群号>]
        """
        from ..backend.packs import storage as pack_storage

        _, target = self._gallery_target_from_args(event, extra)
        if not target:
            yield event.plain_result("❌ 请在群里使用，或用 -g <群号> 指定目标群。")
            return
        try:
            removed = await asyncio.to_thread(
                pack_storage.remove_pack_rule, "session", target
            )
        except Exception as exc:
            yield event.plain_result(f"❌ 解除失败：{exc}")
            return
        if removed:
            yield event.plain_result(f"✅ 群 {target} 的图库绑定已解除，回落默认图库。")
        else:
            yield event.plain_result(f"ℹ️ 群 {target} 本来就没有绑定图库。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("图库绑定")
    async def show_group_gallery(self, event: AstrMessageEvent, extra: str = ""):
        """查询当前群的图库绑定。

        用法：/表情管理 图库绑定 [-g <群号>]
        """
        from ..backend.packs import storage as pack_storage
        from ..backend.packs.resolver import resolve_pack_ids

        _, target = self._gallery_target_from_args(event, extra)
        if not target:
            yield event.plain_result("❌ 请在群里使用，或用 -g <群号> 指定目标群。")
            return
        rule = await asyncio.to_thread(pack_storage.get_pack_rule, "session", target)
        if rule:
            pack_ids = rule.get("pack_ids") or (
                [rule.get("pack_id")] if rule.get("pack_id") else []
            )
            yield event.plain_result(
                f"📌 群 {target} 绑定图库：{', '.join(str(x) for x in pack_ids)}"
            )
            return
        fallback = await asyncio.to_thread(resolve_pack_ids, target, None)
        yield event.plain_result(
            f"ℹ️ 群 {target} 未绑定，走默认图库：{', '.join(fallback)}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("切换图库")
    async def switch_admin_gallery(self, event: AstrMessageEvent, pack: str = ""):
        """设置全局「管理当前包」（添加表情/查看图库/清空… 作用于它）。

        用法：/表情管理 切换图库 <包|默认>
        """
        from ..backend.packs import storage as pack_storage

        token = str(pack or "").strip()
        if not token:
            current = await asyncio.to_thread(pack_storage.get_active_pack_id)
            yield event.plain_result(
                "📌 用法：/表情管理 切换图库 <包|默认>\n"
                f"当前管理包：{current or '（跟随默认图库）'}\n"
                f"可用图库：{', '.join(self._available_pack_ids()) or '(无)'}"
            )
            return
        try:
            result = await asyncio.to_thread(pack_storage.set_active_pack_id, token)
        except Exception as exc:
            yield event.plain_result(f"❌ 切换失败：{exc}")
            return
        if result["active_pack_id"]:
            yield event.plain_result(
                f"✅ 管理当前包已切到：{result['active_pack_id']}\n"
                "之后「添加表情 / 查看图库 / 清空…」都作用于它。"
            )
        else:
            yield event.plain_result("✅ 已清除「管理当前包」，管理类指令回落默认图库。")

    # ========== [patcher:collect-mirror] 按来源配置收集目标（2026-10-13）==========

    def _collect_source_key(self, event: AstrMessageEvent, raw: str) -> tuple:
        """解析收集来源主键。

        ``-g <群号>`` → ``group:<群号>``；否则取当前群聊 ID。私聊且未给 ``-g``
        时返回错误提示（收集配置按群生效）。

        Returns:
            ``(source_key, error)``；``source_key`` 为空时 ``error`` 为提示语。
        """
        text = str(self._gallery_raw_args(event, raw) or "")
        match = re.search(r"(?:^|\s)-g\s+(\S+)", text)
        if match:
            group_id = match.group(1).strip()
            if not group_id.isdigit():
                return "", f"❌ -g 需要纯数字群号，收到：{group_id}"
            return f"group:{group_id}", ""
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if group_id:
            return f"group:{group_id}", ""
        return "", "❌ 收集配置按群生效：请在群里使用，或用 -g <群号> 指定目标群。"

    def _collect_payload(self, event: AstrMessageEvent, raw: str) -> str:
        """取子命令参数里去掉 ``-g <群号>`` 之后的剩余部分。"""
        return str(self._gallery_target_from_args(event, raw)[0] or "").strip()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("收集开关")
    async def toggle_collect_source(self, event: AstrMessageEvent, value: str = ""):
        """开启/关闭某个群的表情包自动收集总闸。

        用法：/表情管理 收集开关 <开|关> [-g <群号>]
        """
        from ..backend import collect_sources

        key, error = self._collect_source_key(event, value)
        if not key:
            yield event.plain_result(error)
            return
        token = self._collect_payload(event, value)
        opened = {"开", "on", "启用", "1", "true", "是"}
        closed = {"关", "off", "停用", "0", "false", "否"}
        if token.lower() not in opened and token.lower() not in closed:
            entry = await asyncio.to_thread(collect_sources.get_source, key)
            state = "开" if (entry or {}).get("enabled") else "关"
            yield event.plain_result(
                "📌 用法：/表情管理 收集开关 <开|关> [-g <群号>]\n"
                f"{key} 当前：{state}"
            )
            return
        enabled = token.lower() in opened
        try:
            entry = await asyncio.to_thread(collect_sources.set_enabled, key, enabled)
        except Exception as exc:
            yield event.plain_result(f"❌ 设置失败：{exc}")
            return
        packs = ", ".join(str(x) for x in (entry.get("packs") or [])) or "（默认图库）"
        yield event.plain_result(
            f"✅ {key} 自动收集已{'开启' if enabled else '关闭'}。\n"
            f"镜像目标：{packs}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("收集目标")
    async def set_collect_targets(self, event: AstrMessageEvent, packs: str = ""):
        """设置某个群收集时把表情包**复制镜像**进哪些图库（可多个）。

        用法：/表情管理 收集目标 <包>[,<包>…] [-g <群号>]
        """
        from ..backend import collect_sources

        key, error = self._collect_source_key(event, packs)
        if not key:
            yield event.plain_result(error)
            return
        tokens = self._split_pack_tokens(self._collect_payload(event, packs))
        entry = await asyncio.to_thread(collect_sources.get_source, key)
        if not tokens:
            current = ", ".join(str(x) for x in ((entry or {}).get("packs") or []))
            gate = "开" if (entry or {}).get("enabled") else "关"
            tips = ", ".join(self._available_pack_ids()) or "(无)"
            yield event.plain_result(
                "📌 用法：/表情管理 收集目标 <包>[,<包>…] [-g <群号>]\n"
                f"{key} 当前目标：{current or '（默认图库，未设置）'}\n"
                f"收集总闸：{gate}\n"
                f"可用图库：{tips}"
            )
            return
        available = set(self._available_pack_ids())
        unknown = [token for token in tokens if token not in available]
        if unknown:
            tips = ", ".join(sorted(available)) or "(无)"
            yield event.plain_result(
                f"❌ 不存在的图库：{', '.join(unknown)}\n可用图库：{tips}"
            )
            return
        try:
            entry = await asyncio.to_thread(collect_sources.set_packs, key, tokens)
        except Exception as exc:
            yield event.plain_result(f"❌ 设置失败：{exc}")
            return
        gate = "开" if entry.get("enabled") else "关"
        suffix = "" if entry.get("enabled") else "（用「收集开关 开」开启收集）"
        yield event.plain_result(
            f"✅ {key} 收集目标已设为："
            f"{', '.join(str(x) for x in entry['packs'])}\n"
            f"收集总闸：{gate}{suffix}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("收集状态")
    async def show_collect_status(self, event: AstrMessageEvent, extra: str = ""):
        """查询某个群的自动收集配置（总闸 + 镜像目标）。

        用法：/表情管理 收集状态 [-g <群号>]
        """
        from ..backend import collect_sources

        key, error = self._collect_source_key(event, extra)
        if not key:
            yield event.plain_result(error)
            return
        entry = await asyncio.to_thread(collect_sources.get_source, key)
        default_packs = await asyncio.to_thread(collect_sources.default_packs)
        defaults = ", ".join(str(x) for x in default_packs)
        if not entry:
            yield event.plain_result(
                f"ℹ️ {key} 不在收集白名单中（不收图）。\n"
                f"默认图库：{defaults}\n"
                "开启：/表情管理 收集开关 开"
            )
            return
        packs = ", ".join(str(x) for x in (entry.get("packs") or []))
        gate = "开" if entry.get("enabled") else "关"
        yield event.plain_result(
            f"📌 {key} 收集配置\n"
            f"总闸：{gate}\n"
            f"镜像目标：{packs or f'（默认图库：{defaults}）'}"
        )
