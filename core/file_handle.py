import os
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import File, Image, Reply, Video
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..utils import download_file


class FileHandle:
    def __init__(self, config: PluginConfig):
        self.data_dir = config.file_dir

    async def _parse_path(
        self, event: AiocqhttpMessageEvent, path: str
    ) -> tuple[str | None, str | None]:
        """
        解析路径，返回 (folder_name, file_name)
        支持：
        - "文件夹名" -> (folder_name, None)
        - "文件.zip" -> (None, file_name)
        - "文件夹名/文件.zip" -> (folder_name, file_name)
        - "数字" -> 用缓存解析 (folder or file)
        - "数字/数字" -> 用缓存解析 (folder序号/文件序号)
        """
        path = path.strip()
        response = await event.bot.get_group_root_files(
            group_id=int(event.get_group_id())
        )
        _, mapping = self._get_folder_info(response, "")

        def resolve_index(
            index: str, kind_filter: str | None = None
        ) -> tuple[str | None, str | None]:
            """根据序号解析文件夹名或文件名，可选过滤类型，返回(kind, name)"""
            if not index.isdigit():
                return None, None
            idx = int(index)
            if idx not in mapping:
                return None, None
            kind, name = mapping[idx]
            if kind_filter and kind != kind_filter:
                return None, None
            return kind, name

        if "/" in path:
            left, right = path.split("/", 1)

            # 先解析左边文件夹
            folder_kind, folder_name = resolve_index(left, "folder")
            folder_name = folder_name or left  # 左边不是数字就直接当字符串用

            # 如果右边是数字，需要进入对应文件夹再解析
            if right.isdigit() and folder_name:
                target_folder = await self._get_folder(event, folder_name)
                if target_folder:
                    folder_files = await event.bot.get_group_files_by_folder(
                        group_id=int(event.get_group_id()),
                        folder_id=target_folder["folder_id"],
                    )
                    _, file_mapping = self._get_folder_info(folder_files, "")
                    idx = int(right)
                    if idx in file_mapping and file_mapping[idx][0] == "file":
                        file_name = file_mapping[idx][1]
                        return folder_name, file_name

            # 如果右边不是纯数字或者没解析到，就直接当文件名
            return folder_name, right

        elif "." in path:
            return None, path

        else:
            if path.isdigit():
                idx = int(path)
                if idx in mapping:
                    kind, name = mapping[idx]
                    return (name, None) if kind == "folder" else (None, name)
                return None, None
            return path, None

    async def _get_folder(
        self, event: AiocqhttpMessageEvent, folder_name: str
    ) -> dict | None:
        """从根目录下找到指定文件夹, 返回文件夹数据"""
        response = await event.bot.get_group_root_files(
            group_id=int(event.get_group_id())
        )
        return next(
            (
                folder
                for folder in response["folders"]
                if folder_name == folder["folder_name"]
            ),
            None,
        )

    def _get_folder_info(self, data: dict, title: str) -> tuple[str, dict[int, str]]:
        """从响应数据里提取文件夹和文件，每个文件夹下文件序号从 1 开始"""
        info = [title]
        mapping = {}

        idx = 1
        for folder in data["folders"]:
            info.append(f"▶{idx}. {folder['folder_name']}")
            mapping[idx] = ("folder", folder["folder_name"])
            idx += 1

        for file in data["files"]:
            info.append(f"📄{idx}. {file['file_name']}")
            mapping[idx] = ("file", file["file_name"])
            idx += 1

        return "\n".join(info), mapping

    @staticmethod
    def _parse_size(file: dict):
        """
        取文件字节数。

        不同协议端字段名不一致（NapCat 用 file_size，旧 go-cqhttp 用 size），
        先找能解析出正数的字段；拿不到正数时再看是否存在可解析的 0（真实空
        文件）；完全缺字段返回 None，避免把「字段缺失」谎报成「文件为空」。
        """
        keys = ("file_size", "size", "fileSize")
        for key in keys:
            raw = file.get(key)
            if raw is None:
                continue
            try:
                value = int(float(str(raw).strip()))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        for key in keys:
            raw = file.get(key)
            if raw is None:
                continue
            try:
                return int(float(str(raw).strip()))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _human_size(size: int) -> str:
        """字节数转人类可读文本"""
        if size < 1024:
            return f"{size} B"
        for unit, step in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
            if size >= step:
                return f"{size / step:.2f} {unit}"
        return f"{size} B"

    def _format_file_info(self, file: dict) -> str:
        """格式化文件信息"""
        lines = [f"【📄 {file.get('file_name', '未知')}】"]
        size = self._parse_size(file)
        if size is None:
            lines.append("文件大小: 未知（协议端未返回大小字段）")
        else:
            lines.append(f"文件大小: {self._human_size(size)}")

        lines.append(
            f"上传者：{file.get('uploader_name', '未知')}({file.get('uploader', '未知')})"
        )
        lines.append(f"下载次数：{file.get('download_times', '未知')}")

        if upload_time := file.get("upload_time", 0):
            lines.append(
                f"上传时间：{datetime.fromtimestamp(upload_time).strftime('%Y-%m-%d %H:%M:%S')}"
            )
        dead_time = file.get("dead_time", 0)
        lines.append(
            f"过期时间：{'永久有效' if dead_time == 0 else datetime.fromtimestamp(dead_time).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if modify_time := file.get("modify_time", 0):
            lines.append(
                f"修改时间：{datetime.fromtimestamp(modify_time).strftime('%Y-%m-%d %H:%M:%S')}"
            )
        logger.debug(f"文件ID：{file.get('file_id', '未知')}")
        return "\n".join(lines)

    async def _get_file_in_folder(
        self, event: AiocqhttpMessageEvent, folder_name: str, file_name: str
    ):
        """返回目标文件夹和文件对象"""
        if not folder_name:
            return None, None
        target_folder = await self._get_folder(event, folder_name=folder_name)
        if not target_folder:
            return None, None
        response = await event.bot.get_group_files_by_folder(
            group_id=int(event.get_group_id()), folder_id=target_folder["folder_id"]
        )
        file = next((f for f in response["files"] if f["file_name"] == file_name), None)
        return target_folder, file

    async def _save_temp_file(self, event: AiocqhttpMessageEvent, file_name: str):
        """获取文件URL并下载，返回保存路径"""
        chain = event.message_obj.message
        reply_chain = chain[0].chain if chain and isinstance(chain[0], Reply) else None
        seg = reply_chain[0] if reply_chain else None
        if seg and isinstance(seg, File | Image | Video):
            if url := getattr(seg, "url", None) or getattr(seg, "file", None):
                logger.info(f"正在从URL下载文件：{url}")
                file_path = self.data_dir / file_name
                await download_file(url, file_path)
                if os.path.exists(file_path):
                    return file_path
                logger.error(f"下载文件失败：{url}")
            else:
                return None

    async def _ensure_folder(self, event: AiocqhttpMessageEvent, folder_name: str):
        """
        确保群文件夹存在，如果不存在则创建，返回目标文件夹数据
        """
        group_id = int(event.get_group_id())
        client = event.bot

        target_folder = await self._get_folder(event, folder_name)
        if target_folder:
            return target_folder

        # 清理非法字符
        safe_name = re.sub(r"[\\/:*?\"<>|]", "", folder_name)[:30]
        await client.create_group_file_folder(
            group_id=group_id,
            folder_name=safe_name,
            parent_id="/",
        )
        # 再次获取，确保拿到 folder_id
        return await self._get_folder(event, safe_name)

    async def upload_group_file(self, event: AiocqhttpMessageEvent, path: str):
        """上传群文件"""
        folder_name, file_name = await self._parse_path(event, path)
        if not file_name:
            return "路径未包含文件名，无法上传"

        group_id = int(event.get_group_id())
        client = event.bot

        # 拼接本地缓存路径
        file_path = await self._save_temp_file(event, file_name)
        if not file_path or not file_path.exists():
            return "请引用一个文件"

        folder_id = None
        if folder_name:
            if target_folder := await self._ensure_folder(event, folder_name):
                folder_id = target_folder["folder_id"]

        try:
            await client.upload_group_file(
                group_id=group_id,
                file=str(file_path),
                name=file_name,
                folder_id=folder_id,
            )
        except Exception as e:
            logger.error(f"上传群文件失败：{e}")
            return f"上传失败：{e}"
        return f"群文件已上传：{path}"

    async def delete_group_file(self, event: AiocqhttpMessageEvent, path: str):
        """删除群文件夹或群文件"""
        folder_name, file_name = await self._parse_path(event, path)
        if not folder_name and not file_name:
            return "请指定要删除的文件夹或文件"
        group_id = int(event.get_group_id())

        # 删除文件
        if file_name:
            file = None
            if folder_name:
                target_folder, file = await self._get_file_in_folder(
                    event, folder_name, file_name
                )
                if not target_folder or not file:
                    return f"{path} 不存在"
            else:
                response = await event.bot.get_group_root_files(group_id=group_id)
                file = next(
                    (f for f in response["files"] if file_name == f["file_name"]),
                    None,
                )
            if file:
                await event.bot.delete_group_file(
                    group_id=group_id, file_id=file["file_id"]
                )
                return f"已删除群文件：📄{file_name}"

        # 删除文件夹
        elif folder_name and not file_name:
            if target_folder := await self._get_folder(event, folder_name):
                await event.bot.delete_group_folder(
                    group_id=group_id, folder_id=target_folder["folder_id"]
                )
                return f"已删除群文件夹：▶{folder_name}"
            else:
                return f"群文件夹【{folder_name}】不存在"
        return "未找到要删除的群文件或文件夹"

    async def _locate_file(
        self, event: AiocqhttpMessageEvent, path: str
    ) -> tuple[dict | None, str]:
        """
        按 path 定位群文件对象，返回 (file_dict, 错误说明)。
        file_dict 为 None 时错误说明可用。
        path 支持：文件名、文件夹名/文件名、序号、序号/序号。
        """
        folder_name, file_name = await self._parse_path(event, str(path))
        group_id = int(event.get_group_id())

        if folder_name and file_name:
            _, file = await self._get_file_in_folder(event, folder_name, file_name)
            if not file:
                return None, f"未能找到群文件：📄{file_name}"
            return file, ""

        if file_name:
            response = await event.bot.get_group_root_files(group_id=group_id)
            file = next(
                (f for f in response["files"] if file_name == f["file_name"]),
                None,
            )
            if not file:
                return None, f"未能找到群文件：📄{file_name}"
            return file, ""

        if folder_name:
            # 可能是根目录下的单个文件
            response = await event.bot.get_group_root_files(group_id=group_id)
            file = next(
                (f for f in response["files"] if folder_name == f["file_name"]),
                None,
            )
            if file:
                return file, ""
            return None, f"【{folder_name}】是文件夹或不存在，请带上文件名，如：文件夹名/文件名"

        return None, "请指定要下载的文件"

    async def _get_group_file_url(
        self, event: AiocqhttpMessageEvent, file: dict
    ) -> tuple[str | None, str]:
        """
        取群文件下载直链，返回 (url, 错误说明)。

        NapCat 的 get_group_file_url 需要 file_id 与 busid：
        - file_id 缺失时无法取链，直接报错；
        - busid 缺省按 102 传（NapCat 默认个人群文件夹）。
        """
        file_id = file.get("file_id")
        if not file_id:
            return None, "该文件记录缺少 file_id，无法获取下载链接"
        payload = {
            "group_id": int(event.get_group_id()),
            "file_id": str(file_id),
        }
        # busid 部分实现必填，缺失时补默认值
        if busid := file.get("busid"):
            payload["busid"] = int(busid)
        else:
            payload["busid"] = 102

        try:
            resp = await event.bot.call_action("get_group_file_url", **payload)
        except Exception as e:
            logger.error(f"获取群文件链接失败：{e!r}")
            return None, f"获取下载链接失败：{e}"

        if not isinstance(resp, dict):
            return None, "协议端返回的下载链接格式异常"
        # 兼容 {"url": ...} 与 {"data": {"url": ...}} 两种包裹
        url = resp.get("url") or (resp.get("data") or {}).get("url")
        if not url:
            return None, "协议端未返回下载链接（可能该文件已过期或被删除）"
        return str(url), ""

    async def download_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str,
        max_mb: int = 100,
    ) -> tuple[Path | None, dict | None, str]:
        """
        下载群文件到本地缓存目录，返回 (本地路径, 文件对象, 错误说明)。

        只负责落盘，不负责发送；发送由调用方决定用群聊还是私聊。
        """
        file, err = await self._locate_file(event, path)
        if not file:
            return None, None, err

        # 体积保护：避免把超大文件拖到手机/服务器上
        size = self._parse_size(file)
        if size is not None and max_mb > 0 and size > max_mb * 1024 * 1024:
            return (
                None,
                file,
                f"文件大小 {self._human_size(size)} 超过下载上限 {max_mb} MB，已拒绝",
            )

        url, err = await self._get_group_file_url(event, file)
        if not url:
            return None, file, err

        raw_name = str(file.get("file_name") or "群文件")
        # 去掉路径分隔符，防止写到目录外
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", raw_name).strip() or "群文件"
        save_path = self.data_dir / safe_name
        save_path.parent.mkdir(parents=True, exist_ok=True)

        if os.path.exists(save_path):
            try:
                save_path.unlink()
            except OSError:
                pass

        downloaded = await download_file(url, save_path)
        if not downloaded or not os.path.exists(save_path):
            return None, file, "下载失败（链接可能已过期，或网络不可达）"

        actual = os.path.getsize(save_path)
        if actual == 0:
            try:
                save_path.unlink()
            except OSError:
                pass
            return None, file, "下载到的文件为 0 字节，已清理"

        return save_path, file, ""

    async def view_group_file(self, event: AiocqhttpMessageEvent, path) -> str:
        """查看群文件/目录，path 可以是 文件夹名、文件名 或 文件夹名/文件名"""
        group_id = int(event.get_group_id())
        client = event.bot
        if not path:
            # 查看根目录
            response = await client.get_group_root_files(group_id=group_id)
            text, _ = self._get_folder_info(response, "【群文件根目录】")
            return text

        folder_name, file_name = await self._parse_path(event, str(path))

        if folder_name and file_name:
            target_folder, file = await self._get_file_in_folder(
                event, folder_name, file_name
            )
            if not file:
                return f"未能找到群文件：📄{file_name}"
            return self._format_file_info(file)

        if folder_name and not file_name:
            target_folder = await self._get_folder(event, folder_name)
            if target_folder:
                response = await client.get_group_files_by_folder(
                    group_id=group_id, folder_id=target_folder["folder_id"]
                )
                text, _ = self._get_folder_info(response, f"【{folder_name}】")
                return text
            else:
                # 根目录单文件
                response = await client.get_group_root_files(group_id=group_id)
                if file := next(
                    (f for f in response["files"] if folder_name == f["file_name"]),
                    None,
                ):
                    return self._format_file_info(file)
                else:
                    return f"未能找到【{folder_name}】"
        elif not folder_name and file_name:
            # 根目录文件
            response = await client.get_group_root_files(group_id=group_id)
            if file := next(
                (f for f in response["files"] if file_name == f["file_name"]),
                None,
            ):
                return self._format_file_info(file)

            else:
                return f"未能找到群文件：📄{file_name}"
        return "未找到群文件"
