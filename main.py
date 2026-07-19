import os
import json
import asyncio
import logging
import random
import tempfile
from typing import Dict, List, Optional, Set
from datetime import datetime
from io import BytesIO

from PIL import Image
import aiohttp

# ============ 修正导入（关键）============
from astrbot.api import Context, MessageEvent, PluginMetadata
from astrbot.api.event import on_message
from astrbot.api.types import MessageChain, Plain, Image as MBImage
from astrbot.core.plugin import AstrBotPlugin   # ← 从 core.plugin 导入
# ========================================

logger = logging.getLogger(__name__)

# ==================== 游戏状态管理 ====================

class GameRoom:
    """单个群聊的游戏房间"""
    def __init__(self, group_id: str):
        self.group_id = group_id
        self.is_playing = False
        self.current_card: Optional[str] = None
        self.current_image_path: Optional[str] = None
        self.guessed_users: Set[str] = set()
        self.round_start_time: Optional[datetime] = None
        self.timer_task: Optional[asyncio.Task] = None
        self.scores: Dict[str, int] = {}
        self.daily_rounds: int = 0
        self.last_round_date: Optional[str] = None

    def reset(self):
        self.is_playing = False
        self.current_card = None
        self.guessed_users = set()
        self.round_start_time = None
        if self.timer_task:
            self.timer_task.cancel()
            self.timer_task = None
        if self.current_image_path and os.path.exists(self.current_image_path):
            try:
                os.remove(self.current_image_path)
            except Exception:
                pass
        self.current_image_path = None

    def add_score(self, user_id: str, points: int):
        self.scores[user_id] = self.scores.get(user_id, 0) + points


class KardsGuessCardPlugin(AstrBotPlugin):
    """Kards 卡牌剪影猜猜猜插件"""

    async def initialize(self):
        self.rooms: Dict[str, GameRoom] = {}
        self.card_pool: List[str] = []
        self.card_image_map: Dict[str, str] = {}

        # 加载卡牌列表
        list_path = os.path.join(os.path.dirname(__file__), "cards_data", "kards_list.json")
        try:
            with open(list_path, "r", encoding="utf-8") as f:
                self.card_pool = json.load(f)
            logger.info(f"已加载 {len(self.card_pool)} 张卡牌名称")
        except Exception as e:
            logger.error(f"加载卡牌列表失败: {e}")
            self.card_pool = []

        # 加载图片文件
        image_dir = self.config.get("card_image_dir", "./cards_images/")
        if not os.path.isabs(image_dir):
            image_dir = os.path.join(os.path.dirname(__file__), image_dir)
        if not os.path.exists(image_dir):
            logger.warning(f"卡牌图片目录不存在: {image_dir}，请创建并放入图片")
        else:
            for filename in os.listdir(image_dir):
                if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
                    name = os.path.splitext(filename)[0]
                    if name in self.card_pool:
                        self.card_image_map[name] = os.path.join(image_dir, filename)
                    else:
                        logger.debug(f"图片 {filename} 对应的卡牌不在列表中，忽略")
            logger.info(f"已加载 {len(self.card_image_map)} 张卡牌图片")

        self.crop_top = self.config.get("crop_top_ratio", 0.22)
        self.crop_bottom = self.config.get("crop_bottom_ratio", 0.30)
        self.crop_left = self.config.get("crop_left_ratio", 0.08)
        self.crop_right = self.config.get("crop_right_ratio", 0.08)
        self.guess_timeout = self.config.get("guess_timeout", 60)
        self.rounds_per_day = self.config.get("rounds_per_day", 20)
        self.points_correct = self.config.get("points_correct", 10)

    def _get_room(self, group_id: str) -> GameRoom:
        if group_id not in self.rooms:
            self.rooms[group_id] = GameRoom(group_id)
        return self.rooms[group_id]

    def _get_today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _can_start_round(self, room: GameRoom) -> bool:
        today = self._get_today()
        if room.last_round_date != today:
            room.daily_rounds = 0
            room.last_round_date = today
        return room.daily_rounds < self.rounds_per_day

    def _crop_card_image(self, image_path: str) -> Optional[str]:
        try:
            with Image.open(image_path) as img:
                if img.mode == 'RGBA':
                    img = img.convert('RGB')
                width, height = img.size
                left = int(width * self.crop_left)
                right = int(width * (1 - self.crop_right))
                top = int(height * self.crop_top)
                bottom = int(height * (1 - self.crop_bottom))
                if left >= right or top >= bottom:
                    logger.warning("裁剪区域无效，使用原始图片")
                    cropped = img
                else:
                    cropped = img.crop((left, top, right, bottom))
                fd, temp_path = tempfile.mkstemp(suffix=".jpg", prefix="kards_guess_")
                os.close(fd)
                cropped.save(temp_path, "JPEG", quality=85)
                return temp_path
        except Exception as e:
            logger.error(f"裁剪图片失败: {e}")
            return None

    def _pick_card(self) -> Optional[str]:
        available = [name for name in self.card_pool if name in self.card_image_map]
        if not available:
            return None
        return random.choice(available)

    async def _start_round(self, room: GameRoom, group_id: str, context: Context):
        if room.is_playing:
            await context.send_message(Plain("⏳ 当前已有游戏进行中，请等待结束"))
            return

        if not self._can_start_round(room):
            await context.send_message(Plain(f"⚠️ 今日游戏次数已达上限（{self.rounds_per_day}轮），明天再来吧！"))
            return

        card_name = self._pick_card()
        if not card_name:
            await context.send_message(Plain("❌ 没有可用的卡牌图片，请先添加卡牌图片到 cards_images/ 目录"))
            return

        img_path = self.card_image_map[card_name]
        cropped_path = self._crop_card_image(img_path)
        if not cropped_path:
            await context.send_message(Plain("❌ 图片裁剪失败，请检查图片格式"))
            return

        room.reset()
        room.is_playing = True
        room.current_card = card_name
        room.current_image_path = cropped_path
        room.round_start_time = datetime.now()
        room.daily_rounds += 1
        room.last_round_date = self._get_today()
        room.guessed_users = set()

        try:
            image_msg = MBImage.from_file_path(cropped_path)
            await context.send_message(
                Plain(f"🔍 **第{room.daily_rounds}轮猜卡牌**\n")
                + Plain("🤔 请根据下图猜出卡牌名称（直接发送卡牌名）\n")
                + Plain(f"⏱️ 限时 {self.guess_timeout} 秒\n")
                + Plain("💡 提示：卡牌名称字数 = " + str(len(card_name)))
            )
            await context.send_message(image_msg)
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            await context.send_message(Plain(f"❌ 发送图片失败: {e}"))
            room.reset()
            return

        room.timer_task = asyncio.create_task(self._guess_timeout_handler(room, group_id, context))

    async def _guess_timeout_handler(self, room: GameRoom, group_id: str, context: Context):
        await asyncio.sleep(self.guess_timeout)
        if room.is_playing:
            await context.send_message(
                Plain(f"⏰ 时间到！正确答案是: **{room.current_card}**\n")
                + Plain("💪 下次加油！")
            )
            room.is_playing = False
            if room.timer_task:
                room.timer_task.cancel()
                room.timer_task = None
            if room.current_image_path and os.path.exists(room.current_image_path):
                try:
                    os.remove(room.current_image_path)
                except Exception:
                    pass
            room.current_image_path = None

    async def _handle_guess(self, room: GameRoom, group_id: str, user_id: str,
                            guess: str, context: Context):
        if not room.is_playing:
            await context.send_message(Plain("⚠️ 当前没有进行中的游戏"))
            return

        if user_id in room.guessed_users:
            await context.send_message(Plain("⏳ 你已经猜过了，等别人猜吧！"))
            return

        room.guessed_users.add(user_id)

        if guess.strip().lower() == room.current_card.lower():
            room.add_score(user_id, self.points_correct)
            await context.send_message(
                Plain(f"🎉 **恭喜 <@{user_id}> 猜对了！**\n")
                + Plain(f"✅ 正确答案: {room.current_card}\n")
                + Plain(f"📊 +{self.points_correct}分！\n")
                + Plain(f"📈 当前总分: {room.scores.get(user_id, 0)}分")
            )
            room.is_playing = False
            if room.timer_task:
                room.timer_task.cancel()
                room.timer_task = None
            if room.current_image_path and os.path.exists(room.current_image_path):
                try:
                    os.remove(room.current_image_path)
                except Exception:
                    pass
            room.current_image_path = None
            await self._show_leaderboard(room, group_id, context)
        else:
            hint = self._get_hint(room.current_card, len(room.guessed_users))
            await context.send_message(
                Plain(f"❌ 不对哦，再想想！\n")
                + Plain(f"💡 {hint}")
            )

    def _get_hint(self, card: str, attempt: int) -> str:
        if attempt <= 2:
            return f"卡牌名称有 {len(card)} 个字"
        elif attempt <= 4:
            return f"第一个字是: {card[0]}"
        elif attempt <= 6:
            return f"最后一个字是: {card[-1]}"
        else:
            chars = list(card)
            hide_count = max(1, len(chars) // 3)
            hide_indices = random.sample(range(len(chars)), min(hide_count, len(chars)-1))
            for i in hide_indices:
                chars[i] = "？"
            return f"提示: {''.join(chars)}"

    async def _show_leaderboard(self, room: GameRoom, group_id: str, context: Context):
        if not room.scores:
            await context.send_message(Plain("📊 暂无积分记录"))
            return
        sorted_scores = sorted(room.scores.items(), key=lambda x: x[1], reverse=True)
        top = sorted_scores[:10]
        msg = Plain("🏆 **本群排行榜**\n")
        for i, (uid, score) in enumerate(top, 1):
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            msg += Plain(f"{medal} <@{uid}>: {score}分\n")
        await context.send_message(msg)

    @on_message
    async def on_group_message(self, event: MessageEvent, context: Context):
        if not event.group_id:
            return

        group_id = str(event.group_id)
        user_id = str(event.user_id)
        room = self._get_room(group_id)

        text = ""
        if event.message and event.message.plain:
            text = event.message.plain.strip()

        if text in ["/猜卡", "/开始猜卡", "猜卡", "开始猜卡"]:
            await self._start_round(room, group_id, context)
            return

        if text in ["/结束猜卡", "结束猜卡"]:
            if not room.is_playing:
                await context.send_message(Plain("⚠️ 当前没有进行中的游戏"))
                return
            await context.send_message(Plain(f"⏹️ 游戏已结束，正确答案是: {room.current_card}"))
            room.is_playing = False
            if room.timer_task:
                room.timer_task.cancel()
                room.timer_task = None
            if room.current_image_path and os.path.exists(room.current_image_path):
                try:
                    os.remove(room.current_image_path)
                except Exception:
                    pass
            room.current_image_path = None
            return

        if text in ["/排行榜", "排行榜", "排名"]:
            await self._show_leaderboard(room, group_id, context)
            return

        if text in ["/我的分数", "我的分数", "分数"]:
            score = room.scores.get(user_id, 0)
            await context.send_message(Plain(f"📊 你的当前得分: {score}分"))
            return

        if text in ["/卡牌列表", "卡牌列表"]:
            if self.card_pool:
                preview = "、".join(self.card_pool[:20])
                if len(self.card_pool) > 20:
                    preview += f" ... 共{len(self.card_pool)}张"
                await context.send_message(Plain(f"📚 可用卡牌（部分）:\n{preview}"))
            else:
                await context.send_message(Plain("❌ 没有加载到卡牌列表"))
            return

        if room.is_playing and text and not text.startswith("/"):
            if len(text) <= 30:
                await self._handle_guess(room, group_id, user_id, text, context)

    async def close(self):
        for room in self.rooms.values():
            room.reset()
        self.rooms.clear()

    def get_metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="astrbot_plugin_kards_guess_card",
            version="1.0.0",
            description="根据裁剪后的卡牌形象猜Kards卡牌名称",
            author="YourName",
            dependencies=["Pillow", "aiohttp"]
        )
