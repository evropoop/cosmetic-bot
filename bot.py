import hmac
import hashlib
import urllib.parse
from aiohttp import web
import os
import asyncio
import logging
import urllib.parse
import re
import base64
from datetime import datetime, timedelta

from prodamus_hmac import HmacHelper, flatten_form, parse_bracket_form
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import CommandStart, StateFilter
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
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

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

PRODAMUS_URL = os.getenv("PRODAMUS_URL")
PRODAMUS_SECRET = os.getenv("PRODAMUS_SECRET")
PRODAMUS_SYS = os.getenv("PRODAMUS_SYS")

UNLIMITED_IDS = [1962088357, 7875791813]  # Owner + Мария (полные права)

CIRCLE_VIDEO_ID = "DQACAgIAAxkBAAI0Mmr4ouA2EcZwkhbQEO0KFGP0YpuGAAOwAALBxMFLbVqZ52ULses9BA"
MAIN_VIDEO_ID = "BAACAgIAAxkBAAI0M2r4ouqOXeD9ZV7gsHOXQFtsra3oAAJMqgAC-23AS3ciA71KEYgfPQQ"

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
    text = re.sub(r'[\[\]\{\}<>()«»"\']', '', text)
    text = re.sub(r'[\n\r\t]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip(' .,;:/')
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


def _reschedule_retention_for_user(telegram_id: int):
    """Планирует retention/renewal если ещё нет pending. Учитывает подписку."""
    try:
        if telegram_id in UNLIMITED_IDS:
            return
        user = db.get_user(telegram_id)
        if not user:
            return
        if user.get("maria_order_status") == "completed_vip":
            return
        if db.has_pending_notifications(telegram_id):
            return

        if db.has_active_subscription(user):
            tariff = user.get("scanner_tariff")
            exp = user.get("scanner_expires_at")
            if tariff and exp:
                db.schedule_renewal_notification(telegram_id, tariff, exp)
                logger.info(f"📅 Renewal rescheduled for {telegram_id} ({tariff})")
            return

        # Отвальные — не чаще раза в 7 дней
        last = db.get_last_scheduled_at(telegram_id)
        if last:
            try:
                if (datetime.now() - datetime.fromisoformat(last)).days < 7:
                    return
            except Exception:
                pass

        db.schedule_retention_notifications(telegram_id)
        logger.info(f"📅 Retention (13) scheduled for {telegram_id}")
    except Exception as e:
        logger.warning(f"Retention reschedule failed for {telegram_id}: {e}")


class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            if hasattr(event, "from_user") and event.from_user:
                uid = event.from_user.id
                db.touch_user_activity(uid)
                _reschedule_retention_for_user(uid)
        except Exception:
            pass
        return await handler(event, data)


class FunnelMiddleware(BaseMiddleware):
    """Логирует каждое сообщение/callback с текущим FSM state — для анализа воронки."""
    async def __call__(self, handler, event, data):
        try:
            state: FSMContext = data.get("state")
            current_state = await state.get_state() if state else None

            user_id = None
            event_type = "unknown"
            preview = ""
            extra = ""

            if hasattr(event, "from_user") and event.from_user:
                user_id = event.from_user.id

            if isinstance(event, types.Message):
                event_type = "message"
                if event.photo:
                    preview = f"[PHOTO file_id={event.photo[-1].file_id}]"
                elif event.video_note:
                    preview = f"[VIDEO_NOTE file_id={event.video_note.file_id}]"
                elif event.video:
                    preview = f"[VIDEO file_id={event.video.file_id}]"
                elif event.text:
                    preview = event.text[:80].replace("\n", " ")
            elif isinstance(event, types.CallbackQuery):
                event_type = "callback"
                preview = event.data or ""
                # Метка шага опроса
                m = re.match(r'q_(\d+)_', preview)
                if m:
                    extra = f" [survey_step={m.group(1)}]"
                elif preview == "start_diagnostics":
                    extra = " [start_survey]"
                elif preview.startswith("cat_"):
                    extra = " [category_chosen]"
                elif preview.startswith("pay_"):
                    extra = " [payment_click]"
                elif preview.startswith("save_product"):
                    extra = " [save_product]"
                elif preview.startswith("skip_product"):
                    extra = " [skip_product]"

            if user_id:
                state_short = str(current_state).replace("Form:", "") if current_state else "None"
                logger.info(
                    f"📊 FUNNEL{extra}: user={user_id} | {event_type} | state={state_short} | data={preview!r}"
                )
        except Exception as e:
            logger.debug(f"FunnelMiddleware error: {e}")

        return await handler(event, data)


dp.message.middleware(ActivityMiddleware())
dp.callback_query.middleware(ActivityMiddleware())
dp.message.middleware(FunnelMiddleware())
dp.callback_query.middleware(FunnelMiddleware())

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


CHECKUP_BTN_TEXT = "🎁 Чекап за 1 499 рублей"
CHECKUP_VARIANTS = (
    CHECKUP_BTN_TEXT,
    "🎁 Чекап за 1 499 ₽",
    "🎁 Чекап за 1499 ₽",
    "🎁 Чекап за 1 499 рублей",
    "🎁 Чекап за 1499 рублей",
    "🎁 Чекап за 1499рублей",
    "🎁 Чекап + 20 000 бонусов",
    "🎁 Чекап + 15 000 бонусов",
)
CHECKUP_ARTICLE_URL = "https://telegra.ph/Kak-projti-polnyj-chekap-na-50-pokazatelej-za-1-990--vmesto-4-422--i-poluchit-20-000-bonusov-11-17"


def get_mode_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Проверить новое средство")],
        [KeyboardButton(text="📋 Меню")]
    ], resize_keyboard=True, is_persistent=True)


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
        [InlineKeyboardButton(text="🔍 Проверить новое средство", callback_data="go_scanner")],
        [InlineKeyboardButton(text="💳 Посмотреть тарифы", callback_data="show_tariffs")] # <--- ДОБАВИЛИ
    ])


@dp.callback_query(F.data == "show_tariffs")
async def show_tariffs_cb(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    # Вызываем ту самую функцию, которая отправляет текст тарифов
    await cmd_tariffs(callback.message, state)
    await callback.answer()


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
    "<b>Выберите формат работы:</b>\n\n"
    "⚡ <b>Тест-драйв сканера (7 проверок) — 99 ₽</b>\n"
    "Проверка 7 любых баночек (по фото или с ЗЯ, WB, Ozon) под профиль вашей кожи в течение 3 дней.\n"
    "Только сканер, без доступа в закрытый клуб.\n\n"
    "💳 <b>Базовый + Закрытый клуб (30 дней) — 1 000 ₽ (хит)</b>\n"
    "Безлимитная проверка любых средств (по фото и в ЗЯ, WB, Ozon) на 30 дней.\n"
    "Доступ в закрытый клуб Skin Protokol: готовые рабочие связки, ссылки на магазины со спецпредложениями, «чёрный список» пустышек и обратная связь.\n\n"
    "💎 <b>Полный протокол + Клуб (30 дней) — 2 500 ₽</b>\n"
    "Всё, что в «Базовом», плюс: Роман запоминает все ваши средства, находит конфликты активов (чтобы не сжечь кожу) и показывает, чего не хватает в уходе.\n\n"
    "💡 <i>Одна случайно купленная сыворотка-пустышка стоит от 2 500 ₽ — доступ окупается с первой же забракованной банки.</i>"
)

WARMUP_TEXT = (
    "🤖 Роман Андреевич: В будущем вы сможете продолжить работу со мной в удобном формате:\n\n"
    "⚡ <b>Тест-драйв сканера (7 проверок) — 99 ₽</b> — только сканер, без закрытого клуба.\n"
    "💳 <b>Базовый + Закрытый клуб (30 дней) — 1 000 ₽</b> — безлимит + закрытый клуб Skin Protokol.\n"
    "💎 <b>Полный протокол + Клуб (30 дней) — 2 500 ₽</b> — ведение всей косметички + закрытый клуб.\n\n"
    "🎁 Но у вас осталась еще 1 бесплатная попытка! Какую категорию проверим следующей?"
)


async def send_paywall(message: types.Message):
    tid = message.chat.id
    logger.info(f"📊 FUNNEL [paywall_shown] user={tid}")
    user_name = db.get_user_name(tid) or "Клиент"
    text_q = f"Здравствуйте, Мария! Я оплатил(а) доступ, но бот не открылся. Мой ID: #{tid}, Имя: {user_name}."
    url_q = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_q)}"
    await message.answer(
        PAYWALL_TEXT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")],
            [InlineKeyboardButton(text="💬 Уже оплатили? Написать Марии", url=url_q)]
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
            ("Аптечные мази (Скинорен и др.)", "pharmacy_creams"),
            ("Гормональные нарушения", "medicine"),
            ("Кожные заболевания (розацеа и др.)", "medicine_skin"),
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
    # === RETENTION-ВОРОНКА (13 касаний за 180 дней) ===
    "2h": "🤖 {user_name}, вы остановились на этапе активации. Ваша анкета сохранена. Чтобы я мог завершить химический анализ вашего ухода и выстроить протокол безопасности, необходимо завершить оформление. Я готов приступить к работе.",
    "24h": "🤖 Прошли сутки с момента паузы. Напоминаю, что каждый день использования неподходящей косметики усугубляет состояние барьера. Если в ваших баночках есть скрытые угрозы, вы наносите их прямо сейчас. Давайте остановим этот процесс. Открываем доступ?",
    "48h": "🤖 Я технически перевожу вашу сессию в режим ожидания. Данные заархивированы. Если решите навести порядок в уходе, перестать тратить деньги на агрессивные средства и доверить сборку системы алгоритму — возвращайтесь.",
    "5d": "🤖 {user_name}, наверняка вы снова смотрели на косметику на маркетплейсах. Красивые обещания часто маскируют дешевую базу и пустышки. Не покупайте новые средства вслепую. Загрузите составы мне перед оплатой, и я уберегу вас от пустой траты денег.",
    "7d": "🤖 Прошла неделя. За это время вы нанесли свой привычный уход минимум 14 раз. Если мы не исключили из вашей системы жесткие ПАВы или конфликтные активы, вы значительно усилили дефицит липидов. Доступ к системе ждет активации.",
    "14d": "🤖 {user_name}, если у вас нет времени самостоятельно сканировать каждый состав — снимите с себя эту задачу. Создатель моего алгоритма, Мария, может собрать для вас индивидуальный протокол с нуля или отправить готовый бокс.",
    "30d": "🤖 Прошло ровно 30 дней. 28–35 дней — это полный цикл обновления эпидермиса. Если вы устали действовать вслепую и готовы выстроить грамотную, физиологичную базу — двери лаборатории открыты.",
    "42d": "🤖 Я — строгий алгоритм, который бракует средства без поблажек. Если вы устали искать банки самостоятельно и вам нужен человеческий подход — обратитесь к моей создательнице, косметологу-эстетисту Марии. Она соберет готовый физиологичный бокс под вашу кожу.",
    "60d": "🤖 Прошло ровно два месяца с вашего первого визита. За это время эпидермис полностью обновился дважды. База, которую вы использовали всё это время, могла исчерпать свой ресурс. Пришло время провести жесткий аудит вашей системы и скорректировать протокол.",
    "90d": "🤖 {user_name}, прошло 3 месяца. Это критический срок (PAO) для большинства активных сывороток. Пептиды деградируют, ретинол теряет стабильность. Наносить такую косметику бесполезно, а иногда и опасно. Ваша система ухода требует генеральной ревизии. Запускаем алгоритм?",
    "120d": "🤖 4 месяца — достаточный срок, чтобы устать от самостоятельного подбора банок методом проб и ошибок. Если вы хотите делегировать эту задачу и получить 100% результат, моя создательница, косметолог Мария, соберет для вас идеальную систему ухода с нуля.",
    "150d": "🤖 {user_name}, климатические условия изменились. То, что спасало вашу кожу прошлым сезоном, сейчас может работать против вас. Переход с легких эмульсий на плотные кремы (или наоборот) требует проверки составов, чтобы не спровоцировать воспаления. Запускаем аудит?",
    "180d": "🤖 {user_name}, прошло ровно полгода с момента вашей регистрации. За это время вы нанесли на лицо десятки составов вслепую. Я технически завершаю ваш сеанс и переношу данные в глубокий архив. Если решите выстроить по-настоящему безопасный, физиологичный уход — активируйте тариф или закажите готовый бокс от Марии. Двери лаборатории всегда открыты.",

    # === ПОДПИСОЧНЫЕ (планируются при оплате, не входят в retention-воронку) ===
    "no_composition_6h": "🤖 {user_name}, я вижу, вы прошли диагностику, но ещё не проверили ни одного средства.\n\nБез проверки первого состава я не могу сказать ничего конкретного о вашей реальной коже. Пришлите состав любого крема (например, того, что сейчас на полке) — это займёт 30 секунд. Я разберу его и покажу, что в нём скрыто.",
    "abandoned_cart": "🤖 {user_name}, вижу, что вы начали оформление доступа к сканеру, но оплата не завершилась.\n\nЕсли возникли технические сложности, не проходит зарубежная карта или есть сомнения по формату ухода — напишите напрямую создательнице алгоритма Марии, мы поможем и сохраним за вами условия 👇",
    "renewal_trial": "🤖 {user_name}, завтра ваш 3-дневный тест-драйв сканера завершается.\nЕсли на вашей полке еще остались непроверенные средства или вы планируете покупки в магазине — продлите «Аудит» (33 ₽/день), чтобы сканер продолжал защищать вашу кожу от ошибок и пустышек.",
    "renewal_audit": "🤖 {user_name}, завтра ваш доступ к сканирующему модулю закрывается.\nНе покупайте новые средства вслепую. Одно средство с жесткими ПАВами или комедогенной базой может спровоцировать воспаление и откатить результаты назад. Продлите «Аудит», чтобы я продолжил жестко фильтровать ваши покупки.\n⬆️ Или перейдите на следующий уровень: выберите «Сборку протокола». Я включу интеллектуальную память, запомню все ваши средства и выстрою из них единую безопасную систему.",
    "renewal_protocol": "🤖 {user_name}, наши 30 дней работы подходят к концу. За 28–35 дней эпидермис проходит полный цикл обновления. Завтра ваш доступ к алгоритму закрывается.\nДавайте подведем итоги. Отметьте ниже этапы ухода, которые мы с вами уже успели закрыть безопасными, одобренными средствами:",
    "reactivation_30d": "🤖 {user_name}, срок вашего доступа истек. Сканирующий модуль отключен. Ваша текущая архитектура ухода сохранена и переведена в архив. Когда потребуется выбрать новые средства — алгоритм будет готов распаковать архив и проверить составы.",
    "reactivation_44d": "🤖 {user_name}, если самостоятельный подбор отнимает слишком много времени — снимите с себя эту задачу. Создатель алгоритма, Мария, собирает Индивидуальные Боксы вручную. Вы получаете готовую систему без необходимости вникать в химию.",
    "reactivation_75d": "🤖 {user_name}, согласно статистике расхода, за 1,5–2 месяца базовая сыворотка или крем подходят к концу. Если вы планируете покупку новых средств, помните: не покупать вслепую. Доступ к лаборатории можно возобновить в любую минуту.",
}

# === СЕГМЕНТАЦИЯ ПРОГРЕВА (RETENTION 2.0) ПО ВЕДУЩИМ БОЛЯМ ===
RETENTION_SEGMENTS = {
    "acne": {
        "24h": "🤖 {user_name}, прошли сутки с момента паузы. Если у вас есть высыпания или закрытые комедоны — каждый день использования непроверенного крема с минеральным маслом, изопропилмиристатом или жесткими ПАВами провоцирует новые бугорки под кожей. Давайте разберем оставшиеся средства и остановим воспалительный цикл?",
        "5d": "🤖 {user_name}, на маркетплейсах тысячи банок с пометкой «от прыщей», которые на деле содержат дешевые комедогенные спирты и забивают поры еще глубже. Не покупайте новые средства вслепую — проверьте их состав перед заказом.",
        "7d": "🤖 Прошла неделя. Если мы не убрали из вашей системы агрессивные умывалки, барьер продолжает истончаться, а сальные железы работают в аварийном режиме профицита себума. Давайте наведем порядок в уходе.",
    },
    "pregnancy": {
        "24h": "🤖 {user_name}, прошли сутки. Во время беременности проницаемость кожи повышена, а тератогенные компоненты (ретиноиды, высокие дозы салициловой кислоты, арбутин) несут скрытые риски для малыша. Давайте завершим аудит вашей косметички и убедимся в 100% безопасности ухода?",
        "5d": "🤖 {user_name}, многие будущие мамы покупают популярные кремы от растяжек и пигментации, не зная, что в них скрыты агрессивные консерванты и химические УФ-фильтры (Benzophenone-3). Проверьте средства до того, как нанесете на кожу.",
        "7d": "🤖 Прошла неделя с момента диагностики. Ваша кожа в особый период нуждается в максимально бережном физиологичном протоколе. Я готов продолжить аудит.",
    },
    "couperose": {
        "24h": "🤖 {user_name}, прошли сутки. При куперозе и реактивной коже даже один случайный тоник с мятой, эвкалиптом, чистым кислым витамином С или спиртом вызывает стойкую сосудистую эритему и химический ожог. Давайте проверим ваши средства до контакта с лицом?",
        "5d": "🤖 {user_name}, маркетинг часто обещает «мгновенное снятие красноты», маскируя составы эфирными маслами, которые только расширяют хрупкие капилляры. Не покупайте косметику вслепую.",
        "7d": "🤖 Прошла неделя. Хрупкие капилляры не восстанавливаются сами, если ежедневно подвергаются скрытому раздражению. Давайте завершим аудит вашей полки.",
    },
}

RETENTION_DELAYS = {
    "2h": 7200, "24h": 86400, "48h": 172800,
    "5d": 432000, "7d": 604800, "14d": 1209600,
    "30d": 2592000, "42d": 3628800, "60d": 5184000,
    "90d": 7776000, "120d": 10368000, "150d": 12960000, "180d": 15552000,
    # Подписочные — без задержки (0), планируются через schedule_single_notification
    "renewal_audit": 0, "renewal_protocol": 0, "renewal_trial": 0,
    "reactivation_30d": 0, "reactivation_44d": 0, "reactivation_75d": 0,
    "no_composition_6h": 6 * 3600,  # 6 часов после анкеты
    "abandoned_cart": 0,
}


def build_system_matrix(telegram_id: int) -> str:
    """Собирает наглядную матрицу текущей системы ухода клиентки (для тарифа 2500)."""
    products = db.get_approved_products(telegram_id)
    if not products:
        return ""

    cleansing = []
    base_cream = []
    actives = []
    spf = []

    # Базовые увлажнители, которые НЕ являются активами
    base_moisturizers = {"squalane", "panthenol", "glycerin", "hyaluronic", "glycerol"}

    for p in products:
        cat = (p.get("category") or "").strip()
        stage = (p.get("stage") or "").strip().lower()
        act = (p.get("key_actives") or "").strip()

        if not act:
            act = "—"

        if cat == "wash":
            cleansing.append(act)
        elif cat in ["care", "eyes"]:
            if "spf" in stage or "защита" in stage:
                spf.append(act)
            elif "актив" in stage:
                # Проверяем, что это НЕ базовый увлажнитель под видом актива
                act_low = act.lower()
                is_real_active = not all(m in act_low for m in [act_low]) or not any(m in act_low for m in base_moisturizers)
                if any(real in act_low for real in ["acid", "retinol", "niacinamide", "vitamin", "peptide", "caffeine", "bakuchiol", "arbutin"]):
                    actives.append(act)
                else:
                    base_cream.append(act)
            else:
                base_cream.append(act)

    # Убираем дубли внутри каждого списка
    def unique_flat(lst):
        """Собирает все уникальные INCI через запятую из списка строк."""
        result = []
        for item in lst:
            for part in item.split(","):
                part = part.strip()
                if part and part.lower() not in [r.lower() for r in result]:
                    result.append(part)
        return result

    cleansing_u = unique_flat(cleansing)
    base_u = unique_flat(base_cream)
    actives_u = unique_flat(actives)
    spf_u = unique_flat(spf)

    lines = ["\n\n📊 <b>СТАТУС СБОРКИ ВАШЕЙ СИСТЕМЫ УХОДА:</b>"]

    if cleansing_u:
        lines.append(f"✅ Очищение: Закрыто ({_escape_html(', '.join(cleansing_u[:2]))})")
    else:
        lines.append("❌ Очищение: НЕ ЗАКРЫТО. Ищите мягкий гель на Coco-Glucoside или Decyl Glucoside")

    if actives_u:
        lines.append(f"✅ Активный уход: {_escape_html(', '.join(actives_u[:3]))}")
    else:
        lines.append("➖ Активный уход: пока нет (необязательно, если кожа в порядке)")

    if base_u:
        lines.append(f"✅ Базовый крем: Закрыто ({_escape_html(', '.join(base_u[:3]))})")
    else:
        lines.append("❌ Базовый крем: НЕ ЗАКРЫТО. Обязательно для восстановления барьера")

    if spf_u:
        lines.append(f"✅ Защита SPF: Закрыто ({_escape_html(', '.join(spf_u[:2]))})")
    else:
        lines.append("❌ Защита SPF: НЕ ЗАКРЫТО. Критически важно при использовании кислот")

    return "\n".join(lines)


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
    user = db.get_user(telegram_id)
    tags = user.get("user_tags", []) if user else []

    # Персонализация прогрева (Retention 2.0) по сегментам кожи
    segment = None
    if "pregnancy" in tags:
        segment = "pregnancy"
    elif any(t in tags for t in ["acne", "comedones", "problem", "seboprofit"]):
        segment = "acne"
    elif any(t in tags for t in ["reactive", "couperose"]):
        segment = "couperose"

    if segment and segment in RETENTION_SEGMENTS and notif_type in RETENTION_SEGMENTS[segment]:
        text_template = RETENTION_SEGMENTS[segment][notif_type]
    else:
        text_template = RETENTION_TEXTS.get(notif_type)

    if not text_template:
        return

    user_name = _escape_html(db.get_user_name(telegram_id) or "Клиент")
    text = text_template.format(user_name=user_name)

    # Клавиатуры по типу уведомления
    if notif_type == "abandoned_cart":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Посмотреть все тарифы", callback_data="show_tariffs")],
            [InlineKeyboardButton(text="💬 Написать Марии", url=f"https://t.me/{MARIYA_USERNAME}")]
        ])
    elif notif_type in ["14d", "42d", "120d", "180d"]:
        # Продажа услуг Марии + тарифы
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Заказать бокс / протокол Марии", url=f"https://t.me/{MARIYA_USERNAME}")],
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
    elif notif_type == "no_composition_6h":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Проверить новое средство", callback_data="go_scanner")],
            [InlineKeyboardButton(text="💳 Посмотреть тарифы", callback_data="show_tariffs")]
        ])
    elif notif_type == "renewal_audit":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Продлить Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Перейти на Полный протокол (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
    elif notif_type == "renewal_trial":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
    elif notif_type == "renewal_protocol":
        kb = build_stage_keyboard(telegram_id)
    elif notif_type == "reactivation_44d":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Заказать бокс от Марии", url=f"https://t.me/{MARIYA_USERNAME}")],
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")]
        ])
    elif notif_type in ["reactivation_30d", "reactivation_75d"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
    else:
        # Остальные retention (2h..150d) — стандартные тарифы с триалом
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])

    try:
        await bot.send_message(telegram_id, text, reply_markup=kb, parse_mode="HTML")
        logger.info(f"📨 Уведомление '{notif_type}' → telegram_id={telegram_id}")
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление {notif_type} юзеру {telegram_id}: {e}")


async def grant_channel_access(user_id: int, tariff_label: str, expires_at):
    """Одноразовая ссылка в закрытый канал / подтверждение продления."""
    if not CHANNEL_ID:
        logger.warning(f"CHANNEL_ID=0, skip grant for {user_id}")
        return
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        is_member = member.status in ("member", "administrator", "creator")
    except Exception:
        is_member = False

    if is_member:
        try:
            await bot.send_message(
                user_id,
                f"✅ Оплата получена. Подписка «{tariff_label}» продлена до "
                f"{expires_at.strftime('%d.%m.%Y')}. Доступ в клуб сохранён."
            )
            logger.info(f"✅ Channel renewed (already member): {user_id}")
        except Exception as e:
            logger.warning(f"send_message to {user_id} failed: {e}")
        return

    # Не в канале → разбанить (если был кикнут) + новая одноразовая ссылка
    try:
        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    except Exception:
        pass

    try:
        _ts = int(expires_at.timestamp()) + 86400  # +1 день буфер
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=_ts,
            name=f"{tariff_label}-{user_id}"
        )
    except Exception as e:
        logger.error(f"❌ create_chat_invite_link failed for {user_id}: {e}")
        try:
            await bot.send_message(
                user_id,
                "⚠️ Оплата получена, но ссылку в клуб создать не удалось. Напишите администратору."
            )
        except Exception:
            pass
        return

    text = (
        f"🎉 Оплата «{tariff_label}» получена! Доступ активирован до "
        f"{expires_at.strftime('%d.%m.%Y')}.\n\n"
        f"👑 Ваша персональная ссылка в закрытый клуб (одноразовая):\n"
        f"{invite.invite_link}\n\n"
        f"Ссылка действует до окончания подписки. Не передавайте её другим."
    )
    try:
        await bot.send_message(user_id, text)
        logger.info(f"✅ Channel invite sent to {user_id}")
    except Exception as e:
        logger.error(f"❌ send invite to {user_id} failed: {e}")


async def channel_worker():
    """Раз в час кикает пользователей с истёкшей подпиской из закрытого канала."""
    logger.info("⏰ Channel-воркер запущен")
    while True:
        try:
            expired = db.get_expired_subscriptions()
            for tid in expired:
                # Двойная проверка: продлил ли в последний момент?
                fresh = db.get_user(tid)
                if fresh and fresh.get("scanner_expires_at"):
                    try:
                        exp = datetime.fromisoformat(fresh["scanner_expires_at"])
                        if exp > datetime.now():
                            logger.info(f"⏭ {tid} renewed before kick, skip")
                            db.clear_channel_kicked(tid)
                            continue
                    except Exception:
                        pass

                if not CHANNEL_ID:
                    continue

                try:
                    await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=tid, revoke_messages=False)
                    await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=tid)
                    db.mark_channel_kicked(tid)
                    logger.info(f"🚪 Kicked expired user {tid} from channel")
                    try:
                        await bot.send_message(
                            tid,
                            "⏰ Ваша подписка истекла. Доступ в закрытый клуб закрыт.\n"
                            "Продлить и вернуться: /tariffs"
                        )
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"Failed to kick {tid}: {e}")
        except Exception as e:
            logger.error(f"Channel worker error: {e}")
        await asyncio.sleep(3600)


@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_user_joined_channel(event: types.ChatMemberUpdated):
    if CHANNEL_ID and event.chat.id == CHANNEL_ID:
        user = event.new_chat_member.user
        logger.info(
            f"🎉 ПОЛЬЗОВАТЕЛЬ ВСТУПИЛ В ЗАКРЫТЫЙ КАНАЛ: "
            f"user_id={user.id} | name='{user.full_name}' | username=@{user.username or 'none'}"
        )


@dp.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_user_left_channel(event: types.ChatMemberUpdated):
    if CHANNEL_ID and event.chat.id == CHANNEL_ID:
        user = event.old_chat_member.user
        logger.info(
            f"🚪 ПОЛЬЗОВАТЕЛЬ ПОКИНУЛ ЗАКРЫТЫЙ КАНАЛ: "
            f"user_id={user.id} | name='{user.full_name}' | username=@{user.username or 'none'}"
        )


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

                if notif_type == "abandoned_cart":
                    user = db.get_user(user_id)
                    if user and (user.get("maria_order_status") == "completed_vip" or db.has_active_subscription(user)):
                        db.mark_notification_sent(n["id"])
                        continue

                if notif_type.startswith("renewal_"):
                    user = db.get_user(user_id)
                    if user and db.has_active_subscription(user):
                        exp = user.get("scanner_expires_at")
                        if exp:
                            try:
                                exp_dt = datetime.fromisoformat(exp)
                                if (exp_dt - datetime.now()).total_seconds() > 25 * 3600:
                                    db.mark_notification_sent(n["id"])
                                    continue
                            except Exception:
                                pass

                last_activity = db.get_last_activity(user_id)

                if notif_type not in ["renewal_audit", "renewal_protocol", "renewal_trial", "reactivation_30d", "reactivation_44d", "reactivation_75d", "no_composition_6h", "abandoned_cart"] and last_activity and delay > 0:
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

    if not name and user:
        name = user.get("user_name")

    if not name:
        telegram_id = user.get("telegram_id") if user else message.chat.id
        try:
            name = db.get_user_name(telegram_id)
        except Exception:
            name = None

    if not name and message.from_user and not getattr(message.from_user, "is_bot", False):
        name = message.from_user.first_name

    if not name:
        name = "Клиент"

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
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
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
    await state.clear()
    telegram_id = message.chat.id
    user = db.get_or_create_user(telegram_id)
    user_name = db.get_user_name(telegram_id) or "Клиент"

    tags_str = _translate_tags(user.get("user_tags", []))

    if telegram_id in UNLIMITED_IDS:
        count = db.count_approved_products(telegram_id)
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"👑 Режим: Безлимитный доступ (владелец)\n"
            f"Одобренных средств в вашей схеме: {count} шт."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Моя собранная база", callback_data="btn_my_base")]
        ])
        await message.answer(text, reply_markup=kb)
        return

    if not db.has_active_subscription(user):
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Статус доступа: Неактивен (Использовано бесплатных проверок: {user.get('free_scans_used', 0)} из 2).\n\n"
            f"<b>Выберите формат работы:</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    elif user.get("scanner_tariff") == "trial_99":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y %H:%M')
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: ⚡ Тест-драйв сканера (7 проверок, без клуба)\n"
            f"Активен до: {exp_date}\n\n"
            f"Продлить на 30 дней с доступом в закрытый клуб:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await message.answer(text, reply_markup=kb)

    elif user.get("scanner_tariff") == "audit_1000":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: 💳 Базовый + Закрытый клуб\n"
            f"Активен до: {exp_date}\n\n"
            f"Продлить или повысить тариф:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")],
            [InlineKeyboardButton(text="💳 Продлить Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")]
        ])
        await message.answer(text, reply_markup=kb)

    elif user.get("scanner_tariff") == "protocol_2500":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        count = db.count_approved_products(telegram_id)
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: 💎 Полный протокол + Закрытый клуб\n"
            f"Активен до: {exp_date}\n"
            f"Одобренных средств в вашей схеме: {count} шт."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Моя собранная база", callback_data="btn_my_base")],
            [InlineKeyboardButton(text="💎 Продлить Полный протокол (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await message.answer(text, reply_markup=kb)

    elif user.get("scanner_tariff") == "pdf_4900":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: 👑 VIP PDF-протокол + Закрытый клуб\n"
            f"Активен до: {exp_date}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Перейти к Марии", url=f"https://t.me/{MARIYA_USERNAME}")],
            [InlineKeyboardButton(text="💎 Продлить Полный протокол (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await message.answer(text, reply_markup=kb)

    else:
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y') if user.get("scanner_expires_at") else "—"
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Статус доступа: Активен до {exp_date}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")]
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
            text += f"{i}. {status_str} {_escape_html(p.get('stage') or '')} ({_escape_html(comp_short)})\n"
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
async def cmd_tariffs(message: types.Message, state: FSMContext = None):
    if state:
        await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
        [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
        [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
    ])
    await message.answer(PAYWALL_TEXT, reply_markup=kb, parse_mode="HTML")


@dp.message(F.text.startswith("/start pay_success_"))
async def handle_payment_return(message: types.Message):
    order_id = message.text.replace("/start pay_success_", "").strip()

    if db.check_payment_exists(order_id):
        await message.answer(
            "✅ Ваш доступ уже активирован ранее. Проверьте свой профиль командой /profile.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Проверить средство", callback_data="btn_check_new")],
                [InlineKeyboardButton(text="👤 Мой профиль", callback_data="menu_profile")]
            ])
        )
        return

    try:
        # Разбираем order_id (формат: user-123456789-audit_1000-1794950638)
        parts = order_id.split("-")
        if len(parts) < 3 or parts[0] != "user":
            await message.answer("⚠️ Неверный код заказа. Обратитесь в поддержку.")
            return

        telegram_id = int(parts[1])
        tariff_type = parts[2]

        _AMOUNT_MAP = {
            "trial_99": 99,
            "audit_1000": 1000,
            "protocol_2500": 2500,
            "pdf_4900": 4900,
        }
        if tariff_type not in _AMOUNT_MAP:
            await message.answer("⚠️ Неизвестный тариф. Обратитесь в поддержку.")
            return

        if telegram_id != message.from_user.id:
            logger.warning(f"⚠️ Попытка чужой активации: sender={message.from_user.id}, order_tid={telegram_id}")
            await message.answer("⚠️ Этот заказ привязан к другому аккаунту Telegram.")
            return

        _amount = _AMOUNT_MAP[tariff_type]
        db.save_payment(order_id, telegram_id, _amount, tariff_type)
        db.clear_channel_kicked(telegram_id)
        db.cancel_single_notification(telegram_id, "abandoned_cart")

        if tariff_type == "pdf_4900":
            db.cancel_retention_notifications(telegram_id)
            db.set_maria_order_status(telegram_id, "completed_vip")
            db.update_user_subscription(telegram_id, "pdf_4900", 30)
            _label = "PDF-протокол"

            await message.answer(
                "🎉 Оплата PDF-протокола (4 900 ₽) успешно получена!\n\n"
                "Мария уже получила данные вашей диагностики. Нажмите на кнопку ниже, "
                "чтобы перейти в личный диалог и согласовать комфортный бюджет на покупку средств 👇",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💬 Перейти к Марии", url=f"https://t.me/{MARIYA_USERNAME}")]
                ])
            )
            try:
                user_name = db.get_user_name(telegram_id) or "Клиент"
                await bot.send_message(
                    ADMIN_ID,
                    f"🎉 ОПЛАЧЕН PDF-ПРОТОКОЛ (4 900 ₽)\nКлиент: {user_name}\nID: {telegram_id}"
                )
            except Exception:
                pass

        elif tariff_type == "trial_99":
            new_expires = db.update_user_subscription(telegram_id, "trial_99", 3)
            db.cancel_retention_notifications(telegram_id)
            db.schedule_renewal_notification(telegram_id, "trial_99", new_expires)
            _label = "Тест-драйв"
            _exp_str = datetime.fromisoformat(new_expires).strftime("%d.%m.%Y %H:%M")
            await message.answer(
                f"🎉 Оплата тест-драйва (99 ₽) успешно получена!\n\n"
                f"Доступ к сканеру продлён и активен до <b>{_exp_str}</b>.\n"
                f"Проверьте свой профиль командой /profile или сразу отправьте состав средства 👇",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Проверить средство", callback_data="btn_check_new")]
                ]),
                parse_mode="HTML"
            )

        else:
            new_expires = db.update_user_subscription(telegram_id, tariff_type, 30)
            db.cancel_retention_notifications(telegram_id)
            db.schedule_renewal_notification(telegram_id, tariff_type, new_expires)
            _label = "Базовый + Клуб" if tariff_type == "audit_1000" else "Полный протокол + Клуб"
            _exp_str = datetime.fromisoformat(new_expires).strftime("%d.%m.%Y")
            await message.answer(
                f"🎉 Оплата успешно получена!\n\n"
                f"Доступ продлён и активен до <b>{_exp_str}</b>.\n"
                f"Проверьте свой профиль командой /profile или сразу отправьте состав средства 👇",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Проверить средство", callback_data="btn_check_new")]
                ]),
                parse_mode="HTML"
            )

        # Выдача одноразовой ссылки в закрытый клуб (ТОЛЬКО для тарифов с клубом, без тест-драйва)
        if tariff_type != "trial_99":
            _u = db.get_user(telegram_id)
            if _u and _u.get("scanner_expires_at"):
                try:
                    _exp = datetime.fromisoformat(_u["scanner_expires_at"])
                    await grant_channel_access(telegram_id, _label, _exp)
                except Exception as e:
                    logger.error(f"grant_channel_access (return) failed: {e}")

    except Exception as e:
        logger.error(f"Ошибка при возврате с оплаты: {e}")
        await message.answer("⚠️ Ошибка активации доступа. Пожалуйста, свяжитесь с поддержкой.")


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
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
                        reply_markup=get_mode_keyboard(),
                        parse_mode="Markdown"
                    )
                    return
            except Exception as e:
                logger.error(f"Ошибка карантина: {e}")

    try:
        await message.answer_video_note(video_note=CIRCLE_VIDEO_ID, reply_markup=get_mode_keyboard())
    except Exception as e:
        logger.warning(f"Не удалось отправить кружок ({e}) — fallback на текст")
        try:
            await message.answer(
                "👋 Добро пожаловать! Нажмите СТАРТ ниже — запустим диагностику.",
                reply_markup=get_mode_keyboard()
            )
        except Exception:
            pass

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


@dp.message(StateFilter("*"), F.text.in_(CHECKUP_VARIANTS))
async def handle_checkup_menu_btn(message: types.Message):
    logger.info(f"📊 FUNNEL [checkup_btn_clicked] user={message.from_user.id}")
    text = (
        "Приветствую 🫂\n\n"
        "Забирайте полезную статью с полным чекапом на 50 показателей со скидкой (всего за 1 499 ₽ вместо 4 422 ₽) 🎁\n\n"
        "🎟 Промокод на скидку: <code>SKINPROTOKOL55</code>\n"
        "<i>(нажмите на промокод, чтобы скопировать)</i>\n\n"
        "🎁 Код на 20 000 бонусов: <code>MRSL-2026</code>\n"
        "<i>(нажмите на код, чтобы скопировать)</i>\n\n"
        "Пошаговая инструкция — по кнопке ниже 👇🏻"
    )
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="ПОЛУЧИТЬ ЧЕКАП (1 499 ₽)", url=CHECKUP_ARTICLE_URL)]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "checkup_get_article")
async def handle_checkup_get_article(callback: types.CallbackQuery):
    logger.info(f"📊 FUNNEL [checkup_article_opened] user={callback.from_user.id}")
    text = (
        "Отлично! Нажимайте на кнопку ниже, чтобы получить чекап за 1 499 ₽👇🏻\n\n"
        "🎟 Промокод на скидку: <code>SKINPROTOKOL55</code> <i>(нажмите, чтобы скопировать)</i>\n"
        "🎁 Код на 20 000 бонусов: <code>MRSL-2026</code> <i>(нажмите, чтобы скопировать)</i>"
    )
    await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Получить чекап за 1 499 ₽", url=CHECKUP_ARTICLE_URL)]
        ]),
        parse_mode="HTML"
    )
    try:
        await callback.answer()
    except Exception:
        pass


@dp.message(F.text.lower() == "бокс")
async def code_box(message: types.Message, state: FSMContext):
    await cmd_start(message, state)


@dp.message(F.text == "🔍 Проверить новое средство")
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


@dp.message(F.text == "💬 Задать вопрос Марии")
async def ask_mariya_question(message: types.Message, state: FSMContext):
    user = db.get_or_create_user(message.from_user.id)
    user_name = await _resolve_user_name(message, state, user)
    tid = message.from_user.id

    try:
        db.save_lead(
            telegram_id=tid,
            username=user.get("username") or "",
            tags=user.get("user_tags", []),
            photos=[]
        )
    except Exception:
        pass

    text_q = f"Привет! У меня вопрос по уходу. Меня зовут: {user_name}. Код анкеты: #{tid}"
    url_q = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_q)}"

    await message.answer(
        "Нажмите кнопку ниже — откроется личный диалог с Марией, она ответит на ваш вопрос по уходу.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать Марии", url=url_q)]
        ])
    )


@dp.callback_query(F.data == "pay_pdf_4900")
async def pay_pdf_4900(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    link = await generate_prodamus_link(telegram_id, 4900, "pdf_4900")
    db.schedule_single_notification(telegram_id, "abandoned_cart", datetime.now() + timedelta(minutes=35))
    await callback.message.answer(
        "📄 <b>Персональный PDF-протокол (4 900 ₽)</b> — уход в формате подробного "
        "PDF-руководства. Мария подберёт косметику строго под ваш бюджет, даст прямые "
        "ссылки на покупку каждого средства и распишет понятную инструкцию (что и как "
        "наносить утром и вечером). Вы покупаете всё сами по готовому списку. "
        "Этот файл останется с вами как надёжная база: Мария добавит позиции на "
        "перспективу, чтобы вы всегда знали, какими средствами дополнить и усилить "
        "свой протокол.\n\n"
        "Для оплаты Персонального PDF-протокола (4 900 ₽) нажмите кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=link)]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()



_PRODUCT_NAMES = {
    "trial_99": "Тест-драйв сканера (7 проверок)",
    "audit_1000": "Базовый + Закрытый клуб (30 дней)",
    "protocol_2500": "Полный протокол + Клуб (30 дней)",
    "pdf_4900": "Персональный PDF-протокол",
}


async def generate_prodamus_link(telegram_id: int, amount: int, tariff_type: str) -> str:
    order_id = f"user-{telegram_id}-{tariff_type}-{int(datetime.now().timestamp())}"
    _start_param = f"pay_success_{order_id}"
    if len(_start_param) > 64:
        logger.error(f"❌ Telegram start param >64: {_start_param!r}")
    logger.info(f"💳 generate_prodamus_link: order_id={order_id!r}, amount={amount}, tariff={tariff_type!r}")

    bot_info = await bot.get_me()
    success_url = f"https://t.me/{bot_info.username}?start={_start_param}"

    data = {
        "do": "pay",
        "sys": PRODAMUS_SYS,
        "order_id": order_id,
        "urlSuccess": success_url,
        "urlReturn": f"https://t.me/{bot_info.username}",
        "products": [
            {
                "name": _PRODUCT_NAMES.get(tariff_type, f"Доступ к алгоритму ({tariff_type})"),
                "price": str(amount),
                "quantity": "1"
            }
        ]
    }

    signature = HmacHelper.create(data, PRODAMUS_SECRET)
    data["signature"] = signature
    flat_params = flatten_form(data)
    return f"{PRODAMUS_URL}?{urllib.parse.urlencode(flat_params)}"



@dp.callback_query(F.data == "pay_trial_99")
async def pay_trial_99(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    link = await generate_prodamus_link(telegram_id, 99, "trial_99")
    db.schedule_single_notification(telegram_id, "abandoned_cart", datetime.now() + timedelta(minutes=35))
    await callback.message.answer(
        "⚡ <b>Тест-драйв сканера (7 проверок) — 99 ₽</b>\n\n"
        "• Проверка 7 любых баночек (по фото или с ЗЯ, WB, Ozon) под профиль вашей кожи в течение 3 дней.\n"
        "• Только сканер, без доступа в закрытый клуб.\n\n"
        "После успешной оплаты доступ активируется автоматически 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 99 ₽", url=link)]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "pay_audit_30")
async def pay_audit_30(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    link = await generate_prodamus_link(telegram_id, 1000, "audit_1000")
    db.schedule_single_notification(telegram_id, "abandoned_cart", datetime.now() + timedelta(minutes=35))
    await callback.message.answer(
        "💳 <b>Базовый + Закрытый клуб (30 дней) — 1 000 ₽ (хит)</b>\n\n"
        "• Безлимит на проверки в ЗЯ, WB и Ozon на 30 дней.\n"
        "• Доступ в закрытый клуб Skin Protokol: готовые рабочие связки, ссылки на магазины со спецпредложениями, «черный список» пустышек и обратная связь.\n\n"
        "Нажмите кнопку ниже для перехода к оплате:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате 1 000 ₽", url=link)]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "pay_restore_90")
async def pay_restore_90(callback: types.CallbackQuery):
    telegram_id = callback.from_user.id
    link = await generate_prodamus_link(telegram_id, 2500, "protocol_2500")
    db.schedule_single_notification(telegram_id, "abandoned_cart", datetime.now() + timedelta(minutes=35))
    await callback.message.answer(
        "💎 <b>Полный протокол + Клуб (30 дней) — 2 500 ₽</b>\n\n"
        "• Всё, что в «Базовом» + Роман запоминает всю вашу косметичку, находит конфликты активов (чтобы не сжечь кожу) и показывает, чего не хватает в уходе.\n\n"
        "Нажмите кнопку ниже для перехода к оплате:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате 2 500 ₽", url=link)]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "start_diagnostics")
async def start_diagnostics(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    telegram_id = callback.from_user.id
    db.clear_user_tags(telegram_id)
    await state.clear()
    await state.set_state(Form.waiting_for_name)
    logger.info(f"📊 FUNNEL [survey_started] user={telegram_id}")
    await state.update_data(step_contrib={})
    await callback.message.answer(
        "Для начала напишите, как к вам обращаться:",
        reply_markup=get_mode_keyboard()
    )
    await callback.answer()


# =====================================================================
# Перехват кнопок Меню/Проверить во время анкеты — НЕ сбрасывают state
# =====================================================================

SURVEY_STATES = (
    Form.waiting_for_name,
    Form.survey,
    Form.waiting_for_medicine,
    Form.waiting_for_allergy,
    Form.waiting_for_category,
)


@dp.message(StateFilter(*SURVEY_STATES), F.text == "📋 Меню")
async def menu_while_in_survey(message: types.Message, state: FSMContext):
    await menu_handler(message, state)
    current = await state.get_state()
    if current == Form.waiting_for_name.state:
        await message.answer("👆 Введите имя буквами (без цифр), чтобы продолжить анкету:")
    elif current == Form.survey.state:
        await message.answer("👆 Вернёмся к анкете — выберите ответ ниже:")
    elif current == Form.waiting_for_medicine.state:
        await message.answer("👆 Напишите ваш вариант текстом:")
    elif current == Form.waiting_for_allergy.state:
        await message.answer("👆 Напишите название аллергена текстом:")
    elif current == Form.waiting_for_category.state:
        await message.answer(
            "👆 Выберите категорию средства:",
            reply_markup=get_category_4_keyboard()
        )
    logger.info(f"📊 FUNNEL [menu_during_survey] user={message.from_user.id} | state={current}")


@dp.message(StateFilter(*SURVEY_STATES), F.text == "🔍 Проверить новое средство")
async def scanner_while_in_survey(message: types.Message, state: FSMContext):
    current = await state.get_state()
    await message.answer(
        "👆 Сначала завершим анкету — она нужна, чтобы я понимал вашу кожу.\n\n"
        "Продолжим, где остановились?"
    )
    if current == Form.waiting_for_name.state:
        await message.answer("Введите имя буквами (без цифр):")
    elif current == Form.waiting_for_medicine.state:
        await message.answer("Напишите ваш вариант текстом:")
    elif current == Form.waiting_for_allergy.state:
        await message.answer("Напишите название аллергена текстом:")
    elif current == Form.waiting_for_category.state:
        await message.answer("Выберите категорию:", reply_markup=get_category_4_keyboard())
    logger.info(f"📊 FUNNEL [scanner_blocked_during_survey] user={message.from_user.id} | state={current}")


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


@dp.callback_query(StateFilter(Form.survey, Form.waiting_for_allergy), F.data.regexp(r"^back_\d+$"))
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
    await state.set_state(Form.survey)
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
            has_tag = any(t in tags for t in ["pharmacy_creams", "medicine", "medicine_skin", "pregnancy", "normal_exclusive", "medicine_other"])
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
            await callback.message.answer(
                "Напишите название компонента, на который у вас аллергия:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_6")]
                ])
            )
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
    _allergen_list = [a.strip() for a in re.split(r'[,;/]|\s+и\s+|\s+and\s+', allergy_text) if a.strip()]
    for _a in _allergen_list:
        _tag = f"allergy_{_a}"
        if _tag not in tags:
            tags.append(_tag)

    await state.update_data(tags=tags)
    await state.set_state(Form.survey)
    await message.answer(f"Записано: аллергия на «{', '.join(_allergen_list)}». Учту это при анализе.")
    await finish_survey(message, state)


def generate_skin_analysis(user_name: str, tags: list) -> str:
    """Генерация текста разбора кожи по тегам (с исправлением багов Блока 2)."""
    diagnosis_text = f"🤖 {user_name},\n\n"

    # === БЛОК 1: КРАСНЫЕ ФЛАГИ ===
    if any(t in tags for t in ["medicine", "medicine_skin"]):
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
    elif any(t in tags for t in ["acne", "comedones", "seboprofit", "pores"]):
        diagnosis_text += (
            "🌿 Фиксируем профицит себума и склонность к гиперкератозу. Наша цель — наладить "
            "качественный отток кожного сала, предотвратить окисление комедонов и снизить "
            "воспалительный потенциал, не допуская разрушения защитного барьера жестким умыванием.\n\n"
        )
    else:
        diagnosis_text += (
            "⚖️ Фиксируем нормальное состояние эпидермиса. Гидролипидная мантия функционирует "
            "стабильно, барьерные структуры не нарушены, компенсаторные реакции отсутствуют. "
            "Наша стратегия — поддерживать физиологичный баланс и предотвращать внешний стресс "
            "через грамотный подбор химических составов.\n\n"
        )

    # === БЛОК 3: СОСУДЫ ===
    if any(t in tags for t in ["reactive", "couperose"]):
        diagnosis_text += (
            "🩸 Сосудистая сетка и реактивность указывают на хрупкость капилляров и локальный "
            "иммунодефицит кожи. Мы исключим агрессивные триггеры, жесткие скрабы и "
            "сфокусируемся на успокаивающих компонентах.\n\n"
        )

    # === БЛОК 4: ЦЕЛЕВЫЕ ЗАДАЧИ ===
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

    return diagnosis_text.strip()


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

    logger.info(f"📊 FUNNEL [diagnostics_complete] user={telegram_id}")

    # Сообщение 1: сразу после 6-го вопроса анкеты (без расшифровки-диагноза)
    msg_1 = (
        f"✅ {user_name}, профиль твоей кожи сохранён — теперь Роман будет анализировать средства именно под тебя. "
        "Давай прямо сейчас проверим, что стоит у тебя на полке."
    )
    await message.answer(msg_1, reply_markup=get_mode_keyboard())

    await asyncio.sleep(0.5)

    # Сообщение 2: условия проверок и кнопки
    msg_2 = (
        "У тебя есть 2 бесплатные проверки. Просто сфотографируй обратную сторону любой банки (где указан состав - INCI) "
        "и отправь фото прямо сюда 👇 \n"
        "Нет банки под рукой? Скопируй состав (INCI) с WB, Ozon или ЗЯ и вставь текстом."
    )
    kb_post_survey = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Пример разбора (гель Garnier)", callback_data="demo_test_scan")],
        [InlineKeyboardButton(text="🎥 Как найти состав? (видео 1 мин)", callback_data="how_to_find_inci_video")]
    ])
    await message.answer(msg_2, reply_markup=kb_post_survey)

    try:
        from datetime import timedelta as _td
        db.schedule_single_notification(
            telegram_id,
            "no_composition_6h",
            datetime.now() + _td(hours=6)
        )
        logger.info(f"📅 no_composition_6h scheduled for {telegram_id}")
    except Exception as e:
        logger.warning(f"no_composition_6h schedule failed: {e}")

    await state.clear()


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


@dp.callback_query(F.data == "demo_test_scan")
async def handle_demo_test_scan(callback: types.CallbackQuery, state: FSMContext):
    logger.info(f"📊 FUNNEL [example_garnier_click] user={callback.from_user.id}")
    await callback.answer("Запускаю демо-разбор состава...")
    sample_inci = (
        "Aqua/water, sodium laureth sulfate, coco-betaine, Sodium chloride, glycerin, "
        "citrus limon fruit extract / lemon fruit extract, Ppg-5-ceteth-20, sodium hydroxide, "
        "ascorbyl glucoside, citric acid, panthenol, Polyquaternium-7, tocopheryl acetate, "
        "sodium benzoate, salicylic acid, linalool, Limonene, benzyl salicylate, "
        "parfum/fragrance. (f.I.L. Z289491/1)."
    )
    user = db.get_or_create_user(callback.from_user.id)
    await callback.message.answer(
        "🧪 <b>Демонстрационный разбор популярного средства:</b>\n"
        "<i>ГЕЛЬ-ПЕНКА ДЛЯ ЛИЦА GARNIER витамин С</i>\n\n"
        f"<code>{sample_inci}</code>",
        parse_mode="HTML"
    )
    await perform_analysis(callback.message, state, user, sample_inci, "wash", is_demo=True)


@dp.callback_query(F.data == "how_to_find_inci_video")
async def handle_how_to_find_inci_video(callback: types.CallbackQuery):
    await callback.answer()
    logger.info(f"📊 FUNNEL [how_to_find_inci_video_click] user={callback.from_user.id}")
    try:
        await callback.message.answer_video(
            video=MAIN_VIDEO_ID,
            caption="🎥 Видео-инструкция: как найти и сфотографировать состав (INCI) на упаковке или маркетплейсе."
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить видео ({e})")
        await callback.message.answer(
            "❌ Не удалось загрузить видео. Просто сфотографируйте мелкий шрифт с составом на обратной стороне банки или скопируйте его с сайта маркетплейса."
        )


async def _check_bad_bottles_alarm(message: types.Message, telegram_id: int, user_name: str):
    """Отключено по запросу: уведомление о риске для барьера не отправляется."""
    return


async def perform_analysis(message: types.Message, state: FSMContext, user: dict, composition: str, category: str, is_demo: bool = False):
    telegram_id = user["telegram_id"]
    user_name = await _resolve_user_name(message, state, user)

    logger.info(f"📊 FUNNEL [analysis_start] user={telegram_id} | cat={category} | len={len(composition)}")
    # 🔒 Жёсткая очистка контекста между анализами — чтобы старые составы/категории не протекали
    await state.update_data(pending_inci=None, photo_urls=[], category=None)
    logger.info(f"🧹 STATE CLEARED before analysis: user={telegram_id}, category={category!r}, comp_len={len(composition)}")

    lock = get_user_lock(telegram_id)
    if lock.locked():
        await message.answer("⏳ Подождите пару секунд, я уже анализирую предыдущий состав...")
        return

    async with lock:
        user = db.get_or_create_user(telegram_id)
        if not is_demo and not check_user_limits(user):
            db.schedule_retention_notifications(telegram_id)
            await send_paywall(message)
            return

        tariff = "free" if is_demo else get_user_tariff(user)
        logger.info(f"🟣 perform_analysis → analyze_composition: category={category!r}, tariff={tariff!r}, is_demo={is_demo}")

        if not is_demo and telegram_id not in UNLIMITED_IDS:
            limit_status = db.check_rate_limit(telegram_id)
            if limit_status:
                if limit_status == "day":
                    await message.answer("Вы проверили уже 40 средств за сегодня — давайте дадим коже и алгоритму передышку и продолжим аудит завтра! 🌿")
                else:
                    await message.answer("⏳ Слишком много запросов. Пожалуйста, подождите немного.")
                return

        if not is_demo:
            db.log_scan(telegram_id)
        tags = user.get("user_tags", [])

        # ⚠️ STATEFUL только для tariff="protocol" (unlimited + реальный 2500). Free/Audit — stateless.
        approved_routine = ""
        if tariff == "protocol":
            products = db.get_approved_products(telegram_id)
            if products:
                lines = []
                for p in products:
                    lines.append(f"Этап: {p['stage']} | Статус: {p['status']} | Активы: {p['key_actives']}")
                approved_routine = "\n".join(lines)
            logger.info(f"📊 STATEFUL MODE: approved_routine={len(approved_routine)} chars for user={telegram_id}, products={len(products) if products else 0}, tariff={tariff!r}")
        else:
            logger.info(f"📊 STATELESS MODE: approved_routine пустой для user={telegram_id}, tariff={tariff!r}")

        loading_msg = await message.answer("🔬 Анализирую состав...")

        try:
            result = await analyze_composition(composition, tags, user_name, category, tariff, approved_routine)
            clean_text = (result["verdict"] or "").strip()[:4000]
            stage = result["stage"]
            actives = result["key_actives"]
            status = result["status"]

            # 🚫 Не косметический состав — показываем шаблон, лимит НЕ списываем
            if status == "not_composition":
                await loading_msg.edit_text(clean_text)
                await state.update_data(category=None, photo_urls=[])
                return

            if status not in ANALYZABLE_STATUSES:
                await loading_msg.edit_text(
                    "⚠️ Модель не смогла выдать вердикт по этому составу. Попробуйте ещё раз — лимит не списан."
                )
                return

            record_id = None
            if not is_demo and category in ['wash', 'care', 'eyes'] and status in ['good', 'hydrophilic', 'conditional']:
                record_id = db.save_approved_product(telegram_id, composition, status, category, stage, actives)

            if not is_demo:
                db.save_analysis(telegram_id, composition, clean_text, status)
                await state.clear()
                try:
                    db.cancel_single_notification(telegram_id, "no_composition_6h")
                except Exception:
                    pass

                if status in ["bad", "hidden_threat"]:
                    db.increment_bad_bottles(telegram_id)
                else:
                    db.reset_bad_bottles(telegram_id)

            if not is_demo and status in ["bad", "hidden_threat"]:
                await _check_bad_bottles_alarm(message, telegram_id, user_name)

            is_unlimited = (telegram_id in UNLIMITED_IDS)
            has_sub = db.has_active_subscription(user)
            is_protocol = is_unlimited or (user.get("scanner_tariff") == "protocol_2500" and has_sub)

            if is_demo:
                await loading_msg.edit_text(clean_text)
                await message.answer(
                    "💡 <b>Это был демонстрационный разбор.</b> Ваш бесплатный лимит не списан!\n\n"
                    "Теперь проверьте свой крем. Выберите категорию средства 👇",
                    reply_markup=get_category_4_keyboard(),
                    parse_mode="HTML"
                )
                await state.set_state(Form.waiting_for_composition)
                return
            elif is_unlimited:
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
            logger.info(f"🎛 КНОПКИ perform_analysis: is_protocol={is_protocol}, record_id={record_id}, status={status!r}, category={category!r}, tariff={tariff!r}")
            if is_protocol and record_id:
                matrix = build_system_matrix(telegram_id)
                if matrix:
                    await message.answer(matrix, parse_mode="HTML")
            await state.update_data(category=None, photo_urls=[])

            if is_protocol and record_id:
                save_skip_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="✅ Оставляю в уходе",
                        callback_data=f"save_product:{record_id}"
                    ),
                    InlineKeyboardButton(
                        text="🔍 Ищу дальше",
                        callback_data=f"skip_product:{record_id}"
                    ),
                ]])
                await message.answer(
                    "Добавить это средство в вашу базу ухода?",
                    reply_markup=save_skip_kb
                )
            else:
                await message.answer(
                    "Проверим следующее средство? Выберите категорию 👇",
                    reply_markup=get_category_4_keyboard()
                )

        except Exception as e:
            logger.error(f"Ошибка анализа: {e}")
            await loading_msg.edit_text("❌ Техническая ошибка. Попробуйте ещё раз — лимит не списан.")


PHOTO_HINT = "📸 Фото состава крупно, без бликов (круглые баночки — с 2-3 сторон). Или скопируйте состав из интернета!"


@dp.callback_query(F.data.startswith("cat_"))
async def process_category_choice(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass

    await safe_delete(callback.message)
    cat_data = callback.data.split("_")
    cat_type = cat_data[1]
    logger.info(f"🟣 process_category_choice: callback_data={callback.data!r} → cat_type={cat_type!r}")
    

    data = await state.get_data()
    pending_inci = data.get("pending_inci")

    await state.update_data(category=cat_type)

    if pending_inci:
        user = db.get_or_create_user(callback.from_user.id)
        await state.update_data(pending_inci=None)
        await callback.message.answer("Категория выбрана. Приступаю к анализу!")
        await perform_analysis(callback.message, state, user, pending_inci, cat_type)
    elif data.get("photo_urls"):
        await callback.message.answer(
            "✅ Категория выбрана.\n\nНажмите «Анализировать», чтобы запустить анализ.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔬 Анализировать", callback_data="analyze_photos")]
            ])
        )
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


@dp.callback_query(F.data == "analyze_photos")
async def analyze_photos(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass

    telegram_id = callback.from_user.id
    user = db.get_or_create_user(telegram_id)
    data = await state.get_data()

    # 🔒 Очистка после чтения — чтобы следующий разбор начался с чистого листа
    await state.update_data(pending_inci=None, category=None)
    photo_urls = data.get("photo_urls", [])
    category_fsm = data.get("category", None)

    if not photo_urls:
        await callback.message.answer("❌ Фото не найдены. Отправьте их ещё раз.")
        return

    if not check_user_limits(user):
        db.schedule_retention_notifications(telegram_id)
        await send_paywall(callback.message)
        return

    lock = get_user_lock(telegram_id)
    if lock.locked():
        await callback.message.answer("⏳ Подождите пару секунд, я уже анализирую предыдущий состав...")
        return

    async with lock:
        user = db.get_or_create_user(telegram_id)
        if not check_user_limits(user):
            db.schedule_retention_notifications(telegram_id)
            await send_paywall(callback.message)
            return

        if telegram_id not in UNLIMITED_IDS:
            limit_status = db.check_rate_limit(telegram_id)
            if limit_status:
                if limit_status == "day":
                    await callback.message.answer("Вы проверили уже 40 средств за сегодня — давайте дадим коже и алгоритму передышку и продолжим аудит завтра! 🌿")
                else:
                    await callback.message.answer("⏳ Слишком много запросов. Пожалуйста, подождите немного.")
                return

        user_name = await _resolve_user_name(callback.message, state, user)
        category = category_fsm
        tariff = get_user_tariff(user)
        logger.info(f"🟣 analyze_photos → analyze_composition: category={category!r}, tariff={tariff!r}")

        loading_msg = await callback.message.answer("🔍 Распознаю текст с фотографии...")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        try:
            extracted_text = await extract_text_from_photos(photo_urls)
        except Exception as e:
            logger.error(f"Ошибка OCR в analyze_photos: {e}")
            await loading_msg.edit_text("❌ Техническая ошибка распознавания фото. Попробуйте ещё раз — лимит не списан.")
            await state.update_data(category=None, photo_urls=[])
            return

        if not validate_inci_text(extracted_text):
            logger.info(
                f"📊 FUNNEL [composition_rejected] user={telegram_id} | "
                f"src=analyze_photos | ocr_len={len(extracted_text)} | "
                f"text={extracted_text[:60]!r}"
            )
            await state.update_data(photo_urls=[])
            await loading_msg.edit_text(
                "Не удалось распознать полный список компонентов (INCI).\n\n"
                "Пожалуйста, убедитесь, что на фото четко виден текст после слова Состав / Ingredients "
                "без обрезанных краев (либо скопируйте состав текстом из карточки товара).\n\n"
                "Ваша попытка не списалась!"
            )
            return

        if not category_fsm:
            await state.update_data(pending_inci=extracted_text, photo_urls=[])
            await loading_msg.edit_text(
                "Состав получен! Уточните, к какой категории относится это средство, чтобы я применил нужную матрицу 👇",
                reply_markup=get_category_4_keyboard()
            )
            await state.set_state(Form.waiting_for_category)
            return

        db.log_scan(telegram_id)
        await loading_msg.edit_text("🔬 Анализирую состав...")

        tags = user.get("user_tags", [])

               # ⚠️ STATEFUL только для tariff="protocol" (unlimited + реальный 2500). Free/Audit — stateless, БЕЗ истории.
        approved_routine = ""
        if tariff == "protocol":
            products = db.get_approved_products(telegram_id)
            if products:
                lines = []
                for p in products:
                    lines.append(f"Этап: {p['stage']} | Статус: {p['status']} | Активы: {p['key_actives']}")
                approved_routine = "\n".join(lines)
            logger.info(f"📊 STATEFUL MODE: approved_routine={len(approved_routine)} chars for user={telegram_id}, tariff={tariff!r}")
        else:
            logger.info(f"📊 STATELESS MODE: approved_routine пустой для user={telegram_id}, tariff={tariff!r}")

        try:
            result = await analyze_composition(extracted_text, tags, user_name, category, tariff, approved_routine)
        except Exception as e:
            logger.error(f"Ошибка анализа фото в analyze_photos: {e}")
            await loading_msg.edit_text("❌ Техническая ошибка при анализе состава. Попробуйте ещё раз — лимит не списан.")
            await state.update_data(category=None, photo_urls=[])
            return

        clean_text = (result["verdict"] or "").strip()[:4000]
        stage = result["stage"]
        actives = result["key_actives"]
        status = result["status"]

        # 🚫 Не косметический состав — показываем шаблон, лимит НЕ списываем
        if status == "not_composition":
            await loading_msg.edit_text(clean_text)
            await state.update_data(category=None, photo_urls=[])
            return

        if status not in ANALYZABLE_STATUSES:
            await loading_msg.edit_text(
                "⚠️ Модель не смогла выдать вердикт по этому составу. Попробуйте ещё раз — лимит не списан."
            )
            return

        record_id = None
        if category in ['wash', 'care', 'eyes'] and status in ['good', 'hydrophilic', 'conditional']:
            record_id = db.save_approved_product(telegram_id, f"[ФОТО] {extracted_text[:100]}...", status, category, stage, actives)

        db.save_analysis(telegram_id, f"[ФОТО] {extracted_text[:100]}...", clean_text, status)
        await state.clear()
        try:
            db.cancel_single_notification(telegram_id, "no_composition_6h")
        except Exception:
            pass

        if status in ["bad", "hidden_threat"]:
            db.increment_bad_bottles(telegram_id)
            await _check_bad_bottles_alarm(callback.message, telegram_id, user_name)
        else:
            db.reset_bad_bottles(telegram_id)

        is_unlimited = (telegram_id in UNLIMITED_IDS)
        has_sub = db.has_active_subscription(user)
        is_protocol = is_unlimited or (user.get("scanner_tariff") == "protocol_2500" and has_sub)

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
                return
            elif remaining <= 0:
                db.schedule_retention_notifications(telegram_id)
                await send_paywall(callback.message)
                await state.update_data(category=None, photo_urls=[])
                return
        else:
            await loading_msg.edit_text(clean_text)
        logger.info(f"🎛 КНОПКИ analyze_photos: is_protocol={is_protocol}, record_id={record_id}, status={status!r}, category={category!r}, tariff={tariff!r}")
        if is_protocol and record_id:
            matrix = build_system_matrix(telegram_id)
            if matrix:
                await callback.message.answer(matrix, parse_mode="HTML")

        await state.update_data(category=None, photo_urls=[])

        if is_protocol and record_id:
            save_skip_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Оставляю в уходе",
                    callback_data=f"save_product:{record_id}"
                ),
                InlineKeyboardButton(
                    text="🔍 Ищу дальше",
                    callback_data=f"skip_product:{record_id}"
                ),
            ]])
            await callback.message.answer(
                "Добавить это средство в вашу базу ухода?",
                reply_markup=save_skip_kb
            )
        else:
            await callback.message.answer(
                "Проверим следующее средство? Выберите категорию 👇",
                reply_markup=get_category_4_keyboard()
            )


@dp.message(StateFilter(Form.waiting_for_composition))
async def process_composition(message: types.Message, state: FSMContext):
    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)
    text = (message.text or "").strip()

    # Если нажата кнопка нижнего меню — перенаправляем на соответствующий обработчик
    if text == "📋 Меню":
        await menu_handler(message, state)
        return
    if text == "🔍 Проверить новое средство":
        await gather_care_start(message, state)
        return
    if text in CHECKUP_VARIANTS:
        await handle_checkup_menu_btn(message)
        return

    data = await state.get_data()

    # Если пользователь отправляет текст состава, сбрасываем старые фото
    if data.get("photo_urls"):
        await state.update_data(photo_urls=[])

    if not check_user_limits(user):
        db.schedule_retention_notifications(telegram_id)
        await send_paywall(message)
        return

    composition = sanitize_input(text, max_len=2000)

    if not validate_inci_text(composition):
        logger.info(
            f"📊 FUNNEL [composition_rejected] user={telegram_id} | "
            f"src=process_composition | len={len(composition)} | "
            f"text={composition[:60]!r}"
        )
        await message.answer(
            "Не удалось распознать полный список компонентов (INCI).\n\n"
            "Пожалуйста, убедитесь, что текст содержит список ингредиентов после слова Состав / Ingredients, "
            "либо пришлите фото этикетки.\n\n"
            "Ваша попытка не списалась!"
        )
        return

    category_fsm = data.get("category", None)

    if not category_fsm:
        logger.info(f"📊 FUNNEL [composition_sent_text] user={telegram_id} | len={len(composition)}")
        await state.update_data(pending_inci=composition, photo_urls=[])
        await message.answer(
            "Состав получен! Уточните, к какой категории относится это средство, чтобы я применил нужную матрицу 👇",
            reply_markup=get_category_4_keyboard()
        )
        await state.set_state(Form.waiting_for_category)
        return

    await perform_analysis(message, state, user, composition, category_fsm)


@dp.message(F.text == "📋 Меню")
async def menu_handler(message: types.Message, state: FSMContext):
    user = db.get_or_create_user(message.from_user.id)
    tid = message.from_user.id
    user_name = db.get_user_name(tid) or "Клиент"

    logger.info(f"📊 FUNNEL [menu_opened] user={tid}")

    text_q = f"Привет, Мария! Вопрос по уходу. Меня зовут: {user_name}. Код анкеты: #{tid}"
    url_q = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_q)}"

    await message.answer(
        "📋 <b>Меню</b>\n\nВыберите раздел:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
                InlineKeyboardButton(text="💳 Тарифы", callback_data="show_tariffs"),
            ],
            [
                InlineKeyboardButton(text="📄 PDF-протокол", callback_data="pay_pdf_4900"),
                InlineKeyboardButton(text="📦 Бокс", callback_data="menu_box"),
            ],
            [
                InlineKeyboardButton(text="💬 Задать вопрос Марии", callback_data="menu_mariya"),
            ],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "menu_profile")
async def menu_profile(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    telegram_id = callback.from_user.id
    user = db.get_or_create_user(telegram_id)
    user_name = db.get_user_name(telegram_id) or "Клиент"
    tags_str = _translate_tags(user.get("user_tags", []))

    if telegram_id in UNLIMITED_IDS:
        count = db.count_approved_products(telegram_id)
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"👑 Режим: Безлимитный доступ (владелец)\n"
            f"Одобренных средств в вашей схеме: {count} шт."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Моя собранная база", callback_data="btn_my_base")]
        ])
        await callback.message.answer(text, reply_markup=kb)
    elif not db.has_active_subscription(user):
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Статус доступа: Неактивен (Использовано бесплатных проверок: {user.get('free_scans_used', 0)} из 2).\n\n"
            f"<b>Выберите формат работы:</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Тест-драйв (7 проверок) — 99 ₽", callback_data="pay_trial_99")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    elif user.get("scanner_tariff") == "trial_99":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y %H:%M')
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: ⚡ Тест-драйв сканера (7 проверок, без клуба)\n"
            f"Активен до: {exp_date}\n\n"
            f"Продлить на 30 дней с доступом в закрытый клуб:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")],
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await callback.message.answer(text, reply_markup=kb)
    elif user.get("scanner_tariff") == "audit_1000":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: 💳 Базовый + Закрытый клуб\n"
            f"Активен до: {exp_date}\n\n"
            f"Продлить или повысить тариф:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")],
            [InlineKeyboardButton(text="💳 Продлить Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")]
        ])
        await callback.message.answer(text, reply_markup=kb)
    elif user.get("scanner_tariff") == "protocol_2500":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        count = db.count_approved_products(telegram_id)
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: 💎 Полный протокол + Закрытый клуб\n"
            f"Активен до: {exp_date}\n"
            f"Одобренных средств в вашей схеме: {count} шт."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Моя собранная база", callback_data="btn_my_base")],
            [InlineKeyboardButton(text="💎 Продлить Полный протокол (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await callback.message.answer(text, reply_markup=kb)
    elif user.get("scanner_tariff") == "pdf_4900":
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y')
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Тариф: 👑 VIP PDF-протокол + Закрытый клуб\n"
            f"Активен до: {exp_date}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Перейти к Марии", url=f"https://t.me/{MARIYA_USERNAME}")],
            [InlineKeyboardButton(text="💎 Продлить Полный протокол (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")]
        ])
        await callback.message.answer(text, reply_markup=kb)
    else:
        exp_date = datetime.fromisoformat(user["scanner_expires_at"]).strftime('%d.%m.%Y') if user.get("scanner_expires_at") else "—"
        text = (
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Статус доступа: Активен до {exp_date}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Полный протокол + Клуб (30 дн.) — 2 500 ₽", callback_data="pay_restore_90")],
            [InlineKeyboardButton(text="💳 Базовый + Клуб (30 дн.) — 1 000 ₽", callback_data="pay_audit_30")]
        ])
        await callback.message.answer(text, reply_markup=kb)

    await callback.answer()


@dp.callback_query(F.data == "menu_box")
async def menu_box(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    tid = callback.from_user.id
    user = db.get_or_create_user(tid)
    user_name = db.get_user_name(tid) or "Клиент"

    try:
        db.save_lead(
            telegram_id=tid,
            username=user.get("username") or "",
            tags=user.get("user_tags", []),
            photos=[]
        )
    except Exception:
        pass

    try:
        await send_client_card_to_admin(user, log_lead=False)
    except Exception:
        pass

    text_box = f"Привет, хочу Индивидуальный Бокс (от 9 000 ₽)! Меня зовут: {user_name}. Код анкеты: #{tid}"
    url_box = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_box)}"

    await callback.message.answer(
        "📦 <b>Индивидуальный Бокс (от 9 000 ₽)</b> — премиальный формат ухода «под ключ». "
        "Вы приобретаете не просто набор косметики, а готовую систему — ваш личный "
        "работающий протокол. Вы получаете эстетичную коробку с полноразмерными средствами, "
        "подобранными строго под вашу кожу, и подробную инструкцию. В эту стоимость уже "
        "включено ведение от Марии: она будет на связи, чтобы контролировать динамику и "
        "довести вас до результата. Вам не нужно ходить по магазинам — забираете посылку "
        "и начинаете свой путь к здоровой коже по протоколу.\n\n"
        "Для консультации с Марией нажмите кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Заказать Индивидуальный Бокс", url=url_box)]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_mariya")
async def menu_mariya(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    tid = callback.from_user.id
    user = db.get_or_create_user(tid)
    user_name = db.get_user_name(tid) or "Клиент"

    try:
        db.save_lead(
            telegram_id=tid,
            username=user.get("username") or "",
            tags=user.get("user_tags", []),
            photos=[]
        )
    except Exception:
        pass

    try:
        await send_client_card_to_admin(user, log_lead=False)
    except Exception:
        pass

    text_q = f"Привет, Мария! Вопрос по уходу. Меня зовут: {user_name}. Код анкеты: #{tid}"
    url_q = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_q)}"

    await callback.message.answer(
        "💬 Напишите Марии напрямую — она косметолог проекта и поможет с вопросами, "
        "которые выходят за рамки химического анализа.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать Марии", url=url_q)]
        ])
    )
    await callback.answer()


KNOWN_BUTTONS = {"📋 Меню", "🔍 Проверить новое средство", "💬 Задать вопрос Марии"}.union(CHECKUP_VARIANTS)

@dp.message(F.text & ~F.text.startswith('/') & ~F.text.in_(KNOWN_BUTTONS))
async def handle_text_no_state(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    telegram_id = message.from_user.id
    user = db.get_or_create_user(telegram_id)

    if user.get("survey_done", 0) == 0:
        await message.answer("Пожалуйста, сначала пройдите короткую диагностику.", reply_markup=get_start_keyboard())
        return

    # Перехват сообщений об оплате (если клиент оплатил, но закрыл вкладку эквайринга)
    lower_text = (message.text or "").strip().lower()
    paid_keywords = ["оплатил", "оплатила", "оплачено", "чек", "оплата", "деньги списали", "я оплатил", "я оплатила"]
    if any(k in lower_text for k in paid_keywords) and len(lower_text) < 40:
        user_name = db.get_user_name(telegram_id) or "Клиент"
        text_q = f"Здравствуйте, Мария! Я оплатил(а) доступ, но бот не открылся автоматически. Мой ID: #{telegram_id}, Имя: {user_name}."
        url_q = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(text_q)}"
        await message.answer(
            "⏳ <b>Вижу ваше сообщение об оплате!</b>\n\n"
            "Если вы только что оплатили на странице платёжной системы, обязательно нажмите на ней кнопку "
            "<b>«Вернуться в магазин»</b> (или «Назад в магазин») — она мгновенно передаст подтверждение в бота.\n\n"
            "Если вы уже закрыли вкладку оплаты или возникла задержка банка — нажмите кнопку ниже, "
            "чтобы написать Марии, и мы вручную подключим вам доступ за 1 минуту 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Написать Марии (ручная активация)", url=url_q)]
            ]),
            parse_mode="HTML"
        )
        return

    if not check_user_limits(user):
        await send_paywall(message)
        return

    composition = sanitize_input(message.text or "", max_len=2000)
    if not validate_inci_text(composition):
        logger.info(
            f"📊 FUNNEL [composition_rejected] user={telegram_id} | "
            f"src=handle_text_no_state | len={len(composition)} | "
            f"text={composition[:60]!r}"
        )
        await message.answer(
            "Не удалось распознать полный список компонентов (INCI).\n\n"
            "Пожалуйста, убедитесь, что текст содержит список ингредиентов после слова Состав / Ingredients, "
            "либо пришлите фото этикетки.\n\n"
            "Ваша попытка не списалась!"
        )
        return

    logger.info(f"📊 FUNNEL [composition_sent_text] user={telegram_id} | len={len(composition)}")
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

    await state.update_data(photo_urls=[b64])
    logger.info(f"📊 FUNNEL [composition_sent_photo] user={telegram_id} | photos=1")

    await message.answer(
        "📸 Фото добавлено.\n\n"
        "Можете отправить ещё фото или нажмите «Анализировать».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔬 Анализировать", callback_data="analyze_photos")]
        ])
    )
    await state.set_state(Form.waiting_for_composition)

@dp.message(F.video | F.voice | F.audio | F.sticker | F.document)
async def reject_other_media_no_state(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    await message.answer(
        "Отправьте обычную фотографию состава (или скриншот) либо скопируйте список ингредиентов текстом. "
        "Попытка сохранена."
    )


@dp.callback_query(F.data == "ignore")
async def cb_ignore(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass


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


@dp.callback_query(F.data.startswith("save_product:"))
async def handle_save_product(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer("✅ Средство оставлено в уходе")
    except Exception:
        pass
    await safe_delete(callback.message)
    await callback.message.answer(
        "✅ Отлично, средство сохранено в вашей базе ухода. Что проверим дальше?",
        reply_markup=get_category_4_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)


@dp.callback_query(F.data.startswith("skip_product:"))
async def handle_skip_product(callback: types.CallbackQuery, state: FSMContext):
    try:
        record_id = int(callback.data.split(":", 1)[1])
        db.delete_approved_product(record_id)
        await callback.answer("🗑 Средство удалено из базы")
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка в данных кнопки", show_alert=True)
        return
    await safe_delete(callback.message)
    await callback.message.answer(
        "Понял, удалил из базы. Что проверим дальше?",
        reply_markup=get_category_4_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)


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
        [InlineKeyboardButton(text="💳 Перейти на базовый Аудит (30 дн.) — 1000 ₽", callback_data="pay_audit_30")]
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


# =====================================================================
# АДМИН-ПУЛЬТ: поиск клиента по хэштегу #telegram_id
# =====================================================================

# =====================================================================
# АДМИН-ПАНЕЛЬ /admin
# =====================================================================

# =====================================================================
# МЕНЮ (📋) — Профиль / Тарифы / PDF / Бокс / Мария
# =====================================================================


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


async def prodamus_webhook(request: web.Request):
    try:
        # Получаем плоские данные из POST-запроса
        post_data = dict(await request.post())
        logger.info(
            f"📥 Prodamus webhook received: order_num={post_data.get('order_num')!r}, "
            f"order_id={post_data.get('order_id')!r}, "
            f"payment_status={post_data.get('payment_status')!r}"
        )
        received_sign = (
            post_data.pop("signature", None)
            or post_data.pop("sign", None)
            or request.headers.get("Sign")
            or request.headers.get("sign")
        )
        
        if not received_sign:
            return web.Response(status=403, text="No signature")

        # Восстанавливаем вложенную структуру для проверки подписи
        nested_data = parse_bracket_form(post_data)

        # Проверяем подпись через официальную библиотеку
        if not HmacHelper.verify(nested_data, PRODAMUS_SECRET, received_sign):
            logger.warning("Prodamus: неверная подпись")
            return web.Response(status=403, text="Invalid signature")

        # Если оплата успешна (ключ может быть payment_status или status - обычно payment_status)
        if post_data.get("payment_status") == "success":
            # В Продамус наш ID заказа (user-<tid>-<tariff>-<ts>) возвращается в поле 'order_num'
            # (а в 'order_id' Продамус передает свой собственный UUID платежа)
            order_id = ""
            for candidate in [post_data.get("order_num"), post_data.get("order_id")]:
                if candidate and str(candidate).startswith("user-"):
                    order_id = str(candidate)
                    break
            if not order_id:
                order_id = str(post_data.get("order_num") or post_data.get("order_id") or "")

            if order_id and not db.check_payment_exists(order_id):
                # order_id = "user-<tid>-<tariff>-<ts>"  (split by "-")
                parts = order_id.split("-")
                if len(parts) >= 3 and parts[0] == "user":
                    telegram_id = int(parts[1])
                    tariff_type = parts[2]
                    amount = int(float(post_data.get("sum", 0)))

                    db.save_payment(order_id, telegram_id, amount, tariff_type)
                    db.clear_channel_kicked(telegram_id)
                    db.cancel_single_notification(telegram_id, "abandoned_cart")

                    if tariff_type == "pdf_4900":
                        db.cancel_retention_notifications(telegram_id)
                        db.set_maria_order_status(telegram_id, "completed_vip")
                        db.update_user_subscription(telegram_id, "pdf_4900", 30)
                        _label = "PDF-протокол"
                        try:
                            await bot.send_message(
                                telegram_id,
                                "🎉 Оплата PDF-протокола (4 900 ₽) успешно получена!\n\n"
                                "Мария уже получила данные вашей диагностики. Нажмите на кнопку ниже, "
                                "чтобы перейти в личный диалог и согласовать комфортный бюджет на покупку средств 👇",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="💬 Перейти к Марии", url=f"https://t.me/{MARIYA_USERNAME}")]
                                ])
                            )
                        except Exception:
                            pass
                        try:
                            user_name = db.get_user_name(telegram_id) or "Клиент"
                            await bot.send_message(
                                ADMIN_ID,
                                f"🎉 ОПЛАЧЕН PDF-ПРОТОКОЛ (4 900 ₽)\nКлиент: {user_name}\nID: {telegram_id}"
                            )
                        except Exception:
                            pass
                    elif tariff_type == "trial_99":
                        new_expires = db.update_user_subscription(telegram_id, "trial_99", 3)
                        db.cancel_retention_notifications(telegram_id)
                        db.schedule_renewal_notification(telegram_id, "trial_99", new_expires)
                        _label = "Тест-драйв"
                        try:
                            _exp_str = datetime.fromisoformat(new_expires).strftime("%d.%m.%Y %H:%M")
                            await bot.send_message(
                                telegram_id,
                                f"🎉 Оплата тест-драйва получена! Сканер продлён и активен до <b>{_exp_str}</b> без ограничений. Проверьте /profile.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    else:
                        new_expires = db.update_user_subscription(telegram_id, tariff_type, 30)
                        db.cancel_retention_notifications(telegram_id)
                        db.schedule_renewal_notification(telegram_id, tariff_type, new_expires)
                        _label = "Базовый + Клуб" if tariff_type == "audit_1000" else "Полный протокол + Клуб"
                        try:
                            _exp_str = datetime.fromisoformat(new_expires).strftime("%d.%m.%Y")
                            await bot.send_message(
                                telegram_id,
                                f"🎉 Оплата успешно получена! Доступ продлён и активен до <b>{_exp_str}</b>. Проверьте /profile.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                    # Выдача одноразовой ссылки в закрытый клуб (ТОЛЬКО для тарифов с клубом, без тест-драйва)
                    if tariff_type != "trial_99":
                        _u = db.get_user(telegram_id)
                        if _u and _u.get("scanner_expires_at"):
                            try:
                                _exp = datetime.fromisoformat(_u["scanner_expires_at"])
                                await grant_channel_access(telegram_id, _label, _exp)
                            except Exception as e:
                                logger.error(f"grant_channel_access (webhook) failed: {e}")
                
        return web.Response(text="success")
    except Exception as e:
        logger.error(f"Prodamus webhook error: {e}")
        return web.Response(status=500, text="Error")

async def main():
    asyncio.create_task(retention_worker())
    asyncio.create_task(channel_worker())

    try:
        await bot.set_my_commands([
            BotCommand(command="start",   description="Запуск"),
            BotCommand(command="profile", description="Профиль и тариф"),
            BotCommand(command="tariffs", description="Тарифы"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось установить команды: {e}")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    # --- ВСТАВЛЯЕМ БЛОК ВЕБХУКА ПРОДАМУС ---
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="BotHost Webhook Server Running OK"))
    app.router.add_get("/prodamus_webhook", lambda r: web.Response(text="BotHost Webhook Server Running OK"))
    app.router.add_post("/prodamus_webhook", prodamus_webhook)
    app.router.add_post("/", prodamus_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Берем порт из переменных окружения (Bothost обычно выдает его сам)
    port = int(os.getenv("PORT", 8080)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Вебхук-сервер запущен на порту {port}")
    # ---------------------------------------

    logger.info("🚀 Бот запущен в режиме polling")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("⛔ Бот остановлен")



