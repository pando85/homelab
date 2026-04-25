MARKDOWNV2_SPECIAL_CHARS = [
    "_",
    "*",
    "[",
    "]",
    "(",
    ")",
    "~",
    ">",
    "#",
    "+",
    "-",
    "=",
    "|",
    "{",
    "}",
    ".",
    "!",
]


def escape_markdownv2(text: str) -> str:
    """
    Escape all MarkdownV2 special characters in text.

    Telegram MarkdownV2 requires escaping these characters: _ * [ ] ( ) ~ > # + - = | { } . !
    """
    for char in MARKDOWNV2_SPECIAL_CHARS:
        text = text.replace(char, f"\\{char}")
    return text
