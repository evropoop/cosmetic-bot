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

# Ссылки на документы
OFFER_URL = "https://docs.google.com/document/d/1FOaPp-S5rpJSlg2qWukQ3cyBCjxhEbfd0LCLaM4gE08/edit?usp=sharing"
CONSENT_URL = "https://docs.google.com/document/d/1OzOPwWki-_hdjY30R0A9Wp3xsdBGvrlnNPRT-IhgynA/edit?usp=sharing"

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env!")

# ================== НАСТРОЙКА ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db.init_db()

# Статусы, при которых лимит СПИСЫВАЕТСЯ
ANALYZABLE_STATUSES = {"bad", "hidden_threat", "conditional", "good", "neutral", "hydrophilic"}


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


def get_next_category_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🫧 Смываемое (всё, что смывается водой)", callback_data="next_wash")],
        [InlineKeyboardButton(text="🧴 Несмываемое (всё, что остается на коже)", callback_data="next_care")],
    ])


def get_accept_offer_keyboard(target: str):
    """target = 'start' или 'pay'"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять оферту", callback_data=f"accept_offer_{target}")]
    ])


def get_accept_consent_keyboard(target: str):
    """target = 'start' или 'pay'"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Даю согласие", callback_data=f"accept_consent_{target}")]
    ])


def get_start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать диагностику", callback_data="start_diagnosis")]
    ])


def get_path_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Пойду проверять с Романом Андреевичем", callback_data="go_scanner")],
        [InlineKeyboardButton(text="👑 Хочу готовый уход от Марии", callback_data="want_mariya_box")],
        [InlineKeyboardButton(text="🤷‍♀️ У меня сейчас нет ухода", callback_data="no_care")],
    ])


def get_subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить Аудит — 1000 ₽", callback_data="pay_audit_30")],
        [InlineKeyboardButton(text="🔥 Оплатить Восстановление — 2500 ₽", callback_data="pay_restore_90")],
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


# ================== RETENTION-УВЕДОМЛЕНИЯ ==================
RETENTION_TEXTS = {
    "1m": (
        "Проверка завершена — доступ к полному алгоритму пока не активирован. "
        "Если передумаете, я на связи."
    ),
    "2h": (
        "Вы остановились на этапе активации доступа. Ваша анкета сохранена. "
        "Чтобы я мог проанализировать вашу косметичку и выстроить протокол "
        "восстановления барьера, завершите оформление. Я готов приступить к работе."
    ),
    "24h": (
        "Прошли сутки с момента нашей паузы. Напоминаю, что каждый день "
        "использования неподходящей косметики усугубляет состояние гидролипидной "
        "мантии. Давайте остановим этот процесс и проведем аудит прямо сейчас. "
        "Открываем доступ?"
    ),
    "48h": (
        "Я закрываю ваш незаконченный сеанс в системе. Если вы решите навести "
        "порядок в домашнем уходе и перестать тратить деньги на агрессивные "
        "средства, возвращайтесь. Бот будет готов к работе, когда вы примете решение."
    ),
    "7d": (
        "Прошла неделя с момента вашего визита. За это время вы нанесли свой "
        "привычный уход минимум 14 раз. Если в вашей косметичке есть скрытые "
        "аллергены или разрушающие барьер активы, за эту неделю вы усугубили "
        "дефицит липидов. Готовы провести химический аудит?"
    ),
    "30d": (
        "Прошел ровно месяц. В дерматологии 28–35 дней — это полный цикл "
        "обновления эпидермиса. Если вы не меняли стратегию ухода, состояние кожи "
        "осталось прежним или ухудшилось. Если вы устали тестировать косметику "
        "вслепую, доступ к системе открыт."
    ),
}


async def _send_retention_notification(user_id: int, notif_type: str):
    text = RETENTION_TEXTS.get(notif_type)
    if not text:
        return
    try:
        await bot.send_message(
            user_id,
            f"🤖 Роман Андреевич: {text}",
            reply_markup=get_subscription_keyboard(),
            parse_mode="HTML"
        )
        logger.info(f"📨 Retention '{notif_type}' → user_id={user_id}")
    except Exception as e:
        logger.warning(f"Не удалось отправить retention {notif_type} юзеру {user_id}: {e}")


async def retention_worker():
    logger.info("⏰ Retention-воркер запущен")
    while True:
        try:
            due = db.get_due_notifications()
            for n in due:
                await _send_retention_notification(n["user_id"], n["type"])
                db.mark_notification_sent(n["id"])
        except Exception as e:
            logger.error(f"Ошибка в retention-воркере: {e}")

        await asyncio.sleep(30)


# ================== ВСПОМОГАТЕЛЬНАЯ: имя ==================
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

    # ШАГ 1: Публичная оферта
    await message.answer(
        f"Для продолжения работы, пожалуйста, ознакомьтесь с <a href='{OFFER_URL}'>Публичной офертой</a>.\n\n"
        "Нажмите кнопку ниже, чтобы принять условия.",
        reply_markup=get_accept_offer_keyboard("start"),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@dp.callback_query(F.data == "accept_offer_start")
async def handle_offer_start(callback: types.CallbackQuery):
    # ШАГ 2: Согласие на обработку
    await callback.message.edit_text(
        f"Теперь, пожалуйста, ознакомьтесь с <a href='{CONSENT_URL}'>Согласием на обработку персональных данных</a>.\n\n"
        "Нажмите кнопку ниже, чтобы дать согласие.",
        reply_markup=get_accept_consent_keyboard("start"),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()


@dp.callback_query(F.data == "accept_consent_start")
async def handle_consent_start(callback: types.CallbackQuery):
    # ШАГ 3: Финальное сообщение с кнопкой старта
    await callback.message.edit_text(
        "Спасибо! Условия приняты.\n\n"
        "Жми на кнопку ниже, чтобы Роман Андреевич запустил алгоритм диагностики 👇",
        reply_markup=get_start_keyboard()
    )
    await callback.answer()


# ================== ПЭЙВОЛ (СРАЗУ ТАРИФЫ) ==================
PAYWALL_TEXT = (
    "Выберите формат работы с Романом Андреевичем:\n\n"
    "💳 <b>Аудит (30 дней) — 1000 ₽</b>\n"
    "Полная ревизия вашей косметички.\n"
    "• Безлимитный разбор составов на месяц.\n"
    "• Поиск скрытых угроз и маркетинговых пустышек.\n"
    "• Защита от покупок комедогенной косметики.\n\n"
    "💳 <b>Восстановление (90 дней) — 2500 ₽ 🔥</b>\n"
    "Сопровождение на полный цикл обновления кожи.\n"
    "• 3 месяца безлимитного разбора составов.\n"
    "• 2 дополнительных перезаполнения анкеты. За 3 месяца кожа восстановится и ее потребности изменятся. Вы сможете заново пройти стартовый опрос, чтобы бот обновил данные и перестроил алгоритм проверки под ваше новое, здоровое состояние.\n\n"
    "👇 Оформите подписку, выбрав тариф ниже:"
)


# ================== СОГЛАСИЯ ПЕРЕД ОПЛАТОЙ ==================
# Срабатывает при нажатии на кнопку тарифа (pay_audit_30 / pay_restore_90)
@dp.callback_query(F.data == "pay_audit_30")
async def pay_audit_30(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(pending_tariff="audit")
    await callback.message.answer(
        f"Для оформления тарифа «Аудит» необходимо принять условия.\n\n"
        f"Ознакомьтесь с <a href='{OFFER_URL}'>Публичной офертой</a>.",
        reply_markup=get_accept_offer_keyboard("pay"),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()


@dp.callback_query(F.data == "pay_restore_90")
async def pay_restore_90(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(pending_tariff="restore")
    await callback.message.answer(
        f"Для оформления тарифа «Восстановление» необходимо принять условия.\n\n"
        f"Ознакомьтесь с <a href='{OFFER_URL}'>Публичной офертой</a>.",
        reply_markup=get_accept_offer_keyboard("pay"),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()


@dp.callback_query(F.data == "accept_offer_pay")
async def handle_offer_pay(callback: types.CallbackQuery):
    await callback.message.edit_text(
        f"Теперь, пожалуйста, ознакомьтесь с <a href='{CONSENT_URL}'>Согласием на обработку персональных данных</a>.",
        reply_markup=get_accept_consent_keyboard("pay"),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await callback.answer()


@dp.callback_query(F.data == "accept_consent_pay")
async def handle_consent_pay(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tariff = data.get("pending_tariff", "audit")

    if tariff == "restore":
        text = (
            "✅ Спасибо! Согласия получены.\n\n"
            "Перенаправляю на оплату тарифа «Восстановление (90 дней) — 2500 ₽».\n\n"
            "🔧 Оплата будет подключена на следующем этапе (Продамус)."
        )
    else:
        text = (
            "✅ Спасибо! Согласия получены.\n\n"
            "Перенаправляю на оплату тарифа «Аудит (30 дней) — 1000 ₽».\n\n"
            "🔧 Оплата будет подключена на следующем этапе (Продамус)."
        )

    await callback.message.edit_text(text)
    await state.update_data(pending_tariff=None)
    await callback.answer()


# ================== СТАРТ ДИАГНОСТИКИ ==================
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


@dp.message(StateFilter(Form.waiting_for_name))
async def process_name(message: types.Message, state: FSMContext):
    name = message.text.strip()

    await state.update_data(user_name=name)
    try:
        db.update_user_name(message.from_user.id, name)
    except Exception as e:
        logger.warning(f"Не удалось сохранить имя в БД: {e}")

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


@dp.callback_query(F.data.regexp(r"^back_\d+$"))
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


@dp.callback_query(F.data.regexp(r"^q_\d+_.+$"))
async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_", 2)
    step = int(parts[1])
    tag = parts[2]

    data = await state.get_data()
    tags = data.get("tags", [])

    if tag == "done":
        await state.update_data(survey_step=step + 1)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()
        return

    if step == 4 and tag == "medicine_other":
        await state.set_state(Form.waiting_for_medicine)
        await callback.message.delete()
        await callback.message.answer("Напишите ваш вариант в ответном сообщении:")
        await callback.answer()
        return

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

    if step == 0 and tag == "normal":
        q1_tags = [t for _, t in SURVEY_QUESTIONS[0]["options"] if t]
        tags = [t for t in tags if t not in q1_tags]
        tags.append("normal")
        await state.update_data(tags=tags, survey_step=1)
        await callback.message.delete()
        await send_question(callback.message, state)
        await callback.answer()
        return

    if step == 0 and tag != "normal" and "normal" in tags:
        tags.remove("normal")
        await state.update_data(tags=tags)

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


# ================== ДИАГНОЗ + РАЗВИЛКА ==================
async def finish_survey(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tags = data.get("tags", [])

    user_name = data.get("user_name")
    if not user_name:
        try:
            user_name = db.get_user_name(message.chat.id)
        except Exception:
            user_name = None
    if not user_name:
        user_name = message.from_user.first_name or "Клиент"

    await state.update_data(user_name=user_name)

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

    await state.set_state(Form.waiting_for_composition)


# ================== РЕЖИМЫ ==================
@dp.message(F.text == "🧼 Очищение")
async def set_mode_wash(message: types.Message, state: FSMContext):
    user = db.get_or_create_user(message.from_user.id)
    await _resolve_user_name(message, state, user)

    await state.update_data(category="wash")
    await message.answer(
        "🧼 Режим: *Очищение / Демакияж*.\n\n"
        "Пришлите состав средства (пенка, гель, гидрофильное масло).",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard()
    )


@dp.message(F.text == "🧴 Несмываемый уход")
async def set_mode_care(message: types.Message, state: FSMContext):
    user = db.get_or_create_user(message.from_user.id)
    await _resolve_user_name(message, state, user)

    await state.update_data(category="care")
    await message.answer(
        "🧴 Режим: *Несмываемый уход*.\n\n"
        "Пришлите состав средства (крем, сыворотка, тоник).",
        parse_mode="Markdown",
        reply_markup=get_mode_keyboard()
    )


@dp.callback_query(F.data == "next_wash")
async def next_category_wash(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.update_data(category="wash")
    await callback.message.answer(
        "🧼 Режим: *Смываемое средство*.\n\n"
        "Пришлите состав (пенка, гель, гидрофильное масло).",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "next_care")
async def next_category_care(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.update_data(category="care")
    await callback.message.answer(
        "🧴 Режим: *Несмываемое средство*.\n\n"
        "Пришлите состав (крем, сыворотка, тоник).",
        parse_mode="Markdown"
    )
    await callback.answer()


# ================== ТРИГГЕР ПРОДАЖ ==================
@dp.message(F.text == "👑 Готовый уход от Марии")
async def offer_mariya_from_menu(message: types.Message, state: FSMContext):
    await message.answer(
        "Отличное решение. Делегировать подбор ухода специалисту — это самый быстрый путь "
        "к здоровой коже. Зачем тратить время на разбор составов и тестирование косметики "
        "вслепую, если можно сразу получить гарантированный результат? Мария берет "
        "ответственность за вашу кожу на себя. Выберите удобный формат работы:\n\n"
        "▫️ Digital-протокол (4 900 ₽) — уход в формате подробного PDF-руководства. "
        "Мария подберет косметику строго под ваш бюджет, даст прямые ссылки на покупку "
        "каждого средства и распишет понятную инструкцию.\n\n"
        "▫️ Индивидуальный Бокс (от 9 000 ₽) — премиальный формат ухода «под ключ». "
        "Эстетичная коробка с полноразмерными средствами + ведение от Марии.",
        reply_markup=get_offer_mariya_keyboard()
    )

    user = db.get_or_create_user(message.from_user.id)
    await _resolve_user_name(message, state, user)

    current_state = await state.get_state()
    if current_state != Form.waiting_for_composition:
        await state.set_state(Form.waiting_for_composition)


# ================== ШАГ 4 ==================
@dp.callback_query(F.data == "go_scanner")
async def go_scanner(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Отличный план! Я даю вам 2 бесплатные проверки. Прямо сейчас откройте сайт "
        "любого косметического магазина (или подойдите к полке), найдите средство, "
        "которое хотите купить или уже используете, и пришлите мне фото его состава (INCI).",
        reply_markup=get_mode_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)
    await callback.answer()


# ================== ШАГ 6 ==================
@dp.callback_query(F.data == "want_mariya_box")
async def want_mariya_box(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()

    await callback.message.answer(
        "Отличное решение. Мария берет ответственность за вашу кожу на себя.\n\n"
        "▫️ Digital-протокол (4 900 ₽) — подробное PDF-руководство: подбор косметики "
        "под ваш бюджет, прямые ссылки на покупку и инструкция по нанесению.\n\n"
        "▫️ Индивидуальный Бокс (от 9 000 ₽) — премиальный уход «под ключ»: "
        "эстетичная коробка с полноразмерными средствами + постоянное ведение от Марии.",
        reply_markup=get_offer_mariya_keyboard()
    )
    await callback.answer()


# ================== ШАГ 7 ==================
@dp.callback_query(F.data == "no_care")
async def no_care_branch(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()

    await callback.message.answer(
        "Это идеальная ситуация! Выстроить грамотный фундамент с чистого листа гораздо "
        "проще и дешевле, чем восстанавливать кожу после агрессивной косметики:\n\n"
        "▫️ Digital-протокол (4 900 ₽) — подробное PDF-руководство: подбор косметики "
        "под ваш бюджет, прямые ссылки на покупку и инструкция по нанесению.\n\n"
        "▫️ Индивидуальный Бокс (от 9 000 ₽) — премиальный уход «под ключ»: "
        "эстетичная коробка с полноразмерными средствами + постоянное ведение от Марии.",
        reply_markup=get_offer_scratch_keyboard()
    )
    await callback.answer()


# ================== ВОЗВРАТ В СКАНЕР ==================
@dp.callback_query(F.data == "back_to_scanner")
async def back_to_scanner(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Отличный план! Я даю вам 2 бесплатные проверки. Пришлите состав средства (INCI).",
        reply_markup=get_mode_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)
    await callback.answer()


# ================== ФОТО СОСТАВА ==================
@dp.message(StateFilter(Form.waiting_for_composition), F.photo)
async def process_photo_composition(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id)

    user_name = await _resolve_user_name(message, state, user)

    data = await state.get_data()
    category_fsm = data.get("category", None)

    if not category_fsm:
        await message.answer(
            "⚠️ Сначала выберите тип средства: смываемое оно или нет.",
            reply_markup=get_next_category_keyboard()
        )
        return

    if category_fsm == "wash":
        category = "cleansing"
    else:
        category = "leave_on"

    is_unlimited = (user_id in UNLIMITED_IDS)

    # ===== ПЭЙВОЛ =====
    if not is_unlimited and user["free_checks"] <= 0 and not db.has_active_subscription(user_id):
        db.schedule_retention_notifications(user_id)
        await message.answer(
            PAYWALL_TEXT,
            reply_markup=get_subscription_keyboard(),
            parse_mode="HTML",
            disable_web_page_preview=True
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
        await loading_msg.edit_text(
            "❌ Не удалось загрузить фото. Попробуйте ещё раз — лимит не списан."
        )
        return

    tags = user.get("tags", [])

    try:
        result = await analyze_composition_photo(photo_url, tags, user_name, category)
        verdict = result["verdict"]
        status = result["status"]

        # ===== НЕ РАСПОЗНАЛ → НЕ СПИСЫВАЕМ =====
        if status not in ANALYZABLE_STATUSES:
            await loading_msg.edit_text(
                "⚠️ Фото не читается полностью.\n\n"
                "Пожалуйста, сделайте более чёткое фото этикетки (при хорошем свете, "
                "крупным планом, чтобы был виден весь список) или пришлите состав текстом.\n\n"
                "Лимит не списан, попробуйте ещё раз."
            )
            return

        db.save_analysis(user_id, f"[ФОТО] {file_id}", verdict, status)

        if status in ["bad", "hidden_threat"]:
            db.increment_bad_bottles(user_id)
        else:
            db.reset_bad_bottles(user_id)

        if is_unlimited:
            await loading_msg.edit_text(verdict)
            await message.answer("👑 Режим безлимита: проверок не ограничено.")
        elif not db.has_active_subscription(user_id):
            db.decrement_free_checks(user_id)
            remaining = db.get_free_checks(user_id)
            await loading_msg.edit_text(verdict)
            if remaining > 0:
                await message.answer(f"Осталось бесплатных проверок: {remaining}")
            else:
                db.schedule_retention_notifications(user_id)
                await message.answer(
                    "Это была ваша последняя бесплатная проверка.",
                    reply_markup=get_subscription_keyboard()
                )
        else:
            await loading_msg.edit_text(verdict)

        await state.update_data(category=None)
        await message.answer(
            "Проверим следующее средство? Уточните, оно смываемое или нет:",
            reply_markup=get_next_category_keyboard()
        )

    except Exception as e:
        logger.error(f"Ошибка анализа фото: {e}")
        await loading_msg.edit_text(
            "❌ Техническая ошибка. Попробуйте ещё раз — лимит не списан."
        )


# ================== ТЕКСТ СОСТАВА ==================
@dp.message(StateFilter(Form.waiting_for_composition))
async def process_composition(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id)

    user_name = await _resolve_user_name(message, state, user)

    data = await state.get_data()
    category_fsm = data.get("category", None)

    if not category_fsm:
        await message.answer(
            "⚠️ Сначала выберите тип средства: смываемое оно или нет.",
            reply_markup=get_next_category_keyboard()
        )
        return

    if category_fsm == "wash":
        category = "cleansing"
    else:
        category = "leave_on"

    is_unlimited = (user_id in UNLIMITED_IDS)

    # ===== ПЭЙВОЛ =====
    if not is_unlimited and user["free_checks"] <= 0 and not db.has_active_subscription(user_id):
        db.schedule_retention_notifications(user_id)
        await message.answer(
            PAYWALL_TEXT,
            reply_markup=get_subscription_keyboard(),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        return

    composition = message.text
    tags = user.get("tags", [])

    loading_msg = await message.answer("🔬 Анализирую состав...")

    try:
        result = await analyze_composition(composition, tags, user_name, category)
        verdict = result["verdict"]
        status = result["status"]

        # ===== НЕ РАСПОЗНАЛ → НЕ СПИСЫВАЕМ =====
        if status not in ANALYZABLE_STATUSES:
            await loading_msg.edit_text(
                "⚠️ В этом сообщении я не вижу состава (INCI).\n\n"
                "Пришлите, пожалуйста, именно список ингредиентов с упаковки — "
                "текстом или фото. Вопросы, жалобы и общие фразы я не анализирую.\n\n"
                "Лимит не списан, попробуйте ещё раз."
            )
            return

        db.save_analysis(user_id, composition, verdict, status)

        if status in ["bad", "hidden_threat"]:
            db.increment_bad_bottles(user_id)
        else:
            db.reset_bad_bottles(user_id)

        if is_unlimited:
            await loading_msg.edit_text(verdict)
            await message.answer("👑 Режим безлимита: проверок не ограничено.")
        elif not db.has_active_subscription(user_id):
            db.decrement_free_checks(user_id)
            remaining = db.get_free_checks(user_id)
            await loading_msg.edit_text(verdict)
            if remaining > 0:
                await message.answer(f"Осталось бесплатных проверок: {remaining}")
            else:
                db.schedule_retention_notifications(user_id)
                await message.answer(
                    "Это была ваша последняя бесплатная проверка.",
                    reply_markup=get_subscription_keyboard()
                )
        else:
            await loading_msg.edit_text(verdict)

        await state.update_data(category=None)
        await message.answer(
            "Проверим следующее средство? Уточните, оно смываемое или нет:",
            reply_markup=get_next_category_keyboard()
        )

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await loading_msg.edit_text(
            "❌ Техническая ошибка. Попробуйте ещё раз — лимит не списан."
        )


# ================== ПОДПИСКА ==================
@dp.message(F.text == "/sub")
async def my_subscription(message: types.Message):
    user_id = message.from_user.id

    if user_id in UNLIMITED_IDS:
        await message.answer(
            "👑 *Режим безлимита активен.*",
            parse_mode="Markdown"
        )
        return

    sub = db.get_subscription(user_id)

    if not sub or sub["expires_at"] <= datetime.now():
        await message.answer(
            "❌ У вас нет активной подписки.\n\nОформите доступ:",
            reply_markup=get_subscription_keyboard()
        )
    else:
        await message.answer(
            f"✅ Ваша подписка активна до:\n"
            f"📅 {sub['expires_at'].strftime('%d.%m.%Y %H:%M')}"
        )


@dp.message(F.text == "/help")
async def help_text(message: types.Message):
    await message.answer(
        "🤖 Я — Роман Андреевич, ИИ-аналитик косметических составов.\n\n"
        "1. Пройдите диагностику\n"
        "2. Выберите тип средства:\n"
        "   🫧 Смываемое — пенки, гели, гидрофильные масла\n"
        "   🧴 Несмываемое — кремы, сыворотки, тоники\n"
        "   👑 Готовый уход от Марии — премиальный сервис подбора\n"
        "3. Пришлите фото состава или текст (INCI)\n"
        "4. Получите разбор\n\n"
        "📌 Связь: @miroslavskayaboks"
    )


# ================== ЗАПУСК ==================
async def main():
    logger.info("🚀 Бот запущен!")

    try:
        await bot.set_my_description(
            description=(
                "Я — Роман Андреевич, ИИ-аналитик косметических составов проекта Skin Protokol. "
                "Провожу диагностику кожи, разбираю состав средств (INCI) и защищаю вашу кожу "
                "от агрессивной косметики.\n\n"
                "2 бесплатные проверки для новых пользователей.\n\n"
                "Исполнитель: Мирославская Мария Владимировна\n"
                "ИНН: 420302908032"
            )
        )
        await bot.set_my_short_description(
            short_description=(
                "AI-аудитор косметики. Разбор составов (INCI) под вашу кожу.\n"
                "Исполнитель: ИП Мирославская М.В., ИНН 420302908032"
            )
        )
        logger.info("✅ Описание бота обновлено (с реквизитами ИП)")
    except Exception as e:
        logger.error(f"Не удалось обновить описание бота: {e}")

    asyncio.create_task(retention_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
