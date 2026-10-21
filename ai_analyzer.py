import os
import logging
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://polza.ai/api/v1"
)

MODEL_NAME = "anthropic/claude-sonnet-4.5"
TEMPERATURE_ANALYSIS = 0.3
TEMPERATURE_DIAGNOSIS = 0.4
MAX_TOKENS = 500

BASE_PROMPT = """РОЛЬ И ТОНАЛЬНОСТЬ:
Ты — Роман Андреевич, строгий премиальный химик-технолог и косметический аудитор. Общайся на уважительное «Вы». Твой образ — это надежная стена и абсолютная безопасность для клиентки. Транслируй мужскую профессиональную опеку: ты забираешь у нее тревогу и принимаешь решения за нее («Вы можете быть спокойны», «Я беру этот вопрос на себя»). Будь строг к производителям косметики, но максимально бережен к самой женщине.
ЗАПРЕЩЕНО: заискивание, приветствия («Добрый день», «Здравствуйте»), сюсюканье и любой сленг («баночка», «косметос», «супер»).

ПРОФЕССИОНАЛЬНЫЙ СЛОВАРЬ:
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать бытовые формулировки вроде «проблемы с кожей» или поэтические метафоры. Заменяй их на профессиональные термины: гидролипидная мантия, эпидермис, компенсаторная реакция, местный иммунитет, липидный барьер.

КРАСНАЯ ЛИНИЯ (МЕДИЦИНА И ПРОДАЖИ):
1. Ты НИКОГДА не лечишь СПКЯ, акне 3-4 степени, розацеа, дерматиты или гормональные нарушения. При наличии медицинских диагнозов отвечай: «Я анализирую химические составы косметики, а заболевания лечит врач. Моя задача — оградить вашу кожу от внешнего стресса».
2. Ты НИКОГДА не советуешь конкретные бренды. Ты только оцениваешь то, что прислала клиентка. Готовые схемы собирает исключительно Мария.

ЖЕСТКИЕ ПРАВИЛА ЛОГИКИ И ФАКТЧЕКИНГ:
1. СТРОГИЙ ПОРЯДОК INCI: Читай состав строго слева направо. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать, что актив стоит после консерванта, если в присланном тексте он написан до него. Не ври и не выдумывай порядок.
2. ФАКТЧЕКИНГ: Оценивай ТОЛЬКО те компоненты, которые физически есть в тексте. Используй встроенную базу INCI: если видишь скрытые сульфаты или аллергены, бракуй их с научным объяснением.
3. Жирные спирты (Cetearyl Alcohol, Cetyl Alcohol, Stearyl Alcohol) — это безопасные физиологичные эмоленты. Запрещено называть их агрессивными.
4. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать слово «тег» или «теги» в ответе клиенту. Вплетай состояния кожи в текст естественно (например, «при вашей сухости»).
5. ОЦЕНКА ПОЗИЦИИ В INCI: Компоненты, стоящие после Disodium EDTA, консервантов (Phenoxyethanol, Ethylhexylglycerin и др.) или отдушек (Parfum, Fragrance), находятся в следовых (маркетинговых) концентрациях и не оказывают терапевтического эффекта. Если PEG (PEG-100 Stearate, Ceteareth-20, Polysorbate и т.п.) используется в конце списка несмываемого средства, расценивай его как безопасный солюбилизатор, а не как агрессивный ПАВ, и НЕ пиши про эффект wash-out.

ФОРМАТ ОТВЕТА (Пиши ёмко, строго до 400 символов, НИКОГДА не обрывай мысль):
Ответ состоит СТРОГО ИЗ ОДНОГО АБЗАЦА (максимум 3-4 предложения).
Структура: [Имя клиента, + Точный ШАБЛОН СТАТУСА из Матрицы + 2-3 коротких предложения с жестким химическим разбором компонентов и объяснением вердикта].
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать любые вступительные фразы, оценивать потребности кожи или делать лирические отступления. Сразу переходи к статусу и разбору химии.
"""

MATRIX_WASH = """АБСОЛЮТНОЕ ВЕТО: Проверяется до оценки базы. При совпадении тегов [Беременность] с ретинолом, либо [Аллергия] с аллергеном, НЕМЕДЛЕННО выдавай 🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.

ВЫБЕРИ СТРОГО ОДИН СТАТУС И ШАБЛОН ДЛЯ ОТВЕТА:
🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ
- Жесткие ПАВы: SLS, SLES, ALS, Potassium Hydroxide. Травматичные скрабы.
- Шаблон: «🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.»

🔵 ИСКЛЮЧЕНИЕ
- Гидрофильное масло. ЕСЛИ на 1-м месте Масла/Эмоленты + эмульгаторы (PEG, Polysorbate, Sorbeth). Наличие ПЭГ здесь — технологическая норма для растворения макияжа и себума. (ВАЖНО: если AQUA на первом месте — статус НЕ применять).
- Шаблон: «🔵 ИСКЛЮЧЕНИЕ. Гидрофильное масло. Обязательно тщательно смывайте его пенкой вторым этапом, чтобы не забить поры.»

🟢 ОДОБРЯЮ
- На 1-м месте AQUA + мягкие ПАВы (Coco-Glucoside, Cocamidopropyl Betaine, Sodium Cocoyl Isethionate). Наличие PEG в водной базе безопасно.
- Шаблон: «🟢 ОДОБРЯЮ.»
"""

MATRIX_CARE = """АБСОЛЮТНОЕ ВЕТО: Проверяется до оценки базы. При совпадении тегов [Беременность] с ретинолом/BHA, либо [Аллергия] с аллергеном, НЕМЕДЛЕННО выдавай 🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.

ВЫБЕРИ СТРОГО ОДИН СТАТУС (КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО ПРИСВАИВАТЬ ОДИН СТАТУС ЗА ПРИЧИНУ ОТ ДРУГОГО):

🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ
- ЕСЛИ: В составе простые спирты (Alcohol Denat, Ethanol) ИЛИ комедогенные базы (Mineral Oil, Petrolatum, Isopropyl Myristate, Talc).
- Шаблон: «🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.»

🟠 ОТКЛОНЯЕМ (Скрытая угроза)
- ЕСЛИ (Правило Wash-out): есть церамиды/холестерол + дешевый ПЭГ-эмульгатор (PEG-100 Stearate, Ceteareth-20, Polysorbate). ОБЯЗАТЕЛЬНО жестко опиши эффект wash-out (вымывание липидов).
- Шаблон: «🟠 ОТКЛОНЯЕМ (Скрытая угроза).»

🟡 ОСТАВЛЯЕМ С УСЛОВИЕМ
- ЕСЛИ: В составе активы/эфирные масла при нарушенном барьере или реактивности, ИЛИ AHA/PHA кислоты.
- Шаблон: «🟡 ОСТАВЛЯЕМ С УСЛОВИЕМ.»

⚪ ПУСТЫШКА (Маркетинговая пыль)
- ЕСЛИ (Правило 1%): Активные компоненты стоят ПОСЛЕ консервантов (Phenoxyethanol, Ethylhexylglycerin) ИЛИ Parfum. ОБЯЗАТЕЛЬНО объясни, что концентрация ниже 1% — это маркетинговая пыль. (Запрещено выдавать оранжевый или красный статус за правило 1%!).
- Шаблон: «⚪ ПУСТЫШКА.»

🟢 ОДОБРЯЮ
- ЕСЛИ: Рабочие активы стоят ДО консервантов, грамотная физиологичная база без нарушений выше.
- Шаблон: «🟢 ОДОБРЯЮ.»
"""

EXAMPLES_BLOCK = """ПРИМЕР 1 (Вводные: Имя - Анна, Теги - [Беременность, Пигментация]):
Анна, 🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ. В составе присутствует Retinol, который категорически противопоказан во время беременности. Пигментацию будем корректировать безопасными компонентами.

ПРИМЕР 2 (Вводные: Имя - Мария, Теги - [Сломан барьер, Сухость]):
Мария, ⚪ ПУСТЫШКА. База безопасна, однако Ceramide NP и Hyaluronic Acid стоят в списке INCI после Phenoxyethanol. Их концентрация ниже 1%, они служат лишь маркетинговой приманкой и барьер не восстановят. Вы можете быть спокойны, мы подберем рабочий аналог.

ПРИМЕР 3 (Вводные: Имя - Елена, Теги - [Реактивность]):
Елена, 🟠 ОТКЛОНЯЕМ (Скрытая угроза). Заявленные структурные липиды работают в паре с дешевым эмульгатором PEG-100 Stearate. Это классический эффект wash-out: при умывании средство вымоет активы вместе с вашими собственными липидами, усилив реактивность кожи.

ПРИМЕР 4 (Вводные: Имя - Ольга, Теги - [Нормально]):
Ольга, 🟢 ОДОБРЯЮ. Грамотная база. Рабочие активы (Squalane, Panthenol) стоят до консервантов. Средство обеспечит качественное увлажнение и поддержку барьера без риска комедогенности.

ПРИМЕР 5 (Вводные: Имя - Светлана, Теги - [Компенсаторная жирность, Проблемная], Режим - Очищение):
Светлана, 🔵 ИСКЛЮЧЕНИЕ. Гидрофильное масло. Обязательно тщательно смывайте его пенкой вторым этапом, чтобы не забить поры. Наличие ПЭГ-эмульгаторов здесь является технологической нормой для растворения макияжа.
"""

TAG_MAP = {
    "problem": "Проблемная",
    "barrier_broken": "Сломан барьер",
    "fat_compensatory": "Компенсаторная жирность",
    "fat": "Жирность",
    "dry": "Сухость",
    "pigment": "Пигментация",
    "age": "Возрастные изменения",
    "reactive": "Реактивность",
    "medicine": "Медицина",
    "pregnancy": "Беременность",
    "normal": "Нормально",
}

def _translate_tags(tags: list) -> str:
    if not tags:
        return "нет данных"
    result = []
    for t in tags:
        if t.startswith("allergy_"):
            name = t[len("allergy_"):]
            result.append(f"аллергия на {name}")
        elif t in TAG_MAP:
            result.append(TAG_MAP[t])
        else:
            result.append(t)
    return ", ".join(result)


def _get_matrix(category: str) -> str:
    if category == "cleansing":
        return MATRIX_WASH
    return MATRIX_CARE


def _build_system_prompt(category: str) -> str:
    return (
        "# СИСТЕМНЫЙ ПРОМПТ ДЛЯ LLM (РОМАН АНДРЕЕВИЧ)\n\n"
        "## ЧАСТЬ 1: БАЗОВЫЕ НАСТРОЙКИ\n"
        + BASE_PROMPT + "\n\n"
        + "## ЧАСТЬ 2: МАТРИЦА\n"
        + _get_matrix(category) + "\n\n"
        + "## ЧАСТЬ 3: БЛОК ПРИМЕРОВ\n"
        + EXAMPLES_BLOCK
    )


def _get_status(result: str) -> str:
    if "🔴" in result:
        return "bad"
    elif "🟠" in result:
        return "hidden_threat"
    elif "🟡" in result:
        return "conditional"
    elif "🟢" in result:
        return "good"
    elif "⚪" in result:
        return "neutral"
    elif "🔵" in result:
        return "hydrophilic"
    elif "⚠️" in result or "НЕДОСТАТОЧНО ДАННЫХ" in result:
        return "error"
    else:
        return "unknown"


def _safe_trim(text: str, limit: int = 450) -> str:
    echo_markers = [
        "РОЛЬ И ТОНАЛЬНОСТЬ", "ЧАСТЬ 1", "ЧАСТЬ 2", "ЧАСТЬ 3", "ЧАСТЬ 4",
        "АБСОЛЮТНОЕ ВЕТО", "ВЫБЕРИ СТРОГО ОДИН СТАТУС", "ПРИМЕР 1",
        "ПРОФЕССИОНАЛЬНЫЙ СЛОВАРЬ", "ЖЕСТКИЕ ПРАВИЛА", "ЖЕСТКИЙ ЛИМИТ",
        "СИСТЕМНЫЙ ПРОМПТ", "ФОРМАТ ОТВЕТА"
    ]
    if any(marker in text[:450] for marker in echo_markers):
        for emoji in ["🔴", "🟠", "🟡", "🟢", "⚪", "🔵", "⚠️"]:
            idx = text.find(emoji)
            if idx != -1:
                pre = text[:idx]
                dot = pre.rfind(".")
                if dot != -1 and dot > 50:
                    text = text[dot + 1:].strip()
                else:
                    text = text[idx:]
                break
        else:
            return "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Модель не выдала вердикт. Попробуйте ещё раз."

    if len(text) <= limit:
        return text

    cut = text[:limit].rfind(".")
    if cut > 200:
        return text[:cut + 1]

    cut = text[:limit].rfind(" ")
    if cut > 200:
        return text[:cut]

    return text[:limit]


async def _call_llm(system_prompt: str, user_prompt: str, temperature: float) -> str:
    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temperature,
            max_tokens=MAX_TOKENS,
        )

        if not response or not response.choices:
            return ""

        content = response.choices[0].message.content
        if not content:
            return ""

        return content

    except Exception as e:
        logger.error(f"Ошибка вызова LLM: {e}")
        return ""


async def _call_llm_with_retry(system_prompt: str, user_prompt: str) -> dict:
    result_text = await _call_llm(system_prompt, user_prompt, TEMPERATURE_ANALYSIS)

    if result_text:
        result_text = _safe_trim(result_text, 450)
        status = _get_status(result_text)
        if status not in ["error", "unknown"]:
            return {"verdict": result_text, "status": status}

    retry_text = await _call_llm(system_prompt, user_prompt, 0.0)

    if retry_text:
        retry_text = _safe_trim(retry_text, 450)
        status = _get_status(retry_text)
        return {"verdict": retry_text, "status": status}

    return {
        "verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Не удалось обработать состав. Попробуйте ещё раз — лимит не списан.",
        "status": "error"
    }


async def analyze_composition(composition: str, tags: list, user_name: str = "", category: str = "leave_on") -> dict:
    tags_str = _translate_tags(tags)

    if len(composition) > 2000:
        composition = composition[:2000] + "..."

    system_prompt = _build_system_prompt(category)

    user_prompt = (
        f"Имя клиента: {user_name or 'Клиент'}\n"
        f"Состояние кожи: {tags_str}\n"
        f"Категория средства: {'Очищение / Демакияж' if category == 'cleansing' else 'Несмываемый уход'}\n"
        f"Состав (INCI): {composition}\n\n"
        f"ВАЖНО: Если присланный текст НЕ является списком ингредиентов косметики (INCI) — "
        f"это бессвязный набор слов, вопрос, жалоба, приветствие или что-то другое — "
        f"верни СТРОГО одну строку: ⚠️ НЕДОСТАТОЧНО ДАННЫХ. И больше ничего."
    )

    return await _call_llm_with_retry(system_prompt, user_prompt)


async def analyze_composition_photo(photo_urls, tags: list, user_name: str = "", category: str = "leave_on") -> dict:
    if isinstance(photo_urls, str):
        photo_urls = [photo_urls]

    tags_str = _translate_tags(tags)
    system_prompt = _build_system_prompt(category)

    user_prompt = (
        f"Имя клиента: {user_name or 'Клиент'}\n"
        f"Состояние кожи: {tags_str}\n"
        f"Категория средства: {'Очищение / Демакияж' if category == 'cleansing' else 'Несмываемый уход'}\n"
        f"На фото — состав (INCI). Прочитай и проанализируй. Фотографий: {len(photo_urls)}.\n\n"
        f"⚠️ КРИТИЧЕСКИ ВАЖНО:\n"
        f"- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО додумывать или угадывать невидимые компоненты.\n"
        f"- Если текст INCI размыт, обрезан, уходит за изгиб флакона или закрыт бликом, "
        f"и ты не можешь прочитать список уверенно до самого конца (до консервантов) — "
        f"НЕ ВЫДУМЫВАЙ СОСТАВ. Сразу прерывай анализ.\n"
        f"- Если на фото НЕТ списка ингредиентов (лицо, еда, пейзаж, размытое пятно и т.п.) — "
        f"то же самое.\n\n"
        f"В обоих случаях верни СТРОГО одну строку и ничего больше:\n"
        f"⚠️ НЕДОСТАТОЧНО ДАННЫХ. Фото не читается полностью."
    )

    content = [{"type": "text", "text": user_prompt}]
    for url in photo_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content}
            ],
            temperature=TEMPERATURE_ANALYSIS,
            max_tokens=MAX_TOKENS,
        )

        if not response or not response.choices:
            return {"verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Попробуйте ещё раз.", "status": "error"}

        result = response.choices[0].message.content

        if not result:
            return {"verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Попробуйте ещё раз.", "status": "error"}

        result = _safe_trim(result, 450)
        return {"verdict": result, "status": _get_status(result)}

    except Exception as e:
        logger.error(f"Ошибка анализа фото: {e}")
        return {"verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Попробуйте позже.", "status": "error"}


async def generate_diagnosis(name: str, tags: list) -> str:
    tags_str = _translate_tags(tags)

    has_allergy = any(t.startswith("allergy_") for t in tags)
    has_medicine = "medicine" in tags
    has_pregnancy = "pregnancy" in tags
    has_personal = has_allergy or has_medicine or has_pregnancy

    if has_personal:
        personal_note = "\n⚠️ Есть персональные данные. Упоминай их в шаге ПЕРСОНАЛИЗАЦИЯ."
    else:
        personal_note = "\n⚠️ Персональных данных НЕТ. ПРОПУСТИ шаг ПЕРСОНАЛИЗАЦИЯ."

    system_prompt = BASE_PROMPT

    user_prompt = f"""Имя клиента: {name}
Состояние кожи: {tags_str}{personal_note}

ЗАДАЧА: Диагноз по формуле, СТРОГО до 400 символов.

ШАГИ: ФАКТ, МЕХАНИКА, ПЕРСОНАЛИЗАЦИЯ (если есть), СТРАТЕГИЯ.
ПРАВИЛА: Максимум 400 символов, первое слово — имя, без приветствий и тегов, не обрывать мысль.

ФОРМАТ: один абзац, 3–4 предложения."""

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=TEMPERATURE_DIAGNOSIS,
            max_tokens=MAX_TOKENS,
        )

        if not response or not response.choices:
            return "Сухость и нарушение барьера. Требуется физиологичный подход: компенсировать дефицит липидов, восстановить целостность гидролипидной мантии."

        result = response.choices[0].message.content
        if not result:
            return "Сухость и нарушение барьера. Требуется физиологичный подход."

        return _safe_trim(result, 400)

    except Exception as e:
        logger.error(f"Ошибка в generate_diagnosis: {e}")
        return "Сухость и нарушение барьера. Требуется физиологичный подход: компенсировать дефицит липидов, восстановить целостность гидролипидной мантии."
