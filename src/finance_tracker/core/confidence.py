def estimate_confidence(parsed: dict) -> float:
    """Simple heuristic confidence score based on field completeness."""
    score = 0.0
    if parsed.get("amount") and isinstance(parsed["amount"], (int, float)):
        score += 0.5
    if parsed.get("category") and parsed["category"] != "otro":
        score += 0.3
    if parsed.get("description"):
        score += 0.2
    return round(score, 2)
