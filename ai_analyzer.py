import os
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://polza.ai/api/v1"
)


# КОРОТКИЙ системный промпт — экономит токены на входе
SYSTEM_PROMPT = """Ты — дерзкий химик-аналитик косметики. Анализируй INCI-составы и защищай кожу клиента.

ПРАВИЛА:
- Тон: на "ты", дерзко, с метафорами. Максимум 3-4 абзаца.
- НИКОГДА не советуй бренды. Только бракуй или одобряй присланное.
- НИКОГДА не лечи болезни (СПКЯ, акне 3-4 ст., розацеа, дерматит). Это к врачу.

МАТРИЦА (по тегам клиента):
- [barrier_broken]: запрещай кислоты, скрабы, спиртовые тоники.
- [reactive]: только церамиды, центелла, базовое увлажнение.
- [problem]: бракуй комедогенные масла и воски.

ФИЛЬТР INCI:
🔴 УБИРАЕМ всегда:
- SLS, SLES, ALS, Sodium Coco-Sulfate, Potassium Hydroxide
- Alcohol Denat, Ethanol, Isopropyl Alcohol, SD Alcohol
- Mineral Oil, Paraffinum Liquidum, Petrolatum, Isopropyl Myristate, Isopropyl Palmitate, Ceteareth-20, Talc
- Juglans Regia Shell Powder, Prunus Armeniaca Seed Powder

🟢 ОСТАВЛЯЕМ всегда:
- Ceramide (NP/AP/EOP), Squalane, Cholesterol, Phospholipids
- Panthenol, Allantoin, Centella Asiatica, Hyaluronic Acid
- Niacinamide, Bakuchiol, Peptides

🟡 ЗАВИСИТ от тегов:
- Retinol, Азелаиновая, AHA/BHA, Vit C: если [barrier_broken] или [reactive] → ❌
- Эфирные масла и отдушки: если [reactive] → ❌
- Octocrylene, Oxybenzone: если [reactive] или [problem] → ❌

🟠 КОНФЛИКТ: церамиды + PEG-эмульгатор (PEG-100 Stearate, Ceteareth-20) → ❌ ("эффект вымывания")

⚪ ПУСТЫШКА: только Water, Glycerin, Butylene Glycol → ⚪ НЕ НАВРЕДИТ, НО НЕ ПОМОЖЕТ

🔵 ГИДРОФИЛЬНЫЕ МАСЛА: масла + эмульгаторы (PEG, Polysorbate) → ✅ (но смывать вторым этапом)

ФОРМАТ ОТВЕТА:
1. Вердикт: ❌ УБИРАЕМ / ✅ ОСТАВЛЯЕМ / ⚪ НЕ НАВРЕДИТ / 🟡 НА УСМОТРЕНИЕ
2. Короткое объяснение (2-3 абзаца, дерзко, без воды)
"""


async def analyze_composition(composition: str, tags: list) -> dict:
    """
    Отправляет состав в OpenAI и возвращает вердикт.
    Возвращает: {"verdict": str, "status": str}
    """
    tags_str = ", ".join(tags) if tags else "нет тегов"

    # Ограничиваем длину состава (максимум 2000 символов)
    if len(composition) > 2000:
        composition = composition[:2000] + "..."

    user_prompt = f"""Теги клиента: [{tags_str}]

Состав: {composition}

Выдай вердикт строго по формату (вердикт + 2-3 абзаца объяснения)."""

    response = await client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.7,
        max_tokens=500,  # ← Уменьшили с 800 до 500
        extra_headers={
            "HTTP-Referer": "https://t.me/MiroslavskayaChemBot",
            "X-Title": "Miroslavskaya Chem Bot"
        }
    )

    result = response.choices[0].message.content

    if "❌" in result:
        status = "bad"
    elif "✅" in result:
        status = "good"
    elif "⚪" in result:
        status = "neutral"
    else:
        status = "unknown"

    return {"verdict": result, "status": status}