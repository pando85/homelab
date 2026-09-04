import asyncio
import logging

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

    Telegram MarkdownV2 requires escaping these characters: _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    for char in MARKDOWNV2_SPECIAL_CHARS:
        text = text.replace(char, f"\\{char}")
    return text


def negative_price_notification(prices) -> str | None:
    """
    Build a notification message listing the hours with negative prices,
    merging consecutive hours into ranges (e.g. "12-16h").

    Returns None when there are no negative prices.
    """
    negative_hours = sorted({p.datetime.hour for p in prices if p.value < 0})
    if not negative_hours:
        return None

    ranges = []
    start = prev = negative_hours[0]
    for hour in negative_hours[1:]:
        if hour == prev + 1:
            prev = hour
            continue
        ranges.append((start, prev))
        start = prev = hour
    ranges.append((start, prev))

    hours = ", ".join(f"{start:02d}-{(end + 1) % 24:02d}h" for start, end in ranges)
    return f"Negative PVPC prices: {hours}"


async def retry_with_backoff(
    func,
    max_retries: int = 3,
    initial_delay: float = 2.0,
    max_delay: float = 10.0,
    logger: logging.Logger = None,
    operation_name: str = "operation",
):
    """
    Retry an async function with exponential backoff.

    Args:
        func: Async callable to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay cap in seconds
        logger: Optional logger for retry messages
        operation_name: Name of operation for logging

    Returns:
        Result of the function call

    Raises:
        Last exception if all retries fail
    """
    delay = initial_delay
    last_exception = None

    for attempt in range(max_retries):
        try:
            return await func()
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                if logger:
                    logger.warning(
                        f"{operation_name} failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay}s"
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    raise last_exception
