"""Application constants, official endpoints and identifiers."""

from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")

GAME_APK_HOST = "ak.hycdn.cn"
GAME_PACKAGE = "com.hypergryph.arknights"
GAME_LATEST_URL = "https://ak.hypergryph.com/downloads/android_lastest"
RESOURCE_VERSION_URL = (
    "https://raw.githubusercontent.com/MaaAssistantArknights/MaaResource/main/resource/version.json"
)
RESOURCE_ARCHIVE_URL = (
    "https://github.com/MaaAssistantArknights/MaaResource/archive/refs/heads/main.zip"
)
RESOURCE_ARCHIVE_PREFIX = "MaaResource-main/resource"
NAVIGATION_API_URL = "https://api.maa.plus/MaaAssistantArknights/api"
COPILOT_API_URL = "https://prts.maa.plus"
COPILOT_CREATOR_ID = "7661"
GITHUB_API_URL = "https://api.github.com"
GITHUB_RAW_URL = "https://raw.githubusercontent.com"
STAGE_DATA_REPOSITORY = "ArknightsAssets/ArknightsGamedata"
