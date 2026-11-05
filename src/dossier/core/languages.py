"""Spoken-language codes: the languages Whisper recognizes, with English names.

Meetings take a language code (``es``, ``en``...) or ``auto``. Codes are the
ones Whisper uses (mostly ISO 639-1); regional forms such as ``es-MX`` and
English names such as ``Spanish`` are accepted and mapped to the code.
"""

from __future__ import annotations

from dossier.core.errors import ConfigError

AUTO = "auto"

LANGUAGES: dict[str, str] = {
    "en": "english",
    "zh": "chinese",
    "de": "german",
    "es": "spanish",
    "ru": "russian",
    "ko": "korean",
    "fr": "french",
    "ja": "japanese",
    "pt": "portuguese",
    "tr": "turkish",
    "pl": "polish",
    "ca": "catalan",
    "nl": "dutch",
    "ar": "arabic",
    "sv": "swedish",
    "it": "italian",
    "id": "indonesian",
    "hi": "hindi",
    "fi": "finnish",
    "vi": "vietnamese",
    "he": "hebrew",
    "uk": "ukrainian",
    "el": "greek",
    "ms": "malay",
    "cs": "czech",
    "ro": "romanian",
    "da": "danish",
    "hu": "hungarian",
    "ta": "tamil",
    "no": "norwegian",
    "th": "thai",
    "ur": "urdu",
    "hr": "croatian",
    "bg": "bulgarian",
    "lt": "lithuanian",
    "la": "latin",
    "mi": "maori",
    "ml": "malayalam",
    "cy": "welsh",
    "sk": "slovak",
    "te": "telugu",
    "fa": "persian",
    "lv": "latvian",
    "bn": "bengali",
    "sr": "serbian",
    "az": "azerbaijani",
    "sl": "slovenian",
    "kn": "kannada",
    "et": "estonian",
    "mk": "macedonian",
    "br": "breton",
    "eu": "basque",
    "is": "icelandic",
    "hy": "armenian",
    "ne": "nepali",
    "mn": "mongolian",
    "bs": "bosnian",
    "kk": "kazakh",
    "sq": "albanian",
    "sw": "swahili",
    "gl": "galician",
    "mr": "marathi",
    "pa": "punjabi",
    "si": "sinhala",
    "km": "khmer",
    "sn": "shona",
    "yo": "yoruba",
    "so": "somali",
    "af": "afrikaans",
    "oc": "occitan",
    "ka": "georgian",
    "be": "belarusian",
    "tg": "tajik",
    "sd": "sindhi",
    "gu": "gujarati",
    "am": "amharic",
    "yi": "yiddish",
    "lo": "lao",
    "uz": "uzbek",
    "fo": "faroese",
    "ht": "haitian creole",
    "ps": "pashto",
    "tk": "turkmen",
    "nn": "nynorsk",
    "mt": "maltese",
    "sa": "sanskrit",
    "lb": "luxembourgish",
    "my": "myanmar",
    "bo": "tibetan",
    "tl": "tagalog",
    "mg": "malagasy",
    "as": "assamese",
    "tt": "tatar",
    "haw": "hawaiian",
    "ln": "lingala",
    "ha": "hausa",
    "ba": "bashkir",
    "jw": "javanese",
    "su": "sundanese",
    "yue": "cantonese",
}
_BY_NAME = {name: code for code, name in LANGUAGES.items()}


def language_code(value: str) -> str | None:
    """``"es"``, ``"es-MX"``, ``"Spanish"`` -> ``"es"``; ``"auto"`` stays; else None."""
    text = value.strip().lower().replace("_", "-")
    if text == AUTO:
        return AUTO
    if text in LANGUAGES:
        return text
    primary = text.split("-", 1)[0]
    if primary in LANGUAGES:
        return primary
    return _BY_NAME.get(text)


def parse_language(value: str, *, allow_auto: bool = True) -> str:
    """Like :func:`language_code`, but raises ConfigError for an unknown value."""
    code = language_code(value)
    if code is None or (code == AUTO and not allow_auto):
        hint = ' or "auto"' if allow_auto else ""
        raise ConfigError(f'unknown language {value!r}: use a code such as "en" or "es"{hint}')
    return code


def language_name(code: str) -> str:
    """``"es"`` -> ``"Spanish"``; unknown codes are returned unchanged."""
    return LANGUAGES.get(code, code).title()
