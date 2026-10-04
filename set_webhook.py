import requests
import linkbot_config as config
from linkbot_app import OWNER_CMDS

API = "https://api.telegram.org/bot%s/" % config.BOT_TOKEN


def call(method, **params):
    return requests.post(API + method, json=params, timeout=30).json()


url = "https://%s%s" % (config.DOMAIN, config.WEBHOOK_PATH)
print("webhook:", call(
    "setWebhook",
    url=url,
    secret_token=config.WEBHOOK_SECRET,
    max_connections=1,
    allowed_updates=["message", "callback_query"],
    drop_pending_updates=True,
))

# کاربر عادی هیچ دستوری نمی‌بیند
print("clear default cmds:", call("deleteMyCommands"))
# منوی / فقط برای مالک
print("owner cmds:", call("setMyCommands", commands=OWNER_CMDS,
                          scope={"type": "chat", "chat_id": config.OWNER_ID}))
print("menu button:", call("setChatMenuButton", menu_button={"type": "default"}))
