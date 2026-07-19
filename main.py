import os
import json
import asyncio
import random
import tempfile
from typing import Dict, List, Optional, Set
from datetime import datetime

from PIL import Image
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

try:
    from astrbot.api.types import Plain, Image as MBImage
except ImportError:
    MBImage = None


class GameRoom:
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


@register(name="kards_guess_card", author="YourName", desc="Kards卡牌剪影猜猜猜", version="1.0.0")
class KardsGuessCardPlugin(Star):
    async def initialize(self):
        self.rooms: Dict[str, GameRoom] = {}
        self.card_pool: List[str] = []
        self.card_image_map: Dict[str, str] = {}

        list_path = os.path.join(os.path.dirname(__file__), "cards_data", "kards_list.json")
        try:
            with open(list_path, "r", encoding="utf-8") as f:
                self.card_pool = json.load(f)
            logger.info(f"已加载 {len(self.card_pool)} 张卡牌名称")
        except Exception as e:
            logger.error(f"加载卡牌列表失败: {e}")
            self.card_pool = []

        image_dir = "./cards_images/"
        if hasattr(self, 'config') and self.config:
            image_dir = self.config.get("card_image_dir", "./cards_images/")
        if not os.path.isabs(image_dir):
            image_dir = os.path.join(os.path.dirname(__file__), image_dir)
        if not os.path.exists(image_dir):
            logger.warning(f"卡牌图片目录不存在: {image_dir}")
        else:
            for filename in os.listdir(image_dir):
                if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
                    name = os.path.splitext(filename)[0]
                    if name in self.card_pool:
                        self.card_image_map[name] = os.path.join(image_dir, filename)
                    else:
                        logger.debug(f"图片 {filename} 对应的卡牌不在列表中，忽略")
            logger.info(f"已加载 {len(self.card_image_map)} 张卡牌图片")

        self.crop_top = 0.22
        self.crop_bottom = 0.30
        self.crop_left = 0.08
        self.crop_right = 0.08
        self.guess_timeout = 60
        self.rounds_per_day = 20
        self.points_correct = 10
        if hasattr(self, 'config') and self.config:
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
            await context.send_message("⏳ 当前已有游戏进行中，请等待结束")
            return
        if not self._can_start_round(room):
            await context.send_message(f"⚠️ 今日游戏次数已达上限（{self.rounds_per_day}轮），明天再来吧！")
            return
        card_name = self._pick_card()
        if not card_name:
            await context.send_message("❌ 没有可用的卡牌图片，请先添加卡牌图片到 cards_images/ 目录")
            return
        img_path = self.card_image_map[card_name]
        cropped_path = self._crop_card_image(img_path)
        if not cropped_path:
            await context.send_message("❌ 图片裁剪失败，请检查图片格式")
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
            await context.send_message(
                f"🔍 **第{room.daily_rounds}轮猜卡牌**\n"
                "🤔 请根据下图猜出卡牌名称\n"
                f"⏱️ 限时 {self.guess_timeout} 秒\n"
                f"💡 提示：卡牌名称字数 = {len(card_name)}\n"
                "📝 使用命令：`猜 卡牌名` 进行作答"
            )
            if MBImage is not None:
                image_msg = MBImage.from_file_path(cropped_path)
                await context.send_message(image_msg)
            else:
                try:
                    with open(cropped_path, 'rb') as f:
                        await context.send_file(f, filename=f"{card_name}.jpg")
                except Exception as e:
                    logger.warning(f"发送文件失败: {e}")
                    await context.send_message(f"[图片已生成: {cropped_path}]")
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            await context.send_message(f"❌ 发送图片失败: {e}")
            room.reset()
            return
        room.timer_task = asyncio.create_task(self._guess_timeout_handler(room, group_id, context))

    async def _guess_timeout_handler(self, room: GameRoom, group_id: str, context: Context):
        await asyncio.sleep(self.guess_timeout)
        if room.is_playing:
            await context.send_message(f"⏰ 时间到！正确答案是: **{room.current_card}**\n💪 下次加油！")
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

    async def _handle_guess(self, room: GameRoom, user_id: str, guess: str, context: Context):
        if not room.is_playing:
            await context.send_message("⚠️ 当前没有进行中的游戏")
            return
        if user_id in room.guessed_users:
            await context.send_message("⏳ 你已经猜过了，等别人猜吧！")
            return
        room.guessed_users.add(user_id)
        if guess.strip().lower() == room.current_card.lower():
            room.add_score(user_id, self.points_correct)
            await context.send_message(
                f"🎉 **恭喜 <@{user_id}> 猜对了！**\n"
                f"✅ 正确答案: {room.current_card}\n"
                f"📊 +{self.points_correct}分！\n"
                f"📈 当前总分: {room.scores.get(user_id, 0)}分"
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
            await self._show_leaderboard(room, context)
        else:
            hint = self._get_hint(room.current_card, len(room.guessed_users))
            await context.send_message(f"❌ 不对哦，再想想！\n💡 {hint}")

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

    async def _show_leaderboard(self, room: GameRoom, context: Context):
        if not room.scores:
            await context.send_message("📊 暂无积分记录")
            return
        sorted_scores = sorted(room.scores.items(), key=lambda x: x[1], reverse=True)
        top = sorted_scores[:10]
        msg = "🏆 **本群排行榜**\n"
        for i, (uid, score) in enumerate(top, 1):
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            msg += f"{medal} <@{uid}>: {score}分\n"
        await context.send_message(msg)

    # ==================== 命令处理（全部使用 @filter.command） ====================

    @filter.command("猜卡")
    async def cmd_start(self, event: AstrMessageEvent, context: Context):
        """开始一轮猜牌"""
        group_id = event.get_group_id()
        if not group_id:
            return
        room = self._get_room(group_id)
        await self._start_round(room, group_id, context)

    @filter.command("结束猜卡")
    async def cmd_end(self, event: AstrMessageEvent, context: Context):
        group_id = event.get_group_id()
        if not group_id:
            return
        room = self._get_room(group_id)
        if not room.is_playing:
            await context.send_message("⚠️ 当前没有进行中的游戏")
            return
        await context.send_message(f"⏹️ 游戏已结束，正确答案是: {room.current_card}")
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

    @filter.command("排行榜")
    async def cmd_rank(self, event: AstrMessageEvent, context: Context):
        group_id = event.get_group_id()
        if not group_id:
            return
        room = self._get_room(group_id)
        await self._show_leaderboard(room, context)

    @filter.command("我的分数")
    async def cmd_score(self, event: AstrMessageEvent, context: Context):
        group_id = event.get_group_id()
        if not group_id:
            return
        user_id = event.get_user_id()
        room = self._get_room(group_id)
        score = room.scores.get(user_id, 0)
        await context.send_message(f"📊 你的当前得分: {score}分")

    @filter.command("卡牌列表")
    async def cmd_list(self, event: AstrMessageEvent, context: Context):
        if self.card_pool:
            preview = "、".join(self.card_pool[:20])
            if len(self.card_pool) > 20:
                preview += f" ... 共{len(self.card_pool)}张"
            await context.send_message(f"📚 可用卡牌（部分）:\n{preview}")
        else:
            await context.send_message("❌ 没有加载到卡牌列表")

    # 猜牌命令：猜 <卡牌名>
    @filter.command("猜")
    async def cmd_guess(self, event: AstrMessageEvent, context: Context):
        """猜牌命令，用法：猜 卡牌名"""
        group_id = event.get_group_id()
        if not group_id:
            return
        user_id = event.get_user_id()
        room = self._get_room(group_id)
        # 获取命令参数（卡牌名）
        args = event.get_args()  # 这个方法可能在部分版本中不存在，使用 fallback
        if hasattr(event, 'get_args'):
            args = event.get_args()
        else:
            # 从消息内容中提取（如果 get_args 不可用）
            msg = event.get_message_str()
            if msg.startswith("猜 "):
                args = msg[2:].strip()
            else:
                args = ""
        if not args:
            await context.send_message("❌ 请输入要猜的卡牌名，例如：`猜 虎式坦克`")
            return
        await self._handle_guess(room, user_id, args, context)

    async def close(self):
        for room in self.rooms.values():
            room.reset()
        self.rooms.clear()
