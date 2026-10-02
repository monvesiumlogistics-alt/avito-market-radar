"""Ручной вход в Avito. Открывает Chromium с профилем бота: логинишься сам (телефон, SMS, капча), закрываешь окно.

Пароль нигде не сохраняется: в профиле остаются только cookies, как в обычном браузере.
Перед запуском останови основной бот: профиль не может быть открыт двумя процессами сразу.
"""

import asyncio

from playwright.async_api import async_playwright

from app.config import playwright_proxy, settings


async def main() -> None:
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            settings.avito_profile_path,
            headless=False,
            proxy=playwright_proxy(settings.avito_proxy),  # вход с того же IP, что и бот
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.avito.ru/#login")
        print("Войди в аккаунт Avito в открывшемся окне, затем просто закрой окно браузера.")
        await ctx.wait_for_event("close", timeout=0)
    print(f"Профиль сохранён в {settings.avito_profile_path}")


if __name__ == "__main__":
    asyncio.run(main())
