import os
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from dotenv import load_dotenv

import database as db
from ai_analyzer import (
    analyze_composition,
    analyze_composition_photo,
    generate_diagnosis
)

# ================== ЗАГРУЗКА ПЕРЕМЕННЫХ ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 1745568601))
MARIYA_ID = int(os.getenv("MARIYA_ID", 7875791813))

UNLIMITED_IDS = [MARIYA_ID, 1745568601, 1962088357, 5858622813]

CIRCLE_VIDEO_ID = "DQACAgIAAxkBAAIBM2rH78dR-IOML_w1nFX_c4uneVzeAALEpQAC4NRASuj2IqXHtc6tPQQ"
MARIYA_URL = "https://t.me/miroslavskayaboks"

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env!")

# ================== НАСТРОЙКА ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db.init_db()


# ================== FSM ==================
class Form(StatesGroup):
    waiting_for_name = State()
    survey = State()
    waiting_for_medicine = State()
    waiting_for_allergy = State()
    waiting_for_composition = State()


# ================== КЛАВИАТУРЫ ==================
def get_mode_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🧼 Очищение")],
        [KeyboardButton(text="🧴 Несмываемый уход")],
        [KeyboardButton(text="👑 Готовый уход от Марии")]
    ], resize_keyboard=True)


def get_start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать диагностику", callback_data="start_diagnosis")]
    ])


def get_path_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Пойду проверять с Романом Андреевичем", callback_data="go_scanner")],
        [InlineKeyboardButton(text="👑 Хочу готовый уход от Марии", callback_data="want_mariya_box")],
        [InlineKeyboardButton(text="🤷‍♀️ У меня сейчас нет ухода", callback_data="no_care")],
    ])


def get_subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Аудит (30 дней) — 1000 ₽", callback_data="pay_audit_30")],
        [InlineKeyboardButton(text="Восстановление (90 дней) — 2500 ₽", callback_data="pay_restore_90")],
    ])


def get_offer_mariya_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать Марии", url=MARIYA_URL)],
        [InlineKeyboardButton(text="🤖 Пойти в магазин с Романом Андреевичем", callback_data="back_to_scanner")],
    ])


def get_offer_scratch_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Забрать готовый уход у Марии", url=MARIYA_URL)],
        [InlineKeyboardButton(text="🤖 Пойти собирать уход с Романом Андреевичем", callback_data="back_to_scanner")],
    ])


# ================== АНКЕТА (6 ВОПРОСОВ) ==================
SURVEY_QUESTIONS = [
    {
        "text": "Вопрос 1. Что вас сейчас беспокоит больше всего?\n(Можно выбрать несколько)",
        "options": [
            ("Акне / активные воспаления", "problem"),
            ("Закрытые комедоны / бугорки", "problem"),
            ("Сильная жирность днём", "fat"),
            ("Чёрные точки / поры", "fat"),
            ("Сухость / стянутость", "dry"),
            ("Шелушения", "dry"),
            ("Пигментация / постакне", "pigment"),
            ("Морщины / потеря тонуса", "age"),
            ("Покраснения / купероз", "reactive"),
            ("Ничего не беспокоит", "normal"),
        ],
        "multi": True,
    },
    {
        "text": "Вопрос 2. Как чувствует себя ваша кожа через 10 минут после умывания просто водой (до крема)?",
        "options": [
            ("Комфортно, не стягивает", None),
            ("Терпимо, хочется нанести крем", None),
            ("Сильно стягивает («пергамент»)", "barrier_broken"),
            ("Сразу блестит от жирности", None),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 3. Как ведёт себя ваша кожа к середине дня?",
        "options": [
            ("Нормально (без сухости и блеска)", None),
            ("Блестит только Т-зона", None),
            ("Сильно блестит всё лицо", "fat_compensatory"),
            ("Сохнет / шелушится", "barrier_broken"),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 4. Насколько ваша кожа чувствительна к раздражителям (холод, жара, новая косметика)?",
        "options": [
            ("Спокойная (редко краснеет)", None),
            ("Слегка чувствительная", None),
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
            ("Другое (напишу текстом)", "medicine_other"),
            ("Ничего из перечисленного", None),
        ],
        "multi": True,
    },
    {
        "text": "Вопрос 6. И последнее — есть ли у вас известная аллергия на конкретные косметические компоненты?\n\n"
                "(Часто реакцию дают витамин С, ниацинамид, мёд, муцин улитки, эфирные масла, растительные экстракты или металлы — например, никель).",
        "options": [
            ("Нет, аллергии нет", "no_allergy"),
            ("Да, есть (напишу текстом)", "has_allergy"),
        ],
        "multi": False,
    },
]


# ================== /START ==================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    user = db.get_or_create_user(user_id, username)

    if user_id not in UNLIMITED_IDS and user.get("survey_done", 0) == 1:
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

    await message.answer(
        "Жми на кнопку ниже, чтобы Роман Андреевич запустил алгоритм диагностики 👇",
        reply_markup=get_start_keyboard()
    )


@dp.message(F.text.lower() == "бокс")
async def code_box(message: types.Message, state: FSMContext):
    await cmd_start(message, state)


# ================== НАЧАЛО ДИАГНОСТИКИ ==================
@dp.callback_query(F.data == "start_diagnosis")
async def start_diagnosis(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.set_state(Form.waiting_for_name)
    await callback.message.answer(
        "Здравствуйте. Я Роман Андреевич, старший аналитик Skin Protokol и ассистент Марии.\n\n"
        "Моя задача — оценить ваш уход строго индивидуально.\n\n"
        "Для начала напишите, как к вам обращаться:",
        reply_markup=get_mode_keyboard()
    )
    await callback.answer()


# ================== ИМЯ ==================
@dp.message(StateFilter(Form.waiting_for_name))
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(user_name=name)
    await state.set_state(Form.survey)
    await state.update_data(survey_step=0, tags=[])

    await message.answer(
        f"{name}, отлично! А теперь ответьте на 6 быстрых вопросов, чтобы я понял, "
        f"с чем мы работаем. 👇"
    )
    await send_question(message, state)


# ================== АНКЕТА ==================
async def send_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    step = data.get("survey_step", 0)

    if step >= len(SURVEY_QUESTIONS):
        await finish_survey(message, state)
        return

    q = SURVEY_QUESTIONS[step]
    buttons = []
    for opt_text, tag in q["options"]:
        buttons.append([InlineKeyboardButton(text=opt_text, callback_data=f"q_{step}_{tag or 'none'}")])

    if q["multi"]:
        buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"q_{step}_done")])

    if step > 0:
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"back_{step}")])

    await message.answer(
        q["text"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("back_"))
async def process_back(callback: types.CallbackQuery, state: FSMContext):
    step = int(callback.data.split("_")[1])
    new_step = step - 1

    data = await state.get_data()
    tags = data.get("tags", [])

    current_q = SURVEY_QUESTIONS[step]
    for _, tag in current_q["options"]:
        if tag and tag in tags:
            tags.remove(tag)

    await state.update_data(survey_step=new_step, tags=tags)
    await callback.message.delete()
    await send_question(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data.startswith("q_"))
async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_", 2)
    step = int(parts[1])
    tag = parts[2]

    data = await state.get_data()
    tags = data.get("tags", [])

    # ===== Готово =====
    if tag == "done":
        await state.update_data(survey_step=step + 1)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()
        return

    # ===== «Другое» (Вопрос 5) =====
    if step == 4 and tag == "medicine_other":
        await state.set_state(Form.waiting_for_medicine)
        await callback.message.delete()
        await callback.message.answer("Напишите ваш вариант в ответном сообщении:")
        await callback.answer()
        return

    # ===== Вопрос 6: аллергия =====
    if step == 5:
        if tag == "no_allergy":
            await state.update_data(survey_step=6)
            await callback.message.delete()
            await send_question(callback.message, state)
            await callback.answer()
            return

        if tag == "has_allergy":
            await state.update_data(survey_step=6)
            await state.set_state(Form.waiting_for_allergy)
            await callback.message.delete()
            await callback.message.answer("Напишите название аллергена в ответном сообщении:")
            await callback.answer()
            return

    # ===== Вопрос 1: «Ничего не беспокоит» → авто-переход =====
    if step == 0 and tag == "normal":
        # Удаляем все теги, относящиеся к Вопросу 1
        q1_tags = [t for _, t in SURVEY_QUESTIONS[0]["options"] if t]
        tags = [t for t in tags if t not in q1_tags]
        # Добавляем "normal"
        tags.append("normal")
        await state.update_data(tags=tags, survey_step=1)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()
        return

    # ===== Вопрос 1: другой тег → убираем "normal", если он был =====
    if step == 0 and tag != "normal" and "normal" in tags:
        tags.remove("normal")
        await state.update_data(tags=tags)

    # ===== Обычное добавление тега =====
    if tag != "none" and tag not in tags:
        tags.append(tag)
        await state.update_data(tags=tags)

    q = SURVEY_QUESTIONS[step]

    if not q["multi"]:
        await state.update_data(survey_step=step + 1)
        await callback.message.delete()
        await send_question(callback.message, state)
    else:
        await callback.answer("Добавлено! Выберите ещё или нажмите «Готово».")
        return

    await callback.answer()


# ================== «ДРУГОЕ» (ВОПРОС 5) ==================
@dp.message(StateFilter(Form.waiting_for_medicine))
async def process_medicine(message: types.Message, state: FSMContext):
    text = message.text.strip()

    data = await state.get_data()
    tags = data.get("tags", [])
    tags.append("medicine")

    await state.update_data(tags=tags)

    other = data.get("medicine_other", [])
    other.append(text)
    await state.update_data(medicine_other=other)

    await state.set_state(Form.survey)
    await message.answer(f"Записано: «{text}».")
    await send_question(message, state)


# ================== АЛЛЕРГИЯ (ВОПРОС 6) ==================
@dp.message(StateFilter(Form.waiting_for_allergy))
async def process_allergy(message: types.Message, state: FSMContext):
    allergy_text = message.text.strip()

    data = await state.get_data()
    tags = data.get("tags", [])
    tags.append(f"allergy_{allergy_text}")

    await state.update_data(tags=tags)
    await state.set_state(Form.survey)
    await message.answer(f"Записано: аллергия на «{allergy_text}». Учту это при анализе.")
    await send_question(message, state)


# ================== ДИАГНОЗ ОТ ИИ + РАЗВИЛКА (ШАГ 3) ==================
async def finish_survey(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tags = data.get("tags", [])
    user_name = data.get("user_name", "Клиент")
    user_id = message.chat.id

    db.update_user_tags(user_id, tags)
    if user_id not in UNLIMITED_IDS:
        db.mark_survey_completed(user_id)
        logger.info(f"🔒 Карантин 30 дней активирован для user_id={user_id}")
    else:
        logger.info(f"👑 Безлимит: карантин НЕ активирован для user_id={user_id}")

    loading_msg = await message.answer("🔬 Анализирую ваши ответы...")

    try:
        diagnosis = await generate_diagnosis(user_name, tags)
    except Exception as e:
        logger.error(f"Ошибка генерации диагноза: {e}")
        diagnosis = f"{user_name}, диагностика выявила нарушение барьерной функции. Требуется физиологичный подход."

    try:
        await loading_msg.edit_text(diagnosis)
    except Exception as e:
        logger.error(f"Не удалось отредактировать сообщение: {e}")
        await message.answer(diagnosis)

    await message.answer(
        "Диагностика завершена. Теперь нам нужно выстроить безопасный физиологичный уход. "
        "Вы можете собрать его самостоятельно, проверяя каждое средство вместе со мной, "
        "или сразу забрать готовую схему от Марии. Если у вас сейчас вообще нет ухода — "
        "мы выстроим его с чистого листа.",
        reply_markup=get_path_keyboard()
    )

    await state.clear()
    await state.set_state(Form.waiting_for_composition)


# ================== ПЕРЕКЛЮЧАТЕЛЬ РЕЖИМОВ ==================
@dp.message(F.text == "🧼 Очищение")
async def set_mode_wash(message: types.Message, state: FSMContext):
    await state.update_data(category="wash")
    await message.answer(
        "🧼 Режим: *Очищение / Демакияж*.\n\n"
        "Пришлите состав средства (пенка, гель, гидрофильное масло). "
        "Я оценю его с учётом этой категории.",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard()
    )


@dp.message(F.text == "🧴 Несмываемый уход")
async def set_mode_care(message: types.Message, state: FSMContext):
    await state.update_data(category="care")
    await message.answer(
        "🧴 Режим: *Несмываемый уход*.\n\n"
        "Пришлите состав средства (крем, сыворотка, тоник). "
        "Я оценю его с учётом этой категории.",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard()
    )


# ================== ТРИГГЕР ПРОДАЖ: «👑 Готовый уход от Марии» ==================
@dp.message(F.text == "👑 Готовый уход от Марии")
async def offer_mariya_from_menu(message: types.Message, state: FSMContext):
    await message.answer(
        "Отличное решение. Делегировать подбор ухода специалисту — это самый быстрый путь "
        "к здоровой коже. Зачем тратить время на разбор составов и тестирование косметики "
        "вслепую, если можно сразу получить гарантированный результат? Мария берет "
        "ответственность за вашу кожу на себя. Выберите удобный формат работы:\n\n"
        "▫️ Digital-протокол (4 900 ₽) — уход в формате подробного PDF-руководства. "
        "Мария подберет косметику строго под ваш бюджет, даст прямые ссылки на покупку "
        "каждого средства и распишет понятную инструкцию (что и как наносить утром и вечером). "
        "Вы покупаете всё сами по готовому списку. Этот файл останется с вами как надежная база: "
        "Мария добавит позиции на перспективу, чтобы вы всегда знали, какими средствами "
        "дополнить и усилить свой протокол.\n\n"
        "▫️ Индивидуальный Бокс (от 9 000 ₽) — премиальный формат ухода «под ключ». "
        "Вы приобретаете не просто набор косметики, а готовую систему — ваш личный работающий "
        "протокол. Вы получаете эстетичную коробку с полноразмерными средствами, подобранными "
        "строго под вашу кожу, и подробную инструкцию. В эту стоимость уже включено ведение "
        "от Марии: она будет на связи, чтобы контролировать динамику и довести вас до результата. "
        "Вам не нужно ходить по магазинам — забираете посылку и начинаете свой путь к здоровой "
        "коже по протоколу.",
        reply_markup=get_offer_mariya_keyboard()
    )
    current_state = await state.get_state()
    if current_state != Form.waiting_for_composition:
        await state.set_state(Form.waiting_for_composition)


# ================== ВЕТКА «РОМАН АНДРЕЕВИЧ» (ШАГ 4) ==================
@dp.callback_query(F.data == "go_scanner")
async def go_scanner(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Отличный план! Я даю вам 2 бесплатные проверки, чтобы вы протестировали меня "
        "в деле. Прямо сейчас откройте сайт любого косметического магазина (или подойдите "
        "к полке), найдите средство, которое хотите купить или уже используете, и пришлите "
        "мне фото его состава (INCI). Проверим, безопасно ли оно для вашей кожи!",
        reply_markup=get_mode_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)
    await callback.answer()


# ================== ВЕТКА «ГОТОВЫЙ УХОД ОТ МАРИИ» (ШАГ 6) ==================
@dp.callback_query(F.data == "want_mariya_box")
async def want_mariya_box(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()

    await callback.message.answer(
        "Отличное решение. Делегировать подбор ухода специалисту — это самый быстрый путь "
        "к здоровой коже. Зачем тратить время на разбор составов и тестирование косметики "
        "вслепую, если можно сразу получить гарантированный результат? Мария берет "
        "ответственность за вашу кожу на себя. Выберите удобный формат работы:\n\n"
        "▫️ Digital-протокол (4 900 ₽) — уход в формате подробного PDF-руководства. "
        "Мария подберет косметику строго под ваш бюджет, даст прямые ссылки на покупку "
        "каждого средства и распишет понятную инструкцию (что и как наносить утром и вечером). "
        "Вы покупаете всё сами по готовому списку. Этот файл останется с вами как надежная база: "
        "Мария добавит позиции на перспективу, чтобы вы всегда знали, какими средствами "
        "дополнить и усилить свой протокол.\n\n"
        "▫️ Индивидуальный Бокс (от 9 000 ₽) — премиальный формат ухода «под ключ». "
        "Вы приобретаете не просто набор косметики, а готовую систему — ваш личный работающий "
        "протокол. Вы получаете эстетичную коробку с полноразмерными средствами, подобранными "
        "строго под вашу кожу, и подробную инструкцию. В эту стоимость уже включено ведение "
        "от Марии: она будет на связи, чтобы контролировать динамику и довести вас до результата. "
        "Вам не нужно ходить по магазинам — забираете посылку и начинаете свой путь к здоровой "
        "коже по протоколу.",
        reply_markup=get_offer_mariya_keyboard()
    )
    await callback.answer()


# ================== ВЕТКА «С ЧИСТОГО ЛИСТА» (ШАГ 7) ==================
@dp.callback_query(F.data == "no_care")
async def no_care_branch(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()

    await callback.message.answer(
        "Это идеальная ситуация! Выстроить грамотный фундамент с чистого листа гораздо "
        "проще и дешевле, чем восстанавливать кожу после агрессивной косметики. Мы можем "
        "собрать вашу первую косметичку вместе, проверяя средства перед покупкой, или вы "
        "можете сразу забрать готовую систему от Марии, где всё продумано до мелочей:\n\n"
        "▫️ Digital-протокол (4 900 ₽) — уход в формате подробного PDF-руководства. "
        "Мария подберет косметику строго под ваш бюджет, даст прямые ссылки на покупку "
        "каждого средства и распишет понятную инструкцию (что и как наносить утром и вечером). "
        "Вы покупаете всё сами по готовому списку. Этот файл останется с вами как надежная база: "
        "Мария добавит позиции на перспективу, чтобы вы всегда знали, какими средствами "
        "дополнить и усилить свой протокол.\n\n"
        "▫️ Индивидуальный Бокс (от 9 000 ₽) — премиальный формат ухода «под ключ». "
        "Вы приобретаете не просто набор косметики, а готовую систему — ваш личный работающий "
        "протокол. Вы получаете эстетичную коробку с полноразмерными средствами, подобранными "
        "строго под вашу кожу, и подробную инструкцию. В эту стоимость уже включено ведение "
        "от Марии: она будет на связи, чтобы контролировать динамику и довести вас до результата. "
        "Вам не нужно ходить по магазинам — забираете посылку и начинаете свой путь к здоровой "
        "коже по протоколу.",
        reply_markup=get_offer_scratch_keyboard()
    )
    await callback.answer()


# ================== ВОЗВРАТ К СКАНЕРУ ИЗ ШАГОВ 6/7 ==================
@dp.callback_query(F.data == "back_to_scanner")
async def back_to_scanner(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Отличный план! Я даю вам 2 бесплатные проверки, чтобы вы протестировали меня "
        "в деле. Прямо сейчас откройте сайт любого косметического магазина (или подойдите "
        "к полке), найдите средство, которое хотите купить или уже используете, и пришлите "
        "мне фото его состава (INCI). Проверим, безопасно ли оно для вашей кожи!",
        reply_markup=get_mode_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)
    await callback.answer()


# ================== ОБРАБОТКА ФОТО СОСТАВА ==================
@dp.message(StateFilter(Form.waiting_for_composition), F.photo)
async def process_photo_composition(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id)
    data = await state.get_data()
    user_name = data.get("user_name", "")
    category_fsm = data.get("category", None)

    if not category_fsm:
        await message.answer(
            "⚠️ Сначала выберите режим внизу экрана:\n"
            "🧼 Очищение или 🧴 Несмываемый уход",
            reply_markup=get_mode_keyboard()
        )
        return

    if category_fsm == "wash":
        category = "cleansing"
    else:
        category = "leave_on"

    is_unlimited = (user_id in UNLIMITED_IDS)

    if not is_unlimited and user["free_checks"] <= 0 and not db.has_active_subscription(user_id):
        await message.answer(
            "Мой лимит на бесплатную базовую диагностику исчерпан. Если вы хотите проверить "
            "остальные составы и выявить скрытые угрозы, необходимо оформить полный доступ. "
            "Важно: после оплаты я буду жестко откалиброван под ваши ответы в анкете. "
            "Не используйте меня для проверки косметики подруг — анализ чужих средств собьет "
            "настройки вашего профиля.",
            reply_markup=get_subscription_keyboard()
        )
        return

    loading_msg = await message.answer("🔬 Анализирую фото состава...")

    photo = message.photo[-1]
    file_id = photo.file_id

    try:
        file = await bot.get_file(file_id)
        photo_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    except Exception as e:
        logger.error(f"Не удалось получить файл: {e}")
        await loading_msg.edit_text("❌ Не удалось загрузить фото. Попробуйте ещё раз.")
        return

    tags = user.get("tags", [])

    try:
        result = await analyze_composition_photo(photo_url, tags, user_name, category)
        verdict = result["verdict"]
        status = result["status"]

        if status == "error":
            await loading_msg.edit_text(verdict)
            return

        db.save_analysis(user_id, f"[ФОТО] {file_id}", verdict, status)

        if status in ["bad", "hidden_threat"]:
            db.increment_bad_bottles(user_id)
        else:
            db.reset_bad_bottles(user_id)

        if is_unlimited:
            await loading_msg.edit_text(verdict)
            await message.answer("👑 Режим безлимита: проверок не ограничено.")
            return

        if not db.has_active_subscription(user_id):
            db.decrement_free_checks(user_id)
            remaining = db.get_free_checks(user_id)
            await loading_msg.edit_text(verdict)
            if remaining > 0:
                await message.answer(f"Осталось бесплатных проверок: {remaining}")
            else:
                await message.answer(
                    "Это была ваша последняя бесплатная проверка.",
                    reply_markup=get_subscription_keyboard()
                )
        else:
            await loading_msg.edit_text(verdict)

    except Exception as e:
        logger.error(f"Ошибка анализа фото: {e}")
        await loading_msg.edit_text("❌ Ошибка при анализе фото. Попробуйте позже.")


# ================== ОБРАБОТКА ТЕКСТА СОСТАВА ==================
@dp.message(StateFilter(Form.waiting_for_composition))
async def process_composition(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id)
    data = await state.get_data()
    user_name = data.get("user_name", "")
    category_fsm = data.get("category", None)

    if not category_fsm:
        await message.answer(
            "⚠️ Сначала выберите режим внизу экрана:\n"
            "🧼 Очищение или 🧴 Несмываемый уход",
            reply_markup=get_mode_keyboard()
        )
        return

    if category_fsm == "wash":
        category = "cleansing"
    else:
        category = "leave_on"

    is_unlimited = (user_id in UNLIMITED_IDS)

    if not is_unlimited and user["free_checks"] <= 0 and not db.has_active_subscription(user_id):
        await message.answer(
            "Мой лимит на бесплатную базовую диагностику исчерпан. Если вы хотите проверить "
            "остальные составы и выявить скрытые угрозы, необходимо оформить полный доступ. "
            "Важно: после оплаты я буду жестко откалиброван под ваши ответы в анкете. "
            "Не используйте меня для проверки косметики подруг — анализ чужих средств собьет "
            "настройки вашего профиля.",
            reply_markup=get_subscription_keyboard()
        )
        return

    composition = message.text
    tags = user.get("tags", [])

    loading_msg = await message.answer("🔬 Анализирую состав...")

    try:
        result = await analyze_composition(composition, tags, user_name, category)
        verdict = result["verdict"]
        status = result["status"]

        if status == "error":
            await loading_msg.edit_text(verdict)
            return

        db.save_analysis(user_id, composition, verdict, status)

        if status in ["bad", "hidden_threat"]:
            db.increment_bad_bottles(user_id)
        else:
            db.reset_bad_bottles(user_id)

        if is_unlimited:
            await loading_msg.edit_text(verdict)
            await message.answer("👑 Режим безлимита: проверок не ограничено.")
            return

        if not db.has_active_subscription(user_id):
            db.decrement_free_checks(user_id)
            remaining = db.get_free_checks(user_id)
            await loading_msg.edit_text(verdict)
            if remaining > 0:
                await message.answer(f"Осталось бесплатных проверок: {remaining}")
            else:
                await message.answer(
                    "Это была ваша последняя бесплатная проверка.",
                    reply_markup=get_subscription_keyboard()
                )
        else:
            await loading_msg.edit_text(verdict)

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await loading_msg.edit_text("❌ Ошибка при анализе. Попробуйте позже.")


# ================== ОПЛАТА (ЗАГЛУШКИ) ==================
@dp.callback_query(F.data == "pay_audit_30")
async def pay_audit_30(callback: types.CallbackQuery):
    await callback.message.answer(
        "🔧 Тариф «Аудит» (30 дней, 1000 ₽) — оплата будет подключена на следующем этапе "
        "(Продамус / ЮKassa)."
    )
    await callback.answer()


@dp.callback_query(F.data == "pay_restore_90")
async def pay_restore_90(callback: types.CallbackQuery):
    await callback.message.answer(
        "🔧 Тариф «Восстановление» (90 дней, 2500 ₽) — оплата будет подключена на следующем "
        "этапе (Продамус / ЮKassa)."
    )
    await callback.answer()


# ================== ПОДПИСКА ==================
@dp.message(F.text == "/sub")
async def my_subscription(message: types.Message):
    user_id = message.from_user.id

    if user_id in UNLIMITED_IDS:
        await message.answer(
            "👑 *Режим безлимита активен.*\n\n"
            "У вас неограниченный доступ к анализу составов.",
            parse_mode="Markdown"
        )
        return

    sub = db.get_subscription(user_id)

    if not sub or sub["expires_at"] <= datetime.now():
        await message.answer(
            "❌ У вас нет активной подписки.\n\n"
            "Оформите доступ:",
            reply_markup=get_subscription_keyboard()
        )
    else:
        await message.answer(
            f"✅ Ваша подписка активна до:\n"
            f"📅 {sub['expires_at'].strftime('%d.%m.%Y %H:%M')}"
        )


# ================== ПОМОЩЬ ==================
@dp.message(F.text == "/help")
async def help_text(message: types.Message):
    await message.answer(
        "🤖 Я — Роман Андреевич, ИИ-аналитик косметических составов.\n\n"
        "1. Пройдите диагностику\n"
        "2. Выберите режим внизу экрана:\n"
        "   🧼 Очищение — для пенок, гелей, гидрофильных масел\n"
        "   🧴 Несмываемый уход — для кремов, сывороток, тоников\n"
        "   👑 Готовый уход от Марии — премиальный сервис подбора\n"
        "3. Пришлите фото состава или текст (INCI)\n"
        "4. Получите разбор\n\n"
        "📌 Связь: @miroslavskayaboks"
    )


# ================== ЗАПУСК ==================
async def main():
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
