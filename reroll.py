import os
import logging
import time
import pyautogui
import pytesseract
import cv2
import random
from datetime import datetime, timezone
from enum import Enum, auto
from adbutils import AdbDevice
from friendseeker import FriendSeeker
from discordmsg import DiscordMsg


LOGGER = logging.getLogger("Reroll")

SCREEN_REGION = (0, 0, 540, 960)
BORDER_REGIONS = [
    (36, 468, 135, 10),
    (198, 468, 135, 10),
    (362, 468, 135, 10),
    (115, 694, 135, 10),
    (281, 694, 135, 10),
]
DEFAULT_DELAY_MS = 300
DEFAULT_GAME_SPEED = 3
DEFAULT_SWIPE_SPEED = 480
MAX_SWIPE_SPEED = 1000
DEFAULT_CONFIDENCE = 0.8
DEFAULT_LANGUAGE = "Chinese"
DEFAULT_TIME_OUT = 45
MAX_FRIEND_TIME_SECOND = 15 * 60
MAX_WAIT_FRIEND_TIME_SECOND = 15
DEFAULT_MAX_PACKS_TO_OPEN = 4
DEFAULT_CHECK_DOUBLE_TWOSTAR = False


class RerollState(Enum):
    INIT = auto()
    REGISTERED = auto()
    RESET = auto()
    RESTART = auto()
    FOUNDGP = auto()
    FOUNDINVALID = auto()
    FINISHED_TUTORIAL = auto()
    AUTOFRIEND = auto()
    COMPLETED = auto()
    BREAKDOWN = auto()


class RerollPack(Enum):
    MEWTWO = (1, "A1")
    CHARIZARD = (2, "A1")
    PIKACHU = (3, "A1")
    MEW = (4, "A1a")
    DIALGA = (5, "A2")
    PALKIA = (6, "A2")
    ARCEUS = (7, "A2a")
    Shining = (8, "A2b")

    def __init__(self, num, series):
        self.num = num
        self.series = series


# 定义异常
class RerollStuckException(Exception):
    pass


class Reroll:
    delay_ms = DEFAULT_DELAY_MS
    game_speed = DEFAULT_GAME_SPEED
    swipe_speed = DEFAULT_SWIPE_SPEED
    confidence = DEFAULT_CONFIDENCE
    timeout = DEFAULT_TIME_OUT
    max_packs_to_open = DEFAULT_MAX_PACKS_TO_OPEN

    def __init__(
        self,
        reroll_pack,
        adb_device: AdbDevice,
        friend_code_seeker: FriendSeeker,
        discord_msg: DiscordMsg,
        debug_mode=False,
        delay_ms=DEFAULT_DELAY_MS,
        game_speed=DEFAULT_GAME_SPEED,
        swipe_speed=DEFAULT_SWIPE_SPEED,
        confidence=DEFAULT_CONFIDENCE,
        timeout=DEFAULT_TIME_OUT,
        language=DEFAULT_LANGUAGE,
        account_name="SlvGP",
        max_packs_to_open=DEFAULT_MAX_PACKS_TO_OPEN,
        check_double_twostar=DEFAULT_CHECK_DOUBLE_TWOSTAR,
    ):
        if isinstance(reroll_pack, RerollPack):
            self.reroll_pack = reroll_pack
        else:
            self.reroll_pack = RerollPack[reroll_pack]
        self.debug_mode = debug_mode
        self.delay_ms = delay_ms
        self.game_speed = game_speed
        self.swipe_speed = swipe_speed
        self.confidence = confidence
        self.timeout = timeout
        self.language = language
        self.account_name = account_name
        self.temp_account_name = account_name
        self.friend_code_seeker = friend_code_seeker
        if isinstance(max_packs_to_open, int):
            self.max_packs_to_open = max(1, min(max_packs_to_open, 4))
        # 初始化
        self.state = RerollState.INIT
        self.total_pack = 0
        self.current_pack = 0
        self.wp_checked = False
        # 连接到 ADB 服务器
        self.adb_device = adb_device
        # 获取设备端口号
        self.adb_port = adb_device.get_serialno().split(":")[-1]
        self.discord_msg = discord_msg
        self.check_double_twostar = check_double_twostar

    def format_log(self, message):
        return f"[127.0.0.1:{self.adb_port}] {message}"

    def reset(self):
        self.current_pack = 0
        self.wp_checked = False
        self.state = RerollState.RESET

    def adb_tap(self, x, y, delay=True):
        """
        使用 ADB 点击模拟器屏幕上的特定位置
        """
        self.adb_device.click(x, y)
        if delay:
            time.sleep(self.delay_ms / 1000)

    def adb_swipe(self, x1, y1, x2, y2, duration=None):
        """
        使用 ADB 模拟滑动操作
        :param x1: 起点 x 坐标
        :param y1: 起点 y 坐标
        :param x2: 终点 x 坐标
        :param y2: 终点 y 坐标
        :param duration: 滑动时间（毫秒）
        """
        if duration is None:
            duration = self.swipe_speed
        self.adb_device.swipe(x1, y1, x2, y2, duration / 1000)
        time.sleep(duration * 1.2 / 1000)

    def adb_input(self, text):
        """
        使用 ADB 输入文本
        """
        self.adb_device.shell(["input", "text", text])
        time.sleep(self.delay_ms / 1000)

    def adb_screenshot(self):
        """
        使用 ADB 捕获设备屏幕内容
        """
        return self.adb_device.screenshot()

    def restart_game_instance(self):
        """
        重启游戏
        """
        self.adb_device.app_stop("jp.pokemon.pokemontcgp")
        time.sleep(1)
        self.adb_device.app_start(
            "jp.pokemon.pokemontcgp", "com.unity3d.player.UnityPlayerActivity"
        )
        time.sleep(1)
        self.wp_checked = True
        if self.state != RerollState.FOUNDGP:
            self.state = RerollState.RESTART

    def backup_account(self, valid=False, friend_code=None):
        """
        备份账户数据
        """
        try:
            # 停止应用
            self.adb_device.app_stop("jp.pokemon.pokemontcgp")
            time.sleep(1)

            # 检查账户数据文件是否存在
            result = self.adb_device.shell(
                "su -c 'ls /data/data/jp.pokemon.pokemontcgp/shared_prefs/deviceAccount:.xml'"
            )
            if "No such file or directory" in result:
                LOGGER.warning(self.format_log("No account data found."))
                self.reset()
                return

            # 备份文件名
            if friend_code:
                backup_filename = f"{friend_code}_{'valid' if valid else 'invalid'}.xml"
            else:
                backup_filename = (
                    f"deviceAccount_{self.adb_port}_{int(time.time())}.xml"
                )

            # 复制账户数据文件到 SD 卡
            result = self.adb_device.shell(
                "su -c 'cp /data/data/jp.pokemon.pokemontcgp/shared_prefs/deviceAccount:.xml /sdcard/'"
            )
            if result:
                raise Exception(result)

            backup_path = os.path.join(os.curdir, "backup", backup_filename)
            # 从 SD 卡拉取备份文件到本地
            result = self.adb_device.sync.pull(
                "/sdcard/deviceAccount:.xml",
                backup_path,
            )
            if int(result) == 0:
                raise Exception("Failed to pull backup file")

            # 删除设备上的账户数据文件
            result = self.adb_device.shell(
                "su -c 'rm -f /data/data/jp.pokemon.pokemontcgp/shared_prefs/deviceAccount:.xml'"
            )
            if result:
                raise Exception(result)

            LOGGER.info(self.format_log(f"Account data backed up as {backup_filename}"))
            self.reset()
            return backup_path

        except Exception as e:
            LOGGER.error(self.format_log(f"Error during backup: {e}"))
            self.state = RerollState.BREAKDOWN

    def image_search(self, image_path, screenshot, region=None, confidence=confidence):
        """
        在图片中搜索指定图像
        """
        try:
            result = pyautogui.locate(
                image_path, screenshot, region=region, confidence=confidence
            )
            if result:
                LOGGER.info(
                    self.format_log(
                        f"Found {image_path} at ({result.left}, {result.top}, {result.left + result.width}, {result.top + result.height})"
                    )
                )
            return result
        except pyautogui.ImageNotFoundException as e:
            LOGGER.debug(self.format_log(f"Image not found: {image_path}"))
            return None
        except Exception as e:
            LOGGER.error(self.format_log(f"Error during image search: {e}"))
            return None

    def get_image_path(self, image_name):
        return os.path.join(os.curdir, "res", self.language, f"{image_name}.png")

    def screen_search(self, image_path, region=None, confidence=confidence):
        """
        在设备屏幕截图中搜索指定图像
        """
        # 获取设备屏幕截图
        screenshot = self.adb_screenshot()

        # 在截图中搜索指定图像
        return self.image_search(image_path, screenshot, region, confidence)

    # 判断是否有异常
    def error_check(self):
        """
        在设备屏幕截图中搜索异常
        """
        # 获取设备屏幕截图
        screenshot = self.adb_screenshot()

        # 在截图中搜索异常图像
        if self.image_search(
            image_path=self.get_image_path("Error"),
            screenshot=screenshot,
            region=(245, 258, 50, 24),
        ):
            LOGGER.warning(self.format_log("Error message found. Clicking retry..."))
            self.adb_tap(235, 675)
            time.sleep(1)
        elif self.image_search(
            image_path=self.get_image_path("App"),
            screenshot=screenshot,
        ):
            LOGGER.warning(
                self.format_log("Found myself at the home page. Restarting...")
            )
            raise RerollStuckException(
                f"Instance {self.adb_port} has been stuck at home page"
            )
        # 判断当前时间是否为 utc 6:00
        now = datetime.now(timezone.utc)
        if now.hour == 6 and now.minute < 5:
            if self.image_search(
                image_path=self.get_image_path("DateChange"),
                screenshot=screenshot,
                region=(235, 405, 54, 19),
            ):
                LOGGER.warning(
                    self.format_log("Found date change. Restarting game instance...")
                )
                self.restart_game_instance()

    def tap_until(
        self,
        image_name,
        region=None,
        confidence=confidence,
        click_x=0,
        click_y=0,
        delay_ms=delay_ms,
        skip_time_ms=0,
        timeout_ms=timeout,
        safe_time=0,
    ):
        image_path = self.get_image_path(image_name)
        click = click_x > 0 and click_y > 0
        start_time = time.time()
        confirmed = False
        click_time = 0

        LOGGER.info(self.format_log(f"Looking for {image_name}"))

        while True:
            if click:
                elapsed_click_time = time.time() - click_time
                if elapsed_click_time > delay_ms / 1000:
                    self.adb_tap(click_x, click_y, delay=False)
                    if delay_ms < 200:
                        time.sleep(delay_ms / 1000)
                        self.adb_tap(click_x, click_y, delay=False)
                    else:
                        time.sleep((delay_ms - 200) / 1000)
                    click_time = time.time()

            if self.screen_search(image_path, region, confidence=confidence):
                confirmed = True
                break
            else:
                elapsed_time = time.time() - start_time
                if elapsed_time >= timeout_ms or safe_time >= timeout_ms:
                    LOGGER.warning(
                        self.format_log(
                            f"Timeout for {image_name}. Elapsed time: {elapsed_time}s"
                        )
                    )
                    if self.debug_mode:
                        # save screenshot
                        stuck_screenshot = self.adb_screenshot()
                        stuck_screenshot_path = os.path.join(
                            os.curdir,
                            "screenshot",
                            f"screenshot_{self.adb_port}_{int(time.time())}.png",
                        )
                        stuck_screenshot.save(stuck_screenshot_path)

                    raise RerollStuckException(
                        f"Instance {self.adb_port} has been stuck at {image_name}"
                    )
                elif elapsed_time >= self.timeout / 3:
                    LOGGER.warning(
                        self.format_log(
                            f"Start error check for {image_name}. Elapsed time: {elapsed_time}s"
                        )
                    )
                    self.error_check()

            if skip_time_ms:
                elapsed_time = time.time() - start_time
                if elapsed_time >= skip_time_ms:
                    return False

        return confirmed

    def rarity_check(self):
        """
        检查是否有稀有卡牌
        """
        check_need = False
        common_card_num = 0
        twostar_card_num = 0
        screenshot = self.adb_screenshot()
        for region in BORDER_REGIONS[0:3]:
            if self.image_search(
                image_path=self.get_image_path("Common"),
                screenshot=screenshot,
                region=region,
            ):
                common_card_num += 1
                break
        is_god_pack = common_card_num == 0
        check_need = True
        
        if not is_god_pack and self.check_double_twostar:
            for region in BORDER_REGIONS[3:5]:
                if self.image_search(
                    image_path=self.get_image_path("RainbowFrame"),
                    screenshot=screenshot,
                    region=region,
                ) or self.image_search(
                    image_path=self.get_image_path("BlackFrame"),
                    screenshot=screenshot,
                    region=region,
                ) or self.image_search(
                    image_path=self.get_image_path("WhiteFrame"),
                    screenshot=screenshot,
                    region=region,
                ):
                    twostar_card_num += 1
        is_double_twostar_pack = twostar_card_num == 2
        
        two_star_num = 0
        LOGGER.info(self.format_log(f"Found {common_card_num} common cards"))
        if is_god_pack:
            # save screenshot
            god_pack_screenshot_path = os.path.join(
                os.curdir,
                "screenshot",
                f"god_pack_{self.adb_port}_{int(time.time())}.png",
            )
            screenshot.save(god_pack_screenshot_path)
            if self.image_search(
                image_path=self.get_image_path("Immerse"),
                screenshot=screenshot,
                region=(26, 445, 468, 260),
            ) or self.image_search(
                image_path=self.get_image_path("Crown"),
                screenshot=screenshot,
                region=(30, 465, 395, 240),
            ) or self.image_search(
                image_path=self.get_image_path("Shining"),
                screenshot=screenshot,
                region=(30, 465, 395, 240),
            ):
                check_need = False
            for border_region in BORDER_REGIONS:
                if not self.image_search(
                    image_path=self.get_image_path("Onestar"),
                    screenshot=screenshot,
                    region=border_region,
                ):
                    two_star_num += 1
        return (
            is_god_pack,
            is_double_twostar_pack,
            check_need,
            two_star_num,
            god_pack_screenshot_path if is_god_pack else None,
        )

    def open_pack(self, pack_num=2):
        """
        打开卡包
        """
        pack_icon_name = (
            self.reroll_pack.series if pack_num > 1 else RerollPack.MEWTWO.series
        )
        swipe_duration = self.swipe_speed

        if pack_num > 0:
            self.current_pack += 1
            if pack_num > 3:
                self.tap_until(
                    region=(467, 888, 32, 32),
                    image_name="Skip",
                    click_x=349,
                    click_y=791,
                )
            else:
                self.tap_until(
                    region=(467, 888, 32, 32),
                    image_name="Skip",
                    click_x=270,
                    click_y=763,
                )
            self.tap_until(
                region=(405, 454, 26, 17),
                image_name=pack_icon_name,
                click_x=487,
                click_y=905,
            )
        else:
            self.tap_until(
                region=(282, 228, 75, 25),
                image_name="ToSwipe",
                click_x=268,
                click_y=754,
            )

        if self.game_speed == 3:
            self.adb_tap(37, 142)
            self.adb_tap(33, 262)

        swipe_times = 0
        while self.screen_search(
            image_path=self.get_image_path(pack_icon_name),
            region=(405, 454, 26, 17),
        ):
            if swipe_times > 1:
                swipe_duration = min(swipe_duration + 50, MAX_SWIPE_SPEED)
            self.adb_swipe(42, 555, 502, 555, duration=swipe_duration)
            swipe_times += 1
        if swipe_times < 2:
            swipe_duration = max(swipe_duration - 10, DEFAULT_SWIPE_SPEED)
        self.swipe_speed = swipe_duration

        if pack_num == 0:
            if self.game_speed == 3:
                self.adb_tap(200, 264)

            self.tap_until(
                region=(114, 821, 32, 11),
                image_name="Weak",
                click_x=268,
                click_y=582,
            )

            if self.game_speed == 3:
                self.adb_tap(33, 262)

            start_time = time.time()
            while self.screen_search(
                self.get_image_path("Weak"),
                region=(114, 821, 32, 11),
            ):
                self.adb_swipe(277, 856, 277, 207, duration=160)
                elapsed_time = time.time() - start_time
                if elapsed_time > 45:
                    raise RerollStuckException(
                        f"Instance {self.adb_port} has been stuck at swipe up first pack"
                    )

            if self.game_speed == 3:
                self.adb_tap(365, 264)

                self.adb_tap(326, 490)

            self.tap_until(
                region=(170, 86, 50, 25),
                image_name="Move",
                click_x=272,
                click_y=606,
            )
            self.adb_tap(268, 861)
            self.adb_tap(368, 639)

        else:
            if self.game_speed == 3:
                self.adb_tap(365, 264)
                self.adb_tap(326, 490)

            self.tap_until(
                region=(220, 54, 100, 25),
                image_name="Result",
                click_x=478,
                click_y=905,
                delay_ms=110,
            )
            time.sleep(self.delay_ms / 1000)
            is_god_pack, is_double_twostar_pack, check_need = False, False, False
            if pack_num > 1:
                pack_screenshot = self.adb_screenshot()
                for border_region in BORDER_REGIONS:
                    while self.image_search(
                        image_path=self.get_image_path("Blank"),
                        screenshot=pack_screenshot,
                        region=border_region,
                    ):
                        time.sleep(1)
                        pack_screenshot = self.adb_screenshot()
                time.sleep(0.5)
                is_god_pack, is_double_twostar_pack, check_need, two_star_num, god_pack_screenshot_path = (
                    self.rarity_check()
                )
            if is_god_pack or is_double_twostar_pack:
                if check_need:
                    self.state = RerollState.FOUNDGP
                elif self.state != RerollState.FOUNDGP:
                    self.state = RerollState.FOUNDINVALID
                if self.discord_msg:
                    if is_god_pack:
                        message = self.get_god_pack_notification(star_num=two_star_num, pack_num=pack_num, valid=check_need)
                    else:
                        message = self.get_double_twostar_pack_notification(pack_num=pack_num, valid=check_need)
                    self.discord_msg.send_message(
                        message,
                        screenshot_file=god_pack_screenshot_path,
                        ping=check_need,
                    )
            
            self.adb_tap(268, 903)
            if pack_num == 1:
                self.tap_until(
                    region=(240, 51, 50, 50),
                    image_name="Dex",
                    click_x=522,
                    click_y=889,
                    delay_ms=110,
                    skip_time_ms=10,
                )
                self.tap_until(
                    region=(207, 405, 54, 20),
                    image_name="Unlock",
                    click_x=272,
                    click_y=853,
                )
            elif pack_num == 3:
                self.tap_until(
                    region=(240, 51, 50, 50),
                    image_name="Dex",
                    click_x=522,
                    click_y=889,
                    delay_ms=110,
                    skip_time_ms=10,
                )
                self.tap_until(
                    region=(283, 171, 147, 51),
                    image_name="Hourglass",
                    click_x=272,
                    click_y=869,
                )
                self.tap_until(
                    region=(194, 634, 27, 27),
                    image_name="Timer",
                    click_x=324,
                    click_y=742,
                )
                self.tap_until(
                    region=(169, 365, 53, 34),
                    image_name="UseHourglass",
                    click_x=324,
                    click_y=735,
                )
                self.tap_until(
                    region=(194, 634, 27, 27),
                    image_name="Timer",
                    click_x=324,
                    click_y=758,
                )
            else:
                self.tap_until(
                    region=(240, 51, 50, 50),
                    image_name="Dex",
                    click_x=522,
                    click_y=889,
                    delay_ms=110,
                    skip_time_ms=10,
                )
                self.tap_until(
                    region=(251, 906, 38, 38),
                    image_name="Home",
                    click_x=340,
                    click_y=861,
                )

    def get_god_pack_notification(self, star_num: int, pack_num: int, valid: bool):
        return (
            "Found god pack!!\n"
            + f"{self.temp_account_name} ()\n"
            + f"[{star_num if star_num >= 0 else 'X'}/5][{pack_num - 1}P] God pack found in instance: {self.adb_port}\n"
            + f"{'Valid' if valid else 'Invalid'}"
        )
    
    def get_double_twostar_pack_notification(self, pack_num: int, valid: bool):
        return (
            "Double two star found\n"
            + f"{self.temp_account_name} ()\n"
            + f"[2x2][{pack_num - 1}P] Double two pack found in instance: {self.adb_port}\n"
            + f"{'Valid' if valid else 'Invalid'}"
        )

    def wonder_pick(self, tutorial_pack=False):
        self.tap_until(
            region=(228, 676, 42, 21),
            image_name="WPComfirm",
            click_x=280,
            click_y=410,
        )
        self.tap_until(
            region=(179, 122, 31, 21),
            image_name="Choose",
            click_x=385,
            click_y=821,
        )
        self.tap_until(
            region=(99, 72, 68, 68),
            image_name="Get",
            click_x=270,
            click_y=350,
        )
        if tutorial_pack:
            self.tap_until(
                region=(240, 51, 50, 50),
                image_name="Dex",
                click_x=522,
                click_y=889,
                delay_ms=110,
                skip_time_ms=8,
            )
            self.tap_until(
                region=(267, 354, 59, 24),
                image_name="Tutorial",
                click_x=272,
                click_y=865,
            )
        else:
            self.tap_until(
                region=(251, 906, 38, 38),
                image_name="Home",
                click_x=522,
                click_y=889,
                skip_time_ms=5,
            )

    def register(self):
        start_time = time.time()
        elapsed_time = 0

        while True:
            self.adb_tap(494, 75)
            if self.tap_until(
                region=(206, 212, 128, 25),
                image_name="Region",
                safe_time=elapsed_time,
                skip_time_ms=1,
            ):
                break
            elif self.tap_until(
                region=(245, 71, 50, 23),
                image_name="Menu",
                safe_time=elapsed_time,
                skip_time_ms=1,
            ):
                self.delete_account(in_game=False)
                break
            self.error_check()

            LOGGER.info(self.format_log("Registering new account"))

            elapsed_time = time.time() - start_time
            LOGGER.info(
                self.format_log(f"Open screen not found. Elapsed time: {elapsed_time}s")
            )

        start_time = time.time()
        elapsed_time = 0
        if self.game_speed > 1 and self.state != RerollState.RESET:
            self.adb_tap(37, 142)
            if self.game_speed == 3:
                self.adb_tap(365, 264)
            else:
                self.adb_tap(200, 264)
            self.adb_tap(326, 490)

        while not self.tap_until(
            region=(261, 494, 72, 20),
            image_name="ConfirmBirth",
            click_x=275,
            click_y=859,
            safe_time=elapsed_time,
            skip_time_ms=1,
        ):
            # select country/region
            open_screenshot = self.adb_screenshot()
            if self.image_search(
                image_path=self.get_image_path(image_name="RegionUnselected"),
                screenshot=open_screenshot,
                region=(95, 361, 108, 31),
            ):
                self.adb_tap(278, 378)
                self.adb_tap(274, 680)
                self.adb_tap(274, 817)
            elif self.image_search(
                image_path=self.get_image_path(image_name="ChooseRegion"),
                screenshot=open_screenshot,
                region=(182, 226, 50, 25),
            ):
                self.adb_tap(274, 680)
                self.adb_tap(274, 817)

            if not self.screen_search(
                image_path=self.get_image_path(image_name="Selected"),
                region=(444, 691, 24, 18),
            ):
                elapsed_time = time.time() - start_time
                LOGGER.info(f"Select year. Elapsed time: {elapsed_time}s")
                self.adb_tap(378, 697)
                self.adb_tap(389, 642)

            if not self.screen_search(
                image_path=self.get_image_path(image_name="Selected"),
                region=(211, 691, 24, 18),
            ):
                elapsed_time = time.time() - start_time
                LOGGER.info(f"Select month. Elapsed time: {elapsed_time}s")
                self.adb_tap(156, 697)
                self.adb_tap(160, 655)

        self.tap_until(
            region=(179, 211, 72, 24),
            image_name="TosScreen",
            click_x=378,
            click_y=634,
            delay_ms=1000,
        )
        self.tap_until(
            region=(238, 832, 64, 64),
            image_name="Close",
            click_x=254,
            click_y=500,
            delay_ms=1000,
        )
        self.tap_until(
            region=(179, 211, 72, 24),
            image_name="TosScreen",
            click_x=275,
            click_y=856,
        )
        self.tap_until(
            region=(238, 832, 64, 64),
            image_name="Close",
            click_x=255,
            click_y=576,
            delay_ms=1000,
        )
        self.tap_until(
            region=(179, 211, 72, 24),
            image_name="TosScreen",
            click_x=275,
            click_y=856,
        )

        self.adb_tap(80, 642)
        self.adb_tap(84, 705)
        self.adb_tap(275, 859)
        self.adb_tap(263, 592)

        if not self.screen_search(
            image_path=self.get_image_path("Uncomplete"),
            region=(235, 365, 34, 18),
        ):
            self.tap_until(
                region=(144, 472, 72, 20),
                image_name="Download",
                click_x=264,
                click_y=826,
            )
            self.tap_until(
                region=(252, 419, 72, 20),
                image_name="Complete",
                click_x=439,
                click_y=630,
            )

        self.adb_tap(276, 630)

        if self.game_speed == 3:
            self.adb_tap(37, 142)
            self.adb_tap(33, 262)

        self.tap_until(
            region=(77, 587, 72, 18),
            image_name="Welcome",
            click_x=490,
            click_y=910,
        )

        if self.game_speed == 3:
            self.adb_tap(365, 264)
            self.adb_tap(326, 490)

        self.tap_until(
            region=(280, 479, 72, 20), image_name="Name", click_x=338, click_y=765
        )
        self.adb_tap(262, 410)
        self.adb_tap(262, 410)

        start_time = time.time()
        elapsed_time = 0
        self.temp_account_name = self.account_name + str(random.randint(1, 999))
        self.adb_input(self.temp_account_name)

        self.adb_tap(478, 910)

        while not self.tap_until(
            region=(280, 479, 72, 20),
            image_name="Name",
            click_x=405,
            click_y=632,
            skip_time_ms=5,
        ):
            elapsed_time = time.time() - start_time
            LOGGER.info(f"Stuck at name. Elapsed time: {elapsed_time}s")
            self.adb_tap(262, 410)
            self.adb_tap(262, 410)
            self.adb_input("1")
            self.adb_tap(478, 910)
            if elapsed_time > self.timeout:
                raise RerollStuckException(
                    f"Instance {self.adb_port} has been stuck at Name"
                )
        self.adb_tap(349, 637)
        self.adb_tap(353, 637)

        self.state = RerollState.REGISTERED

    def pass_tutorial(self):
        self.tap_until(
            region=(238, 873, 64, 64),
            image_name="Back",
            click_x=268,
            click_y=585,
        )

        # Tutorial pack
        self.open_pack(pack_num=0)
        self.tap_until(
            region=(30, 332, 24, 24),
            image_name="DexTask",
            click_x=483,
            click_y=826,
        )
        self.tap_until(
            region=(252, 201, 36, 18),
            image_name="Reward",
            click_x=47,
            click_y=406,
        )
        self.tap_until(
            region=(363, 520, 36, 48),
            image_name="Full",
            click_x=274,
            click_y=889,
        )

        self.tap_until(
            region=(309, 641, 72, 18),
            image_name="Notification",
            click_x=268,
            click_y=366,
            timeout_ms=45,
        )
        self.adb_tap(336, 763)

        # First pack
        self.open_pack(pack_num=1)

        self.tap_until(
            region=(120, 681, 49, 29),
            image_name="WonderIcon",
            click_x=268,
            click_y=611,
        )
        self.tap_until(
            region=(189, 547, 72, 18),
            image_name="Wonder",
            click_x=148,
            click_y=695,
        )
        self.tap_until(
            region=(237, 804, 64, 64),
            image_name="Back",
            click_x=340,
            click_y=775,
        )
        self.wonder_pick(tutorial_pack=True)

        self.tap_until(
            region=(463, 816, 34, 40),
            image_name="Task",
            click_x=347,
            click_y=793,
        )

        if self.state != RerollState.FOUNDGP:
            self.state = RerollState.FINISHED_TUTORIAL

    def open_234_pack(self):
        if self.reroll_pack.series == "A1":
            self.tap_until(
                region=(464, 700, 28, 48),
                image_name="Point",
                click_x=403,
                click_y=320,
            )
            time.sleep(1)
            if self.reroll_pack == RerollPack.CHARIZARD:
                self.adb_tap(108, 529)
            elif self.reroll_pack == RerollPack.PIKACHU:
                self.adb_tap(422, 529)
        elif self.reroll_pack.series == "A1a":
            self.tap_until(
                region=(250, 820, 38, 32),
                image_name="SmallBack",
                click_x=184,
                click_y=366,
            )
        elif self.reroll_pack.series == "A2":
            self.tap_until(
                region=(464, 700, 28, 48),
                image_name="Point",
                click_x=420,
                click_y=312,
            )
            if self.reroll_pack == RerollPack.PALKIA:
                self.adb_tap(422, 529)
        elif self.reroll_pack.series == "A2a":
            self.tap_until(
                region=(464, 700, 28, 48),
                image_name="Point",
                click_x=420,
                click_y=312,
            )
        elif self.reroll_pack.series == "A2b":
            self.tap_until(
                region=(464, 700, 28, 48),
                image_name="Point",
                click_x=268,
                click_y=312,
            )
        else:
            LOGGER.error(
                self.format_log(f"Invalid pack series: {self.reroll_pack.series}")
            )
        if self.state != RerollState.FOUNDGP and self.max_packs_to_open > 1:
            self.open_pack(pack_num=2)
            self.total_pack += 1
        if self.state != RerollState.FOUNDGP and self.max_packs_to_open > 2:
            self.open_pack(pack_num=3)
            self.total_pack += 1
        # 4th pack
        if self.state != RerollState.FOUNDGP and self.max_packs_to_open > 3:
            self.open_pack(pack_num=4)
            self.total_pack += 1

        if self.state == RerollState.FOUNDGP or self.state == RerollState.FOUNDINVALID:
            self.tap_until(
                region=(251, 906, 38, 38),
                image_name="Home",
                click_x=276,
                click_y=889,
            )
        else:
            self.state = RerollState.COMPLETED

    def change_tag(self):
        self.tap_until(
            region=(120, 681, 49, 29),
            image_name="WonderIcon",
            click_x=276,
            click_y=832,
        )
        self.tap_until(
            region=(412, 456, 27, 27),
            image_name="Profile",
            click_x=269,
            click_y=93,
        )
        self.tap_until(
            region=(436, 488, 19, 13),
            image_name="Checked",
            click_x=267,
            click_y=521,
            delay_ms=500,
        )
        self.tap_until(
            region=(234, 601, 72, 19),
            image_name="Badge",
            click_x=268,
            click_y=821,
        )

    def add_friends(self):
        """
        添加好友
        """
        self.tap_until(
            region=(251, 907, 36, 36),
            image_name="OnCommu",
            click_x=270,
            click_y=924,
        )
        self.tap_until(
            region=(158, 136, 20, 15),
            image_name="FriendNum",
            click_x=70,
            click_y=831,
        )
        while not self.screen_search(
            image_path=self.get_image_path("Search"),
            region=(432, 784, 30, 30),
        ):
            self.adb_tap(485, 143)
            self.adb_tap(251, 795)
        is_start = True
        friend_code_list = self.friend_code_seeker.get_friend_codes()
        for check_id in friend_code_list:
            if not is_start:
                while not self.screen_search(
                    image_path=self.get_image_path("Search"),
                    region=(432, 784, 30, 30),
                ):
                    self.adb_tap(485, 143)
                while not self.screen_search(
                    image_path=self.get_image_path("OK"),
                    region=(481, 899, 23, 24),
                ):
                    self.adb_tap(382, 795)
                for _ in range(16):
                    self.adb_device.keyevent(67)
            is_start = False
            self.adb_input(check_id)
            self.tap_until(
                region=(479, 304, 24, 24),
                image_name="FriendResult",
                click_x=445,
                click_y=795,
                skip_time_ms=5,
            )
            if self.screen_search(
                image_path=self.get_image_path("NotFound"),
                region=(162, 389, 72, 19),
            ):
                self.adb_tap(271, 666)
                self.tap_until(
                    region=(44, 798, 44, 40),
                    image_name="Commu",
                    click_x=271,
                    click_y=919,
                )
                continue
            if self.screen_search(
                image_path=self.get_image_path("Apply"),
                region=(324, 407, 55, 43),
            ):
                self.adb_tap(469, 422)
                time.sleep(self.delay_ms / 500)
        self.tap_until(
            region=(44, 798, 44, 40),
            image_name="Commu",
            click_x=271,
            click_y=882,
        )
        # wait be accepted
        start_time = time.time()
        while True:
            self.tap_until(
                region=(158, 136, 30, 15),
                image_name="FriendNum",
                click_x=70,
                click_y=831,
            )
            self.adb_tap(269, 823)
            friend_screenshot = self.adb_screenshot()
            if self.image_search(
                image_path=self.get_image_path("FriendAll"),
                screenshot=friend_screenshot,
                region=(171, 444, 72, 20),
            ):
                break
            self.tap_until(
                region=(44, 798, 44, 40),
                image_name="Commu",
                click_x=271,
                click_y=882,
            )
            if time.time() - start_time > MAX_WAIT_FRIEND_TIME_SECOND:
                LOGGER.info(self.format_log("Timeout for checking friend request"))
                # back to home
                break
        self.tap_until(
            region=(44, 798, 44, 40),
            image_name="Commu",
            click_x=271,
            click_y=882,
        )
        self.tap_until(
            region=(120, 681, 49, 29),
            image_name="WonderIcon",
            click_x=70,
            click_y=924,
        )

    def auto_unfriend_all(self):
        # unfriend
        self.tap_until(
            region=(44, 798, 44, 40),
            image_name="Commu",
            click_x=270,
            click_y=924,
        )
        while True:
            self.tap_until(
                region=(158, 136, 20, 15),
                image_name="FriendNum",
                click_x=70,
                click_y=831,
            )
            if self.screen_search(
                image_path=self.get_image_path("NoFriend"),
                region=(225, 445, 72, 18),
            ):
                self.tap_until(
                    region=(44, 798, 44, 40),
                    image_name="Commu",
                    click_x=271,
                    click_y=882,
                )
                break
            self.tap_until(
                region=(164, 703, 24, 16),
                image_name="Friended",
                click_x=271,
                click_y=277,
            )
            self.adb_tap(271, 705)
            self.tap_until(
                region=(149, 690, 55, 43),
                image_name="Apply",
                click_x=438,
                click_y=632,
            )
            self.tap_until(
                region=(44, 798, 44, 40),
                image_name="Commu",
                click_x=271,
                click_y=882,
            )

    def auto_friend(self, friend_code):
        """
        自动接受好友
        """
        start_time = time.time()
        while (time.time() - start_time) < MAX_FRIEND_TIME_SECOND:
            self.tap_until(
                region=(44, 798, 44, 40),
                image_name="Commu",
                click_x=268,
                click_y=884,
            )
            self.tap_until(
                region=(158, 136, 20, 15),
                image_name="FriendNum",
                click_x=70,
                click_y=831,
            )
            self.adb_tap(434, 821)
            if self.screen_search(
                image_path=self.get_image_path("ToAccept"),
                region=(440, 291, 55, 55),
            ):
                self.adb_tap(467, 318)
            gp_valid = self.checker.get_valid(check_id=friend_code)
            if gp_valid == 1:
                return True
            elif gp_valid == -1:
                return False
        self.checker.set_valid(check_id=friend_code, valid=-1)
        return False

    def get_friend_code(self):
        """
        获取朋友ID
        """
        self.tap_until(
            region=(251, 907, 36, 36),
            image_name="OnCommu",
            click_x=270,
            click_y=924,
        )
        self.tap_until(
            region=(158, 136, 20, 15),
            image_name="FriendNum",
            click_x=70,
            click_y=831,
        )
        self.adb_tap(485, 143)
        time.sleep(self.delay_ms / 1000)
        # ocr (157, 566, 382, 599)
        screenshot = self.adb_screenshot()
        cropped_image = screenshot[566:599, 157:382]

        # 使用 pytesseract 进行 OCR 识别
        friend_code = pytesseract.image_to_string(
            cropped_image, config="--psm 6 digits -c tessedit_char_whitelist=0123456789"
        )
        return friend_code.strip()

    def delete_account(self, in_game=True):
        """
        删除账号
        """
        if in_game:
            self.tap_until(
                region=(190, 762, 32, 32),
                image_name="Setting",
                click_x=474,
                click_y=893,
            )
            self.tap_until(
                region=(52, 393, 35, 38),
                image_name="AccountM",
                click_x=270,
                click_y=786,
            )
            self.tap_until(
                region=(114, 781, 78, 21),
                image_name="NinAccount",
                click_x=235,
                click_y=415,
            )
        else:
            self.adb_tap(224, 435)
        self.tap_until(
            region=(239, 149, 60, 60),
            image_name="WarrningDelete",
            click_x=467,
            click_y=905,
        )
        self.tap_until(
            region=(207, 505, 54, 18),
            image_name="ComfirmDelete",
            click_x=387,
            click_y=865,
        )
        self.tap_until(
            region=(172, 365, 125, 20),
            image_name="Deleted",
            click_x=457,
            click_y=635,
        )
        self.adb_tap(277, 635)
        self.reset()

    def reroll(self):
        while True:
            try:
                if self.state == RerollState.INIT:
                    self.error_check()
                    self.register()
                if self.state == RerollState.RESTART or self.state == RerollState.RESET:
                    self.register()
                elif self.state == RerollState.REGISTERED:
                    self.pass_tutorial()
                elif self.state == RerollState.FINISHED_TUTORIAL:
                    self.add_friends()
                    if self.max_packs_to_open > 1:
                        self.open_234_pack()
                    else:
                        self.state = RerollState.COMPLETED
                elif self.state == RerollState.COMPLETED:
                    # self.auto_unfriend_all()
                    # backup account
                    self.delete_account()
                elif self.state == RerollState.FOUNDGP:
                    self.change_tag()
                    self.backup_account()
                elif self.state == RerollState.FOUNDINVALID:
                    self.auto_unfriend_all()
                    self.backup_account()
                elif self.state == RerollState.BREAKDOWN:
                    LOGGER.error(self.format_log("Breakdown"))
                    break
                else:
                    LOGGER.error(self.format_log("Invalid reroll state"))
                    break
            except RerollStuckException as e:
                LOGGER.error(self.format_log(f"Reroll stuck: {e}"))
                self.restart_game_instance()
            except Exception as e:
                LOGGER.error(self.format_log(f"Error: {e}"))
                break

    def start(self):
        self.reroll()

    def status(self):
        return {
            "port": self.adb_port,
            "total_pack": self.total_pack,
        }
