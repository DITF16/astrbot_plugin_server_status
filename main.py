import sqlite3
import psutil
import re
import os
import json
import uuid
import asyncio
from datetime import datetime, timedelta

import pandas as pd
import matplotlib
from anyio import Path

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger


class ServerStatusPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_server_status")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.tmp_dir = self.data_dir / "tmp"
        self.db_path = self.data_dir / "status.db"
        self.config_path = self.data_dir / "config.json"
        self.group_config_path = self.data_dir / "groups.txt"
        self.font_dir = Path(__file__).parent / "font"
        self.font_dir.mkdir(exist_ok=True)
        self.font_path = self.font_dir / "font.ttf"

        self.tmp_dir.mkdir(exist_ok=True)

        self.BOT_QQ_ID = "924558440"  # 临时的固定QQ号
        # self.BOT_QQ_ID = "110624219"

        self.scheduler = None
        self.display_mode = "percent"
        self.target_groups = set()
        self.group_last_update_time = {}
        self.font_prop = None

        self._init_plugin_data()

    def _init_plugin_data(self):
        try:
            if self.font_path.exists():
                self.font_prop = fm.FontProperties(fname=self.font_path)
                logger.info(f"成功加载字体文件: {self.font_path}")
            else:
                logger.warning(f"字体文件未找到: {self.font_path}，图表中的中文可能显示为方块。")
                logger.warning("请在插件数据目录中创建 /font 文件夹，并放入一个名为 font.ttf 的中文字体文件。")

            self._sync_load_config()
            logger.info(f"成功加载显示模式配置，当前模式: {self.display_mode}")

            self._sync_load_groups()
            logger.info(f"成功加载群号配置，将为 {len(self.target_groups)} 个群聊更新状态。")

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS status_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME NOT NULL,
                        cpu_percent REAL NOT NULL,
                        memory_percent REAL NOT NULL,
                        memory_used_mb REAL NOT NULL,
                        memory_total_mb REAL NOT NULL
                    )
                ''')
            logger.info("数据库已确认并初始化。")

            self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
            self.scheduler.add_job(self.scheduled_db_record, 'interval', minutes=5, id='job_record_status')
            self.scheduler.add_job(self.scheduled_db_cleanup, 'cron', hour=4, minute=0, id='job_cleanup_db')
            self.scheduler.start()
            logger.info("定时任务调度器已启动。")

        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)

    async def get_system_stats(self) -> dict:
        cpu_percent = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        return {
            "cpu": cpu_percent,
            "mem_percent": mem.percent,
            "mem_used_mb": mem.used / (1024 * 1024),
            "mem_total_mb": mem.total / (1024 * 1024)
        }

    async def update_db(self, stats: dict):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_update_db, stats)

    def _sync_update_db(self, stats: dict):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO status_records (timestamp, cpu_percent, memory_percent, memory_used_mb, memory_total_mb) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(), stats['cpu'], stats['mem_percent'], stats['mem_used_mb'], stats['mem_total_mb'])
            )
            conn.commit()


    async def update_all_nicknames(self, client, stats: dict):
        if not self.target_groups:
            return
        update_tasks = [self.update_nickname_for_group(client, group_id, stats) for group_id in self.target_groups]
        await asyncio.gather(*update_tasks)

    async def update_nickname_for_group(self, client, group_id: str, stats: dict):
        if self.display_mode == 'percent':
            status_text = f"(理智使用{stats['cpu']:.1f}% 脑容量使用{stats['mem_percent']:.1f}%)"
        else:
            mem_used_gb = stats['mem_used_mb'] / 1024
            mem_total_gb = stats['mem_total_mb'] / 1024
            status_text = f"(理智使用{stats['cpu']:.1f}% 脑容量使用{mem_used_gb:.1f}G/{mem_total_gb:.1f}G)"

        try:
            info_payload = {"group_id": int(group_id), "user_id": int(self.BOT_QQ_ID)}
            member_info = await client.api.call_action('get_group_member_info', **info_payload)
            current_card = member_info.get('card', '')

            match = re.search(r"(\s*\(理智使用.*?\s*脑容量使用.*?\))$", current_card)
            if match:
                base_name = current_card[:match.start()].rstrip()
                new_card = f"{base_name} {status_text}" if base_name else status_text
            else:
                new_card = f"{current_card} {status_text}".lstrip()

            if new_card == current_card:
                return

            set_payload = {"group_id": int(group_id), "user_id": int(self.BOT_QQ_ID), "card": new_card}

            max_retries = 3
            for i in range(max_retries):
                try:
                    await client.api.call_action('set_group_card', **set_payload)
                    logger.info(f"成功更新群 {group_id} 的昵称为: {new_card}")
                    break
                except Exception as e:
                    logger.warning(f"更新群 {group_id} 昵称失败 (第 {i + 1} 次尝试): {e}")
                    if i == max_retries - 1:
                        logger.error(f"更新群 {group_id} 昵称在 {max_retries} 次重试后彻底失败。")
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"处理群 {group_id} 昵称更新时发生错误: {e}")

    async def scheduled_db_record(self):
        logger.info("执行定时数据库记录...")
        stats = await self.get_system_stats()
        await self.update_db(stats)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def update_on_message(self, event: AstrMessageEvent):
        client = self._get_client_from_event(event)
        if not client:
            return

        group_id = event.get_group_id()
        if group_id not in self.target_groups:
            return

        now = datetime.now()
        last_update = self.group_last_update_time.get(group_id)
        if last_update and (now - last_update) < timedelta(minutes=5):
            return

        logger.info(f"群 {group_id} 消息触发昵称更新 (冷却时间已过)。")
        self.group_last_update_time[group_id] = now

        stats = await self.get_system_stats()
        await self.update_nickname_for_group(client, group_id, stats)

    async def scheduled_db_cleanup(self):
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._sync_db_cleanup)
            logger.info("成功清理了10天前的旧数据。")
        except Exception as e:
            logger.error(f"数据库清理失败: {e}")

    def _sync_db_cleanup(self):
        ten_days_ago = datetime.now() - timedelta(days=10)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM status_records WHERE timestamp < ?", (ten_days_ago,))
            conn.commit()

    @filter.command("大脑开启")
    async def brain_on(self, event: AstrMessageEvent, group_id: str):
        if not group_id.isdigit():
            yield event.plain_result(f"错误：'{group_id}' 不是有效的群号。")
            return

        if group_id in self.target_groups:
            yield event.plain_result(f"群 {group_id} 已在监控列表中，无需重复添加。")
            return

        self.target_groups.add(group_id)
        await self._save_groups()
        logger.info(f"用户 {event.get_sender_id()} 添加了群 {group_id} 到监控列表。")
        yield event.plain_result(f"成功开启！已将群 {group_id} 加入大脑监控列表。")

    @filter.command("大脑关闭")
    async def brain_off(self, event: AstrMessageEvent, group_id: str):
        if not group_id.isdigit():
            yield event.plain_result(f"错误：'{group_id}' 不是有效的群号。")
            return

        if group_id not in self.target_groups:
            yield event.plain_result(f"群 {group_id} 不在监控列表中。")
            return

        self.target_groups.remove(group_id)
        await self._save_groups()
        logger.info(f"用户 {event.get_sender_id()} 从监控列表移除了群 {group_id}。")
        yield event.plain_result(f"操作成功！已将群 {group_id} 从大脑监控列表移除。")

    @filter.command("大脑更新")
    async def brain_update(self, event: AstrMessageEvent):
        client = self._get_client_from_event(event)
        if not client:
            yield event.plain_result("错误：当前平台不支持或无法获取客户端。")
            return

        logger.info(f"用户 {event.get_sender_id()} 触发了'大脑更新'指令。")
        stats = await self.get_system_stats()
        await self.update_db(stats)
        await self.update_all_nicknames(client, stats)
        yield event.plain_result("大脑更新成功！已为所有受监控的群聊刷新状态。")

    @filter.command("理智记录")
    async def cpu_record(self, event: AstrMessageEvent, time_str: str):
        image_path = None
        try:
            delta = self._parse_time_arg(time_str)
            if delta is None:
                yield event.plain_result("格式错误！请使用 'n天' 或 'n小时'，例如: /理智记录 2天")
                return

            end_time = datetime.now()
            start_time = end_time - delta

            loop = asyncio.get_running_loop()
            query = f"SELECT timestamp, cpu_percent FROM status_records WHERE timestamp BETWEEN '{start_time}' AND '{end_time}'"

            df = await loop.run_in_executor(None, self._sync_read_from_db, query)

            if df.empty:
                yield event.plain_result(f"最近 {time_str} 内没有理智记录。")
                return

            df['timestamp'] = pd.to_datetime(df['timestamp'])

            image_path = self._plot_graph(
                df, 'timestamp', 'cpu_percent',
                f'最近 {time_str} 理智使用记录 (CPU %)', 'CPU 占用 (%)'
            )
            yield event.image_result(str(image_path))

        except Exception as e:
            logger.error(f"处理理智记录指令失败: {e}", exc_info=True)
            yield event.plain_result("生成图表时发生内部错误。")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except OSError as e:
                    logger.error(f"删除临时图片失败: {e}")

    @filter.command("脑容量记录")
    async def memory_record(self, event: AstrMessageEvent, time_str: str):
        image_path = None
        try:
            delta = self._parse_time_arg(time_str)
            if delta is None:
                yield event.plain_result("格式错误！请使用 'n天' 或 'n小时'，例如: /脑容量记录 12小时")
                return

            end_time = datetime.now()
            start_time = end_time - delta

            loop = asyncio.get_running_loop()
            query = f"SELECT timestamp, memory_percent FROM status_records WHERE timestamp BETWEEN '{start_time}' AND '{end_time}'"

            df = await loop.run_in_executor(None, self._sync_read_from_db, query)

            if df.empty:
                yield event.plain_result(f"最近 {time_str} 内没有脑容量记录。")
                return

            df['timestamp'] = pd.to_datetime(df['timestamp'])

            image_path = self._plot_graph(
                df, 'timestamp', 'memory_percent',
                f'最近 {time_str} 脑容量使用记录 (内存 %)', '内存占用 (%)'
            )
            yield event.image_result(str(image_path))

        except Exception as e:
            logger.error(f"处理脑容量记录指令失败: {e}", exc_info=True)
            yield event.plain_result("生成图表时发生内部错误。")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except OSError as e:
                    logger.error(f"删除临时图片失败: {e}")

    @filter.command("改变脑容量显示")
    async def toggle_display_mode(self, event: AstrMessageEvent):
        if self.display_mode == 'percent':
            self.display_mode = 'absolute'
            reply_text = "脑容量显示方式已切换为：绝对值 (G/G)。"
        else:
            self.display_mode = 'percent'
            reply_text = "脑容量显示方式已切换为：百分比 (%)。"

        await self._save_config()
        logger.info(f"显示模式已切换为 {self.display_mode} 并已保存。")

        stats = await self.get_system_stats()

        client = self._get_client_from_event(event)
        group_id = event.get_group_id()
        if client and group_id:
            await self.update_nickname_for_group(client, group_id, stats)

        yield event.plain_result(reply_text)

    def _get_client_from_event(self, event: AstrMessageEvent):
        if event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    return event.bot
                else:
                    logger.warning("Event platform is aiocqhttp, but event is not of type AiocqhttpMessageEvent.")
                    return None
            except ImportError:
                logger.error("无法导入 AiocqhttpMessageEvent。请确认已正确安装 aiocqhttp 平台适配。")
                return None
        return None

    def _parse_time_arg(self, time_str: str) -> timedelta | None:
        match = re.match(r"(\d+)\s*(天|小时)", time_str)
        if not match:
            return None
        num = int(match.group(1))
        unit = match.group(2)
        if unit == "天":
            return timedelta(days=num)
        elif unit == "小时":
            return timedelta(hours=num)
        return None

    def _plot_graph(self, df: pd.DataFrame, x_col: str, y_col: str, title: str, ylabel: str) -> str:
        plt.style.use('seaborn-v0_8-darkgrid')
        fig, ax = plt.subplots(figsize=(12, 6), dpi=100)

        ax.plot(df[x_col], df[y_col], marker='o', linestyle='-', markersize=3, label=ylabel)

        if self.font_prop:
            ax.set_title(title, fontsize=16, fontproperties=self.font_prop)
            ax.set_xlabel("时间", fontsize=12, fontproperties=self.font_prop)
            ax.set_ylabel(ylabel, fontsize=12, fontproperties=self.font_prop)
            ax.legend(prop=self.font_prop)
        else:  # Fallback if font is not found
            ax.set_title(title, fontsize=16)
            ax.set_xlabel("Time", fontsize=12)
            ax.set_ylabel(ylabel, fontsize=12)
            ax.legend()

        ax.grid(True)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        fig.autofmt_xdate()

        plt.tight_layout()

        filename = f"{uuid.uuid4()}.png"
        filepath = self.tmp_dir / filename
        plt.savefig(filepath, format='png')
        plt.close(fig)

        return str(filepath)


    def _sync_read_from_db(self, query: str) -> pd.DataFrame:
        """
        在当前线程建立新连接以读取数据库，保证线程安全。
        """
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query(query, conn)
            return df

    def _sync_load_groups(self):
        try:
            if self.group_config_path.exists():
                with open(self.group_config_path, 'r', encoding='utf-8') as f:
                    self.target_groups = {line.strip() for line in f if line.strip().isdigit()}
        except Exception as e:
            logger.error(f"加载群号文件 '{self.group_config_path}' 失败: {e}", exc_info=True)

    async def _save_groups(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_save_groups)

    def _sync_save_groups(self):
        try:
            with open(self.group_config_path, 'w', encoding='utf-8') as f:
                for group_id in sorted(list(self.target_groups)):
                    f.write(f"{group_id}\n")
        except Exception as e:
            logger.error(f"保存群号文件 '{self.group_config_path}' 失败: {e}", exc_info=True)

    def _sync_load_config(self):
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.display_mode = config.get('display_mode', 'percent')
        except Exception as e:
            logger.warning(f"加载配置文件失败: {e}，将使用默认配置。")

    async def _save_config(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_save_config)

    def _sync_save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump({'display_mode': self.display_mode}, f)
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")

    async def terminate(self):
        logger.info("正在关闭 ServerStatus 插件...")
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("定时任务调度器已关闭。")
        logger.info("ServerStatus 插件已成功关闭。")