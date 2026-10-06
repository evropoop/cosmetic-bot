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
    survey = State()
    waiting_for_composition = State()
    waiting_for_photos = State()

# ================== КНОПКИ ==================
def get_main_keyboard():
    buttons = [
        [KeyboardButton(text="📋 Моя подписка")],
        [KeyboardButton(text="🆘 Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_first_composition_keyboard():
    buttons = [
        [InlineKeyboardButton(text="📸 Загрузить фото состава", callback_data="upload_composition")],
        [InlineKeyboardButton(text="🌱 У меня пока нет ухода", callback_data="no_care")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_no_care_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🛒 Пойду выбирать со сканером", callback_data="go_with_scanner")],
        [InlineKeyboardButton(text="👑 Хочу готовый уход от Марии", callback_data="want_mariya_box")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_subscription_keyboard():
    buttons = [
        [InlineKeyboardButton(text="💳 Купить безлимитный сканер — 1000 ₽", callback_data="pay_scanner")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_lead_keyboard():
    buttons = [
        [InlineKeyboardButton(text="👑 Хочу к Марии", callback_data="want_mariya")],
        [InlineKeyboardButton(text="🚶 Соберу сама", callback_data="go_alone")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_final_keyboard():
    buttons = [
        [InlineKeyboardButton(text="💳 Digital-протокол — 3 900 ₽", callback_data="pay_digital")],
        [InlineKeyboardButton(text="💳 Депозит за Box — 2 000 ₽", callback_data="pay_deposit")],
        [InlineKeyboardButton(text="💬 Обсудить мой случай", callback_data="discuss_case")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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

    # TODO: здесь будет отправка кружочка Марии
    await message.answer(
        "Привет! Я — Роман Андреевич, старший ассистент Марии Мирославской. 🧪\n\n"
        "Я — беспристрастный ИИ-аналитик косметических составов. Моя задача — "
        "оградить вашу кожу от агрессивной косметики.\n\n"
        "Ответьте, пожалуйста, на 5 коротких вопросов. Это займёт минуту."
    )

    await start_survey(message, state)


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
        await callback.answer("Добавлено! Выберите ещё или нажмите «Готово».")
        return

    await callback.answer()


# ================== МИНИ-ДИАГНОЗ + РАЗВИЛКА ==================
async def finish_survey(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tags = data.get("tags", [])
    user_id = message.chat.id

    db.update_user_tags(user_id, tags)
    await state.clear()

    diagnosis = "Анамнез собран. Позвольте перевести на понятный язык, что сейчас происходит с вашей кожей:\n\n"

    if "barrier_broken" in tags:
        diagnosis += "— Ваш липидный барьер нарушен. Кожа не удерживает влагу, поэтому её стягивает после умывания. Вы смываете собственный защитный слой.\n\n"

    if "fat_compensatory" in tags or ("fat" in tags and "barrier_broken" in tags):
        diagnosis += "— Жирный блеск днём — это не «жирная кожа», это компенсаторная реакция. Кожа пересушена и защищается, заливая лицо себумом.\n\n"

    if "reactive" in tags:
        diagnosis += "— Кожа реактивна. Любая агрессивная отдушка или актив воспринимается как раздражитель.\n\n"

    if "problem" in tags:
        diagnosis += "— Есть склонность к воспалениям и забитым порам. Тяжёлые масла и плотные воски сейчас противопоказаны.\n\n"

    if "medicine" in tags or "pregnancy" in tags:
        diagnosis += "— Вижу, что в ходу мощные аптечные активы или есть диагноз. Моя задача — подобрать базу, которая не будет мешать лечению у врача.\n\n"

    if not any(t in tags for t in ["barrier_broken", "fat_compensatory", "reactive", "problem", "medicine", "pregnancy"]):
        diagnosis += "— Глобально всё неплохо, база держится. Главная задача — не испортить то, что есть, и подобрать грамотную поддержку.\n\n"

    diagnosis += "Теперь пришлите состав первого средства на проверку."

    await message.answer(diagnosis, reply_markup=get_first_composition_keyboard())
    await state.set_state(Form.waiting_for_composition)


# ================== РАЗВИЛКА: НЕТ УХОДА ==================
@dp.callback_query(F.data == "no_care")
async def process_no_care(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Понял вас. Умываетесь просто водой или тем, что первое попадётся под руку?\n\n"
        "С одной стороны — это чистый лист. С другой — прямо сейчас ваша кожа абсолютно "
        "беззащитна перед водопроводной водой, пылью и перепадами температур. И судя по "
        "вашей анкете, барьер уже подаёт сигналы SOS.\n\n"
        "Вам жизненно необходим хотя бы строгий фундамент: мягкое умывание, тоник и "
        "барьерный крем.\n\n"
        "Как мы можем это решить:\n\n"
        "🛒 Путь 1: Вы идёте в магазин сами. Но чтобы не накупить пустышек и не сжечь "
        "кожу, вы берёте с собой меня. Я открываю доступ к ИИ-сканеру составов.\n\n"
        "👑 Путь 2: Делегируете всё Марии. Она всё рассчитывает и выдаёт готовую схему "
        "(Digital-протокол) или сама собирает и отправляет посылку с идеальными "
        "полноразмерными средствами под ключ.\n\n"
        "Какой путь выбираете?",
        reply_markup=get_no_care_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "go_with_scanner")
async def process_go_with_scanner(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Отличный план! Даю вам 2 бесплатные проверки.\n\n"
        "Откройте сайт магазина или подойдите к полке, найдите средство и пришлите "
        "скриншот/фото состава. Проверим, стоит ли оно ваших денег!",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)
    await callback.answer()


@dp.callback_query(F.data == "want_mariya_box")
async def process_want_mariya_box(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await show_final_offer(callback.message, state)
    await callback.answer()


# ================== ФИНАЛЬНОЕ ПРЕДЛОЖЕНИЕ (ШАГ 6) ==================
async def show_final_offer(message: types.Message, state: FSMContext):
    await message.answer(
        "Отличное решение! Давайте выберем удобный для вас формат работы.\n\n"
        "🎁 Приятный бонус: ту 1000 рублей, которую вы оплатили за доступ к сканеру, "
        "я полностью вычту из стоимости любого формата!\n\n"
        "📱 Формат 1: «Skin Protokol (Digital)» — 4 900 ₽ (для вас 3 900 ₽)\n"
        "Для кого: вы живёте за границей или любите покупать косметику сами.\n"
        "Что внутри: Мария составляет подробную схему (утро/вечер) с прямыми ссылками "
        "на конкретные средства в проверенных магазинах.\n\n"
        "📦 Формат 2: «Кастомный Box под ключ»\n"
        "Мария составляет протокол на профессиональной космецевтике, комплектует "
        "эстетичную посылку с полноразмерными флаконами и отправляет вам. Подробная "
        "инструкция (ваш Skin Protokol) идёт бонусом!\n\n"
        "Важно: мы восполняем только пробелы в уходе. Если ваша текущая умывалка "
        "работает отлично — мы её оставляем.\n\n"
        "🤍 «Базовый» (до 11 500 ₽) — строгий фундамент (до 3 средств).\n"
        "🤍 «Оптимальный» (до 16 500 ₽) — база + прицельный актив. 🎁 Доставка по РФ в подарок.\n"
        "🤍 «Максимальный» (до 24 500 ₽) — полноценный салонный уход дома (5-7 средств). 🎁 Доставка по РФ в подарок.\n\n"
        "Для старта сборки бокса сейчас вносится только депозит 2000 ₽. Остаток — перед отправкой.",
        reply_markup=get_final_keyboard()
    )
    await state.set_state(Form.waiting_for_photos)


# ================== ПРИЁМ СОСТАВА + АНАЛИЗ ==================
@dp.message(StateFilter(Form.waiting_for_composition))
async def process_composition(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = db.get_or_create_user(user_id)

    if message.text in ["📋 Моя подписка", "🆘 Помощь"]:
        if message.text == "📋 Моя подписка":
            await my_subscription(message)
        else:
            await help_text(message)
        return

    is_unlimited = (user_id in UNLIMITED_IDS)

    if not is_unlimited and user["free_checks"] <= 0 and not db.has_active_subscription(user_id):
        await message.answer(
            "Демо-доступ закрыт. Мы проверили 2 средства, но это только верхушка айсберга.\n\n"
            "Хотите разобрать всю косметичку, перестать сливать деньги на пустышки "
            "и задавать вопросы по уходу 24/7?\n\n"
            "Безлимитный ИИ-сканер — 1000 ₽/мес.",
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

        # Счётчик «Системная ошибка»: 3 плохих подряд (🔴 или 🟠)
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
                    "Хотите безлимит? Оформляйте подписку за 1000 ₽/мес:",
                    reply_markup=get_subscription_keyboard()
                )
        else:
            await message.answer(verdict)

        bad_count = db.get_bad_bottles_count(user_id)
        if bad_count >= 3:
            await message.answer(
                "Я проанализировал ваш текущий уход и вынужден сказать прямо: он работает "
                "против вас. То, что сейчас стоит на вашей полке, методично истощает эпидермис.\n\n"
                "Нам нужно остановить этот процесс и полностью сменить фундамент на "
                "правильные физиологичные составы (Мирославская-протокол).\n\n"
                "Вы можете попытаться выстроить новую базу самостоятельно, скрупулёзно "
                "проверяя каждую баночку через мой сканер. Или мы можем снять с вас эту "
                "головную боль прямо сейчас: Мария лично подберёт и соберёт для вас готовый "
                "индивидуальный бокс с гарантированным результатом.\n\n"
                "Какой путь выберем? Доверитесь Марии?",
                reply_markup=get_lead_keyboard()
            )
            db.reset_bad_bottles(user_id)

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await message.answer(
            "❌ Ошибка при анализе. Попробуйте позже или свяжитесь с @miroslavskayaboks"
        )


# ================== РАЗВИЛКА «СОБЕРУ САМА» ==================
@dp.callback_query(F.data == "go_alone")
async def process_go_alone(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Я принимаю ваш выбор. Выстроить физиологичный уход самостоятельно — сложная, "
        "но вполне реальная задача, если внимательно изучать биохимию составов.\n\n"
        "Моя главная рекомендация для вас: сделайте фокус на мягкое очищение и базовое "
        "восстановление барьера. Избегайте агрессивных кислот, жёстких ПАВ и спирта.\n\n"
        "Мой сканер всегда к вашим услугам: присылайте составы перед покупкой, чтобы "
        "не рисковать здоровьем кожи. А если решите делегировать задачу — просто "
        "напишите сюда фразу «Хочу к Марии».",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(Form.waiting_for_composition)
    await callback.answer()


# ================== «ХОЧУ К МАРИИ» ==================
@dp.callback_query(F.data == "want_mariya")
async def process_want_mariya(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await show_final_offer(callback.message, state)
    await callback.answer()


# ================== ФИНАЛ: ОПЛАТА И ФОТО ==================
@dp.callback_query(F.data.startswith("pay_"))
async def process_payment(callback: types.CallbackQuery, state: FSMContext):
    # TODO: здесь будет интеграция с Продамус / ЮKassa
    await callback.message.answer(
        "🔧 Оплата будет подключена на следующем этапе.\n\n"
        "Сейчас мы настроим приём платежей через Продамус."
    )
    await callback.answer()


@dp.callback_query(F.data == "discuss_case")
async def process_discuss_case(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Хорошо! Я передал вашу анкету Марии с пометкой «Запрос на личную консультацию».\n\n"
        "Она свяжется с вами в личных сообщениях в ближайшее время."
    )
    # Уведомление Марии
    try:
        await bot.send_message(
            chat_id=MARIYA_ID,
            text=f"💬 Запрос на личную консультацию\n"
                 f"👤 @{callback.from_user.username or 'без username'}\n"
                 f"🆔 ID: {callback.from_user.id}"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить Марию: {e}")
    await callback.answer()


# ================== ПРИЁМ ФОТО ДЛЯ МАРИИ ==================
@dp.message(StateFilter(Form.waiting_for_photos), F.photo)
async def process_photos(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    user = db.get_or_create_user(user_id)
    tags = user.get("tags", [])

    file_id = message.photo[-1].file_id

    # Отправляем Марии
    try:
        await bot.send_photo(
            chat_id=MARIYA_ID,
            photo=file_id,
            caption=f"📸 Фото лица от клиентки\n"
                    f"👤 @{username or 'без username'}\n"
                    f"🆔 ID: {user_id}\n"
                    f"🏷 Теги: {', '.join(tags) if tags else 'нет'}"
        )
        await message.answer(
            "Фото получено! Передал Марии. Она изучит вашу анкету и фотографии, "
            "после чего свяжется с вами."
        )
    except Exception as e:
        logger.error(f"Не удалось отправить фото Марии: {e}")
        await message.answer("❌ Ошибка при передаче фото. Попробуйте позже.")
    await state.clear()


# ================== ПОДПИСКА ==================
@dp.message(F.text == "📋 Моя подписка")
async def my_subscription(message: types.Message):
    user_id = message.from_user.id
    sub = db.get_subscription(user_id)

    if not sub or sub["expires_at"] <= datetime.now():
        await message.answer(
            "❌ У вас нет активной подписки.\n\n"
            "Оформите безлимитный сканер за 1000 ₽/мес:",
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
        "1. Заполните анкету\n"
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
