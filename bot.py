import os
import io
import re
import time
import asyncio
import logging
import urllib.parse
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
)
from dotenv import load_dotenv

import database as db
from ai_analyzer import (
    validate_inci_before_llm,
    extract_inci_from_image_bytes,
    analyze_cosmetic_inci
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 1745568601))
MARIYA_ID = int(os.getenv("MARIYA_ID", 7875791813))
ADMIN_ID = int(os.getenv("ADMIN_ID", MARIYA_ID))

CIRCLE_VIDEO_ID = "DQACAgIAAxkBAAIBM2rH78dR-IOML_w1nFX_c4uneVzeAALEpQAC4NRASuj2IqXHtc6tPQQ"

OFFER_URL = "https://docs.google.com/document/d/1FOaPp-S5rpJSlg2qWukQ3cyBCjxhEbfd0LCLaM4gE08/edit?usp=sharing"
CONSENT_URL = "https://docs.google.com/document/d/1OzOPwWki-_hdjY30R0A9Wp3xsdBGvrlnNPRT-IhgynA/edit?usp=sharing"

MARIYA_USERNAME = "miroslavskayaboks"

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db.init_db()

USER_SCAN_LOCKS: dict[int, asyncio.Lock] = {}
USER_RATE_TIMESTAMPS: dict[int, list[float]] = {}
HANDLED_MEDIA_GROUPS: dict[str, float] = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_SCAN_LOCKS:
        USER_SCAN_LOCKS[user_id] = asyncio.Lock()
    return USER_SCAN_LOCKS[user_id]


def check_rate_limit_3_per_min(user_id: int) -> bool:
    now = time.time()
    history = USER_RATE_TIMESTAMPS.get(user_id, [])
    history = [t for t in history if now - t < 60.0]
    if len(history) >= 3:
        USER_RATE_TIMESTAMPS[user_id] = history
        return False
    history.append(now)
    USER_RATE_TIMESTAMPS[user_id] = history
    return True


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


class SurveyStates(StatesGroup):
    waiting_for_name = State()
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()
    q5 = State()
    waiting_for_medical_text = State()
    q6 = State()
    waiting_for_allergy_text = State()
    waiting_for_inci = State()


SURVEY_ACTIVE_STATES = {
    SurveyStates.waiting_for_name.state,
    SurveyStates.q1.state,
    SurveyStates.q2.state,
    SurveyStates.q3.state,
    SurveyStates.q4.state,
    SurveyStates.q5.state,
    SurveyStates.waiting_for_medical_text.state,
    SurveyStates.q6.state,
    SurveyStates.waiting_for_allergy_text.state,
}


def sanitize_user_name(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[<>\[\]\{\}]", "", text)
    cleaned = re.sub(r"[^a-zA-Zа-яА-ЯёЁ\s\-]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:30]


def sanitize_short_input(text: str, max_len: int = 100) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[<>\[\]\{\}\r\n]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


Q1_OPTIONS = [
    ("Акне / активные воспаления", "Проблемная"),
    ("Закрытые комедоны / бугорки", "Проблемная"),
    ("Сильная жирность днем", "Себопрофицит"),
    ("Черные точки / поры", "Расширенные поры"),
    ("Сухость / стянутость", "Сухость"),
    ("Шелушения", "Шелушения"),
    ("Пигментация / постакне", "Пигментация"),
    ("Морщины / потеря тонуса", "Возрастные изменения"),
    ("Покраснения / купероз", "Купероз и покраснения"),
    ("Ничего не беспокоит", "EXCLUSIVE_NONE"),
]

Q2_OPTIONS = [
    ("Комфортно, не стягивает", None),
    ("Терпимо, хочется нанести крем", None),
    ("Сильно стягивает («пергамент»)", "Сломан барьер"),
    ("Сразу блестит от жирности", "Себопрофицит"),
]

Q3_OPTIONS = [
    ("Нормально (без сухости и блеска)", None),
    ("Блестит только Т-зона", None),
    ("Сильно блестит всё лицо", "CONDITIONAL_SHINE"),
    ("Сохнет / шелушится", "Сломан барьер"),
]

Q4_OPTIONS = [
    ("Спокойная (редко краснеет)", None),
    ("Слегка чувствительная", None),
    ("Реактивная (горит, краснеет)", "Реактивность"),
]

Q5_OPTIONS = [
    ("Аптечные мази (Скинорен и др.)", "Реактивность"),
    ("Гормональные нарушения", "Медицина"),
    ("Кожные заболевания (розацеа и др.)", "Медицина"),
    ("Беременность / лактация", "Беременность"),
    ("Ничего из перечисленного", "EXCLUSIVE_NONE"),
    ("Другое", "OTHER_MED"),
]


def build_q1_keyboard(selected_indices: list[int]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, (label, _) in enumerate(Q1_OPTIONS):
        mark = "✅ " if idx in selected_indices else ""
        buttons.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"q1_toggle:{idx}")])
    buttons.append([InlineKeyboardButton(text="Далее ➔", callback_data="q1_next")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_single_choice_keyboard(options: list, prefix: str, show_back: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    for idx, (label, _) in enumerate(options):
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{idx}")])
    if show_back:
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"back_to_{prefix}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_q5_keyboard(selected_indices: list[int]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, (label, _) in enumerate(Q5_OPTIONS):
        mark = "✅ " if idx in selected_indices else ""
        buttons.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"q5_toggle:{idx}")])
    buttons.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_q4"),
        InlineKeyboardButton(text="Далее ➔", callback_data="q5_next")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_q6_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Нет, аллергии нет", callback_data="q6:no")],
        [InlineKeyboardButton(text="Да, есть", callback_data="q6:yes")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_q5")]
    ])


def get_categories_keyboard(extra_top_button: InlineKeyboardButton = None) -> InlineKeyboardMarkup:
    rows = []
    if extra_top_button:
        rows.append([extra_top_button])
    rows.extend([
        [InlineKeyboardButton(text="🫧 Смываемое (умывалки, гидрофильное масло)", callback_data="cat:wash")],
        [InlineKeyboardButton(text="🧴 Несмываемое (тоники, сыворотки, кремы, SPF)", callback_data="cat:care")],
        [InlineKeyboardButton(text="👁 Уход для век (кремы и сыворотки)", callback_data="cat:eyes")],
        [InlineKeyboardButton(text="🦵 Уход для тела (гели, лосьоны, кремы)", callback_data="cat:body")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_step3_fork_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Собрать уход с Романом", callback_data="branch_roman")],
        [InlineKeyboardButton(text="👑 Хочу готовый уход от Марии", callback_data="branch_maria:1")],
        [InlineKeyboardButton(text="🤷‍♀️ У меня сейчас нет ухода", callback_data="branch_maria:2")],
    ])


def get_paywall_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Аудит (30 дней) — 1000 ₽", callback_data="buy:audit_1000")],
        [InlineKeyboardButton(text="💎 Сборка протокола (30 дн.) — 2500 ₽", callback_data="buy:protocol_2500")],
        [InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="branch_maria:1")],
    ])


def get_step6_maria_keyboard(user_name: str, telegram_id: int) -> InlineKeyboardMarkup:
    safe_name = user_name or "Клиент"
    box_text = f"Привет, хочу Индивидуальный Бокс (от 9 000 ₽)! Меня зовут: {safe_name}. Код анкеты: #{telegram_id}"
    ask_text = f"Привет! Хочу проконсультироваться по поводу Персонального PDF-протокола / Бокса. Меня зовут: {safe_name}. Код анкеты: #{telegram_id}"

    box_url = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(box_text)}"
    ask_url = f"https://t.me/{MARIYA_USERNAME}?text={urllib.parse.quote(ask_text)}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить PDF-протокол — 4 900 ₽", callback_data="buy:pdf_4900")],
        [InlineKeyboardButton(text="📦 Заказать Индивидуальный Бокс (от 9 000 ₽)", url=box_url)],
        [InlineKeyboardButton(text="💬 Задать вопрос Марии по PDF / Боксу", url=ask_url)],
    ])


WARMUP_AFTER_FIRST_SCAN_TEXT = (
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

PAYWALL_MESSAGE_TEXT = (
    "Бесплатный лимит исчерпан. По двум банкам архитектуру не выстроить, "
    "а наносить непроверенные средства на вашу кожу сейчас небезопасно.\n\n"
    "Выбирайте тариф ниже, и давайте соберем ваш идеальный уход:\n\n"
    "💳 <b>АУДИТ (1000 ₽ / 30 дней)</b>: безлимитный сканер и поиск скрытых угроз.\n"
    "💎 <b>СБОРКА ПРОТОКОЛА (2500 ₽ / 30 дней)</b>: моя интеллектуальная память, "
    "контроль конфликтов и сборка безопасной базы.\n\n"
    "Устали подбирать сами? Закажите Индивидуальный Бокс или PDF-протокол у создателя проекта, Марии:"
)

INVALID_INCI_TEXT = (
    "Не удалось распознать полный список компонентов (INCI). Пожалуйста, убедитесь, что на фото четко виден "
    "текст после слова Состав / Ingredients без обрезанных краев (либо скопируйте состав текстом из карточки товара). "
    "Ваша попытка не списалась!"
)

MARIA_PRODUCTS_DESCRIPTION = (
    "📄 <b>Персональный PDF-протокол (4 900 ₽)</b> — уход в формате подробного PDF-руководства. "
    "Мария подберет косметику строго под ваш бюджет, даст прямые ссылки на покупку каждого средства и распишет "
    "понятную инструкцию (что и как наносить утром и вечером). Вы покупаете всё сами по готовому списку. "
    "Этот файл останется с вами как надежная база: Мария добавит позиции на перспективу, чтобы вы всегда знали, "
    "какими средствами дополнить и усилить свой протокол.\n\n"
    "📦 <b>Индивидуальный Бокс (от 9 000 ₽)</b> — премиальный формат ухода «под ключ». "
    "Вы приобретаете не просто набор косметики, а готовую систему — ваш личный работающий протокол. "
    "Вы получаете эстетичную коробку с полноразмерными средствами, подобранными строго под вашу кожу, "
    "и подробную инструкцию. В эту стоимость уже включено ведение от Марии: она будет на связи, чтобы "
    "контролировать динамику и довести вас до результата. Вам не нужно ходить по магазинам — забираете посылку "
    "и начинаете свой путь к здоровой коже по протоколу.\n\n"
    "Выберите нужный формат ниже: PDF-протокол можно оплатить сразу, а для заказа Бокса или консультации "
    "перейдите в личный диалог с Марией 👇"
)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    db.get_or_create_user(user_id, username)

    try:
        await message.answer_video_note(video_note=CIRCLE_VIDEO_ID)
    except Exception as e:
        logger.warning(f"Не удалось отправить кружочек: {e}")

    await message.answer(
        f"Жми на кнопку ниже, чтобы Роман Андреевич запустил алгоритм диагностики 👇\n"
        f"Нажимая кнопку, вы принимаете условия <a href='{OFFER_URL}'>Публичной оферты</a> "
        f"и даете <a href='{CONSENT_URL}'>Согласие на обработку персональных данных</a>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="СТАРТ", callback_data="start_diagnostics")]
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@dp.callback_query(F.data == "start_diagnostics")
async def start_diagnostics_cb(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    db.get_or_create_user(user_id, callback.from_user.username)
    db.clear_user_tags_for_new_survey(user_id)

    await state.clear()
    await state.set_state(SurveyStates.waiting_for_name)

    await callback.message.answer("Для начала напишите, как к вам обращаться:")
    await callback.answer()


@dp.message(StateFilter(SurveyStates.waiting_for_name))
async def process_name_step(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, напишите ваше имя текстом:")
        return

    clean_name = sanitize_user_name(message.text)
    if not clean_name:
        await message.answer("Пожалуйста, введите корректное имя (буквами, до 30 символов):")
        return

    db.update_user_name(message.from_user.id, clean_name)
    await state.update_data(
        user_name=clean_name,
        q1_selected=[],
        q5_selected=[],
        tags=[],
        q2_pergament=False,
        medical_note=""
    )

    await message.answer("Отлично! А теперь ответьте на 6 быстрых вопросов, чтобы я понял, с чем мы работаем. 👇")
    await state.set_state(SurveyStates.q1)
    await message.answer(
        "Вопрос 1. Что вас сейчас беспокоит больше всего?\n(Можно выбрать несколько вариантов)",
        reply_markup=build_q1_keyboard([])
    )


@dp.callback_query(StateFilter(SurveyStates.q1), F.data.startswith("q1_toggle:"))
async def q1_toggle_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    selected: list[int] = data.get("q1_selected", [])
    exclusive_idx = len(Q1_OPTIONS) - 1

    if idx == exclusive_idx:
        selected = [] if exclusive_idx in selected else [exclusive_idx]
    else:
        if exclusive_idx in selected:
            selected.remove(exclusive_idx)
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)

    await state.update_data(q1_selected=selected)
    try:
        await callback.message.edit_reply_markup(reply_markup=build_q1_keyboard(selected))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q1), F.data == "q1_next")
async def q1_next_cb(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: list[int] = data.get("q1_selected", [])

    if not selected:
        await callback.answer("Пожалуйста, отметьте хотя бы один вариант!", show_alert=True)
        return

    tags: list[str] = data.get("tags", [])
    for idx in sorted(selected):
        _, tag = Q1_OPTIONS[idx]
        if tag and tag != "EXCLUSIVE_NONE" and tag not in tags:
            tags.append(tag)

    await state.update_data(tags=tags)
    await state.set_state(SurveyStates.q2)
    try:
        await callback.message.edit_text(
            "Вопрос 2. Как чувствует себя ваша кожа через 10 минут после умывания просто водой (до крема)?",
            reply_markup=build_single_choice_keyboard(Q2_OPTIONS, "q2")
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 2. Как чувствует себя ваша кожа через 10 минут после умывания просто водой (до крема)?",
            reply_markup=build_single_choice_keyboard(Q2_OPTIONS, "q2")
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q2), F.data == "back_to_q2")
async def back_to_q1_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SurveyStates.q1)
    data = await state.get_data()
    selected = data.get("q1_selected", [])
    try:
        await callback.message.edit_text(
            "Вопрос 1. Что вас сейчас беспокоит больше всего?\n(Можно выбрать несколько вариантов)",
            reply_markup=build_q1_keyboard(selected)
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 1. Что вас сейчас беспокоит больше всего?\n(Можно выбрать несколько вариантов)",
            reply_markup=build_q1_keyboard(selected)
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q2), F.data.startswith("q2:"))
async def q2_answer_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    _, tag = Q2_OPTIONS[idx]
    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    if tag and tag not in tags:
        tags.append(tag)

    q2_pergament = (idx == 2)
    await state.update_data(tags=tags, q2_pergament=q2_pergament)
    await state.set_state(SurveyStates.q3)
    try:
        await callback.message.edit_text(
            "Вопрос 3. Как ведет себя ваша кожа к середине дня?",
            reply_markup=build_single_choice_keyboard(Q3_OPTIONS, "q3")
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 3. Как ведет себя ваша кожа к середине дня?",
            reply_markup=build_single_choice_keyboard(Q3_OPTIONS, "q3")
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q3), F.data == "back_to_q3")
async def back_to_q2_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SurveyStates.q2)
    try:
        await callback.message.edit_text(
            "Вопрос 2. Как чувствует себя ваша кожа через 10 минут после умывания просто водой (до крема)?",
            reply_markup=build_single_choice_keyboard(Q2_OPTIONS, "q2")
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 2. Как чувствует себя ваша кожа через 10 минут после умывания просто водой (до крема)?",
            reply_markup=build_single_choice_keyboard(Q2_OPTIONS, "q2")
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q3), F.data.startswith("q3:"))
async def q3_answer_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    _, raw_tag = Q3_OPTIONS[idx]
    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    q2_pergament = data.get("q2_pergament", False)

    resolved_tag = "Компенсаторная жирность" if (raw_tag == "CONDITIONAL_SHINE" and q2_pergament) else (
        "Себопрофицит" if raw_tag == "CONDITIONAL_SHINE" else raw_tag
    )
    if resolved_tag and resolved_tag not in tags:
        tags.append(resolved_tag)

    await state.update_data(tags=tags)
    await state.set_state(SurveyStates.q4)
    try:
        await callback.message.edit_text(
            "Вопрос 4. Насколько ваша кожа чувствительна к раздражителям?",
            reply_markup=build_single_choice_keyboard(Q4_OPTIONS, "q4")
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 4. Насколько ваша кожа чувствительна к раздражителям?",
            reply_markup=build_single_choice_keyboard(Q4_OPTIONS, "q4")
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q4), F.data == "back_to_q4")
async def back_to_q3_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SurveyStates.q3)
    try:
        await callback.message.edit_text(
            "Вопрос 3. Как ведет себя ваша кожа к середине дня?",
            reply_markup=build_single_choice_keyboard(Q3_OPTIONS, "q3")
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 3. Как ведет себя ваша кожа к середине дня?",
            reply_markup=build_single_choice_keyboard(Q3_OPTIONS, "q3")
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q4), F.data.startswith("q4:"))
async def q4_answer_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    _, tag = Q4_OPTIONS[idx]
    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    if tag and tag not in tags:
        tags.append(tag)

    await state.update_data(tags=tags, q5_selected=[])
    await state.set_state(SurveyStates.q5)
    try:
        await callback.message.edit_text(
            "Вопрос 5. Есть ли у вас сейчас что-то из перечисленного?\n(Можно выбрать несколько вариантов)",
            reply_markup=build_q5_keyboard([])
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 5. Есть ли у вас сейчас что-то из перечисленного?\n(Можно выбрать несколько вариантов)",
            reply_markup=build_q5_keyboard([])
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q5), F.data == "back_to_q5")
async def back_to_q4_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SurveyStates.q4)
    try:
        await callback.message.edit_text(
            "Вопрос 4. Насколько ваша кожа чувствительна к раздражителям?",
            reply_markup=build_single_choice_keyboard(Q4_OPTIONS, "q4")
        )
    except Exception:
        await callback.message.answer(
            "Вопрос 4. Насколько ваша кожа чувствительна к раздражителям?",
            reply_markup=build_single_choice_keyboard(Q4_OPTIONS, "q4")
        )
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q5), F.data.startswith("q5_toggle:"))
async def q5_toggle_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    selected: list[int] = data.get("q5_selected", [])
    exclusive_idx = 4

    if idx == exclusive_idx:
        selected = [] if exclusive_idx in selected else [exclusive_idx]
    else:
        if exclusive_idx in selected:
            selected.remove(exclusive_idx)
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)

    await state.update_data(q5_selected=selected)
    try:
        await callback.message.edit_reply_markup(reply_markup=build_q5_keyboard(selected))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q5), F.data == "q5_next")
async def q5_next_cb(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: list[int] = data.get("q5_selected", [])

    if not selected:
        await callback.answer("Пожалуйста, отметьте хотя бы один вариант!", show_alert=True)
        return

    tags: list[str] = data.get("tags", [])
    has_other_med = False

    for idx in sorted(selected):
        _, tag = Q5_OPTIONS[idx]
        if tag == "OTHER_MED":
            has_other_med = True
            if "Медицина" not in tags:
                tags.append("Медицина")
        elif tag and tag != "EXCLUSIVE_NONE" and tag not in tags:
            tags.append(tag)

    await state.update_data(tags=tags)

    if has_other_med:
        await state.set_state(SurveyStates.waiting_for_medical_text)
        try:
            await callback.message.edit_text("Напишите ваш вариант (кратко одним сообщением):")
        except Exception:
            await callback.message.answer("Напишите ваш вариант (кратко одним сообщением):")
    else:
        await state.set_state(SurveyStates.q6)
        try:
            await callback.message.edit_text(
                "Вопрос 6. Известная аллергия на компоненты?",
                reply_markup=build_q6_keyboard()
            )
        except Exception:
            await callback.message.answer(
                "Вопрос 6. Известная аллергия на компоненты?",
                reply_markup=build_q6_keyboard()
            )
    await callback.answer()


@dp.message(StateFilter(SurveyStates.waiting_for_medical_text))
async def process_medical_text_step(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, напишите ваш вариант текстом:")
        return

    clean_med = sanitize_short_input(message.text, 100)
    await state.update_data(medical_note=clean_med)
    await state.set_state(SurveyStates.q6)
    await message.answer(
        "Вопрос 6. Известная аллергия на компоненты?",
        reply_markup=build_q6_keyboard()
    )


@dp.callback_query(StateFilter(SurveyStates.q6), F.data == "q6:no")
async def q6_no_cb(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await finalize_survey_and_show_diagnosis(callback.message, callback.from_user.id, state)
    await callback.answer()


@dp.callback_query(StateFilter(SurveyStates.q6), F.data == "q6:yes")
async def q6_yes_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SurveyStates.waiting_for_allergy_text)
    try:
        await callback.message.edit_text("Напишите название компонента, на который у вас аллергия:")
    except Exception:
        await callback.message.answer("Напишите название компонента, на который у вас аллергия:")
    await callback.answer()


@dp.message(StateFilter(SurveyStates.waiting_for_allergy_text))
async def process_allergy_text_step(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, напишите название компонента текстом:")
        return

    clean_allergy = sanitize_short_input(message.text, 100)
    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    if clean_allergy:
        tags.append(f"Аллергия на {clean_allergy}")
    await state.update_data(tags=tags)

    await finalize_survey_and_show_diagnosis(message, message.from_user.id, state)


async def finalize_survey_and_show_diagnosis(message: types.Message, user_id: int, state: FSMContext):
    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    medical_note: str = data.get("medical_note", "")

    if not tags:
        tags = ["Норма"]

    db.save_survey_results(user_id, tags, medical_note)
    await state.clear()

    paragraphs = []
    if "Медицина" in tags:
        paragraphs.append(
            "Вижу по анкете, что у вас есть дерматологический диагноз. Важно: я ИИ-технолог, а не врач, "
            "и я не назначаю лечение. Моя задача — помочь вам собрать безопасную базу, которая не спровоцирует обострение."
        )
    if "Сломан барьер" in tags:
        paragraphs.append(
            "Фиксируем нарушение гидролипидной мантии. Кожа теряет влагу и беззащитна. "
            "Если сейчас агрессивно вводить антиэйдж или кислоты, мы получим воспаление."
        )
    if not paragraphs:
        paragraphs.append(
            "Фиксируем нормальное состояние эпидермиса. Гидролипидная мантия функционирует стабильно. "
            "Наша стратегия — поддерживать баланс и предотвращать стресс."
        )

    await message.answer("\n\n".join(paragraphs))
    await message.answer(
        "Диагностика завершена. Теперь нам нужно выстроить безопасный физиологичный уход. "
        "Вы можете собрать его самостоятельно, проверяя каждое средство вместе со мной, Романом Андреевичем, "
        "или сразу забрать готовую схему от Марии. Если у вас сейчас вообще нет ухода — мы выстроим его с чистого листа.",
        reply_markup=get_step3_fork_keyboard()
    )


@dp.callback_query(F.data == "branch_roman")
async def branch_roman_cb(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = db.get_or_create_user(user_id, callback.from_user.username)
    has_sub, _ = db.has_active_scanner_sub(user_id)
    free_used = user.get("free_scans_used", 0)

    if has_sub:
        await callback.message.answer(
            "Доступ к лаборатории открыт на 30 дней! Присылайте составы текстом или фотографией этикетки. "
            "Выберите категорию средства для проверки 👇",
            reply_markup=get_categories_keyboard()
        )
    elif free_used == 0:
        await callback.message.answer(
            "Отличный план! Я даю вам 2 бесплатные проверки, чтобы вы протестировали меня в деле. "
            "Прямо сейчас сфотографируйте состав на вашей баночке (или сделайте скриншот состава на маркетплейсе) "
            "либо просто скопируйте его текстом. Но для начала выберите, к какой категории относится средство 👇",
            reply_markup=get_categories_keyboard()
        )
    elif free_used == 1:
        await callback.message.answer(
            "🎁 Но у вас осталась еще 1 бесплатная попытка! Какую категорию проверим следующей?",
            reply_markup=get_categories_keyboard()
        )
    else:
        await callback.message.answer(
            PAYWALL_MESSAGE_TEXT,
            reply_markup=get_paywall_keyboard(),
            parse_mode="HTML"
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def select_category_cb(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split(":")[1]
    user_id = callback.from_user.id
    user = db.get_or_create_user(user_id, callback.from_user.username)

    if user.get("survey_done", 0) == 0:
        await callback.message.answer(
            "Пожалуйста, сначала завершите короткую диагностику кожи 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="СТАРТ", callback_data="start_diagnostics")]
            ])
        )
        await callback.answer()
        return

    has_sub, _ = db.has_active_scanner_sub(user_id)
    if not has_sub and user.get("free_scans_used", 0) >= 2:
        await callback.message.answer(
            PAYWALL_MESSAGE_TEXT,
            reply_markup=get_paywall_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    data = await state.get_data()
    pending_inci = data.get("pending_inci")
    await callback.answer()

    if pending_inci:
        await state.update_data(pending_inci=None, selected_category=None)
        await state.clear()
        await execute_scan_with_lock(
            message=callback.message,
            user_id=user_id,
            username=callback.from_user.username,
            category=category,
            raw_inci_text=pending_inci
        )
        return

    await state.update_data(selected_category=category)
    await state.set_state(SurveyStates.waiting_for_inci)
    await callback.message.answer(
        "Категория выбрана. Отправьте фотографию состава (четкое фото этикетки или скриншот из интернет-магазина) "
        "либо пришлите список компонентов (INCI) текстом 👇"
    )


async def _ensure_user_can_scan(message: types.Message, user_id: int, username: str, state: FSMContext) -> bool:
    user = db.get_or_create_user(user_id, username)
    current_state = await state.get_state()

    if user.get("survey_done", 0) == 0:
        if current_state in SURVEY_ACTIVE_STATES:
            await message.answer("Пожалуйста, ответьте на текущий вопрос анкеты выше ☝️")
        else:
            await message.answer(
                "Чтобы я мог проанализировать состав под вашу кожу, сначала пройдите короткую диагностику 👇",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="СТАРТ", callback_data="start_diagnostics")]
                ])
            )
        return False

    has_sub, _ = db.has_active_scanner_sub(user_id)
    if not has_sub and user.get("free_scans_used", 0) >= 2:
        await message.answer(
            PAYWALL_MESSAGE_TEXT,
            reply_markup=get_paywall_keyboard(),
            parse_mode="HTML"
        )
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")
    scans_today = user.get("scans_today", 0) if user.get("last_scan_date") == today_str else 0
    if scans_today >= 40 or (user.get("scans_month") or 0) >= 500:
        await message.answer("Вы проверили уже 40 средств за сегодня — давайте дадим коже и алгоритму передышку и продолжим аудит завтра! 🌿")
        return False

    return True


async def execute_scan_with_lock(
    message: types.Message,
    user_id: int,
    username: str,
    category: str,
    raw_inci_text: str
):
    lock = get_user_lock(user_id)
    if lock.locked():
        await message.answer("⏳ Подождите пару секунд, я уже анализирую предыдущий состав...")
        return

    async with lock:
        if not check_rate_limit_3_per_min(user_id):
            await message.answer("⏳ Слишком много запросов подряд. Подождите 15–20 секунд и отправьте состав снова.")
            return

        user = db.get_or_create_user(user_id, username)
        has_sub, tariff = db.has_active_scanner_sub(user_id)

        if not has_sub and user.get("free_scans_used", 0) >= 2:
            await message.answer(
                PAYWALL_MESSAGE_TEXT,
                reply_markup=get_paywall_keyboard(),
                parse_mode="HTML"
            )
            return

        wait_msg = await message.answer("🔬 Провожу химический аудит состава...")

        is_protocol = (has_sub and tariff == "protocol_2500")
        routine_text = db.format_approved_routine_for_llm(user_id) if is_protocol else "Пока нет сохраненных средств."
        user_name = user.get("user_name") or "Клиент"
        user_tags = user.get("user_tags", [])

        result = await analyze_cosmetic_inci(
            user_name=user_name,
            user_tags=user_tags,
            category=category,
            raw_inci_text=raw_inci_text,
            is_protocol_tariff=is_protocol,
            approved_routine_text=routine_text
        )

        status_emoji = result.get("status_emoji")
        clean_text = result.get("text_for_user", "")

        if not status_emoji:
            try:
                await wait_msg.edit_text(clean_text or INVALID_INCI_TEXT)
            except Exception:
                await message.answer(clean_text or INVALID_INCI_TEXT)
            return

        allowed, reason = db.check_and_increment_scan_usage(user_id, is_paid_sub=has_sub)
        if not allowed:
            try:
                await wait_msg.delete()
            except Exception:
                pass
            if reason in ("daily_limit", "monthly_limit"):
                await message.answer("Вы проверили уже 40 средств за сегодня — давайте дадим коже и алгоритму передышку и продолжим аудит завтра! 🌿")
            else:
                await message.answer(
                    PAYWALL_MESSAGE_TEXT,
                    reply_markup=get_paywall_keyboard(),
                    parse_mode="HTML"
                )
            return

        record_id = None
        if result.get("should_save_to_db"):
            record_id = db.add_approved_product(
                telegram_id=user_id,
                category=category,
                status=status_emoji,
                stage=result["stage"],
                key_actives=result["key_actives"]
            )

        try:
            await wait_msg.delete()
        except Exception:
            pass

        updated_user = db.get_user(user_id)
        free_used_now = updated_user.get("free_scans_used", 0)

        if has_sub:
            extra_btn = None
            if is_protocol and record_id is not None:
                extra_btn = InlineKeyboardButton(
                    text="🗑 Я только смотрю в магазине (Не добавлять в мой уход)",
                    callback_data=f"remove_item:{record_id}"
                )
            await message.answer(
                clean_text,
                reply_markup=get_categories_keyboard(extra_top_button=extra_btn)
            )
        else:
            await message.answer(clean_text)
            if free_used_now == 1:
                await message.answer(
                    WARMUP_AFTER_FIRST_SCAN_TEXT,
                    reply_markup=get_categories_keyboard(),
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    PAYWALL_MESSAGE_TEXT,
                    reply_markup=get_paywall_keyboard(),
                    parse_mode="HTML"
                )


@dp.message(F.photo)
async def handle_photo_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    current_state = await state.get_state()

    if current_state in SURVEY_ACTIVE_STATES:
        await message.answer("Пожалуйста, завершите ответ на вопрос анкеты (текстом или кнопкой выше) ☝️")
        return

    if message.media_group_id is not None:
        now = time.time()
        mg_id = str(message.media_group_id)
        for k in list(HANDLED_MEDIA_GROUPS.keys()):
            if now - HANDLED_MEDIA_GROUPS[k] > 30:
                del HANDLED_MEDIA_GROUPS[k]
        if mg_id in HANDLED_MEDIA_GROUPS:
            return
        HANDLED_MEDIA_GROUPS[mg_id] = now
        await message.answer("Пожалуйста, отправляйте строго по одной фотографии за раз, чтобы я проверил каждое средство отдельно. Ваша попытка сохранена.")
        return

    lock = get_user_lock(user_id)
    if lock.locked():
        await message.answer("⏳ Подождите пару секунд, я уже анализирую предыдущий состав...")
        return

    if not await _ensure_user_can_scan(message, user_id, username, state):
        return

    data = await state.get_data()
    selected_category = data.get("selected_category")

    async with lock:
        ocr_msg = await message.answer("🔍 Считываю текст состава с фотографии...")
        try:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            bio = io.BytesIO()
            await bot.download_file(file_info.file_path, destination=bio)
            extracted_inci = await extract_inci_from_image_bytes(bio.getvalue())
        except Exception:
            extracted_inci = "NO_INCI_FOUND"

        try:
            await ocr_msg.delete()
        except Exception:
            pass

        if not validate_inci_before_llm(extracted_inci):
            await message.answer(INVALID_INCI_TEXT)
            return

        if not selected_category:
            await state.update_data(pending_inci=extracted_inci)
            await message.answer(
                "Состав получен! Уточните, к какой категории относится это средство, чтобы я применил нужную матрицу 👇",
                reply_markup=get_categories_keyboard()
            )
            return

    await state.update_data(selected_category=None, pending_inci=None)
    await state.clear()
    await execute_scan_with_lock(
        message=message,
        user_id=user_id,
        username=username,
        category=selected_category,
        raw_inci_text=extracted_inci
    )


@dp.message(F.video | F.voice | F.sticker | F.document | F.audio | F.video_note | F.animation)
async def handle_unsupported_media(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in SURVEY_ACTIVE_STATES:
        await message.answer("Пожалуйста, введите ответ текстом или выберите кнопку выше.")
        return
    await message.answer("Отправьте обычную фотографию состава (или скриншот) либо скопируйте список ингредиентов текстом. Попытка сохранена.")


@dp.message(F.text)
async def handle_text_inci_or_buffer(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    current_state = await state.get_state()

    if current_state in {
        SurveyStates.q1.state, SurveyStates.q2.state, SurveyStates.q3.state,
        SurveyStates.q4.state, SurveyStates.q5.state, SurveyStates.q6.state
    }:
        await message.answer("Пожалуйста, выберите вариант ответа с помощью кнопок под вопросом ☝️")
        return

    lock = get_user_lock(user_id)
    if lock.locked():
        await message.answer("⏳ Подождите пару секунд, я уже анализирую предыдущий состав...")
        return

    if not await _ensure_user_can_scan(message, user_id, username, state):
        return

    raw_text = message.text.strip()
    if not validate_inci_before_llm(raw_text):
        await message.answer(INVALID_INCI_TEXT)
        return

    data = await state.get_data()
    selected_category = data.get("selected_category")

    if not selected_category:
        await state.update_data(pending_inci=raw_text)
        await message.answer(
            "Состав получен! Уточните, к какой категории относится это средство, чтобы я применил нужную матрицу 👇",
            reply_markup=get_categories_keyboard()
        )
        return

    await state.update_data(selected_category=None, pending_inci=None)
    await state.clear()
    await execute_scan_with_lock(
        message=message,
        user_id=user_id,
        username=username,
        category=selected_category,
        raw_inci_text=raw_text
    )


async def send_admin_lead_card(user_data: dict, target_bot: Bot = bot):
    telegram_id = user_data["telegram_id"]
    user_name = user_data.get("user_name") or "Без имени"
    username = user_data.get("username") or "нет_ника"
    tags = user_data.get("user_tags", [])
    medical = user_data.get("medical_note") or "нет"

    card_text = (
        f"🔔 Клиент открыл раздел готового ухода Марии (Шаг 6)\n"
        f"👤 Имя: {user_name} (@{username}, код: `#{telegram_id}`)\n"
        f"🏷 Теги кожи: {', '.join(tags) if tags else 'нет'}\n"
        f"💊 Медицина / пометки: {medical}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Пауза пушей на 72 часа", callback_data=f"admin_pause:{telegram_id}")],
        [InlineKeyboardButton(text="✅ Купил(а) Бокс / PDF (Отключить пуши)", callback_data=f"admin_vip:{telegram_id}")]
    ])

    try:
        await target_bot.send_message(ADMIN_ID, card_text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Не удалось отправить лид администратору ADMIN_ID: {e}")


@dp.callback_query(F.data.startswith("branch_maria:"))
async def branch_maria_cb(callback: types.CallbackQuery):
    scenario = callback.data.split(":")[1]
    user_id = callback.from_user.id
    user = db.get_or_create_user(user_id, callback.from_user.username)
    user_name = user.get("user_name") or callback.from_user.first_name or "Клиент"

    asyncio.create_task(send_admin_lead_card(user))

    intro = "Прекрасный выбор. Чистый лист — это идеальная возможность сразу выстроить грамотный фундамент. Вы можете заказать у Марии:\n\n" if scenario == "2" else "Прекрасный выбор. Вы можете заказать у Марии:\n\n"

    await callback.message.answer(
        intro + MARIA_PRODUCTS_DESCRIPTION,
        reply_markup=get_step6_maria_keyboard(user_name, user_id),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_pause:"))
async def admin_pause_cb(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    target_id = int(callback.data.split(":")[1])
    db.set_maria_order_status(target_id, "paused_72h")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text(callback.message.text + "\n\n⏸ <b>Пуши остановлены на 72ч</b>", parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("Пауза пушей на 72 часа установлена.")


@dp.callback_query(F.data.startswith("admin_vip:"))
async def admin_vip_cb(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return

    target_id = int(callback.data.split(":")[1])
    db.set_maria_order_status(target_id, "completed_vip")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Клиент переведен в статус VIP (Пуши отключены навсегда)</b>", parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("Клиент переведен в VIP, воронка дожима отключена.")


@dp.message(F.text.regexp(r"#(\d+)"))
async def admin_search_client_by_hashtag(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    match = re.search(r"#(\d+)", message.text)
    if not match:
        return

    target_id = int(match.group(1))
    client_data = db.get_user(target_id)
    if not client_data:
        await message.answer(f"Клиент с кодом #{target_id} не найден в базе.")
        return

    await send_admin_lead_card(client_data, message.bot)


@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id, message.from_user.username)
    user_name = user.get("user_name") or message.from_user.first_name or "Не указано"
    tags_list = user.get("user_tags", [])
    tags_str = ", ".join(tags_list) if tags_list else "Анкета еще не пройдена"
    free_used = user.get("free_scans_used", 0)

    has_sub, tariff = db.has_active_scanner_sub(user_id)
    expires_raw = user.get("scanner_expires_at")
    expires_fmt = datetime.fromisoformat(expires_raw).strftime("%d.%m.%Y %H:%M") if expires_raw else ""

    if not has_sub:
        await message.answer(
            f"👤 Имя: {user_name}\n"
            f"Ваши диагностические маркеры: {tags_str}\n"
            f"Статус доступа: Неактивен (Использовано бесплатных проверок: {free_used} из 2).\n"
            f"Выберите тариф в меню /tariffs, чтобы включить сканер составов."
        )
    elif tariff == "audit_1000":
        await message.answer(
            f"👤 Имя: {user_name}\n"
            f"Тариф: 💳 Аудит (Безлимитный сканер)\n"
            f"Активен до: {expires_fmt}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Проверить новое средство", callback_data="branch_roman")]
            ])
        )
    elif tariff == "protocol_2500":
        items = db.get_approved_products(user_id)
        await message.answer(
            f"👤 Имя: {user_name}\n"
            f"Тариф: 💎 Сборка протокола\n"
            f"Активен до: {expires_fmt}\n"
            f"Одобренных средств в вашей схеме: {len(items)} шт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Проверить новое средство", callback_data="branch_roman")],
                [InlineKeyboardButton(text="🗂 Моя собранная база (Просмотр / Удаление)", callback_data="view_routine")]
            ])
        )


@dp.callback_query(F.data == "view_routine")
async def view_routine_cb(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    items = db.get_approved_products(user_id)
    if not items:
        await callback.answer("В вашей схеме пока нет сохраненных средств.", show_alert=True)
        return

    lines = ["🗂 <b>Ваша собранная база средств:</b>\n"]
    buttons = []
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {item['status']} <b>{item['stage']}</b> — {item['key_actives']}")
        buttons.append([InlineKeyboardButton(text=f"🗑 Удалить #{idx} ({item['stage']})", callback_data=f"del_routine:{item['id']}")])

    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("del_routine:"))
async def delete_routine_item_cb(callback: types.CallbackQuery):
    record_id = int(callback.data.split(":")[1])
    db.remove_approved_product(record_id, callback.from_user.id)
    await callback.answer("✅ Средство удалено из вашей схемы.")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await view_routine_cb(callback)


@dp.callback_query(F.data.startswith("remove_item:"))
async def remove_item_after_scan_cb(callback: types.CallbackQuery):
    record_id = int(callback.data.split(":")[1])
    db.remove_approved_product(record_id, callback.from_user.id)
    confirmed_btn = InlineKeyboardButton(text="✅ Средство удалено из вашей схемы", callback_data="noop")
    try:
        await callback.message.edit_reply_markup(reply_markup=get_categories_keyboard(extra_top_button=confirmed_btn))
    except Exception:
        pass
    await callback.answer("Средство удалено из вашей схемы")


@dp.callback_query(F.data == "noop")
async def noop_cb(callback: types.CallbackQuery):
    await callback.answer()


@dp.message(Command("tariffs"))
async def cmd_tariffs(message: types.Message):
    await message.answer(
        "🤖 Роман Андреевич: Выберите формат работы с алгоритмом или закажите готовую систему у Марии:\n\n"
        "💳 <b>АУДИТ (1000 ₽ / 30 дней)</b>: безлимитный сканер составов и защита от скрытых угроз.\n"
        "💎 <b>СБОРКА ПРОТОКОЛА (2500 ₽ / 30 дней)</b>: интеллектуальная память бота, выстраивание системы ухода без пробелов и контроль конфликтов.\n"
        "📦 <b>ГОТОВЫЙ УХОД</b>: Индивидуальный Бокс или PDF-протокол от создателя проекта.",
        reply_markup=get_paywall_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("buy:"))
async def buy_tariff_placeholder_cb(callback: types.CallbackQuery):
    tariff_code = callback.data.split(":")[1]
    if tariff_code in ("audit_1000", "protocol_2500", "pdf_4900"):
        db.process_successful_payment(
            payment_id=f"test_pay_{int(time.time())}",
            telegram_id=callback.from_user.id,
            amount=1000 if tariff_code=="audit_1000" else (2500 if tariff_code=="protocol_2500" else 4900),
            tariff_type=tariff_code
        )
        await callback.message.answer(f"✅ Оплата успешно симулирована! Тариф активирован, воронка дожима отключена.")
        await callback.answer()
        return


async def background_retention_and_push_worker():
    logger.info("⏰ Фоновый воркер пушей (ТЗ v4.0) запущен")
    while True:
        try:
            conn = db.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE survey_done = 1")
            users = [dict(r) for r in cursor.fetchall()]
            conn.close()

            now = datetime.now()

            for u in users:
                uid = u["telegram_id"]
                maria_status = u.get("maria_order_status", "none")
                pause_until_str = u.get("pause_until")

                if maria_status == "completed_vip":
                    continue
                if pause_until_str:
                    try:
                        if datetime.fromisoformat(pause_until_str) > now:
                            continue
                    except Exception:
                        pass

                has_sub, tariff = db.has_active_scanner_sub(uid)

                last_act_str = u.get("last_activity_at") or u.get("created_at")
                try:
                    last_act = datetime.fromisoformat(last_act_str)
                except Exception:
                    last_act = now
                elapsed_hours = (now - last_act).total_seconds() / 3600.0

                if not has_sub and tariff == "none" and maria_status in ("none", "paused_72h"):
                    push_schedule = [
                        (2.0, "p_2h", "🤖 {user_name}, вы остановились на этапе активации. Ваша анкета сохранена. Чтобы я мог завершить химический анализ вашего ухода и выстроить протокол безопасности, необходимо завершить оформление. Я готов приступить к работе.", get_paywall_keyboard()),
                        (24.0, "p_24h", "🤖 Прошли сутки с момента паузы. Напоминаю, что каждый день использования неподходящей косметики усугубляет состояние барьера. Если в ваших баночках есть скрытые угрозы, вы наносите их прямо сейчас. Давайте остановим этот процесс. Открываем доступ?", get_paywall_keyboard()),
                        (48.0, "p_48h", "🤖 Я технически закрываю ваш незаконченный сеанс. Данные заархивированы. Если решите навести порядок в уходе, перестать тратить деньги на агрессивные средства и доверить сборку системы алгоритму — возвращайтесь.", get_paywall_keyboard()),
                        (120.0, "p_5d", "🤖 {user_name}, наверняка вы снова смотрели на косметику на маркетплейсах. Красивые обещания часто маскируют дешевую базу и пустышки. Не покупайте новые средства вслепую. Загрузите составы мне перед оплатой, и я уберегу вас от пустой траты денег.", get_paywall_keyboard()),
                        (168.0, "p_7d", "🤖 Прошла неделя. За это время вы нанесли свой привычный уход минимум 14 раз. Если мы не исключили из вашей системы жесткие ПАВы или конфликтные активы, вы значительно усилили дефицит липидов. Доступ к системе ждет активации.", get_paywall_keyboard()),
                        (240.0, "p_10d", "🤖 {user_name}, алгоритмы соцсетей продолжают продавать вам «волшебные» банки. Блогеры рекомендуют агрессивные активы, не зная вашей физиологии. Прежде чем оплатить корзину — пропустите состав через мою матрицу безопасности.", get_paywall_keyboard()),
                        (336.0, "p_14d", "🤖 {user_name}, если у вас нет времени самостоятельно сканировать каждый состав — снимите с себя эту задачу. Создатель моего алгоритма, Мария, может собрать для вас индивидуальный протокол с нуля или отправить готовый бокс.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="branch_maria:1")]])),
                        (504.0, "p_21d", "🤖 {user_name}, пока гидролипидная мантия не восстановлена, кожа находится в состоянии перманентного стресса. Кожа в стрессе увядает в разы быстрее — появляются преждевременные морщины. Нам нужно срочно выстроить защитную архитектуру.", get_paywall_keyboard()),
                        (720.0, "p_30d", "🤖 Прошел ровно месяц. 28–35 дней — это полный цикл обновления эпидермиса. Если вы устали действовать вслепую и готовы выстроить грамотную, физиологичную базу — двери лаборатории открыты.", get_paywall_keyboard()),
                        (1440.0, "p_60d", "🤖 {user_name}, прошло два месяца. Изменились внешние факторы: температура, влажность. Кожа адаптировалась, и база, спасавшая вас раньше, может давать сбои. Необходимо провести аудит вашей системы и скорректировать протокол.", get_paywall_keyboard()),
                        (2160.0, "p_90d", "🤖 {user_name}, 90 дней — критический срок (PAO) для многих активов. Витамин С окисляется, пептиды теряют стабильность. Наносить такую косметику небезопасно. Пришло время провести ревизию и собрать новую архитектуру ухода.", get_paywall_keyboard()),
                        (4320.0, "p_180d", "🤖 Полгода назад вы инициировали запуск алгоритма. У вас есть два пути: активировать алгоритм и жестко фильтровать каждую банку, либо делегировать это Марии и получить готовый бокс. Выбор за вами.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Аудит — 1000 ₽", callback_data="buy:audit_1000")], [InlineKeyboardButton(text="💎 Сборка — 2500 ₽", callback_data="buy:protocol_2500")], [InlineKeyboardButton(text="👑 Уход от Марии", callback_data="branch_maria:1")]]))
                    ]

                    for req_hours, p_key, p_text, p_kb in push_schedule:
                        if elapsed_hours >= req_hours:
                            if not db_push_already_sent(uid, p_key):
                                formatted_text = p_text.format(user_name=u.get("user_name") or "Клиент")
                                try:
                                    await bot.send_message(uid, formatted_text, reply_markup=p_kb, parse_mode="HTML")
                                    db_mark_push_sent(uid, p_key)
                                except Exception:
                                    pass
                                await asyncio.sleep(0.3)

                elif has_sub and u.get("scanner_expires_at"):
                    try:
                        exp_dt = datetime.fromisoformat(u["scanner_expires_at"])
                        time_left_days = (exp_dt - now).total_days()
                        if 0 <= time_left_days <= 1.0:
                            p_key = f"sub_renew_{exp_dt.strftime('%Y%m%d')}"
                            if not db_push_already_sent(uid, p_key):
                                safe_name = u.get("user_name") or "Клиент"
                                if tariff == "audit_1000":
                                    renew_text = (
                                        f"🤖 {safe_name}, завтра ваш доступ к сканирующему модулю закрывается.\n"
                                        "Не покупайте новые средства вслепую. Одно средство с жесткими ПАВами или комедогенной базой "
                                        "может спровоцировать воспаление и откатить результаты назад. Продлите «Аудит», чтобы я продолжил "
                                        "жестко фильтровать ваши покупки.\n\n"
                                        "⬆️ Или перейдите на следующий уровень: выберите «Сборку протокола»."
                                    )
                                    renew_kb = InlineKeyboardMarkup(inline_keyboard=[
                                        [InlineKeyboardButton(text="💳 Продлить Аудит (30 дн.) — 1000 ₽", callback_data="buy:audit_1000")],
                                        [InlineKeyboardButton(text="💎 Перейти на Сборку протокола — 2500 ₽", callback_data="buy:protocol_2500")]
                                    ])
                                else:
                                    renew_text = (
                                        f"🤖 {safe_name}, наш месяц работы подходит к концу. За 28–35 дней эпидермис проходит полный цикл обновления. "
                                        "Завтра ваш доступ к алгоритму закрывается.\n\n"
                                        "Давайте подведем итоги. Ваша собранная база защищена, теперь вы можете продлить доступ:"
                                    )
                                    renew_kb = InlineKeyboardMarkup(inline_keyboard=[
                                        [InlineKeyboardButton(text="💎 Продлить Сборку протокола (30 дн.) — 2500 ₽", callback_data="buy:protocol_2500")],
                                        [InlineKeyboardButton(text="💳 Перейти на базовый Аудит (30 дн.) — 1000 ₽", callback_data="buy:audit_1000")],
                                        [InlineKeyboardButton(text="👑 Устала искать сама. Нужен готовый уход от Марии", callback_data="branch_maria:1")]
                                    ])

                                await bot.send_message(uid, renew_text, reply_markup=renew_kb)
                                db_mark_push_sent(uid, p_key)
                    except Exception:
                        pass

        except Exception:
            pass

        await asyncio.sleep(60)


def db_push_already_sent(telegram_id: int, push_key: str) -> bool:
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM sent_pushes WHERE telegram_id = ? AND push_key = ?", (telegram_id, push_key))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def db_mark_push_sent(telegram_id: int, push_key: str):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sent_pushes (telegram_id, push_key, sent_at) VALUES (?, ?, ?)",
            (telegram_id, push_key, datetime.now().isoformat())
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


async def setup_bot_commands():
    commands = [
        BotCommand(command="profile", description="👤 Мой статус"),
        BotCommand(command="tariffs", description="💳 Тарифы и оплата"),
        BotCommand(command="start", description="🔄 Перезапустить диагностику"),
    ]
    await bot.set_my_commands(commands)


async def main():
    logger.info("🚀 Бот (Финальная сборка ТЗ v4.0 с кнопкой Назад) запущен!")
    try:
        await setup_bot_commands()
    except Exception as e:
        logger.warning(f"Не удалось установить команды меню: {e}")

    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(background_retention_and_push_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
