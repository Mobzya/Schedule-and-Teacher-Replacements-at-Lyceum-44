from app.ui import main_keyboard


def test_unselected_class_button_is_not_duplicated():
    keyboard = main_keyboard(class_selected=False)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    class_buttons = [button for button in buttons if button.callback_data == "class:choose"]
    assert len(class_buttons) == 1
    assert class_buttons[0].text == "🎓 Выбрать класс"


def test_replacements_list_is_in_main_menu():
    keyboard = main_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any(button.callback_data == "changes" and "Список замен" in button.text for button in buttons)


def test_compact_main_and_nested_settings():
    from app.ui import menu_markup, notification_settings_view, settings_view

    menu = menu_markup(is_admin=True)
    assert len(menu["inline_keyboard"]) == 2
    assert {b["callback_data"] for r in menu["inline_keyboard"] for b in r} == {
        "today",
        "changes",
        "f:list:0",
        "settings",
    }
    user = dict(notifications=1, replacement_notifications=1, morning_summary=0, reminder_minutes=15)
    _, root = settings_view(user, "5а", True)
    assert {"admin", "f:manage:0", "settings:notifications"} <= {
        b["callback_data"] for r in root["inline_keyboard"] for b in r
    }
    _, notification = notification_settings_view(user, "5а")
    assert "settings" in {b["callback_data"] for r in notification["inline_keyboard"] for b in r}


def test_week_keeps_complete_html_and_all_seven_dates():
    from datetime import date
    from html.parser import HTMLParser

    from app.domain import EffectiveLesson
    from app.ui import render_week, week_markup

    class BalancedHTML(HTMLParser):
        def __init__(self):
            super().__init__()
            self.open_tags = []

        def handle_starttag(self, tag, attrs):
            self.open_tags.append(tag)

        def handle_endtag(self, tag):
            assert self.open_tags.pop() == tag

    lesson = EffectiveLesson(1, "Физика & <математика> " * 100, "101", None, None, None)
    days = [{"state": "ready", "lessons": [lesson] * 8} for _ in range(7)]
    monday = date(2026, 10, 19)
    text = render_week(monday, "5а", days)
    assert len(text) <= 3900
    parser = BalancedHTML()
    parser.feed(text)
    assert not parser.open_tags
    dates = [
        b["callback_data"]
        for row in week_markup(monday, 0)["inline_keyboard"]
        for b in row
        if b["callback_data"].startswith("day:")
    ]
    assert len(dates) == 7
    assert dates[-1] == "day:2026-10-25"
