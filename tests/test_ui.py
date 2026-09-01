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
