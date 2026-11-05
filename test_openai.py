import asyncio
from ai_analyzer import analyze_composition

async def test():
    composition = """
    Aqua, Glycerin, Niacinamide, Panthenol, Tocopherol,
    Sodium Hyaluronate, Ceramide NP, Ceramide AP,
    Beeswax, Sodium Cocoate, Sodium Palmate,
    Aloe Barbadensis Leaf Juice, Mentha Piperita Extract,
    Butyrospermum Parkii Butter, Camellia Sinensis Leaf Extract
    """
    tags = ["problem", "reactive"]
    
    result = await analyze_composition(composition, tags)
    print("ВЕРДИКТ:")
    print(result["verdict"])
    print("\nСТАТУС:", result["status"])

if __name__ == "__main__":
    asyncio.run(test())