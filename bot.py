import os
import asyncio
import logging
import urllib.parse
import re
import base64
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
)
from dotenv import load_dotenv

import database as db
from ai_analyzer import (
    analyze_composition,
    extract_text_from_photos,
    _translate_tags
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 1745568601))
MARIYA_ID = int(os.getenv("MARIYA_ID", 7875791813))
ADMIN_ID = int(os.getenv("ADMIN_ID", MARIYA_ID))

UNLIMITED_IDS = [MARIYA_ID, OWNER_ID, 1962088357, 5858622813]

CIRCLE_VIDEO_ID = "DQACAgIAAxkBAAIBM2rH78dR-IOML_w1nFX_c4uneVzeAALEpQAC4NRASuj2IqXHtc6tPQQ"

OFFER_URL = "https://docs.google.com/document/d/1FOaPp-S5rpJSlg2qWukQ3cyBCjxhEbfd0LCLaM4gE08/edit?usp=sharing"
CONSENT_URL = "https://docs.google.com/document/d/1OzOPwWki-_hdjY30R0A9Wp3xsdBGvrlnNPRT-IhgynA/edit?usp=sharing"

MARIYA_USERNAME = "miroslavskayaboks"

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_VERSION = "v7-2026-10-26-red-flags"
logger.info(f"🔥 Запущена версия: {BOT_VERSION}")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db.init_db()


def sanitize_input(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    text = re.sub(r'[\[\]\{\}<>]', '', text)
    text = re.sub(r'[\n\r]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_len]


def sanitize_name(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'[^\w\s\-]', '', text, flags=re.UNICODE)
    text = re.sub(r'[\d_]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:30]


def validate_inci_text(text: str) -> bool:
    if not text or text == "NO_INCI_FOUND":
        return False
    if len(text) < 25:
        return False
    text_lower = text.lower()
    bad_links = ["http", "https", "t.me", "wildberries", "ozon", "goldapple"]
    if any(link in text_lower for link in bad_links):
        return False
    delimiters = text.count(',') + text.count(';') + text.count('/') + text.count('.') + text.count('•')
    if delimiters < 2:
        return False
    return True


async def safe_delete(message: types.Message):
    try:
        await message.delete()
    except Exception:
        pass


def _escape_html(text: str) -> str:
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


async def download_photo_as_base64(photo: types.PhotoSize):
    try:
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        data = buf.read()
        return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        logger.error(f"Не удалось скачать фото: {e}")
        return None


user_locks = {}


def get_user_lock(user_id):
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]


class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            if hasattr(event, "from_user") and event.from_user:
                db.touch_user_activity(event.from_user.id)
        except Exception:
            pass
        return await handler(event, data)


dp.message.middleware(ActivityMiddleware())
dp.callback_query.middleware(ActivityMiddleware())

ANALYZABLE_STATUSES = {"bad", "hidden_threat", "conditional", "good", "neutral", "hydrophilic"}


class Form(StatesGroup):
    waiting_for_name = State()
    survey = State()
    waiting_for_medicine = State()
    waiting_for_allergy = State()
    waiting_for_composition = State()
    waiting_for_category = State()


def check_user_limits(user: dict) -> bool:
    if user["telegram_id"] in UNLIMITED_IDS:
        return True
    if db.has_active_subscription(user):
        return True
    if user.get("free_scans_used", 0) < 2:
        return True
    return False


def get_user_tariff(user: dict) -> str:
    if user["telegram_id"] in UNLIMITED_IDS:
        return "protocol"
    if db.has_active_subscription(user):
        return "protocol" if user["scanner_tariff"] == "protocol_2500" else "audit"
    return "free"


def get_mode_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🤖 Собрать уход с Романом")],
        [KeyboardButton(text="📝 Забрать уход от Марии")]
    ], resize_keyboard=True)


def get_category_4_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🫧 Смываемое (умывалки, гидрофильное масло)", callback_data="cat_wash")],
        [InlineKeyboardButton(text="🧴 Несмываемое (тоники, сыворотки, кремы, SPF)", callback_data="cat_care")],
        [InlineKeyboardButton(text="👁 Уход для век (кремы и сыворотки)", callback_data="cat_eyes")],
        [InlineKeyboardButton(text="🦵 Уход для тела (гели, лосьоны, кремы)", callback_data="cat_body")],
    ])


def get_start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="СТАРТ", callback_data="start_diagnostics")]
    ])


def get_path_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Собрать уход с Романом", callback_data="go_scanner")],
        [InlineKeyboardButton(text="👑 Хочу готовый уход от Марии", callback_data="want_mariya_box")],
        [InlineKeyboardButton(text="🤷‍♀️ У меня сейчас нет ухода", callback_data="no_care")],
    ])


def get_mariya_payment_keyboard(user_name: str, user_id: int):
    text_box = f"Привет, хочу Индивидуальный Бокс (от 9 000 ₽)! Меня зовут: {user_name}. Код анкеты: #{user_id}"
    text_q = f"Привет! Хочу проконсультироваться по поводу Персонального PDF-протокола / Бокса. Меня зовут: {user_name}. Код анкеты: #{user_id}"

    url_box = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_box)}"
    url_q = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_q)}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить PDF-протокол — 4 900 ₽", callback_data="pay_pdf_4900")],
        [InlineKeyboardButton(text="📦 Заказать Индивидуальный Бокс (от 9 000 ₽)", url=url_box)],
        [InlineKeyboardButton(text="💬 Задать вопрос Марии по PDF / Боксу", url=url_q)]
    ])


PAYWALL_TEXT = (
    "Бесплатный лимит исчерпан. По двум банкам архитектуру не выстроить, а наносить непроверенные средства на вашу кожу сейчас небезопасно.\n\n"
    "Выбирайте тариф ниже, и давайте соберем ваш идеальный уход:\n\n"
    "💳 <b>АУДИТ (1000 ₽ / 30 дней)</b>: безлимитный сканер и поиск скрытых угроз.\n"
    "💎 <b>СБОРКА ПРОТОКОЛА (2500 ₽ / 30 дней)</b>: моя интеллектуальная память, контроль конфликтов и сборка безопасной базы.\n\n"
    "Устали подбирать сами? Закажите Индивидуальный Бокс или PDF-протокол у создателя проекта, Марии:"
)

WARMUP_TEXT = (
    "🤖 Роман Андреевич: В будущем вы сможете продолжить работу со мной в двух форматах:\n\n"
    "💳 <b>АУДИТ (1000 ₽ / 30 дней)</b>\n"
    "Безлимитный сканер составов. Жесткая фильтрация от скрытых угроз, комедогенных баз и маркетинговых пустышек.\n\n"
    "💎 <b>СБОРКА ПРОТОКОЛА (2500 ₽ / 30 дней)</b>\n"
    "• Интеллектуальная память: запомню все ваши средства.\n"
    "• Архитектура: выстрою систему ухода без пробелов.\n"
    "• Матрица безопасности: исключу химические конфликты и ожоги от агрессивных активов.\n"
    "• Навигатор INCI: выдам точные названия рабочих компонентов для покупки.\n\n"
    "🎁 Но у вас осталась еще 1 бесплатная попытка! Какую категорию проверим следующей?"
)


async def send_paywall(message: types.Message):
    await message.answer(
        PAYWALL_TEXT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Аудит (30 дней) — 1000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Сборка протокола (30 дн.) — 2500 ₽", callback_data="pay_restore_90")],
            [InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="want_mariya_box")]
        ]),
        parse_mode="HTML"
    )


SURVEY_QUESTIONS = [
    {
        "text": "Вопрос 1. Что вас сейчас беспокоит больше всего?\n(Можно выбрать несколько)",
        "options": [
            ("Акне / активные воспаления", "acne"),
            ("Закрытые комедоны / бугорки", "comedones"),
            ("Сильная жирность днем", "seboprofit"),
            ("Черные точки / поры", "pores"),
            ("Сухость / стянутость", "dry"),
            ("Шелушения", "peeling"),
            ("Пигментация / постакне", "pigment"),
            ("Морщины / потеря тонуса", "age"),
            ("Покраснения / купероз", "couperose"),
            ("Ничего не беспокоит", "normal"),
        ],
        "multi": True,
    },
    {
        "text": "Вопрос 2. Как чувствует себя ваша кожа через 10 минут после умывания просто водой (до крема)?",
        "options": [
            ("Комфортно, не стягивает", "none"),
            ("Терпимо, хочется нанести крем", "none"),
            ("Сильно стягивает («пергамент»)", "barrier_broken"),
            ("Сразу блестит от жирности", "seboprofit"),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 3. Как ведет себя ваша кожа к середине дня?",
        "options": [
            ("Нормально (без сухости и блеска)", "none"),
            ("Блестит только Т-зона", "none"),
            ("Сильно блестит всё лицо", "CONDITIONAL_BLINK"),
            ("Сохнет / шелушится", "barrier_broken"),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 4. Насколько ваша кожа чувствительна к раздражителям?",
        "options": [
            ("Спокойная (редко краснеет)", "none"),
            ("Слегка чувствительная", "none"),
            ("Реактивная (горит, краснеет)", "reactive"),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 5. Есть ли у вас сейчас что-то из перечисленного?\n(Можно выбрать несколько)",
        "options": [
            ("Аптечные мази (Скинорен и др.)", "reactive"),
            ("Гормональные нарушения", "medicine"),
            ("Кожные заболевания (розацеа и др.)", "medicine"),
            ("Беременность / лактация", "pregnancy"),
            ("Ничего из перечисленного", "normal_exclusive"),
            ("Другое", "medicine_other"),
        ],
        "multi": True,
    },
    {
        "text": "Вопрос 6. Известная аллергия на компоненты?",
        "options": [
            ("Нет, аллергии нет", "no_allergy"),
            ("Да, есть", "has_allergy"),
        ],
        "multi": False,
    },
]

RETENTION_TEXTS = {
    "2h": "🤖 {user_name}, вы остановились на этапе активации. Ваша анкета сохранена. Чтобы я мог завершить химический анализ вашего ухода и выстроить протокол безопасности, необходимо завершить оформление. Я готов приступить к работе.",
    "24h": "🤖 Прошли сутки с момента паузы. Напоминаю, что каждый день использования неподходящей косметики усугубляет состояние барьера. Если в ваших баночках есть скрытые угрозы, вы наносите их прямо сейчас. Давайте остановим этот процесс. Открываем доступ?",
    "48h": "🤖 Я технически закрываю ваш незаконченный сеанс. Данные заархивированы. Если решите навести порядок в уходе, перестать тратить деньги на агрессивные средства и доверить сборку системы алгоритму — возвращайтесь.",
    "5d": "🤖 {user_name}, наверняка вы снова смотрели на косметику на маркетплейсах. Красивые обещания часто маскируют дешевую базу и пустышки. Не покупайте новые средства вслепую. Загрузите составы мне перед оплатой, и я уберегу вас от пустой траты денег.",
    "7d": "🤖 Прошла неделя. За это время вы нанесли свой привычный уход минимум 14 раз. Если мы не исключили из вашей системы жесткие ПАВы или конфликтные активы, вы значительно усилили дефицит липидов. Доступ к системе ждет активации.",
    "10d": "🤖 {user_name}, алгоритмы соцсетей продолжают продавать вам «волшебные» банки. Блогеры рекомендуют агрессивные активы, не зная вашей физиологии. Прежде чем оплатить корзину — пропустите состав через мою матрицу безопасности.",
    "14d": "🤖 {user_name}, если у вас нет времени самостоятельно сканировать каждый состав — снимите с себя эту задачу. Создатель моего алгоритма, Мария, может собрать для вас индивидуальный протокол с нуля или отправить готовый бокс.",
    "21d": "🤖 {user_name}, пока гидролипидная мантия не восстановлена, кожа находится в состоянии перманентного стресса. Кожа в стрессе увядает в разы быстрее — появляются преждевременные морщины. Нам нужно срочно выстроить защитную архитектуру.",
    "30d": "🤖 Прошел ровно месяц. 28–35 дней — это полный цикл обновления эпидермиса. Если вы устали действовать вслепую и готовы выстроить грамотную, физиологичную базу — двери лаборатории открыты.",
    "60d": "🤖 {user_name}, прошло два месяца. Изменились внешние факторы: температура, влажность. Кожа адаптировалась, и база, спасавшая вас раньше, может давать сбои. Необходимо провести аудит вашей системы и скорректировать протокол.",
    "90d": "🤖 {user_name}, 90 дней — критический срок (PAO) для многих активов. Витамин С окисляется, пептиды теряют стабильность. Наносить такую косметику небезопасно. Пришло время провести ревизию и собрать новую архитектуру ухода.",
    "180d": "🤖 Полгода назад вы инициировали запуск алгоритма. У вас есть два пути: активировать алгоритм и жестко фильтровать каждую банку, либо делегировать это Марии и получить готовый бокс. Выбор за вами.",
    "renewal_audit": "🤖 {user_name}, завтра ваш доступ к сканирующему модулю закрывается.\nНе покупайте новые средства вслепую. Одно средство с жесткими ПАВами или комедогенной базой может спровоцировать воспаление и откатить результаты назад. Продлите «Аудит», чтобы я продолжил жестко фильтровать ваши покупки.\n⬆️ Или перейдите на следующий уровень: выберите «Сборку протокола». Я включу интеллектуальную память, запомню все ваши средства и выстрою из них единую безопасную систему.",
    "renewal_protocol": "🤖 {user_name}, наш месяц работы подходит к концу. За 28–35 дней эпидермис проходит полный цикл обновления. Завтра ваш доступ к алгоритму закрывается.\nДавайте подведем итоги. Отметьте ниже этапы ухода, которые мы с вами уже успели закрыть безопасными, одобренными средствами:",
    "reactivation_30d": "🤖 {user_name}, срок вашего доступа истек. Сканирующий модуль отключен. Ваша текущая архитектура ухода сохранена и переведена в архив. Когда потребуется выбрать новые средства — алгоритм будет готов распаковать архив и проверить составы.",
    "reactivation_44d": "🤖 {user_name}, если самостоятельный подбор отнимает слишком много времени — снимите с себя эту задачу. Создатель алгоритма, Мария, собирает Индивидуальные Боксы вручную. Вы получаете готовую систему без необходимости вникать в химию.",
    "reactivation_75d": "🤖 {user_name}, согласно статистике расхода, за 1,5–2 месяца базовая сыворотка или крем подходят к концу. Если вы планируете покупку новых средств, помните: не покупать вслепую. Доступ к лаборатории можно возобновить в любую минуту."
}

RETENTION_DELAYS = {
    "2h": 7200, "24h": 86400, "48h": 172800, "5d": 432000, "7d": 604800,
    "10d": 864000, "14d": 1209600, "21d": 1814400, "30d": 2592000,
    "60d": 5184000, "90d": 7776000, "180d": 15552000,
    "renewal_audit": 0, "renewal_protocol": 0,
    "reactivation_30d": 0, "reactivation_44d": 0, "reactivation_75d": 0
}


def build_stage_keyboard(telegram_id: int):
    stages = db.get_user_stages(telegram_id)
    labels = {
        "cleansing": "Мягкое очищение",
        "toning": "Тонизация",
        "cream": "Базовый крем",
        "spf": "Защита SPF"
    }
    buttons = []
    for key, label in labels.items():
        status = "✅" if stages.get(key, False) else "❌"
        buttons.append([InlineKeyboardButton(text=f"{status} {label}", callback_data=f"toggle_stage:{key}")])
    buttons.append([InlineKeyboardButton(text="🏁 Завершить аудит системы", callback_data="finish_audit")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_retention_notification(telegram_id: int, notif_type: str):
    text_template = RETENTION_TEXTS.get(notif_type)
    if not text_template:
        return

    user_name = db.get_user_name(telegram_id) or "Клиент"
    text = text_template.format(user_name=user_name)

    kb = None

    if notif_type == "14d":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="want_mariya_box")]
        ])
    elif notif_type == "180d":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Аудит (30 дн.) — 1000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Сборка протокола (30 дн.) — 2500 ₽", callback_data="pay_restore_90")],
            [InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="want_mariya_box")]
        ])
    elif notif_type == "renewal_audit":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Продлить Аудит (30 дн.) — 1000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Перейти на Сборку протокола — 2500 ₽", callback_data="pay_restore_90")]
        ])
    elif notif_type == "renewal_protocol":
        kb = build_stage_keyboard(telegram_id)
    elif notif_type == "reactivation_44d":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="want_mariya_box")]
        ])
    elif notif_type in ["reactivation_30d", "reactivation_75d"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Продлить Аудит — 1000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Продлить Сборку — 2500 ₽", callback_data="pay_restore_90")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Аудит (30 дн.) — 1000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Сборка протокола (30 дн.) — 2500 ₽", callback_data="pay_restore_90")]
        ])

    try:
        await bot.send_message(telegram_id, text, reply_markup=kb, parse_mode="HTML")
        logger.info(f"📨 Уведомление '{notif_type}' → telegram_id={telegram_id}")
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление {notif_type} юзеру {telegram_id}: {e}")


async def retention_worker():
    logger.info("⏰ Retention-воркер запущен")
    while True:
        try:
            due = db.get_due_notifications()
            for n in due:
                user_id = n["telegram_id"]
                notif_type = n["type"]
                delay = RETENTION_DELAYS.get(notif_type, 0)

                if notif_type.startswith("reactivation_"):
                    user = db.get_user(user_id)
                    if user and (user.get("maria_order_status") == "completed_vip" or db.has_active_subscription(user)):
                        db.mark_notification_sent(n["id"])
                        continue

                last_activity = db.get_last_activity(user_id)

                if notif_type not in ["renewal_audit", "renewal_protocol", "reactivation_30d", "reactivation_44d", "reactivation_75d"] and last_activity and delay > 0:
                    elapsed = (datetime.now() - last_activity).total_seconds()
                    if elapsed < delay:
                        new_time = last_activity + timedelta(seconds=delay)
                        db.reschedule_notification(n["id"], new_time)
                        continue

                await _send_retention_notification(user_id, notif_type)
                db.mark_notification_sent(n["id"])
        except Exception as e:
            logger.error(f"Ошибка в retention-воркере: {e}")
        await asyncio.sleep(30)


async def _resolve_user_name(message: types.Message, state: FSMContext, user: dict) -> str:
    data = await state.get_data()
    name = data.get("user_name")

    if not name:
        try:
            name = db.get_user_name(message.from_user.id)
        except Exception:
            name = None

    if not name:
        name = message.from_user.first_name or "Клиент"

    if data.get("user_name") != name:
        await state.update_data(user_name=name)

    return name


async def send_client_card_to_admin(client_user: dict, log_lead: bool = True) -> bool:
    user_name = _escape_html(client_user.get("user_name") or "Клиент")
    username = client_user.get("username") or "нет_username"
    username_html = f"@{_escape_html(username)}" if client_user.get("username") else "без username"
    telegram_id = client_user["telegram_id"]
    tags = client_user.get("user_tags", [])
    medical_note = _escape_html(client_user.get("medical_note") or "Нет")

    try:
        tags_str = _escape_html(_translate_tags(tags)) if tags else "нет"
    except Exception:
        tags_str = "нет"

    text = (
        "🔔 <b>Клиент открыл раздел готового ухода Марии (Шаг 6)</b>\n"
        f"👤 Имя: {user_name} ({username_html}, код: <code>#{telegram_id}</code>)\n"
        f"🏷 Теги кожи: {tags_str}\n"
        f"💊 Медицина: {medical_note}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏸ Пауза пушей на 72 часа (Идет диалог)",
            callback_data=f"admin_pause:{telegram_id}"
        )],
        [InlineKeyboardButton(
            text="✅ Купила Бокс / PDF (Отключить пуши навсегда)",
            callback_data=f"admin_vip:{telegram_id}"
        )]
    ])

    if log_lead:
        try:
            db.save_lead(
                telegram_id=telegram_id,
                username=client_user.get("username") or "",
                tags=tags,
                photos=[]
            )
        except Exception as e:
            logger.warning(f"Не удалось сохранить лид {telegram_id}: {e}")

    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="HTML")
        logger.info(f"📇 Карточка лида #{telegram_id} отправлена админу {ADMIN_ID}")
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить карточку админу: {e}")
        return False


async def handle_scanner_entry(message: types.Message, state: FSMContext, user: dict):
    telegram_id = user["telegram_id"]
    has_sub = db.has_active_subscription(user)

    if telegram_id in UNLIMITED_IDS or has_sub:
        await message.answer(
            "Какое уходовое средство вы хотите проверить? Выберите категорию 👇",
            reply_markup=get_category_4_keyboard()
        )
        await state.set_state(Form.waiting_for_composition)
        return

    free_used = user.get("free_scans_used", 0)

    if free_used == 0:
        await message.answer(
            "Отличный план! Я даю вам 2 бесплатные проверки, чтобы вы протестировали меня в деле. "
            "Прямо сейчас сфотографируйте состав на вашей баночке (или сделайте скриншот состава "
            "на маркетплейсе) либо просто скопируйте его текстом. Но для начала выберите, "
            "к какой категории относится средство 👇",
            reply_markup=get_category_4_keyboard()
        )
        await state.set_state(Form.waiting_for_composition)

    elif free_used == 1:
        await message.answer(
            "У вас осталась 1 бесплатная проверка. Выберите категорию средства 👇",
            reply_markup=get_category_4_keyboard()
        )
        await state.set_state(Form.waiting_for_composition)

    else:
        db.schedule_retention_notifications(telegram_id)
        await send_paywall(message)


@dp.message(F.text == "/profile")
async def cmd_profile(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)
    user_name = db.get_user_name(telegram_id) or "Клиент"

    tags_str = _translate_tags(user.get("user_tags", []))

    if not db.has_active_subscription(user):
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Статус доступа: Неактивен (Использовано бесплатных проверок: {user.get('free_scans_used', 0)} из 2).\n"
            f"Выберите тариф в меню /tariffs, чтобы включить сканер составов."
        )
        await message.answer(text)

    elif user.get("scanner_tariff") == "audit_1000":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        text = (
            f"👤 Имя: {user_name}\n"
            f"Тариф: 💳 Аудит (Безлимитный сканер)\n"
            f"Активен до: {exp_date}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить новое средство", callback_data="btn_check_new")]
        ])
        await message.answer(text, reply_markup=kb)

    elif user.get("scanner_tariff") == "protocol_2500":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        count = db.count_approved_products(telegram_id)
        text = (
            f"👤 Имя: {user_name}\n"
            f"Тариф: 💎 Сборка протокола\n"
            f"Активен до: {exp_date}\n"
            f"Одобренных средств в вашей схеме: {count} шт."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить новое средство", callback_data="btn_check_new")],
            [InlineKeyboardButton(text="🗂 Моя собранная база (Просмотр / Удаление)", callback_data="btn_my_base")]
        ])
        await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "btn_check_new")
async def handle_btn_check_new(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    user = db.get_or_create_user(callback.from_user.id)
    await handle_scanner_entry(callback.message, state, user)
    await callback.answer()


async def show_user_base(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    products = db.get_approved_products(telegram_id)

    if not products:
        text = "🗂 Ваша база пока пуста."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="back_to_profile")]])
    else:
        text = "🗂 <b>Ваша собранная база средств:</b>\n\n"
        kb_buttons = []
        for i, p in enumerate(products[:15], 1):
            status_str = "🟢" if p["status"] == "good" else ("🔵" if p["status"] == "hydrophilic" else "🟡")
            comp_short = p["composition"][:30] + "..." if len(p["composition"]) > 30 else p["composition"]
            if comp_short.startswith("[ФОТО"):
                comp_short = "Фото состава"
            text += f"{i}. {status_str} {p['stage']} ({comp_short})\n"
            kb_buttons.append([InlineKeyboardButton(text=f"🗑 Удалить #{i}", callback_data=f"del_base_{p['id']}")])
        if len(products) > 15:
            text += f"\n... и еще {len(products) - 15} средств."
        kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="back_to_profile")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "btn_my_base")
async def handle_btn_my_base(callback: types.CallbackQuery):
    await show_user_base(callback)
    await callback.answer()


@dp.callback_query(F.data.startswith("del_base_"))
async def handle_del_base(callback: types.CallbackQuery):
    record_id = int(callback.data.split("_")[-1])
    db.delete_approved_product(record_id)
    await callback.answer("Средство удалено.", show_alert=False)
    await show_user_base(callback)


@dp.callback_query(F.data == "back_to_profile")
async def handle_back_to_profile(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    await cmd_profile(callback.message, state)
    await callback.answer()


@dp.message(F.text == "/tariffs")
async def cmd_tariffs(message: types.Message):
    text = (
        "🤖 Роман Андреевич: Выберите формат работы с алгоритмом или закажите готовую систему у Марии:\n\n"
        "💳 <b>АУДИТ (1000 ₽ / 30 дней)</b>: безлимитный сканер составов и защита от скрытых угроз.\n\n"
        "💎 <b>СБОРКА ПРОТОКОЛА (2500 ₽ / 30 дней)</b>: интеллектуальная память бота, выстраивание системы ухода без пробелов и контроль конфликтов.\n\n"
        "📦 <b>ГОТОВЫЙ УХОД</b>: Индивидуальный Бокс или PDF-протокол от создателя проекта."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Аудит (30 дн.) — 1000 ₽", callback_data="pay_audit_30")],
        [InlineKeyboardButton(text="💎 Сборка протокола (30 дн.) — 2500 ₽", callback_data="pay_restore_90")],
        [InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="want_mariya_box")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    username = message.from_user.username
    user = db.get_or_create_user(telegram_id, username)

    # Карантин 30 дней (только для НЕ-unlimited)
    if telegram_id not in UNLIMITED_IDS and user.get("survey_done", 0) == 1:
        completed_at = user.get("survey_completed_at")
        if completed_at:
            try:
                completed_date = datetime.fromisoformat(completed_at)
                days_passed = (datetime.now() - completed_date).days

                if days_passed < 30:
                    days_left = 30 - days_passed
                    await message.answer(
                        f"Ваша диагностическая карта зафиксирована. Сейчас мы работаем с текущим "
                        f"состоянием вашей кожи, ей нужно время на восстановление. ⏳\n\n"
                        f"Обновить профиль и пройти диагностику заново можно будет через "
                        f"*{days_left}* дней.",
                        parse_mode="Markdown"
                    )
                    return
            except Exception as e:
                logger.error(f"Ошибка карантина: {e}")

    try:
        await message.answer_video_note(video_note=CIRCLE_VIDEO_ID)
    except Exception as e:
        logger.error(f"Не удалось отправить кружок: {e}")

    # 👑 UNLIMITED + анкета уже пройдена — даём выбор: перепройти или сканировать
    if telegram_id in UNLIMITED_IDS and user.get("survey_done", 0) == 1:
        await state.clear()
        await message.answer(
            "👑 Режим безлимита. Вы можете:\n\n"
            "🔄 Перепройти диагностику заново — нажмите кнопку ниже\n"
            "🔍 Или сразу выбрать категорию средства для проверки",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Перепройти анкету заново", callback_data="start_diagnostics")],
                *get_category_4_keyboard().inline_keyboard,
            ])
        )
        return

    # Новая анкета (ни анкета не пройдена, ни unlimited-ветка не сработала)
    if user.get("survey_done", 0) == 0:
        await message.answer(
            f"Жми на кнопку ниже, чтобы Роман Андреевич запустил алгоритм диагностики 👇\n\n"
            f"Нажимая кнопку, вы принимаете условия <a href='{OFFER_URL}'>Публичной оферты</a> "
            f"и даете <a href='{CONSENT_URL}'>Согласие на обработку персональных данных</a>.",
            reply_markup=get_start_keyboard(),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        return

    # Обычные пользователи, анкета пройдена, карантин истёк
    await handle_scanner_entry(message, state, user)
    await message.answer(
        "Также вы можете забрать готовый уход от Марии, нажав на кнопку внизу.",
        reply_markup=get_mode_keyboard()
    )


@dp.message(F.text.lower() == "бокс")
async def code_box(message: types.Message, state: FSMContext):
    await cmd_start(message, state)


@dp.message(F.text == "🤖 Собрать уход с Романом")
async def gather_care_start(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)

    if user.get("survey_done", 0) == 0:
        await message.answer(
            "Для того чтобы я мог качественно проанализировать ваш уход, мне нужно узнать вашу кожу. "
            "Пожалуйста, пройдите короткую диагностику 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Пройти анкету", callback_data="start_diagnostics")]
            ])
        )
        return

    await handle_scanner_entry(message, state, user)


MARIA_TEXT = (
    "📄 <b>Персональный PDF-протокол (4 900 ₽)</b> — уход в формате подробного PDF-руководства. "
    "Мария подберет косметику строго под ваш бюджет, даст прямые ссылки на покупку каждого средства "
    "и распишет понятную инструкцию (что и как наносить утром и вечером). Вы покупаете всё сами по готовому списку. "
    "Этот файл останется с вами как надежная база: Мария добавит позиции на перспективу, чтобы вы всегда знали, "
    "какими средствами дополнить и усилить свой протокол.\n\n"
    "📦 <b>Индивидуальный Бокс (от 9 000 ₽)</b> — премиальный формат ухода «под ключ». "
    "Вы приобретаете не просто набор косметики, а готовую систему — ваш личный работающий протокол. "
    "Вы получаете эстетичную коробку с полноразмерными средствами, подобранными строго под вашу кожу, "
    "и подробную инструкцию. В эту стоимость уже включено ведение от Марии: она будет на связи, "
    "чтобы контролировать динамику и довести вас до результата. Вам не нужно ходить по магазинам — "
    "забираете посылку и начинаете свой путь к здоровой коже по протоколу."
)


@dp.callback_query(F.data.in_(["want_mariya_box", "no_care"]))
async def protocol_branch(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    telegram_id = callback.from_user.id
    user = db.get_or_create_user(telegram_id)
    user_name = await _resolve_user_name(callback.message, state, user)

    await send_client_card_to_admin(user)

    if callback.data == "want_mariya_box":
        intro = "Прекрасный выбор. Вы можете заказать у Марии:\n\n"
    else:
        intro = "Прекрасный выбор. Чистый лист — это идеальная возможность сразу выстроить грамотный фундамент. Вы можете заказать у Марии:\n\n"

    full_text = (
        intro + MARIA_TEXT +
        "\n\nВыберите нужный формат ниже: PDF-протокол можно оплатить сразу, "
        "а для заказа Бокса или консультации перейдите в личный диалог с Марией 👇"
    )
    await callback.message.answer(
        full_text,
        reply_markup=get_mariya_payment_keyboard(user_name, telegram_id),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(F.text == "📝 Забрать уход от Марии")
async def offer_mariya_from_menu(message: types.Message, state: FSMContext):
    user = db.get_or_create_user(message.from_user.id)
    user_name = await _resolve_user_name(message, state, user)

    await send_client_card_to_admin(user)

    full_text = (
        "Прекрасный выбор. Вы можете заказать у Марии:\n\n" + MARIA_TEXT +
        "\n\nВыберите нужный формат ниже: PDF-протокол можно оплатить сразу, "
        "а для заказа Бокса или консультации перейдите в личный диалог с Марией 👇"
    )
    await message.answer(
        full_text,
        reply_markup=get_mariya_payment_keyboard(user_name, message.from_user.id),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "pay_pdf_4900")
async def pay_pdf_4900(callback: types.CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    user_name = db.get_user_name(telegram_id) or "Клиент"

    db.cancel_retention_notifications(telegram_id)
    db.set_maria_order_status(telegram_id, "completed_vip")

    try:
        await bot.send_message(ADMIN_ID, f"🎉 ОПЛАЧЕН PDF-ПРОТОКОЛ (4 900 ₽)\nКлиент: {user_name}\nID: {telegram_id}")
    except Exception:
        pass

    await callback.message.answer(
        "Оплата успешно принята! Мария уже получила данные вашей диагностики. "
        "Нажмите на кнопку ниже, чтобы перейти в личный диалог с Марией и согласовать комфортный бюджет на покупку средств 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Перейти к Марии", url=f"https://t.me/{MARIYA_USERNAME}")]
        ])
    )
    await callback.answer()


@dp.callback_query(F.data == "pay_audit_30")
async def pay_audit_30(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id

    payment_id = f"test_{telegram_id}_{datetime.now().timestamp()}"
    db.save_payment(payment_id, telegram_id, 1000, "audit_1000")
    db.update_user_subscription(telegram_id, "audit_1000", 30)

    db.cancel_retention_notifications(telegram_id)

    now = datetime.now()
    db.schedule_single_notification(telegram_id, "renewal_audit", now + timedelta(days=29))
    db.schedule_single_notification(telegram_id, "reactivation_30d", now + timedelta(days=30))
    db.schedule_single_notification(telegram_id, "reactivation_44d", now + timedelta(days=44))
    db.schedule_single_notification(telegram_id, "reactivation_75d", now + timedelta(days=75))

    await callback.message.answer(
        "Доступ к лаборатории открыт на 30 дней! Присылайте составы текстом или фотографией этикетки. "
        "Выберите категорию средства для проверки 👇",
        reply_markup=get_category_4_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "pay_restore_90")
async def pay_restore_90(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id

    payment_id = f"test_{telegram_id}_{datetime.now().timestamp()}"
    db.save_payment(payment_id, telegram_id, 2500, "protocol_2500")
    db.update_user_subscription(telegram_id, "protocol_2500", 30)

    db.cancel_retention_notifications(telegram_id)

    now = datetime.now()
    db.schedule_single_notification(telegram_id, "renewal_protocol", now + timedelta(days=29))
    db.schedule_single_notification(telegram_id, "reactivation_30d", now + timedelta(days=30))
    db.schedule_single_notification(telegram_id, "reactivation_44d", now + timedelta(days=44))
    db.schedule_single_notification(telegram_id, "reactivation_75d", now + timedelta(days=75))

    await callback.message.answer(
        "Доступ к лаборатории открыт на 30 дней! Присылайте составы текстом или фотографией этикетки. "
        "Выберите категорию средства для проверки 👇",
        reply_markup=get_category_4_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "start_diagnostics")
async def start_diagnostics(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    telegram_id = callback.from_user.id
    db.clear_user_tags(telegram_id)
    await state.clear()
    await state.set_state(Form.waiting_for_name)
    await state.update_data(step_contrib={})
    await callback.message.answer(
        "Для начала напишите, как к вам обращаться:",
        reply_markup=get_mode_keyboard()
    )
    await callback.answer()


@dp.message(StateFilter(Form.waiting_for_name))
async def process_name(message: types.Message, state: FSMContext):
    name = sanitize_name(message.text or "")

    if not name:
        await message.answer(
            "Пожалуйста, введите имя буквами (без цифр и символов):",
            reply_markup=get_mode_keyboard()
        )
        return

    await state.update_data(user_name=name)
    try:
        db.update_user_name(message.from_user.id, name)
    except Exception:
        pass

    await state.set_state(Form.survey)
    await state.update_data(survey_step=0, tags=[], q2_answer=None, step_contrib={})

    msg = await message.answer(
        "Отлично! А теперь ответьте на 6 быстрых вопросов, чтобы я понял, "
        "с чем мы работаем. 👇"
    )
    await asyncio.sleep(1.5)
    try:
        await msg.delete()
    except Exception:
        pass

    await send_question(message, state)


async def send_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    step = data.get("survey_step", 0)
    tags = data.get("tags", [])

    if step >= len(SURVEY_QUESTIONS):
        await finish_survey(message, state)
        return

    q = SURVEY_QUESTIONS[step]
    buttons = []

    for opt_text, tag in q["options"]:
        if q["multi"]:
            is_selected = tag in tags if tag not in ["none", "medicine_other", "normal", "normal_exclusive"] else False
            prefix = "✅" if is_selected else "⬜️"
            buttons.append([InlineKeyboardButton(text=f"{prefix} {opt_text}", callback_data=f"q_{step}_{tag or 'none'}")])
        else:
            buttons.append([InlineKeyboardButton(text=opt_text, callback_data=f"q_{step}_{tag or 'none'}")])

    if q["multi"]:
        buttons.append([InlineKeyboardButton(text="Далее ➔", callback_data=f"q_{step}_done")])

    if step > 0:
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"back_{step}")])

    await message.answer(
        q["text"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(StateFilter(Form.survey), F.data.regexp(r"^back_\d+$"))
async def process_back(callback: types.CallbackQuery, state: FSMContext):
    step = int(callback.data.split("_")[1])
    new_step = step - 1

    data = await state.get_data()
    tags = data.get("tags", [])
    step_contrib = data.get("step_contrib", {})

    for t in step_contrib.get(str(step), []):
        if t in tags:
            tags.remove(t)
    step_contrib.pop(str(step), None)

    await state.update_data(survey_step=new_step, tags=tags, step_contrib=step_contrib)
    await callback.message.delete()
    await send_question(callback.message, state)
    await callback.answer()


@dp.callback_query(StateFilter(Form.survey), F.data.regexp(r"^q_\d+_.+$"))
async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_", 2)
    step = int(parts[1])
    tag = parts[2]

    data = await state.get_data()
    tags = data.get("tags", [])
    q2_answer = data.get("q2_answer")

    if tag == "done":
        if step == 0:
            has_tag = any(t in tags for t in ["acne", "comedones", "seboprofit", "pores", "dry", "peeling", "pigment", "age", "couperose", "normal"])
            if not has_tag:
                await callback.answer("Выберите хотя бы один вариант!", show_alert=True)
                return
        elif step == 4:
            has_tag = any(t in tags for t in ["reactive", "medicine", "pregnancy", "normal_exclusive", "medicine_other"])
            if not has_tag:
                await callback.answer("Выберите хотя бы один вариант!", show_alert=True)
                return

        await state.update_data(survey_step=step + 1)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()
        return

    if step == 4 and tag == "medicine_other":
        await state.set_state(Form.waiting_for_medicine)
        await callback.message.delete()
        await callback.message.answer("Напишите ваш вариант (кратко одним сообщением):")
        await callback.answer()
        return

    if step == 5:
        if tag == "no_allergy":
            await state.update_data(survey_step=6)
            await callback.message.delete()
            await finish_survey(callback.message, state)
            await callback.answer()
            return
        if tag == "has_allergy":
            await state.update_data(survey_step=6)
            await state.set_state(Form.waiting_for_allergy)
            await callback.message.delete()
            await callback.message.answer("Напишите название компонента, на который у вас аллергия:")
            await callback.answer()
            return

    if step == 1:
        await state.update_data(q2_answer=tag)

    if tag in ["normal", "normal_exclusive"]:
        step_tags = [t for _, t in SURVEY_QUESTIONS[step]["options"] if t not in ["none", "medicine_other", "normal", "normal_exclusive"]]
        tags = [t for t in tags if t not in step_tags]
        if tag not in tags:
            tags.append(tag)

        # Автопереход на следующий вопрос
        step_contrib = data.get("step_contrib", {})
        step_contrib[str(step)] = [tag]

        await state.update_data(tags=tags, survey_step=step + 1, step_contrib=step_contrib)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()
        return

    if tag not in ["none", "medicine_other"]:
        if "normal" in tags and step == 0:
            tags.remove("normal")
        if "normal_exclusive" in tags and step == 4:
            tags.remove("normal_exclusive")

    q = SURVEY_QUESTIONS[step]
    if q["multi"]:
        if tag in tags:
            tags.remove(tag)
        else:
            if tag != "none":
                tags.append(tag)
        await state.update_data(tags=tags)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()
        return

    if not q["multi"]:
        added = []

        if step == 2 and tag == "CONDITIONAL_BLINK":
            if q2_answer == "barrier_broken":
                tags = [t for t in tags if t != "CONDITIONAL_BLINK"]
                tags.append("fat_compensatory")
                added.append("fat_compensatory")
            else:
                tags = [t for t in tags if t != "CONDITIONAL_BLINK"]
                tags.append("seboprofit")
                added.append("seboprofit")
        elif tag != "none":
            if tag not in tags:
                tags.append(tag)
                added.append(tag)

        step_contrib = data.get("step_contrib", {})
        step_contrib[str(step)] = added

        await state.update_data(tags=tags, survey_step=step + 1, step_contrib=step_contrib)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()


@dp.message(StateFilter(Form.waiting_for_medicine))
async def process_medicine(message: types.Message, state: FSMContext):
    text = sanitize_input(message.text or "", max_len=100)

    if not text:
        await message.answer("Пожалуйста, напишите ваш вариант текстом:")
        return

    data = await state.get_data()
    tags = data.get("tags", [])
    if "medicine" not in tags:
        tags.append("medicine")

    other = data.get("medicine_other", [])
    other.append(text)

    await state.update_data(tags=tags, medicine_other=other, survey_step=5)
    await state.set_state(Form.survey)
    await message.answer(f"Записано: «{text}».")
    await send_question(message, state)


@dp.message(StateFilter(Form.waiting_for_allergy))
async def process_allergy(message: types.Message, state: FSMContext):
    allergy_text = sanitize_input(message.text or "", max_len=100)

    if not allergy_text:
        await message.answer("Пожалуйста, напишите название аллергена текстом:")
        return

    data = await state.get_data()
    tags = data.get("tags", [])
    tags.append(f"allergy_{allergy_text}")

    await state.update_data(tags=tags)
    await state.set_state(Form.survey)
    await message.answer(f"Записано: аллергия на «{allergy_text}». Учту это при анализе.")
    await finish_survey(message, state)


async def finish_survey(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tags = data.get("tags", [])
    medical_note = None
    if "medicine_other" in data:
        medical_note = ", ".join(data["medicine_other"])

    telegram_id = message.chat.id
    user_name = data.get("user_name") or db.get_user_name(telegram_id) or "Клиент"

    if not tags:
        tags.append("normal")

    db.update_user_tags(telegram_id, tags, medical_note)
    if telegram_id not in UNLIMITED_IDS:
        db.mark_survey_completed(telegram_id)

    db.schedule_retention_notifications(telegram_id)

    # =========================================================
    # СООБЩЕНИЕ 1
    # =========================================================
    await message.answer("📊 Диагностика завершена.")

    # =========================================================
    # СООБЩЕНИЕ 2
    # Начинается СТРОГО с f"🤖 {user_name},\n\n"
    # Абзацы добавляются по условиям из tags, между абзацами \n\n
    # =========================================================
    diagnosis_text = f"🤖 {user_name},\n\n"

    # === БЛОК 1: КРАСНЫЕ ФЛАГИ ===
    if "medicine" in tags:
        diagnosis_text += (
            "❗️ Вижу по анамнезу дерматологический диагноз. Напоминаю: я химик-технолог, "
            "а не врач. Моя компетенция — анализ химических формул и выстраивание безопасного "
            "косметического фундамента, который не спровоцирует клиническое обострение.\n\n"
        )

    if "pregnancy" in tags:
        diagnosis_text += (
            "🤰 Фиксирую статус беременности/лактации. В мой алгоритм внесен строгий запрет "
            "на тератогенные активы (ретиноиды) и салициловую кислоту. Мы соберем максимально "
            "безопасный протокол.\n\n"
        )

    allergy_names = [t[len("allergy_"):] for t in tags if t.startswith("allergy_")]
    if allergy_names:
        allergy_text = ", ".join(allergy_names)
        diagnosis_text += (
            f"🛑 В базу данных внесен строгий стоп-лист на ваш аллерген: {allergy_text}. "
            "Любой продукт с его содержанием будет автоматически блокироваться матрицей.\n\n"
        )
    # === END БЛОК 1 ===

    # === БЛОК 2: БАЗИС (СТРОГО ОДНО СОВПАДЕНИЕ) ===
    if "fat_compensatory" in tags:
        diagnosis_text += (
            "💧 Фиксируем критическое нарушение гидролипидной мантии. Сухость провоцирует "
            "компенсаторную гиперсекрецию себума — кожа пытается восстановить защитный барьер "
            "через усиленную работу сальных желез. Наша стратегия — восстановить барьер "
            "физиологичными эмолентами, нормализовать трансэпидермальную потерю влаги и "
            "разорвать этот замкнутый круг.\n\n"
        )
    elif any(t in tags for t in ["barrier_broken", "dry", "peeling"]):
        diagnosis_text += (
            "💧 Фиксируем дефицит собственных липидов и уязвимость барьера. Эпидермис теряет "
            "влагу и беззащитен перед внешними триггерами. Наша первоочередная стратегия — "
            "нормализовать трансэпидермальную потерю влаги и восстановить липидный каркас, "
            "категорически исключив агрессивные ПАВы.\n\n"
        )
    elif any(t in tags for t in ["problem", "seboprofit", "pores"]):
        diagnosis_text += (
            "🌿 Фиксируем профицит себума и склонность к гиперкератозу. Наша цель — наладить "
            "качественный отток кожного сала, предотвратить окисление комедонов и снизить "
            "воспалительный потенциал, не допуская разрушения защитного барьера жестким умыванием.\n\n"
        )
    elif "normal" in tags:
        diagnosis_text += (
            "⚖️ Фиксируем нормальное состояние эпидермиса. Гидролипидная мантия функционирует "
            "стабильно, барьерные структуры не нарушены, компенсаторные реакции отсутствуют. "
            "Наша стратегия — поддерживать физиологичный баланс и предотвращать внешний стресс "
            "через грамотный подбор химических составов.\n\n"
        )
    # === END БЛОК 2 ===

    # === БЛОК 3: СОСУДЫ ===
    if any(t in tags for t in ["reactive", "couperose"]):
        diagnosis_text += (
            "🩸 Сосудистая сетка и реактивность указывают на хрупкость капилляров и локальный "
            "иммунодефицит кожи. Мы исключим агрессивные триггеры, жесткие скрабы и "
            "сфокусируемся на успокаивающих компонентах.\n\n"
        )
    # === END БЛОК 3 ===

    # === БЛОК 4: ЦЕЛЕВЫЕ ЗАДАЧИ (с проверкой беременности) ===
    if any(t in tags for t in ["age", "pigment"]):
        if "pregnancy" in tags:
            diagnosis_text += (
                "🎯 Также я зафиксировал ваш запрос на работу с тоном и клеточной стимуляцией. "
                "Учитывая ваше положение, мы выстроим рутину исключительно на безопасных активах "
                "(витамин С, пептиды, мягкие энзимы), которые разрешены в этот период и не имеют "
                "системного действия на организм.\n\n"
            )
        else:
            diagnosis_text += (
                "🎯 Также я зафиксировал ваш запрос на работу с тоном и клеточной стимуляцией. "
                "Мы выстроим рутину так, чтобы безопасно внедрить стимулирующие активы "
                "(кислоты, витамин С, ретиноиды), когда ваш роговой слой будет к этому полностью готов.\n\n"
            )
    # === END БЛОК 4 ===
    
    await message.answer(diagnosis_text.strip())

    # =========================================================
    # Переход к выбору пути (НЕ диагноз — оставляем как было)
    # =========================================================
    await message.answer(
        "Теперь нам нужно выстроить безопасный физиологичный уход. "
        "Вы можете собрать его самостоятельно, проверяя каждое средство вместе со мной, Романом Андреевичем, "
        "или сразу забрать готовую схему от Марии. Если у вас сейчас вообще нет ухода — мы выстроим его с чистого листа.",
        reply_markup=get_path_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)


@dp.callback_query(F.data == "go_scanner")
async def go_scanner(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    user = db.get_or_create_user(callback.from_user.id)
    await handle_scanner_entry(callback.message, state, user)
    await callback.answer()


@dp.callback_query(F.data == "back_to_scanner")
async def back_to_scanner(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    user = db.get_or_create_user(callback.from_user.id)
    await handle_scanner_entry(callback.message, state, user)
    await callback.answer()


async def perform_analysis(message: types.Message, state: FSMContext, user: dict, composition: str, category: str):
    telegram_id = user["telegram_id"]
    user_name = await _resolve_user_name(message, state, user)

    lock = get_user_lock(telegram_id)
    if lock.locked():
        await message.answer("⏳ Подождите пару секунд, я уже анализирую предыдущий состав...")
        return

    async with lock:
        user = db.get_or_create_user(telegram_id)
        if not check_user_limits(user):
            db.schedule_retention_notifications(telegram_id)
            await send_paywall(message)
            return

        tariff = get_user_tariff(user)

        if telegram_id not in UNLIMITED_IDS:
            limit_status = db.check_rate_limit(telegram_id)
            if limit_status:
                if limit_status == "day":
                    await message.answer("Вы проверили уже 40 средств за сегодня — давайте дадим коже и алгоритму передышку и продолжим аудит завтра! 🌿")
                else:
                    await message.answer("⏳ Слишком много запросов. Пожалуйста, подождите немного.")
                return

        db.log_scan(telegram_id)
        tags = user.get("user_tags", [])

        approved_routine = ""
        if tariff == "protocol":
            products = db.get_approved_products(telegram_id)
            if products:
                lines = []
                for p in products:
                    lines.append(f"Этап: {p['stage']} | Статус: {p['status']} | Активы: {p['key_actives']}")
                approved_routine = "\n".join(lines)

        loading_msg = await message.answer("🔬 Анализирую состав...")

        try:
            result = await analyze_composition(composition, tags, user_name, category, tariff, approved_routine)
            clean_text = result["verdict"]
            stage = result["stage"]
            actives = result["key_actives"]
            status = result["status"]

            if status not in ANALYZABLE_STATUSES:
                await loading_msg.edit_text(
                    "⚠️ Модель не смогла выдать вердикт по этому составу. Попробуйте ещё раз — лимит не списан."
                )
                return

            record_id = None
            if category in ['wash', 'care', 'eyes'] and status in ['good', 'hydrophilic', 'conditional']:
                record_id = db.save_approved_product(telegram_id, composition, status, category, stage, actives)

            db.save_analysis(telegram_id, composition, clean_text, status)

            if status in ["bad", "hidden_threat"]:
                db.increment_bad_bottles(telegram_id)
            else:
                db.reset_bad_bottles(telegram_id)

            is_unlimited = (telegram_id in UNLIMITED_IDS)
            has_sub = db.has_active_subscription(user)
            is_protocol = user.get("scanner_tariff") == "protocol_2500" and has_sub

            if is_unlimited:
                await loading_msg.edit_text(clean_text)
                await message.answer("👑 Режим безлимита: проверок не ограничено.")
            elif not has_sub:
                db.increment_free_scans_used(telegram_id)
                updated_user = db.get_or_create_user(telegram_id)
                remaining = 2 - updated_user.get("free_scans_used", 0)

                await loading_msg.edit_text(clean_text)

                if remaining == 1:
                    await message.answer(WARMUP_TEXT, reply_markup=get_category_4_keyboard(), parse_mode="HTML")
                    await state.update_data(category=None, photo_urls=[])
                    return
                elif remaining <= 0:
                    db.schedule_retention_notifications(telegram_id)
                    await send_paywall(message)
                    await state.update_data(category=None, photo_urls=[])
                    return
            else:
                await loading_msg.edit_text(clean_text)

            rows = []
            if is_protocol and record_id:
                rows.append([InlineKeyboardButton(
                    text="🗑 Я только смотрю в магазине (Не добавлять в мой уход)",
                    callback_data=f"remove_item:{record_id}"
                )])
            rows.extend(get_category_4_keyboard().inline_keyboard)
            kb = InlineKeyboardMarkup(inline_keyboard=rows)

            await state.update_data(category=None, photo_urls=[])
            await message.answer(
                "Проверим следующее средство? Выберите категорию 👇",
                reply_markup=kb
            )

        except Exception as e:
            logger.error(f"Ошибка анализа: {e}")
            await loading_msg.edit_text("❌ Техническая ошибка. Попробуйте ещё раз — лимит не списан.")


PHOTO_HINT = "📸 Фото состава крупно, без бликов (круглые баночки — с 2-3 сторон). Или скопируйте состав из интернета!"


@dp.callback_query(F.data.startswith("cat_"))
async def process_category_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    cat_data = callback.data.split("_")
    cat_type = cat_data[1]

    data = await state.get_data()
    pending_inci = data.get("pending_inci")

    await state.update_data(category=cat_type)

    if pending_inci:
        user = db.get_or_create_user(callback.from_user.id)
        await state.update_data(pending_inci=None)
        await callback.message.answer("Категория выбрана. Приступаю к анализу!")
        await perform_analysis(callback.message, state, user, pending_inci, cat_type)
    else:
        await callback.message.answer(
            "Категория выбрана. Отправьте фотографию состава (четкое фото этикетки или скриншот из интернет-магазина) "
            "либо пришлите список компонентов (INCI) текстом 👇"
        )
        await state.set_state(Form.waiting_for_composition)

    await callback.answer()


@dp.message(StateFilter(Form.waiting_for_composition), F.photo)
async def process_photo_composition(message: types.Message, state: FSMContext):
    if message.media_group_id:
        await message.answer(
            "Пожалуйста, отправляйте строго по одной фотографии за раз, чтобы я проверил каждое средство отдельно. "
            "Ваша попытка сохранена."
        )
        return

    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)
    data = await state.get_data()
    category_fsm = data.get("category", None)

    if not category_fsm:
        await message.answer("⚠️ Сначала выберите категорию средства.", reply_markup=get_category_4_keyboard())
        return

    if not check_user_limits(user):
        db.schedule_retention_notifications(telegram_id)
        await send_paywall(message)
        return

    photo = message.photo[-1]
    b64 = await download_photo_as_base64(photo)
    if not b64:
        await message.answer("❌ Не удалось загрузить фото. Попробуйте ещё раз.")
        return

    photo_urls = data.get("photo_urls", [])
    if not isinstance(photo_urls, list):
        photo_urls = []
    photo_urls.append(b64)
    await state.update_data(photo_urls=photo_urls)

    await message.answer(
        f"📸 Фото добавлено.\n\nМожете отправить ещё фото или нажмите «Анализировать».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔬 Анализировать", callback_data="analyze_photos")]
        ])
    )


@dp.message(StateFilter(Form.waiting_for_composition), F.video | F.voice | F.audio | F.sticker | F.document)
async def reject_other_media(message: types.Message):
    await message.answer(
        "Отправьте обычную фотографию состава (или скриншот) либо скопируйте список ингредиентов текстом. "
        "Попытка сохранена."
    )


@dp.callback_query(F.data.startswith("cat_"))
async def process_category_choice(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass

    await safe_delete(callback.message)
    cat_data = callback.data.split("_")
    cat_type = cat_data[1]

    data = await state.get_data()
    pending_inci = data.get("pending_inci")

    await state.update_data(category=cat_type)

    if pending_inci:
        user = db.get_or_create_user(callback.from_user.id)
        await state.update_data(pending_inci=None)
        await callback.message.answer("Категория выбрана. Приступаю к анализу!")
        await perform_analysis(callback.message, state, user, pending_inci, cat_type)
    else:
        await callback.message.answer(
            "Категория выбрана. Отправьте фотографию состава (четкое фото этикетки или скриншот из интернет-магазина) "
            "либо пришлите список компонентов (INCI) текстом 👇"
        )
        await state.set_state(Form.waiting_for_composition)


        record_id = None
        if category in ['wash', 'care', 'eyes'] and status in ['good', 'hydrophilic', 'conditional']:
            record_id = db.save_approved_product(telegram_id, f"[ФОТО] {extracted_text[:100]}...", status, category, stage, actives)

        db.save_analysis(telegram_id, f"[ФОТО] {extracted_text[:100]}...", clean_text, status)

        if status in ["bad", "hidden_threat"]:
            db.increment_bad_bottles(telegram_id)
        else:
            db.reset_bad_bottles(telegram_id)

        is_unlimited = (telegram_id in UNLIMITED_IDS)
        has_sub = db.has_active_subscription(user)
        is_protocol = user.get("scanner_tariff") == "protocol_2500" and has_sub

        if is_unlimited:
            await loading_msg.edit_text(clean_text)
            await callback.message.answer("👑 Режим безлимита: проверок не ограничено.")
        elif not has_sub:
            db.increment_free_scans_used(telegram_id)
            updated_user = db.get_or_create_user(telegram_id)
            remaining = 2 - updated_user.get("free_scans_used", 0)

            await loading_msg.edit_text(clean_text)

            if remaining == 1:
                await callback.message.answer(WARMUP_TEXT, reply_markup=get_category_4_keyboard(), parse_mode="HTML")
                await state.update_data(category=None, photo_urls=[])
                await callback.answer()
                return
            elif remaining <= 0:
                db.schedule_retention_notifications(telegram_id)
                await send_paywall(callback.message)
                await state.update_data(category=None, photo_urls=[])
                await callback.answer()
                return
        else:
            await loading_msg.edit_text(clean_text)

        rows = []
        if is_protocol and record_id:
            rows.append([InlineKeyboardButton(
                text="🗑 Я только смотрю в магазине (Не добавлять в мой уход)",
                callback_data=f"remove_item:{record_id}"
            )])
        rows.extend(get_category_4_keyboard().inline_keyboard)
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        await state.update_data(category=None, photo_urls=[])
        await callback.message.answer(
            "Проверим следующее средство? Выберите категорию 👇",
            reply_markup=kb
        )

    await callback.answer()


@dp.message(StateFilter(Form.waiting_for_composition))
async def process_composition(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)
    data = await state.get_data()

    if data.get("photo_urls"):
        await message.answer(
            "📸 У вас уже есть загруженные фото. Нажмите «Анализировать» или отправьте ещё фото.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔬 Анализировать", callback_data="analyze_photos")]
            ])
        )
        return

    category_fsm = data.get("category", None)

    if not category_fsm:
        await message.answer("⚠️ Сначала выберите категорию средства.", reply_markup=get_category_4_keyboard())
        return

    if not check_user_limits(user):
        db.schedule_retention_notifications(telegram_id)
        await send_paywall(message)
        return

    composition = sanitize_input(message.text or "", max_len=2000)

    if not validate_inci_text(composition):
        await message.answer(
            "Не удалось распознать полный список компонентов (INCI).\n\n"
            "Пожалуйста, убедитесь, что текст содержит список ингредиентов после слова Состав / Ingredients, "
            "либо пришлите фото этикетки.\n\n"
            "Ваша попытка не списалась!"
        )
        return

    await perform_analysis(message, state, user, composition, category_fsm)


@dp.message(F.text & ~F.text.startswith('/'))
async def handle_text_no_state(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)

    if user.get("survey_done", 0) == 0:
        await message.answer("Пожалуйста, сначала пройдите короткую диагностику.", reply_markup=get_start_keyboard())
        return

    if not check_user_limits(user):
        await send_paywall(message)
        return

    composition = sanitize_input(message.text or "", max_len=2000)
    if not validate_inci_text(composition):
        await message.answer(
            "Не удалось распознать полный список компонентов (INCI).\n\n"
            "Пожалуйста, убедитесь, что текст содержит список ингредиентов после слова Состав / Ingredients, "
            "либо пришлите фото этикетки.\n\n"
            "Ваша попытка не списалась!"
        )
        return

    await state.update_data(pending_inci=composition)
    await message.answer(
        "Состав получен! Уточните, к какой категории относится это средство, чтобы я применил нужную матрицу 👇",
        reply_markup=get_category_4_keyboard()
    )
    await state.set_state(Form.waiting_for_category)


@dp.message(F.photo)
async def handle_photo_no_state(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)

    if user.get("survey_done", 0) == 0:
        await message.answer("Пожалуйста, сначала пройдите короткую диагностику.", reply_markup=get_start_keyboard())
        return

    if not check_user_limits(user):
        await send_paywall(message)
        return

    if message.media_group_id:
        await message.answer(
            "Пожалуйста, отправляйте строго по одной фотографии за раз, чтобы я проверил каждое средство отдельно. "
            "Ваша попытка сохранена."
        )
        return

    photo = message.photo[-1]
    b64 = await download_photo_as_base64(photo)
    if not b64:
        await message.answer("❌ Не удалось загрузить фото. Попробуйте ещё раз.")
        return

    loading_msg = await message.answer("🔍 Распознаю текст с фотографии...")
    extracted_text = await extract_text_from_photos([b64])

    if not validate_inci_text(extracted_text):
        await loading_msg.edit_text(
            "Не удалось распознать полный список компонентов (INCI).\n\n"
            "Пожалуйста, убедитесь, что на фото четко виден текст после слова Состав / Ingredients "
            "без обрезанных краев (либо скопируйте состав текстом из карточки товара).\n\n"
            "Ваша попытка не списалась!"
        )
        return

    await state.update_data(pending_inci=extracted_text)
    await loading_msg.edit_text(
        "Состав получен! Уточните, к какой категории относится это средство, чтобы я применил нужную матрицу 👇",
        reply_markup=get_category_4_keyboard()
    )
    await state.set_state(Form.waiting_for_category)


@dp.message(F.video | F.voice | F.audio | F.sticker | F.document)
async def reject_other_media_no_state(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    await message.answer(
        "Отправьте обычную фотографию состава (или скриншот) либо скопируйте список ингредиентов текстом. "
        "Попытка сохранена."
    )


@dp.callback_query(F.data.startswith("remove_item:"))
async def remove_item(callback: types.CallbackQuery, state: FSMContext):
    record_id = int(callback.data.split(":")[1])
    db.delete_approved_product(record_id)

    rows = [[InlineKeyboardButton(text="✅ Средство удалено из вашей схемы", callback_data="ignore")]]
    rows.extend(get_category_4_keyboard().inline_keyboard)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass

    await callback.answer("Средство удалено из вашего протокола.")


@dp.callback_query(F.data.startswith("toggle_stage:"))
async def toggle_stage(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    stage_key = callback.data.split(":")[1]

    stages = db.get_user_stages(telegram_id)
    new_val = not stages.get(stage_key, False)
    db.set_user_stage(telegram_id, stage_key, new_val)

    kb = build_stage_keyboard(telegram_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "finish_audit")
async def finish_audit(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    stages = db.get_user_stages(telegram_id)
    checked_count = sum(1 for v in stages.values() if v)

    if checked_count == 4:
        text = (
            "🤖 Идеально. Базовая архитектура выстроена, барьер защищен. Теперь мы переходим на следующий уровень. "
            "Начинаем аккуратно вводить «тяжелую артиллерию» (кислоты, витамин С, ретинол). "
            "Я проконтролирую, чтобы новые активы не сожгли лицо и не конфликтовали с базой. "
            "Продлите доступ, чтобы открыть следующий уровень 👇"
        )
    else:
        text = (
            "🤖 Я вижу незакрытые этапы. Оставлять архитектуру ухода с пробелами — значит продолжать терять липиды. "
            "Мы продолжаем работу. Я буду сканировать составы и блокировать агрессивные компоненты, "
            "пока мы не закроем каждый базовый этап. Продлите доступ, чтобы мы завершили сборку 👇"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Продлить Сборку протокола (30 дн.) — 2500 ₽", callback_data="pay_restore_90")],
        [InlineKeyboardButton(text="💳 Перейти на базовый Аудит (30 дн.) — 1000 ₽", callback_data="pay_audit_30")],
        [InlineKeyboardButton(text="👑 Устала искать сама. Нужен готовый уход от Марии", callback_data="want_mariya_box")]
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)

    await callback.answer()


# =====================================================================
# АДМИН-ПУЛЬТ: обработчики кнопок-глушилок
# =====================================================================

def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.callback_query(F.data.startswith("admin_pause:"))
async def admin_pause(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    try:
        tid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Повреждённые данные кнопки", show_alert=True)
        return

    db.set_maria_order_status(tid, "paused_72h")

    try:
        db.delay_retention_notifications(tid, hours=72)
    except Exception as e:
        logger.error(f"Не удалось отложить пуши для {tid}: {e}")
        await callback.answer("⚠️ Статус сохранён, но пуши не перенеслись. Проверьте логи.",
                              show_alert=True)
        return

    try:
        original = callback.message.html_text or callback.message.text or ""
        await callback.message.edit_text(
            f"{original}\n\n⏸ <b>Пуши остановлены на 72ч</b>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as e:
        logger.warning(f"Не удалось отредактировать карточку {tid}: {e}")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer("Пуши отложены на 72 часа", show_alert=True)
    logger.info(f"⏸ admin_pause: клиент {tid} → paused_72h (админ {callback.from_user.id})")


@dp.callback_query(F.data.startswith("admin_vip:"))
async def admin_vip(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    try:
        tid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Повреждённые данные кнопки", show_alert=True)
        return

    db.set_maria_order_status(tid, "completed_vip")

    try:
        db.cancel_retention_notifications(tid)
    except Exception as e:
        logger.error(f"Не удалось отменить пуши для {tid}: {e}")
        await callback.answer("⚠️ Статус сохранён, но пуши не отменены. Проверьте логи.",
                              show_alert=True)
        return

    try:
        original = callback.message.html_text or callback.message.text or ""
        await callback.message.edit_text(
            f"{original}\n\n✅ <b>Клиент переведен в статус VIP (Пуши отключены навсегда)</b>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as e:
        logger.warning(f"Не удалось отредактировать карточку {tid}: {e}")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer("Клиент помечен как VIP, пуши отключены", show_alert=True)
    logger.info(f"✅ admin_vip: клиент {tid} → completed_vip (админ {callback.from_user.id})")


# =====================================================================
# АДМИН-ПУЛЬТ: поиск клиента по хэштегу #telegram_id
# =====================================================================

@dp.message(F.from_user.id == ADMIN_ID, F.text.regexp(r"#\d{5,}"))
async def admin_search_client(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        return

    match = re.search(r"#(\d{5,})", message.text)
    if not match:
        return

    tid = int(match.group(1))

    user = db.get_user(tid)
    if not user:
        await message.answer(f"❌ Клиент с кодом <code>#{tid}</code> не найден в базе",
                             parse_mode="HTML")
        logger.info(f"🔍 admin_search: клиент #{tid} не найден (искал {message.from_user.id})")
        return

    ok = await send_client_card_to_admin(user, log_lead=False)
    if not ok:
        await message.answer("⚠️ Карточка не отправлена (см. логи). Проверьте, что админ доступен.")
    else:
        logger.info(f"🔍 admin_search: карточка #{tid} отправлена (искал {message.from_user.id})")


# =====================================================================
# АДМИН-ПАНЕЛЬ /admin
# =====================================================================

@dp.message(F.text == "/admin")
async def cmd_admin(message: types.Message):
    telegram_id = message.from_user.id
    if telegram_id not in [OWNER_ID, MARIYA_ID, ADMIN_ID]:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    conn = db.get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE survey_done = 1")
    surveyed_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE scanner_expires_at > ?", (datetime.now().isoformat(),))
    active_subs = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM leads WHERE status = 'new'")
    new_leads = cur.fetchone()[0]

    conn.close()

    text = (
        "👑 <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"📋 Прошли анкету: {surveyed_users}\n"
        f"💳 Активных подписок: {active_subs}\n"
        f"🆕 Новых заявок (лидов): {new_leads}\n"
    )
    await message.answer(text, parse_mode="HTML")


# =====================================================================
# ENTRY POINT
# =====================================================================


@dp.callback_query()
async def debug_unhandled_callbacks(callback: types.CallbackQuery):
    logger.warning(f"🐛 НЕОБРАБОТАННЫЙ callback: data={callback.data!r} from_user={callback.from_user.id}")
    try:
        await callback.answer("Отладка: см. логи", show_alert=False)
    except Exception:
        pass


async def main():
    asyncio.create_task(retention_worker())

    try:
        await bot.set_my_commands([
            BotCommand(command="start",   description="Запуск"),
            BotCommand(command="profile", description="Профиль и тариф"),
            BotCommand(command="tariffs", description="Тарифы"),
            BotCommand(command="admin",   description="Админ-панель"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось установить команды: {e}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    logger.info("🚀 Бот запущен в режиме polling")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("⛔ Бот остановлен")
