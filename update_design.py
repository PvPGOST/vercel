# update_design.py
import os
import json
import base64
import asyncio

from bot import load_projects, deploy_to_vercel

async def main():
    projects = load_projects()
    here = os.path.dirname(__file__)
    tpl_dir = os.path.join(here, "template")

    # 1) Читаем шаблоны
    with open(os.path.join(tpl_dir, "index.html"), "r", encoding="utf-8") as f:
        tpl_html = f.read()
    with open(os.path.join(tpl_dir, "style.css"), "r", encoding="utf-8") as f:
        tpl_css = f.read()

    # 2) Для каждой записи собираем новые поля и деплоим
    for key, entry in projects.items():
        # Основное лого
        logo1_name = entry.get("logo_name")
        logo1_b64  = entry.get("logo_data")
        if not logo1_name or not logo1_b64:
            print(f"[{key}] пропущено: нет logo_name/logo_data")
            continue

        # Для кнопок — либо свои, либо тот же самый
        logo2_name = entry.get("logo2_name", logo1_name)
        logo2_b64  = entry.get("logo2_data", logo1_b64)
        logo3_name = entry.get("logo3_name", logo1_name)
        logo3_b64  = entry.get("logo3_data", logo1_b64)

        # Собираем HTML
        html = (tpl_html
            .replace("%%TITLE%%", entry["title"])
            .replace("%%LOGO1%%", logo1_name)
            .replace("%%LINK1%%", entry["link1"])
            .replace("%%LOGO2%%", logo2_name)
            .replace("%%LINK2%%", entry["link2"])
            .replace("%%LOGO3%%", logo3_name)
            .replace("%%LINK3%%", entry["link3"])
        )

        css = tpl_css

        # Декодируем bytes только для header-лого
        logo1_bytes = base64.b64decode(logo1_b64)

        # 3) Вызываем deploy_to_vercel — кидаем в тот же проект
        project = entry["project"]
        url, _ = await deploy_to_vercel(html, css, logo1_bytes, logo1_name, project=project)
        print(f"[{key}] обновлён → {url}")

if __name__ == "__main__":
    asyncio.run(main())
