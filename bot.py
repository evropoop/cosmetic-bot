import os
import asyncio
import logging
from datetime import datetime

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
from ai_analyzer import analyze_composition

# ================== ЗАГРУЗКА ПЕРЕМЕННЫХ ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 1745568601))
MARIYA_ID = int(os.getenv("MARIYA_ID", 7875791813))

# ===== БЕЗЛИМИТНЫЕ ID =====
UNLIMITED_IDS = [MARIYA_ID, 1962088357]  # Мария + твой ID

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env!")

# ================== НАСТРОЙКА ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db.init_db()

# ================== FSM СОСТОЯНИЯ ==================
class Form(StatesGroup):
    survey = State()
    waiting_for_composition = State()

# ================== КНОПКИ ==================
def get_main_keyboard():
    buttons = [
        [KeyboardButton(text="📋 Моя подписка")],
        [KeyboardButton(text="🆘 Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_subscription_keyboard():
    buttons = [
        [InlineKeyboardButton(text="💳 Оплатить подписку — 1000 ₽/мес", callback_data="pay_subscription")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_lead_keyboard():
    buttons = [
        [InlineKeyboardButton(text="✅ Хочу к Марии", callback_data="want_mariya")],
        [InlineKeyboardButton(text="🚶 Пойду сама", callback_data="go_alone")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ================== АНКЕТА ==================
SURVEY_QUESTIONS = [
    {
        "text": "Вопрос 1. Что тебя сейчас беспокоит больше всего?\n(Можно выбрать несколько)",
        "options": [
            ("Акне, активные воспаления", "problem"),
            ("Закрытые комедоны", "problem"),
            ("Повышенная жирность", "fat"),
            ("Расширенные поры", "fat"),
            ("Сухость, стянутость", "dry"),
            ("Шелушения", "dry"),
            ("Пигментные пятна / постакне", "pigment"),
            ("Мелкая сетка морщин", "age"),
            ("Покраснения, купероз", "reactive"),
        ],
        "multi": True,
    },
    {
        "text": "Вопрос 2. Как чувствует себя кожа через 10 минут после умывания водой?",
        "options": [
            ("Комфортно, нет стянутости", None),
            ("Стягивает терпимо", None),
            ("Стягивает сильно, кожа как пергамент", "barrier_broken"),
            ("Сразу начинает блестеть", None),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 3. Как ведёт себя кожа к середине дня?",
        "options": [
            ("Остаётся нормальной", None),
            ("Блеск только в Т-зоне", None),
            ("Сильно блестит всё лицо", "fat_compensatory"),
            ("Кожа сохнет, макияж проваливается", "barrier_broken"),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 4. Насколько кожа чувствительна к раздражителям?",
        "options": [
            ("Спокойная", None),
            ("Слегка чувствительная", None),
            ("Реактивная (краснеет, горит)", "reactive"),
        ],
        "multi": False,
    },
    {
        "text": "Вопрос 5. Есть ли что-то из перечисленного?\n(Можно выбрать несколько)",
        "options": [
            ("Использую Скинорен/Базирон/ретиноиды", "reactive"),
            ("Гормональные нарушения (СПКЯ, щитовидка)", "medicine"),
            ("Кожные заболевания (розацеа, дерматит)", "medicine"),
            ("Беременность или лактация", "pregnancy"),
            ("Ничего из перечисленного", None),
        ],
        "multi": True,
    },
]


# ================== /START ==================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    db.get_or_create_user(user_id, username)

    await message.answer(
        "Привет! Я — твой личный химик-аналитик. 🧪\n\n"
        "Прежде чем мы начнём зачистку твоей косметички от агрессоров и пустышек, "
        "мне нужно понять, с чем мы работаем.\n\n"
        "Прокликай 5 быстрых вопросов ниже. Погнали? 👇"
    )

    await start_survey(message, state)


# ================== КОДОВОЕ СЛОВО «БОКС» ==================
@dp.message(F.text.lower() == "бокс")
async def code_box(message: types.Message, state: FSMContext):
    await cmd_start(message, state)


# ================== АНКЕТА ==================
async def start_survey(message: types.Message, state: FSMContext):
    await state.set_state(Form.survey)
    await state.update_data(survey_step=0, tags=[])
    await send_question(message, state)


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

    await message.answer(
        q["text"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("q_"))
async def process_answer(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
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

    if tag != "none" and tag not in tags:
        tags.append(tag)
        await state.update_data(tags=tags)

    q = SURVEY_QUESTIONS[step]

    if not q["multi"]:
        await state.update_data(survey_step=step + 1)
        await callback.message.delete()
        await send_question(callback.message, state)
    else:
        await callback.answer("Добавлено! Выбери ещё или нажми «Готово».")
        return

    await callback.answer()


# ================== МИНИ-ДИАГНОЗ ==================
async def finish_survey(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tags = data.get("tags", [])
    user_id = message.chat.id

    db.update_user_tags(user_id, tags)
    await state.clear()

    diagnosis = "Анамнез собран. Давай переведу на понятный язык, что сейчас происходит с твоим лицом:\n\n"

    if "barrier_broken" in tags:
        diagnosis += "— Твой липидный барьер разрушен. Кожа не держит влагу, поэтому её так сильно стягивает после умывания. Ты буквально умываешься «до скрипа», смывая собственный иммунитет.\n\n"

    if "fat_compensatory" in tags or ("fat" in tags and "barrier_broken" in tags):
        diagnosis += "— Тот жирный блеск, который появляется днём, — это не «жирная кожа», это паника твоего организма. Кожа пересушена и пытается защитить себя единственным доступным способом — заливая лицо себумом.\n\n"

    if "reactive" in tags:
        diagnosis += "— Лицо оголено и реактивно. Сейчас кожа воспринимает любую агрессивную отдушку или актив как соль на открытую рану.\n\n"

    if "problem" in tags:
        diagnosis += "— Плюс есть склонность к высыпаниям и забитым порам, так что тяжёлые масла и дешёвые воски нам сейчас категорически противопоказаны.\n\n"

    if "medicine" in tags or "pregnancy" in tags:
        diagnosis += "— Вижу, что в ходу мощные аптечные активы или есть диагноз. Моя задача сейчас — подобрать тебе такую базу, которая успокоит этот пожар и не будет мешать лечению у врача.\n\n"

    if not any(t in tags for t in ["barrier_broken", "fat_compensatory", "reactive", "problem", "medicine", "pregnancy"]):
        diagnosis += "— Глобально всё неплохо, база держится. Главная задача сейчас — не испортить то, что есть, и подобрать грамотную поддержку.\n\n"

    diagnosis += (
        "Ну что, давай посмотрим, чем ты пытаешься спасать ситуацию. "
        "Пришли мне фото состава (INCI) или скопируй текст состава первого средства, "
        "и я скажу, стоит ли мазать это на лицо.\n\n"
        "У тебя есть 2 бесплатные проверки. Жду! 👇"
    )

    await message.answer(diagnosis, reply_markup=get_main_keyboard())
    await state.set_state(Form.waiting_for_composition)


# ================== ПРИЁМ СОСТАВА + АНАЛИЗ ИИ ==================
@dp.message(StateFilter(Form.waiting_for_composition))
async def process_composition(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id)

    # Игнорируем нажатия на кнопки меню
    if message.text in ["📋 Моя подписка", "🆘 Помощь"]:
        if message.text == "📋 Моя подписка":
            await my_subscription(message)
        else:
            await help_text(message)
        return

    # ===== БЕЗЛИМИТ ДЛЯ МАРИИ И ТЕБЯ =====
    is_unlimited = (user_id in UNLIMITED_IDS)

    # Проверка лимита (только для обычных пользователей)
    if not is_unlimited and user["free_checks"] <= 0 and not db.has_active_subscription(user_id):
        await message.answer(
            "Демо-доступ закрыт. Мы проверили 2 средства, но это только верхушка айсберга.\n\n"
            "Хочешь разобрать всю косметичку, перестать сливать деньги на пустышки "
            "и задавать вопросы по уходу 24/7?\n\n"
            "Подписка стоит 1000 ₽/мес. Оформляй и сканируй составы безлимитно!",
            reply_markup=get_subscription_keyboard()
        )
        return

    composition = message.text
    tags = user.get("tags", [])

    await message.answer("🔬 Анализирую состав... Это займёт 10–15 секунд.")

    try:
        result = await analyze_composition(composition, tags)
        verdict = result["verdict"]
        status = result["status"]

        db.save_analysis(user_id, composition, verdict, status)

        if status == "bad":
            db.increment_bad_bottles(user_id)
        else:
            db.reset_bad_bottles(user_id)

        # ===== ДЛЯ БЕЗЛИМИТНЫХ — БЕЗ СЧЁТЧИКОВ И PAYWALL =====
        if is_unlimited:
            await message.answer(verdict)
            await message.answer("👑 Режим безлимита: проверок не ограничено.")
            return

        # Обычные пользователи
        if not db.has_active_subscription(user_id):
            db.decrement_free_checks(user_id)
            remaining = db.get_free_checks(user_id)
            await message.answer(verdict)
            if remaining > 0:
                await message.answer(f"Осталось бесплатных проверок: {remaining}")
            else:
                await message.answer(
                    "Это была твоя последняя бесплатная проверка.\n\n"
                    "Хочешь безлимит? Оформляй подписку за 1000 ₽/мес:",
                    reply_markup=get_subscription_keyboard()
                )
        else:
            await message.answer(verdict)

        # Триггер «Кладбище банок» (только для обычных)
        bad_count = db.get_bad_bottles_count(user_id)
        if bad_count >= 3:
            await message.answer(
                "Слушай, твоя база просто трещит по швам. Мы забраковали уже третью банку подряд.\n\n"
                "Продолжать мазать это на лицо — значит методично уничтожать кожу. "
                "Тут не обойтись заменой одной умывалки, нам нужно менять фундамент.\n\n"
                "Нужна помощь Марии в подборе полноценного физиологичного протокола?",
                reply_markup=get_lead_keyboard()
            )
            db.reset_bad_bottles(user_id)

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await message.answer(
            "❌ Ошибка при анализе. Попробуйте позже или свяжитесь с @miroslavskayaboks"
        )


# ================== МОЯ ПОДПИСКА ==================
@dp.message(F.text == "📋 Моя подписка")
async def my_subscription(message: types.Message):
    user_id = message.from_user.id
    sub = db.get_subscription(user_id)

    if not sub or sub["expires_at"] <= datetime.now():
        await message.answer(
            "❌ У вас нет активной подписки.\n\n"
            "Оформите подписку за 1000 ₽/мес и сканируйте составы безлимитно:",
            reply_markup=get_subscription_keyboard()
        )
    else:
        await message.answer(
            f"✅ Ваша подписка активна до:\n"
            f"📅 {sub['expires_at'].strftime('%d.%m.%Y %H:%M')}"
        )


# ================== ПОМОЩЬ ==================
@dp.message(F.text == "🆘 Помощь")
async def help_text(message: types.Message):
    await message.answer(
        "🤖 Я — твой личный химик-аналитик косметики.\n\n"
        "1. Заполни анкету\n"
        "2. Пришли состав (фото или текст)\n"
        "3. Получи разбор\n\n"
        "📌 Связь: @miroslavskayaboks"
    )


# ================== ЗАПУСК ==================
async def main():
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
