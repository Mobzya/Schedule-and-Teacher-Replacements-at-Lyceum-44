"""Private, manually labelled class bookmarks. Shared by Workers and aiogram."""

from __future__ import annotations

import re
from datetime import datetime

from ..ui import button, class_picker, fit_lines, lesson_line, markup, short
from ..utils import esc

PAGE_SIZE = 3


def validate_label(text: str) -> str:
    label = " ".join(text.split())
    if not 1 <= len(label) <= 48 or any(ord(c) < 32 for c in text):
        raise ValueError("Имя должно содержать 1–48 символов, без переносов строк.")
    if label.startswith("@") and not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", label):
        raise ValueError("Укажите имя или @username: 5–32 латинских буквы, цифры и подчёркивания.")
    return label


class FriendsService:
    def __init__(self, db, settings, schedules):
        self.db, self.settings, self.schedules = db, settings, schedules

    async def page(self, owner: int, page: int, now: datetime, manage: bool = False):
        friends = await self.db.list_friends(owner)
        last = max(0, (len(friends) - 1) // PAGE_SIZE)
        page = max(0, min(last, page))
        selected = friends[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        lines = [f"👥 <b>{'Настроить друзей' if manage else 'Друзья'}</b> · {len(friends)}/30"]
        rows = []
        prefix = "f:manage" if manage else "f:list"
        if not friends:
            lines += [
                "",
                "Добавьте имя или @username и выберите класс.",
                "Список виден только вам. Это закладки расписания, а не поиск аккаунтов Telegram.",
            ]
        elif manage:
            lines += ["", "Выберите друга, чтобы изменить имя, класс или удалить запись."]
        else:
            lines += [f"{now:%d.%m · %H:%M}"]
        cache = {}
        for friend in selected:
            suffix = f" · {short(friend['profile'], 40)}" if friend["profile"] else ""
            display = f"{friend['class_name'].upper()}{suffix}"
            if manage:
                rows.append([button(short(f"{friend['label']} · {display}", 50), f"f:edit:{friend['id']}:{page}")])
                continue
            lines += ["", f"<b>{esc(friend['label'])}</b> · {esc(display)}"]
            if friend["academic_year"] != self.settings.academic_year:
                lines.append("⚠️ Новый учебный год — обновите класс в настройках друзей.")
                continue
            key = (friend["class_name"], friend["profile"])
            if key not in cache:
                cache[key] = await self.schedules.current_and_next(*key, now)
            current, nxt, info = cache[key]
            if info["state"] != "ready":
                lines.append(f"ℹ️ {esc(short(info['message'], 160))}")
                continue
            if current:
                lines.append("Сейчас: " + lesson_line(current))
            elif nxt:
                lines.append("Сейчас: перемена / до начала занятий")
            if nxt:
                lines.append("Далее: " + lesson_line(nxt))
            elif current:
                lines.append("Далее: занятий нет")
            elif not info.get("bells_known", True) or any(
                not lesson.start_time for lesson in info.get("lessons", []) if not lesson.cancelled
            ):
                lines.append("🕘 Время звонков неизвестно — текущий урок определить нельзя.")
            elif info.get("lessons") and any(not lesson.cancelled for lesson in info["lessons"]):
                lines.append("✅ На сегодня занятия закончились")
            else:
                lines.append("На сегодня опубликованных уроков нет / все отменены")
        if friends:
            nav = []
            if page:
                nav.append(button("‹", f"{prefix}:{page - 1}"))
            nav.append(button(f"↻ {page + 1}/{last + 1}", f"{prefix}:{page}"))
            if page < last:
                nav.append(button("›", f"{prefix}:{page + 1}"))
            rows.append(nav)
        if manage:
            if len(friends) < 30:
                rows.append([button("＋ Добавить друга", "f:add")])
            rows.append([button("← Друзья", f"f:list:{page}"), button("← Настройки", "settings")])
        else:
            rows.append([button("⚙️ Настроить друзей", f"f:manage:{page}"), button("← Меню", "home")])
        return (
            fit_lines(lines, truncated_message="\n… Сократите длинные имена или откройте следующую страницу."),
            markup(*rows),
            None,
            {},
            "",
        )

    async def picker(self, draft: dict, grade=None):
        text, keyboard = class_picker(await self.db.classes(), grade)
        for row in keyboard["inline_keyboard"]:
            for item in row:
                old = item["callback_data"]
                if old.startswith("grade:"):
                    item["callback_data"] = "f:g:" + old.split(":", 1)[1]
                elif old.startswith("classn:"):
                    item["callback_data"] = "f:c:" + old.split(":", 1)[1]
                elif old == "class:choose":
                    item["callback_data"] = "f:classes"
                elif old == "home":
                    item["text"], item["callback_data"] = "Отмена", f"f:manage:{draft.get('page', 0)}"
        text = f"👤 <b>{esc(draft['label'])}</b>\n\n" + text.replace("выберите свой", "выберите класс друга")
        return text, keyboard, "friend:class", draft, ""

    async def callback(self, owner: int, data: str, state: str | None, draft: dict, now: datetime):
        parts = data.split(":")
        lengths = {
            "list": 3,
            "manage": 3,
            "add": 2,
            "edit": 4,
            "rename": 4,
            "class": 4,
            "delete": 4,
            "remove": 4,
            "classes": 2,
            "g": 3,
            "c": 3,
            "p": 4,
        }
        if len(parts) < 2 or len(parts) != lengths.get(parts[1]):
            raise ValueError("Кнопка устарела. Откройте список друзей заново.")
        action = parts[1]
        if action in {"list", "manage"}:
            return await self.page(owner, int(parts[2]), now, action == "manage")
        if action == "add":
            if len(await self.db.list_friends(owner)) >= 30:
                raise ValueError("В списке уже 30 друзей. Удалите ненужную запись.")
            return (
                "👤 Пришлите имя или @username друга (до 48 символов).\n"
                "Затем выберем его класс. Запись видна только вам.",
                markup([button("Отмена", "f:manage:0")]),
                "friend:name",
                {},
                "",
            )
        if action in {"edit", "rename", "class", "delete", "remove"}:
            friend_id, page = int(parts[2]), max(0, int(parts[3]))
            friend = await self.db.get_friend(owner, friend_id)
            if friend is None:
                raise ValueError("Запись не найдена. Обновите список друзей.")
            draft = {
                "friend_id": friend_id,
                "label": friend["label"],
                "class_name": friend["class_name"],
                "profile": friend["profile"],
                "academic_year": friend["academic_year"],
                "page": page,
            }
            if action == "edit":
                profile = f" · {friend['profile']}" if friend["profile"] else ""
                return (
                    f"👤 <b>{esc(friend['label'])}</b> · {esc(friend['class_name'].upper() + profile)}",
                    markup(
                        [button("Имя", f"f:rename:{friend_id}:{page}"), button("Класс", f"f:class:{friend_id}:{page}")],
                        [button("Удалить", f"f:delete:{friend_id}:{page}"), button("← Назад", f"f:manage:{page}")],
                    ),
                    None,
                    {},
                    "",
                )
            if action == "rename":
                draft["rename"] = True
                return (
                    "Пришлите новое имя или @username.",
                    markup([button("Отмена", f"f:edit:{friend_id}:{page}")]),
                    "friend:name",
                    draft,
                    "",
                )
            if action == "class":
                return await self.picker(draft)
            if action == "delete":
                return (
                    f"Удалить <b>{esc(friend['label'])}</b> из вашего списка?",
                    markup(
                        [
                            button("Удалить", f"f:remove:{friend_id}:{page}"),
                            button("Отмена", f"f:edit:{friend_id}:{page}"),
                        ]
                    ),
                    None,
                    {},
                    "",
                )
            await self.db.delete_friend(owner, friend_id)
            return await self.page(owner, page, now, True)
        if state != "friend:class" or not draft.get("label"):
            raise ValueError("Выбор устарел. Откройте настройки друзей заново.")
        if action in {"classes", "g"}:
            return await self.picker(draft, parts[2] if action == "g" else None)
        classes = await self.db.classes()
        class_name = parts[2]
        choices = [(c, p) for c, p in classes if c == class_name]
        if not choices:
            raise ValueError("Класс больше недоступен. Выберите другой.")
        profiles = sorted({p for _, p in choices if p})
        if action == "c" and profiles:
            return (
                f"👤 {esc(draft['label'])} · {esc(class_name.upper())}\nВыберите профиль:",
                markup(
                    *[[button(p, f"f:p:{class_name}:{i}")] for i, p in enumerate(profiles)],
                    [button("← Классы", "f:classes")],
                ),
                "friend:class",
                draft,
                "",
            )
        profile = None
        if action == "p":
            index = int(parts[3])
            if index < 0 or index >= len(profiles):
                raise ValueError("Профиль изменился. Выберите класс заново.")
            profile = profiles[index]
        elif action != "c":
            raise ValueError("Неизвестное действие")
        await self.save(owner, draft, class_name, profile, self.settings.academic_year)
        return await self.page(owner, draft.get("page", 0), now, True)

    async def save(self, owner, draft, class_name, profile, academic_year):
        saved = await self.db.save_friend(
            owner, draft["label"], class_name, profile, academic_year, draft.get("friend_id")
        )
        if not saved:
            raise ValueError("Такое имя уже есть, список заполнен или запись удалена. Измените имя / обновите список.")

    async def message(self, owner: int, text: str, draft: dict, now: datetime):
        draft = {**draft, "label": validate_label(text)}
        if draft.get("rename"):
            await self.save(owner, draft, draft["class_name"], draft["profile"], draft["academic_year"])
            return await self.page(owner, draft.get("page", 0), now, True)
        if any(f["label_key"] == draft["label"].casefold() for f in await self.db.list_friends(owner)):
            raise ValueError("Это имя уже есть в списке. Пришлите другое имя.")
        return await self.picker(draft)
