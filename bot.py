import os
import re
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


# =====================================================================
# MIDDLEWARE АКТИВНОСТИ ПОЛЬЗОВАТЕЛЯ
# =====================================================================
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


# =====================================================================
# СОСТОЯНИЯ FSM (БЛОК 2 и БЛОК 5)
# =====================================================================
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


# =====================================================================
# САНИТИЗАЦИЯ ВВОДА (БЛОК 8, п. 2)
# =====================================================================
def sanitize_user_name(text: str) -> str:
    """Только буквы, пробел и дефис, обрезка до 30 символов."""
    if not text:
        return ""
    cleaned = re.sub(r"[<>\[\]\{\}]", "", text)
    cleaned = re.sub(r"[^a-zA-Zа-яА-ЯёЁ\s\-]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:30]


def sanitize_short_input(text: str, max_len: int = 100) -> str:
    """Удаляет скобки и переносы строк, обрезает до 100 символов (для медицины и аллергии)."""
    if not text:
        return ""
    cleaned = re.sub(r"[<>\[\]\{\}\r\n]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


# =====================================================================
# ДАННЫЕ АНКЕТЫ (БЛОК 2) и КЛАВИАТУРЫ
# =====================================================================
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
        mark = "✅" if idx in selected_indices else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"q1_toggle:{idx}")])
    buttons.append([InlineKeyboardButton(text="Далее ➔", callback_data="q1_next")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_single_choice_keyboard(options: list, prefix: str) -> InlineKeyboardMarkup:
    buttons = []
    for idx, (label, _) in enumerate(options):
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_q5_keyboard(selected_indices: list[int]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, (label, _) in enumerate(Q5_OPTIONS):
        mark = "✅" if idx in selected_indices else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"q5_toggle:{idx}")])
    buttons.append([InlineKeyboardButton(text="Далее ➔", callback_data="q5_next")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_q6_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Нет, аллергии нет", callback_data="q6:no")],
        [InlineKeyboardButton(text="Да, есть", callback_data="q6:yes")],
    ])


def get_categories_keyboard(extra_top_button: InlineKeyboardButton = None) -> InlineKeyboardMarkup:
    """Блок 5: 4 Inline-кнопки меню категорий."""
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
    """Шаг 3: Развилка после диагноза."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Собрать уход с Романом", callback_data="branch_roman")],
        [InlineKeyboardButton(text="👑 Хочу готовый уход от Марии", callback_data="branch_maria:1")],
        [InlineKeyboardButton(text="🤷‍♀️ У меня сейчас нет ухода", callback_data="branch_maria:2")],
    ])


def get_paywall_keyboard() -> InlineKeyboardMarkup:
    """Шаг 5: Кнопки жесткого Пэйвола."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Аудит (30 дней) — 1000 ₽", callback_data="buy:audit_1000")],
        [InlineKeyboardButton(text="💎 Сборка протокола (30 дн.) — 2500 ₽", callback_data="buy:protocol_2500")],
        [InlineKeyboardButton(text="👑 Готовый уход от Марии", callback_data="branch_maria:1")],
    ])


def get_step6_maria_keyboard(user_name: str, telegram_id: int) -> InlineKeyboardMarkup:
    """Шаг 6: 3 кнопки под предложением готового ухода от Марии."""
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


# =====================================================================
# ШАГ 1. ТОЧКА ВХОДА (/start)
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    db.get_or_create_user(user_id, username)

    try:
        await message.answer_video_note(video_note=CIRCLE_VIDEO_ID)
    except Exception as e:
        logger.warning(f"Не удалось отправить кружочек video_note: {e}")

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


# =====================================================================
# ШАГ 2. СБОР АНАМНЕЗА (АНКЕТА + ХАРДКОД-ДИАГНОЗ)
# =====================================================================
@dp.callback_query(F.data == "start_diagnostics")
async def start_diagnostics_cb(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    db.get_or_create_user(user_id, callback.from_user.username)

    # Очищаем старые теги в БД, сохраняя счетчик free_scans_used и подписки
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


# --- ВОПРОС 1 (Множественный выбор) ---
@dp.callback_query(StateFilter(SurveyStates.q1), F.data.startswith("q1_toggle:"))
async def q1_toggle_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    selected: list[int] = data.get("q1_selected", [])

    exclusive_idx = len(Q1_OPTIONS) - 1  # "Ничего не беспокоит"

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


# --- ВОПРОС 2 (Один вариант) ---
@dp.callback_query(StateFilter(SurveyStates.q2), F.data.startswith("q2:"))
async def q2_answer_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    _, tag = Q2_OPTIONS[idx]

    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    if tag and tag not in tags:
        tags.append(tag)

    q2_pergament = (idx == 2)  # Сильно стягивает («пергамент»)
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


# --- ВОПРОС 3 (Один вариант + условная логика) ---
@dp.callback_query(StateFilter(SurveyStates.q3), F.data.startswith("q3:"))
async def q3_answer_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    _, raw_tag = Q3_OPTIONS[idx]

    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    q2_pergament = data.get("q2_pergament", False)

    if raw_tag == "CONDITIONAL_SHINE":
        resolved_tag = "Компенсаторная жирность" if q2_pergament else "Себопрофицит"
    else:
        resolved_tag = raw_tag

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


# --- ВОПРОС 4 (Один вариант) ---
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


# --- ВОПРОС 5 (Множественный выбор) ---
@dp.callback_query(StateFilter(SurveyStates.q5), F.data.startswith("q5_toggle:"))
async def q5_toggle_cb(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    selected: list[int] = data.get("q5_selected", [])

    exclusive_idx = 4  # "Ничего из перечисленного"

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
        await message.answer("Пожалуйста, напишите ваш вариант текстом (кратко одним сообщением):")
        return

    clean_med = sanitize_short_input(message.text, 100)
    await state.update_data(medical_note=clean_med)
    await state.set_state(SurveyStates.q6)

    await message.answer(
        "Вопрос 6. Известная аллергия на компоненты?",
        reply_markup=build_q6_keyboard()
    )


# --- ВОПРОС 6 (Аллергия и финализация анкеты) ---
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
    """
    Финализирует теги после Вопроса 6, выдает ХАРДКОД-диагноз (без вызова ИИ!)
    и переводит на Шаг 3 (Развилка).
    """
    data = await state.get_data()
    tags: list[str] = data.get("tags", [])
    medical_note: str = data.get("medical_note", "")

    if not tags:
        tags = ["Норма"]

    db.save_survey_results(user_id, tags, medical_note)
    await state.clear()

    # Формируем хардкод-диагноз строго по Шагу 2 ТЗ
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

    diagnosis_text = "\n\n".join(paragraphs)
    await message.answer(diagnosis_text)

    # Шаг 3. Развилка (Выбор пути)
    await message.answer(
        "Диагностика завершена. Теперь нам нужно выстроить безопасный физиологичный уход. "
        "Вы можете собрать его самостоятельно, проверяя каждое средство вместе со мной, Романом Андреевичем, "
        "или сразу забрать готовую схему от Марии. Если у вас сейчас вообще нет ухода — мы выстроим его с чистого листа.",
        reply_markup=get_step3_fork_keyboard()
    )


# =====================================================================
# ШАГ 3 (ВЕТВЛЕНИЕ), ШАГ 4 (ВЕТКА А) и ШАГ 5 (ПЭЙВОЛ)
# =====================================================================
PAYWALL_MESSAGE_TEXT = (
    "Бесплатный лимит исчерпан. По двум банкам архитектуру не выстроить, "
    "а наносить непроверенные средства на вашу кожу сейчас небезопасно.\n\n"
    "Выбирайте тариф ниже, и давайте соберем ваш идеальный уход:\n\n"
    "💳 <b>АУДИТ (1000 ₽ / 30 дней)</b>: безлимитный сканер и поиск скрытых угроз.\n"
    "💎 <b>СБОРКА ПРОТОКОЛА (2500 ₽ / 30 дней)</b>: моя интеллектуальная память, "
    "контроль конфликтов и сборка безопасной базы.\n\n"
    "Устали подбирать сами? Закажите Индивидуальный Бокс или PDF-протокол у создателя проекта, Марии:"
)


@dp.callback_query(F.data == "branch_roman")
async def branch_roman_cb(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = db.get_or_create_user(user_id, callback.from_user.username)
    has_sub, _ = db.has_active_scanner_sub(user_id)
    free_used = user.get("free_scans_used", 0)

    if has_sub:
        # Шаг 5.1: Платный режим
        await callback.message.answer(
            "Доступ к лаборатории открыт! Присылайте составы текстом или фотографией этикетки. "
            "Выберите категорию средства для проверки 👇",
            reply_markup=get_categories_keyboard()
        )
    elif free_used == 0:
        # Шаг 4: Первая бесплатная попытка
        await callback.message.answer(
            "Отличный план! Я даю вам 2 бесплатные проверки, чтобы вы протестировали меня в деле. "
            "Прямо сейчас сфотографируйте состав на вашей баночке (или сделайте скриншот состава на маркетплейсе) "
            "либо просто скопируйте его текстом. Но для начала выберите, к какой категории относится средство 👇",
            reply_markup=get_categories_keyboard()
        )
    elif free_used == 1:
        # Вторая бесплатная попытка
        await callback.message.answer(
            "🎁 У вас осталась еще 1 бесплатная попытка! Выберите категорию средства для проверки 👇",
            reply_markup=get_categories_keyboard()
        )
    else:
        # Шаг 5: Бесплатный лимит исчерпан (>= 2) -> Пэйвол
        await callback.message.answer(
            PAYWALL_MESSAGE_TEXT,
            reply_markup=get_paywall_keyboard(),
            parse_mode="HTML"
        )
    await callback.answer()


# =====================================================================
# БЛОК 5. ВЫБОР КАТЕГОРИИ СРЕДСТВА (UI)
# =====================================================================
@dp.callback_query(F.data.startswith("cat:"))
async def select_category_cb(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split(":")[1]
    await state.update_data(selected_category=category)
    await state.set_state(SurveyStates.waiting_for_inci)

    await callback.message.answer(
        "Категория выбрана. Отправьте фотографию состава (четкое фото этикетки или скриншот из интернет-магазина) "
        "либо пришлите список компонентов (INCI) текстом 👇"
    )
    await callback.answer()


# =====================================================================
# ШАГ 6. ВЕТКА Б (ГОТОВЫЙ УХОД ОТ МАРИИ)
# =====================================================================
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


@dp.callback_query(F.data.startswith("branch_maria:"))
async def branch_maria_cb(callback: types.CallbackQuery):
    scenario = callback.data.split(":")[1]
    user_id = callback.from_user.id
    user = db.get_or_create_user(user_id, callback.from_user.username)
    user_name = user.get("user_name") or callback.from_user.first_name or "Клиент"

    if scenario == "2":
        intro = "Прекрасный выбор. Чистый лист — это идеальная возможность сразу выстроить грамотный фундамент. Вы можете заказать у Марии:\n\n"
    else:
        intro = "Прекрасный выбор. Вы можете заказать у Марии:\n\n"

    await callback.message.answer(
        intro + MARIA_PRODUCTS_DESCRIPTION,
        reply_markup=get_step6_maria_keyboard(user_name, user_id),
        parse_mode="HTML"
    )
    await callback.answer()


# =====================================================================
# БЛОК 7. ПОСТОЯННОЕ МЕНЮ КОМАНД (/profile, /tariffs)
# =====================================================================
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
    expires_fmt = ""
    if expires_raw:
        try:
            expires_fmt = datetime.fromisoformat(expires_raw).strftime("%d.%m.%Y %H:%M")
        except Exception:
            expires_fmt = expires_raw

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
        buttons.append([
            InlineKeyboardButton(
                text=f"🗑 Удалить #{idx} ({item['stage']})",
                callback_data=f"del_routine:{item['id']}"
            )
        ])

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
    """Шаг 5.1: Кнопка 'Я только смотрю в магазине (Не добавлять в мой уход)'."""
    record_id = int(callback.data.split(":")[1])
    db.remove_approved_product(record_id, callback.from_user.id)

    # Меняем первую кнопку на подтверждение удаления, оставляя 4 категории ниже
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
    names = {
        "audit_1000": "💳 Аудит (30 дней) — 1 000 ₽",
        "protocol_2500": "💎 Сборка протокола (30 дней) — 2 500 ₽",
        "pdf_4900": "📄 Персональный PDF-протокол — 4 900 ₽",
    }
    await callback.message.answer(
        f"🔧 Формирование платежной ссылки для тарифа:\n<b>{names.get(tariff_code, tariff_code)}</b>\n\n"
        f"(Модуль платежного шлюза подключается на этапе оплаты).",
        parse_mode="HTML"
    )
    await callback.answer()


async def setup_bot_commands():
    commands = [
        BotCommand(command="profile", description="👤 Мой статус"),
        BotCommand(command="tariffs", description="💳 Тарифы и оплата"),
        BotCommand(command="start", description="🔄 Перезапустить диагностику"),
    ]
    await bot.set_my_commands(commands)


async def main():
    logger.info("🚀 Бот (Этап 2: Каркас и Анкета) запущен!")
    try:
        await setup_bot_commands()
    except Exception as e:
        logger.warning(f"Не удалось установить команды меню: {e}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
