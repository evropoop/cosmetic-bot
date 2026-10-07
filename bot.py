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
from ai_analyzer import analyze_composition, generate_diagnosis

# ================== ЗАГРУЗКА ПЕРЕМЕННЫХ ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 1745568601))
MARIYA_ID = int(os.getenv("MARIYA_ID", 7875791813))

UNLIMITED_IDS = [MARIYA_ID, 1962088357]

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
    waiting_for_composition = State()

# ================== КНОПКИ ==================
def get_start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать диагностику", callback_data="start_diagnosis")]
    ])


def get_main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Моя подписка")],
        [KeyboardButton(text="🆘 Помощь")]
    ], resize_keyboard=True)


def get_path_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Пойду выбирать со сканером", callback_data="go_scanner")],
        [InlineKeyboardButton(text="👑 Хочу готовый уход от Марии", callback_data="want_mariya_box")],
        [InlineKeyboardButton(text="🤷‍♀️ У меня сейчас нет ухода", callback_data="no_care")],
    ])


def get_subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить доступ (1000 ₽)", callback_data="pay_scanner")],
    ])


def get_final_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать Марии", url="https://t.me/miroslavskayaboks")],
    ])


# ================== АНКЕТА ==================
SURVEY_QUESTIONS = [
    {
        "text": "Вопрос 1. Что вас сейчас беспокоит больше всего?\n(Можно выбрать несколько)",
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

    # TODO: здесь будет кружочек Марии
    await message.answer(
        "👋 Жми на кнопку ниже, чтобы Роман Андреевич запустил алгоритм диагностики 👇",
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
        "Моя задача — оценить ваш уход строго индивидуально. Напишите, пожалуйста, "
        "как я могу к вам обращаться?"
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
        f"{name}, очень приятно. Теперь ответьте, пожалуйста, на 5 коротких вопросов.",
        reply_markup=get_main_keyboard()
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
        await callback.answer("Добавлено! Выберите ещё или нажмите «Готово».")
        return

    await callback.answer()


# ================== ДИАГНОЗ ОТ ИИ + РАЗВИЛКА ==================
async def finish_survey(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tags = data.get("tags", [])
    user_name = data.get("user_name", "Клиент")
    user_id = message.chat.id

    db.update_user_tags(user_id, tags)

    await message.answer("🔬 Анализирую ваши ответы...")

    try:
        diagnosis = await generate_diagnosis(user_name, tags)
    except Exception as e:
        logger.error(f"Ошибка генерации диагноза: {e}")
        diagnosis = f"{user_name}, анализ завершён. Я беру вашу ситуацию под контроль: наша задача — выстроить надёжный физиологичный фундамент."

    await message.answer(diagnosis)

    await message.answer(
        "Диагностика завершена. Теперь нам нужно выстроить безопасный физиологичный уход. "
        "Вы можете собрать его самостоятельно, проверяя каждую банку через мой сканер, "
        "или сразу забрать готовую схему от Марии. Если у вас сейчас вообще нет ухода — "
        "мы выстроим его с чистого листа.",
        reply_markup=get_path_keyboard()
    )

    await state.clear()
    await state.set_state(Form.waiting_for_composition)


# ================== ВЕТКА «СКАНЕР» ==================
@dp.callback_query(F.data == "go_scanner")
async def go_scanner(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Отличный план! Я даю вам 2 бесплатные проверки, чтобы вы протестировали меня в деле.\n\n"
        "Прямо сейчас откройте сайт любого косметического магазина (или подойдите к полке), "
        "найдите средство, которое хотите купить или уже используете, и пришлите мне фото "
        "его состава (INCI). Проверим, безопасно ли оно для вашей кожи!",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)
    await callback.answer()


# ================== ВЕТКИ «ГОТОВЫЙ УХОД» И «НЕТ УХОДА» ==================
@dp.callback_query(F.data.in_(["want_mariya_box", "no_care"]))
async def want_mariya_box(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()

    if callback.data == "no_care":
        intro = (
            "Прекрасный выбор. Чистый лист — это идеальная возможность сразу выстроить "
            "грамотный фундамент без ошибок и лишних трат. Мария берёт ответственность "
            "за результат на себя.\n\n"
        )
    else:
        intro = (
            "Прекрасный выбор. Мария берёт ответственность за результат на себя.\n\n"
        )

    await callback.message.answer(
        intro +
        "Выберите удобный формат работы:\n\n"
        "▫️ Digital-протокол (4 900 ₽) — уход в формате подробного PDF-руководства. "
        "Мария подберёт косметику строго под ваш бюджет, даст прямые ссылки на покупку "
        "каждого средства и распишет понятную инструкцию (что и как наносить утром и вечером). "
        "Вы покупаете всё сами по готовому списку. Этот файл останется с вами как надёжная база: "
        "Мария добавит позиции на перспективу, чтобы вы всегда знали, какими средствами "
        "дополнить и усилить свой протокол, когда кожа восстановится и будет готова к более "
        "активным компонентам.\n\n"
        "▫️ Индивидуальный Бокс (от 9 000 ₽) — премиальный формат ухода «под ключ». "
        "Вы приобретаете не просто набор косметики, а готовую систему — ваш личный работающий "
        "протокол. Вы получаете эстетичную коробку с полноразмерными средствами, подобранными "
        "строго под вашу кожу, и подробную инструкцию. В эту стоимость уже включено ведение "
        "от Марии: она будет на связи, чтобы контролировать динамику и довести вас до результата. "
        "Вам не нужно ходить по магазинам — забираете посылку и начинаете свой путь к здоровой "
        "коже по протоколу.",
        reply_markup=get_final_keyboard()
    )
    await callback.answer()


# ================== АНАЛИЗ СОСТАВА ==================
@dp.message(StateFilter(Form.waiting_for_composition))
async def process_composition(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id)
    user_name = (await state.get_data()).get("user_name", "")

    if message.text in ["📋 Моя подписка", "🆘 Помощь"]:
        if message.text == "📋 Моя подписка":
            await my_subscription(message)
        else:
            await help_text(message)
        return

    is_unlimited = (user_id in UNLIMITED_IDS)

    if not is_unlimited and user["free_checks"] <= 0 and not db.has_active_subscription(user_id):
        await message.answer(
            "Мой лимит на бесплатную базовую диагностику исчерпан.\n\n"
            "Если вы хотите проверить остальные составы и выявить скрытые угрозы, "
            "необходимо оформить полный доступ к алгоритму. Стоимость безлимитного "
            "доступа — 1000 рублей.\n\n"
            "Важно: после оплаты алгоритм будет жёстко откалиброван под ваши ответы "
            "в анкете. Не используйте сканер для проверки косметики подруг — анализ "
            "чужих средств собьёт настройки вашего профиля.",
            reply_markup=get_subscription_keyboard()
        )
        return

    composition = message.text
    tags = user.get("tags", [])

    await message.answer("🔬 Анализирую состав...")

    try:
        result = await analyze_composition(composition, tags, user_name)
        verdict = result["verdict"]
        status = result["status"]

        db.save_analysis(user_id, composition, verdict, status)

        if status in ["bad", "hidden_threat"]:
            db.increment_bad_bottles(user_id)
        else:
            db.reset_bad_bottles(user_id)

        if is_unlimited:
            await message.answer(verdict)
            await message.answer("👑 Режим безлимита: проверок не ограничено.")
            return

        if not db.has_active_subscription(user_id):
            db.decrement_free_checks(user_id)
            remaining = db.get_free_checks(user_id)
            await message.answer(verdict)
            if remaining > 0:
                await message.answer(f"Осталось бесплатных проверок: {remaining}")
            else:
                await message.answer(
                    "Это была ваша последняя бесплатная проверка.\n\n"
                    "Хотите безлимит? Оформляйте доступ за 1000 ₽:",
                    reply_markup=get_subscription_keyboard()
                )
        else:
            await message.answer(verdict)

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await message.answer("❌ Ошибка при анализе. Попробуйте позже.")


# ================== ОПЛАТА (ЗАГЛУШКА) ==================
@dp.callback_query(F.data == "pay_scanner")
async def pay_scanner(callback: types.CallbackQuery):
    await callback.message.answer(
        "🔧 Оплата будет подключена на следующем этапе (Продамус / ЮKassa)."
    )
    await callback.answer()


# ================== ПОДПИСКА ==================
@dp.message(F.text == "📋 Моя подписка")
async def my_subscription(message: types.Message):
    user_id = message.from_user.id
    sub = db.get_subscription(user_id)

    if not sub or sub["expires_at"] <= datetime.now():
        await message.answer(
            "❌ У вас нет активной подписки.\n\n"
            "Оформите безлимитный сканер за 1000 ₽:",
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
        "🤖 Я — Роман Андреевич, ИИ-аналитик косметических составов.\n\n"
        "1. Пройдите диагностику\n"
        "2. Пришлите состав (фото или текст)\n"
        "3. Получите разбор\n\n"
        "📌 Связь: @miroslavskayaboks"
    )


# ================== ЗАПУСК ==================
async def main():
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
