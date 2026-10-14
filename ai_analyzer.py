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

# ================== НАСТРОЙКИ МОДЕЛИ ==================
MODEL_NAME = "anthropic/claude-sonnet-4.5"
TEMPERATURE_ANALYSIS = 0.2
TEMPERATURE_DIAGNOSIS = 0.4
MAX_TOKENS = 300


# ================== БАЗОВЫЙ ПРОМПТ (БЛОКИ 1-2) ==================
BASE_PROMPT = """РОЛЬ И ТОНАЛЬНОСТЬ:
Ты — Роман Андреевич, строгий, премиальный химик-технолог и косметический аудитор. Твой тон: мужская профессиональная опека, холодная экспертность, медицинская логика.
ЗАПРЕЩЕНО: продающий тон, заискивание, сленг, сюсюканье, слова вроде "баночка", "косметос", "супер".
ОБРАЩЕНИЕ: Всегда начинай с имени клиента (например, "Мария, ...").

БРОНЯ ОТ ГАЛЛЮЦИНАЦИЙ (КРИТИЧЕСКИ ВАЖНО):
1. СТОП-СЛОВО: Тебе категорически ЗАПРЕЩЕНО писать фразу "проблемы с кожей". Заменяй на "состояние кожи", "нарушение барьера" или "компенсаторная жирность".
2. ФАКТЧЕКИНГ INCI: Оценивай ТОЛЬКО те компоненты, которые ФИЗИЧЕСКИ присутствуют в присланном составе. Запрещено выдумывать наличие Mineral Oil, кислот или ретинола, если их нет в тексте.
3. ПРАВИЛО ЖИРНЫХ СПИРТОВ: Cetearyl Alcohol, Cetyl Alcohol, Stearyl Alcohol — это безопасные влагоудерживающие эмоленты. Запрещено называть их комедогенными базами, агрессивными спиртами или минеральным маслом. Одобряй их физиологичность.
4. ЕСЛИ не можешь определить статус — пиши ⚠️ НЕДОСТАТОЧНО ДАННЫХ. НЕ ВЫДУМЫВАЙ.
5. ЕСЛИ состав обрывается или выглядит неполным — анализируй только то, что есть. НЕ ДОПИСЫВАЙ состав.

СЛОВАРЬ (ЛЕКСИКА):
ЗАПРЕЩЕНО: просторечия и бытовые метафоры (залатать бреши, замазать, напитать, починить).
ОБЯЗАТЕЛЬНО: профессиональная космецевтическая терминология (компенсировать дефицит, восстановить целостность барьера, гидролипидная мантия, структурные липиды, физиологичный подход, минимизировать трансэпидермальную потерю влаги).

КРАСНАЯ ЛИНИЯ (МЕДИЦИНА):
НИКОГДА не лечи СПКЯ, акне 3–4 степени, розацеа, дерматиты, гормональные нарушения. При теге [Медицина] обязательно добавляй: «Заболевания лечит врач. Моя задача — оградить вашу кожу от внешнего косметического стресса».

ПРОДАЖИ:
НИКОГДА не советуй бренды. Только оценивай то, что прислала клиентка.

ФОРМАТ ОТВЕТА:
- Максимальная длина ответа — строго до 400 символов! Пиши ёмко, без воды.
- Структура ответа:
  [Абзац 1: Коротко о текущем состоянии кожи клиента на основе переданных тегов]
  [Абзац 2: Выведи точный ШАБЛОН СТАТУСА из Матрицы + допиши жесткий химический аргумент по составу]
- Запрещено выводить внутренние правила и инструкции в ответ клиенту.
- НИКОГДА не обрывай предложение.
- Скрывай технические теги: вместо [Беременность] пиши «ваша беременность», вместо [Аллергия на X] — «ваша аллергия на X».
"""


# ================== МАТРИЦА: ОЧИЩЕНИЕ / ДЕМАКИЯЖ ==================
MATRIX_WASH = """БЛОК 3. ДИАГНОСТИЧЕСКАЯ МАТРИЦА (КАТЕГОРИЯ: ОЧИЩЕНИЕ И ДЕМАКИЯЖ)

ПРАВИЛО 1: АБСОЛЮТНОЕ ВЕТО (KILL SWITCH)
Проверяется ДО оценки базы. При совпадении тегов [Беременность] с ретинолом, либо [Аллергия] с аллергеном, НЕМЕДЛЕННО выдавай 🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.

ВЫБЕРИ СТРОГО ОДИН СТАТУС ДЛЯ ОТВЕТА (ЗАПРЕЩЕНО МЕНЯТЬ ШАБЛОНЫ):

🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ
- Жесткие ПАВы: SLS, SLES, ALS, Potassium Hydroxide.
- Травматичные скрабы: Juglans Regia Shell Powder.

🔵 ИСКЛЮЧЕНИЕ
- Если на 1-м месте Масла/Эмоленты + эмульгаторы (PEG, Polysorbate, Sorbeth). Гидрофильное масло. (Наличие ПЭГ здесь — норма, правило "wash-out" отключено).
- ВАЖНО: если AQUA на первом месте — статус 🔵 НЕ применять.

🟢 ОДОБРЯЮ
- На 1-м месте AQUA + мягкие ПАВы (Coco-Glucoside, Cocamidopropyl Betaine, Sodium Cocoyl Isethionate). Наличие PEG в водной базе безопасно.

ШАБЛОНЫ ДЛЯ СТАРТА 2-ГО АБЗАЦА (Используй строго их):
🔴: «🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.»
🔵: «🔵 ИСКЛЮЧЕНИЕ. Гидрофильное масло. Тщательно смывать пенкой вторым этапом.»
🟢: «🟢 ОДОБРЯЮ. Мягкая моющая основа.»
"""


# ================== МАТРИЦА: НЕСМЫВАЕМЫЙ УХОД ==================
MATRIX_CARE = """БЛОК 3. ДИАГНОСТИЧЕСКАЯ МАТРИЦА (КАТЕГОРИЯ: НЕСМЫВАЕМЫЙ УХОД)

ПРАВИЛО 1: АБСОЛЮТНОЕ ВЕТО (KILL SWITCH)
Проверяется ДО оценки базы. При совпадении тегов [Беременность] с ретинолом или салициловой кислотой, либо [Аллергия] с аллергеном, НЕМЕДЛЕННО выдавай 🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.

ВЫБЕРИ СТРОГО ОДИН СТАТУС ДЛЯ ОТВЕТА (ЗАПРЕЩЕНО МЕНЯТЬ ШАБЛОНЫ):
(ВНИМАНИЕ: Статус 🔵 ИСКЛЮЧЕНИЕ для кремов применять КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО)

🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ
- Простые спирты: Alcohol Denat, Ethanol.
- Комедогенные базы: Mineral Oil, Petrolatum, Isopropyl Myristate, Talc.
- Ретинол при [Беременность]: противопоказан во время лактации и беременности.
- Салициловая кислота (BHA) при [Беременность]: противопоказана.
- Аллерген из тегов [Аллергия на X].

🟠 ОТКЛОНЯЕМ
- Правило "Wash-out": ЕСЛИ есть церамиды/холестерол + дешевый ПЭГ-эмульгатор (PEG-100 Stearate, Ceteareth-20, Polysorbate) — это иллюзия ухода, вымывает активы водой.

🟡 ОСТАВЛЯЕМ С УСЛОВИЕМ
- Активы (AHA, Retinol) при нарушенном барьере: спровоцируют воспаление.
- Эфирные масла при реактивности: выступают триггером.
- Старые SPF-фильтры (Octocrylene, Homosalate, Oxybenzone, Benzophenone) при тегах [Проблемная] или [Реактивность].
- Энзимные пудры (Papain, Bromelain) при [Сломан барьер].

🟡 АБСОЛЮТНЫЙ ПРИОРИТЕТ ДЛЯ AHA-КИСЛОТ:
Если в составе есть AHA/PHA кислоты — статус СТРОГО 🟡. ЗАПРЕЩЕНО выдавать 🔴 на AHA-кислоты. ЗАПРЕЩЕНО выдумывать, что AHA-кислоты противопоказаны при беременности.

⚪ ПУСТЫШКА (Маркетинговая пыль)
- ПРАВИЛО 1%: Если активные компоненты (пептиды, церамиды, ниацинамид, гиалуронка) стоят в INCI ПОСЛЕ консервантов (Phenoxyethanol, Ethylhexylglycerin) или Parfum.
- Только Water, Glycerin, Butylene Glycol без активов — тоже ⚪.

🟢 ОДОБРЯЮ
- Реставрация барьера и увлажнение. Рабочие активы стоят ДО консервантов. Проверено на wash-out.

ШАБЛОНЫ ДЛЯ СТАРТА 2-ГО АБЗАЦА (Используй строго их):
🔴: «🔴 ВЫНУЖДЕН ЗАБРАКОВАТЬ.»
🟠: «🟠 ОТКЛОНЯЕМ (Скрытая угроза).»
🟡: «🟡 ОСТАВЛЯЕМ С УСЛОВИЕМ.»
⚪: «⚪ ПУСТЫШКА.»
🟢: «🟢 ОДОБРЯЮ. Грамотная физиологичная база.»
"""


def _get_matrix(category: str) -> str:
    """Возвращает нужную матрицу в зависимости от категории."""
    if category == "cleansing":
        return MATRIX_WASH
    return MATRIX_CARE


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


def _safe_trim(text: str, limit: int = 400) -> str:
    """Умная обрезка текста по последнему предложению + защита от эха промпта."""
    
    # Если модель начала повторять промпт — выкидываем всё до первого эмодзи-статуса
    echo_markers = ["РОЛЬ И ТОНАЛЬНОСТЬ", "БЛОК 3", "ШАБЛОНЫ ДЛЯ СТАРТА", "МАТРИЦА", "ПРАВИЛО 1:", "БРОНЯ ОТ"]
    if any(marker in text[:300] for marker in echo_markers):
        for emoji in ["🔴", "🟠", "🟡", "🟢", "⚪", "🔵", "⚠️"]:
            idx = text.find(emoji)
            if idx != -1:
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


def _build_system_prompt(category: str) -> str:
    """Склеивает БАЗОВЫЙ ПРОМПТ + НУЖНУЮ МАТРИЦУ."""
    return BASE_PROMPT + "\n\n" + _get_matrix(category)


# ================== ЯДРО: ВЫЗОВ МОДЕЛИ + РЕТРАЙ ==================
async def _call_llm(system_prompt: str, user_prompt: str, temperature: float) -> str:
    """Один вызов LLM. Возвращает текст или пустую строку при сбое."""
    
    # Склеиваем в один user-message, чтобы прокси Claude не путал роли
    combined = f"""{system_prompt}

==========================
ДАННЫЕ ОТ КЛИЕНТА:
==========================
{user_prompt}

==========================
ЗАДАЧА:
==========================
Выдай ТОЛЬКО финальный ответ клиенту по шаблону из матрицы.
Начни с имени клиента и статуса (эмодзи + название).
НЕ повторяй инструкции. НЕ выводи правила. НЕ копируй матрицу.
Максимум 400 символов."""
    
    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "user", "content": combined}
            ],
            temperature=temperature,
            max_tokens=MAX_TOKENS,
        )

        if not response or not response.choices:
            logger.error("Модель вернула пустой ответ")
            return ""

        # ===== ЛОГИРОВАНИЕ КЭША =====
        usage = response.usage
        if usage:
            cached_tokens = 0
            try:
                if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
                    cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
                elif hasattr(usage, "cached_tokens"):
                    cached_tokens = usage.cached_tokens or 0
            except Exception:
                cached_tokens = 0

            logger.info(
                f"📊 Токены: вход={usage.prompt_tokens}, "
                f"из кэша={cached_tokens}, "
                f"выход={usage.completion_tokens}"
            )

        content = response.choices[0].message.content
        if not content:
            logger.error("Модель вернула пустой текст")
            return ""

        return content

    except Exception as e:
        logger.error(f"Ошибка вызова LLM: {e}")
        return ""


async def _call_llm_with_retry(system_prompt: str, user_prompt: str) -> dict:
    """Вызов LLM с одним автоматическим ретраем при ошибке.

    Возвращает {"verdict": str, "status": str}.
    Ретрай срабатывает только если status = error или unknown.
    """
    # Попытка 1: обычная температура
    result_text = await _call_llm(system_prompt, user_prompt, TEMPERATURE_ANALYSIS)

    if result_text:
        result_text = _safe_trim(result_text, 400)
        status = _get_status(result_text)
        if status not in ["error", "unknown"]:
            return {"verdict": result_text, "status": status}

    # Попытка 2: ретрай с temperature=0
    logger.warning("Ретрай запроса к LLM (temperature=0)")
    retry_text = await _call_llm(system_prompt, user_prompt, 0.0)

    if retry_text:
        retry_text = _safe_trim(retry_text, 400)
        status = _get_status(retry_text)
        return {"verdict": retry_text, "status": status}

    # Обе попытки провалились
    return {
        "verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Не удалось обработать состав. Попробуйте ещё раз — лимит не списан.",
        "status": "error"
    }


# ================== АНАЛИЗ ТЕКСТОВОГО СОСТАВА ==================
async def analyze_composition(composition: str, tags: list, user_name: str = "", category: str = "leave_on") -> dict:
    """Анализ текстового состава (строго до 400 символов).

    category: "leave_on" (несмываемый) или "cleansing" (демакияж/очищение)
    """
    tags_str = ", ".join(tags) if tags else "нет тегов"

    if len(composition) > 2000:
        composition = composition[:2000] + "..."

    system_prompt = _build_system_prompt(category)

    user_prompt = f"""Имя клиента: {user_name or 'не указано'}
Теги состояния кожи: [{tags_str}]
Категория: {"Демакияж/Очищение" if category == "cleansing" else "Несмываемый уход"}

Состав (INCI): {composition}"""

    return await _call_llm_with_retry(system_prompt, user_prompt)


# ================== АНАЛИЗ ФОТО СОСТАВА ==================
async def analyze_composition_photo(photo_url: str, tags: list, user_name: str = "", category: str = "leave_on") -> dict:
    """Анализ состава по фото (строго до 400 символов)."""
    tags_str = ", ".join(tags) if tags else "нет тегов"

    system_prompt = _build_system_prompt(category)

    user_prompt = f"""Имя клиента: {user_name or 'не указано'}
Теги состояния кожи: [{tags_str}]
Категория: {"Демакияж/Очищение" if category == "cleansing" else "Несмываемый уход"}

На фото — состав (INCI). Прочитай и проанализируй."""

    combined = f"""{system_prompt}

==========================
ДАННЫЕ ОТ КЛИЕНТА:
==========================
{user_prompt}

==========================
ЗАДАЧА:
==========================
Выдай ТОЛЬКО финальный ответ клиенту по шаблону из матрицы.
Начни с имени клиента и статуса (эмодзи + название).
НЕ повторяй инструкции. НЕ выводи правила. НЕ копируй матрицу.
Если фото нечитаемое — верни ⚠️ НЕДОСТАТОЧНО ДАННЫХ.
Максимум 400 символов."""

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": combined},
                        {"type": "image_url", "image_url": {"url": photo_url}}
                    ]
                }
            ],
            temperature=TEMPERATURE_ANALYSIS,
            max_tokens=MAX_TOKENS,
        )

        if not response or not response.choices:
            logger.error("Модель вернула пустой ответ (фото)")
            return {"verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Попробуйте ещё раз.", "status": "error"}

        # ===== ЛОГИРОВАНИЕ КЭША (фото) =====
        usage = response.usage
        if usage:
            cached_tokens = 0
            try:
                if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
                    cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
                elif hasattr(usage, "cached_tokens"):
                    cached_tokens = usage.cached_tokens or 0
            except Exception:
                cached_tokens = 0

            logger.info(
                f"📊 [ФОТО] Токены: вход={usage.prompt_tokens}, "
                f"из кэша={cached_tokens}, "
                f"выход={usage.completion_tokens}"
            )

        result = response.choices[0].message.content

        if not result:
            logger.error("Модель вернула пустой текст (фото)")
            return {"verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Попробуйте ещё раз.", "status": "error"}

        result = _safe_trim(result, 400)
        return {"verdict": result, "status": _get_status(result)}

    except Exception as e:
        logger.error(f"Ошибка анализа фото: {e}")
        return {"verdict": "⚠️ НЕДОСТАТОЧНО ДАННЫХ. Попробуйте позже.", "status": "error"}


# ================== ГЕНЕРАЦИЯ ДИАГНОЗА ==================
async def generate_diagnosis(name: str, tags: list) -> str:
    """Генерирует короткий диагноз (строго до 400 символов)."""
    tags_str = ", ".join(tags) if tags else "нет тегов"

    has_allergy = any(t.startswith("allergy_") for t in tags)
    has_medicine = "medicine" in tags
    has_pregnancy = "pregnancy" in tags
    has_personal = has_allergy or has_medicine or has_pregnancy

    if has_personal:
        personal_note = "\n\n⚠️ Есть персональные теги (аллергия/медицина/беременность). Упоминай их в шаге ПЕРСОНАЛИЗАЦИЯ прямо и кратко, без пояснений."
    else:
        personal_note = "\n\n⚠️ Персональных тегов НЕТ. ПРОПУСТИ шаг ПЕРСОНАЛИЗАЦИЯ полностью."

    user_prompt = f"""Имя: {name}
Теги: [{tags_str}]{personal_note}

ЗАДАЧА: Диагноз по формуле, СТРОГО до 400 символов.

ШАГИ:
1. ФАКТ — состояние кожи.
2. МЕХАНИКА — причина (профессиональные термины).
3. ПЕРСОНАЛИЗАЦИЯ — ТОЛЬКО если есть теги.
4. СТРАТЕГИЯ — вектор работы.

ЖЁСТКИЕ ПРАВИЛА:
- Максимум 400 символов.
- ЗАПРЕЩЕНЫ: вводные слова, лирика.
- ЗАПРЕЩЕНО выводить технические теги (вместо [Беременность] — «ваша беременность»).
- ПИШИ ПО-РУССКИ.
- НИКОГДА не обрывай предложение.

ЭТАЛОН (с персонализацией):
«Мария, фиксируем выраженную реактивность. Это прямое следствие нарушения целостности эпидермиса и дефицита структурных липидов. Зафиксированы ваша беременность, медицинский диагноз и аллергия на гиалуроновую кислоту. Наша стратегия — использовать физиологичный подход: компенсировать дефицит липидов, минимизировать трансэпидермальную потерю влаги и восстановить гидролипидную мантию».

ФОРМАТ: один абзац, 3–4 предложения."""

    combined = f"""{BASE_PROMPT}

==========================
ДАННЫЕ ОТ КЛИЕНТА:
==========================
{user_prompt}"""

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": combined}],
            temperature=TEMPERATURE_DIAGNOSIS,
            max_tokens=MAX_TOKENS,
        )

        if not response or not response.choices:
            return "Сухость и нарушение барьера. Требуется физиологичный подход: компенсировать дефицит липидов, восстановить целостность гидролипидной мантии."

        result = response.choices[0].message.content

        if not result:
            return "Сухость и нарушение барьера. Требуется физиологичный подход: компенсировать дефицит липидов, восстановить целостность гидролипидной мантии."

        return _safe_trim(result, 400)

    except Exception as e:
        logger.error(f"Ошибка в generate_diagnosis: {e}")
        return "Сухость и нарушение барьера. Требуется физиологичный подход: компенсировать дефицит липидов, восстановить целостность гидролипидной мантии."
