"""Anti-plagiarism, trademark avoidance, and deduplication guardrails for stock generation."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import List, Tuple

logger = logging.getLogger("stockforge.images.guardrails")

# Prohibited corporate brands, trademarks, and protected products
PROHIBITED_TRADEMARKS: set[str] = {
    # Tech & Consumer Electronics
    "apple", "iphone", "ipad", "macbook", "imac", "airpods", "ipod", "ios",
    "samsung", "galaxy", "sony", "playstation", "xbox", "nintendo", "switch",
    "microsoft", "windows", "surface", "intel", "amd", "nvidia", "rtx",
    "google", "pixel", "android", "chromebook", "huawei", "xiaomi", "lenovo",
    "dell", "hp", "canon", "nikon", "panasonic", "fujifilm", "leica",
    # Apparel & Fashion
    "nike", "air jordan", "jordan", "adidas", "puma", "reebok", "under armour",
    "gucci", "prada", "louis vuitton", "chanel", "hermes", "balenciaga",
    "versace", "armani", "dior", "burberry", "rolex", "omega", "cartier",
    "zara", "h&m", "uniqlo", "levi's", "levis", "ray-ban", "rayban",
    # Automotive
    "tesla", "bmw", "mercedes", "mercedes-benz", "audi", "porsche", "ferrari",
    "lamborghini", "maserati", "toyota", "honda", "ford", "chevrolet",
    "volkswagen", "nissan", "hyundai", "kia", "volvo", "jeep", "harley-davidson",
    # Food & Beverage
    "coca-cola", "cocacola", "coke", "pepsi", "starbucks", "mcdonald's", "mcdonalds",
    "burger king", "kfc", "subway", "domino's", "pizza hut", "nestle", "red bull",
    "heineken", "budweiser", "corona", "nutella",
    # Digital Platforms & Media
    "instagram", "facebook", "meta", "tiktok", "twitter", "x.com", "youtube",
    "netflix", "spotify", "amazon", "disney", "disney+", "disneyland",
}

# Prohibited copyrighted characters and franchises
PROHIBITED_CHARACTERS: set[str] = {
    "mickey mouse", "donald duck", "spiderman", "spider-man", "batman", "superman",
    "iron man", "captain america", "thor", "hulk", "wolverine", "avengers",
    "star wars", "darth vader", "luke skywalker", "yoda", "pokemon", "pikachu",
    "mario", "luigi", "zelda", "sonic", "barbie", "hello kitty", "harry potter",
    "marvel", "dc comics", "pixar", "studio ghibli",
}

# Prohibited artist mimicry and style copying keywords
PROHIBITED_ARTIST_PATTERNS: list[str] = [
    r"\bin the style of\b",
    r"\bstyle of\b",
    r"\bart by\b",
    r"\bpainted by\b",
    r"\bdrawn by\b",
    r"\btrending on artstation\b",
    r"\bby greg rutkowski\b",
    r"\bby thomas kinkade\b",
    r"\bby alphonse mucha\b",
    r"\bby artgerm\b",
]

# Prohibited watermark, signature, and metadata keywords
PROHIBITED_WATERMARK_TERMS: set[str] = {
    "watermark", "signature", "copyright", "all rights reserved",
    "stock photo watermark", "getty images", "shutterstock", "adobe stock",
}

# Generic replacement mappings for common accidental brand mentions
GENERIC_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(iphone|galaxy phone|smartphone with apple logo)\b", re.IGNORECASE), "modern generic smartphone"),
    (re.compile(r"\b(macbook|surface laptop|dell xps)\b", re.IGNORECASE), "sleek modern unbranded laptop"),
    (re.compile(r"\b(ipad|galaxy tab)\b", re.IGNORECASE), "unbranded touchscreen tablet"),
    (re.compile(r"\b(nike shoes|nike sneakers|adidas sneakers)\b", re.IGNORECASE), "unbranded athletic running shoes"),
    (re.compile(r"\b(starbucks cup|starbucks coffee)\b", re.IGNORECASE), "generic paper takeaway coffee cup"),
    (re.compile(r"\b(tesla car|tesla model \w+)\b", re.IGNORECASE), "sleek generic electric vehicle"),
    (re.compile(r"\b(coca-cola can|pepsi can)\b", re.IGNORECASE), "unbranded soda can"),
]

# Mandatory commercial stock safety suffix appended to prompt
COMMERCIAL_SAFETY_SUFFIX = (
    ", commercial stock photography, 100% original composition, completely generic unbranded objects, "
    "no logos, no text, no watermarks, no trademarks, no copyrighted characters"
)


def validate_prompt_anti_plagiarism(prompt: str) -> Tuple[bool, List[str]]:
    """Validate that prompt does not contain prohibited brands, characters, or artist mimicry.
    
    Returns:
        Tuple[bool, List[str]]: (passed, list_of_violations)
    """
    if not prompt:
        return True, []

    p_lower = prompt.lower()
    violations: List[str] = []

    # 1. Check trademarked brands
    for brand in PROHIBITED_TRADEMARKS:
        pattern = rf"\b{re.escape(brand)}\b"
        if re.search(pattern, p_lower):
            violations.append(f"Merek dagang terlarang terdeteksi: '{brand}'")

    # 2. Check copyrighted characters
    for char in PROHIBITED_CHARACTERS:
        pattern = rf"\b{re.escape(char)}\b"
        if re.search(pattern, p_lower):
            violations.append(f"Karakter berhak cipta terdeteksi: '{char}'")

    # 3. Check artist mimicry patterns
    for pat in PROHIBITED_ARTIST_PATTERNS:
        if re.search(pat, p_lower):
            violations.append(f"Pola peniruan artis / plagiat gaya terdeteksi: '{pat}'")

    # 4. Check watermark / signature terms
    for term in PROHIBITED_WATERMARK_TERMS:
        pattern = rf"\b{re.escape(term)}\b"
        if re.search(pattern, p_lower):
            violations.append(f"Term watermark/hak cipta terdeteksi: '{term}'")

    return len(violations) == 0, violations


def sanitize_and_enforce_plagiarism_free_prompt(prompt: str) -> str:
    """Sanitize prompt by stripping artist mimicry, replacing brand names with generic terms,
    and enforcing commercial stock safety rules.
    """
    if not prompt:
        return "Clean original commercial stock photo" + COMMERCIAL_SAFETY_SUFFIX

    cleaned = prompt.strip()

    # 1. Apply generic replacements for brand mentions
    for pattern, generic_term in GENERIC_REPLACEMENTS:
        cleaned = pattern.sub(generic_term, cleaned)

    # 2. Strip artist mimicry phrases
    for pat in PROHIBITED_ARTIST_PATTERNS:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)

    # 3. Strip explicit watermark / signature terms
    for term in PROHIBITED_WATERMARK_TERMS:
        cleaned = re.sub(rf"\b{re.escape(term)}\b", "", cleaned, flags=re.IGNORECASE)

    # 4. Collapse extra whitespace and commas
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r",\s*,+", ", ", cleaned).strip(", ")

    # 5. Enforce commercial stock safety suffix if not already present
    if "100% original composition" not in cleaned:
        cleaned = f"{cleaned}{COMMERCIAL_SAFETY_SUFFIX}"

    logger.debug("Prompt sanitized for commercial anti-plagiarism compliance: %s", cleaned[:80])
    return cleaned


def compute_prompt_fingerprint(prompt: str) -> str:
    """Compute canonical hash for prompt deduplication."""
    normalized = re.sub(r"[^\w\s]", "", prompt.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
