from datetime import date

from app.parsers import declared_academic_year, parse_date_schedule, parse_replacements, parse_weekly_schedule, read_csv
from app.source_urls import google_sheet_export_url

WEEKLY = """Расписание учебных занятий,,,,,,,,\nна 2026-2027 учебный год,,,,,,,,\n,,9а,каб,9е,каб,,,,9ю,каб\nпонедельник,,,,,,,,,,\n,1,Алгебра,316,Физика,301,,,1,Литература,210\n,2,Русский язык,211,Химия,305,,,2,История,104\nвторник,,,,,,,,,,\n,1,Биология,306,География,209,,,1,Физика,301\n"""

DAILY = """Расписание на 02.09.2026,,,,\n,9а,каб,9е,каб\n1,Алгебра,316,Физика,301\n2,Русский язык,211,Химия,305\n"""

REPL = """дата,урок,Класс,Предмет,Заменяющий учитель,Кабинет,примечание\n02.09,2,9а,Физика,Иванов И.И.,301,\n02.09,3,9а,,,,урок отменён\n10.01,4,9а,Алгебра,Петров П.П.,316,\n"""


def test_year_detection_not_class_header():
    rows = read_csv(WEEKLY)
    assert declared_academic_year(rows) == "2026-2027"


def test_weekly_two_blocks():
    lessons, year = parse_weekly_schedule(WEEKLY)
    assert year == "2026-2027"
    assert any(
        x.class_name == "9а" and x.weekday == 0 and x.lesson_number == 1 and x.subject == "Алгебра" for x in lessons
    )
    assert any(
        x.class_name == "9ю" and x.weekday == 0 and x.lesson_number == 2 and x.subject == "История" for x in lessons
    )


def test_date_schedule_forced_or_embedded():
    lessons, _, day = parse_date_schedule(DAILY)
    assert day == date(2026, 9, 2)
    assert len(lessons) == 4


def test_replacements_short_date_academic_year_boundary():
    repl, _ = parse_replacements(REPL, fallback_year=2026)
    assert any(x.lesson_date == date(2026, 9, 2) and x.lesson_number == 2 for x in repl)
    assert any(x.lesson_date == date(2027, 1, 10) and x.lesson_number == 4 for x in repl)
    assert any(x.lesson_number == 3 and x.cancelled for x in repl)


def test_google_url_conversion_query_and_fragment():
    a = google_sheet_export_url("https://docs.google.com/spreadsheets/d/abc_123/edit?gid=55")
    b = google_sheet_export_url("https://docs.google.com/spreadsheets/d/abc_123/edit#gid=77")
    assert a.endswith("gid=55") and b.endswith("gid=77")


def test_daily_tabular_format():
    text = (
        """Дата 03.09.2026\nКласс,Урок,Предмет,Кабинет\n10а,1,Алгебра,316\n10а,2,Физика,301\n10б,1,Литература,210\n"""
    )
    lessons, _, day = parse_date_schedule(text)
    assert day == date(2026, 9, 3)
    assert len(lessons) == 3


def test_weekly_tabular_format():
    text = """2026-2027 учебный год\nДень,Класс,Урок,Предмет,Кабинет\nпонедельник,10а,1,Алгебра,316\nвторник,10а,1,Физика,301\n"""
    lessons, year = parse_weekly_schedule(text)
    assert year == "2026-2027" and {x.weekday for x in lessons} == {0, 1}


def test_user_temporary_sheet_share_link():
    url = "https://docs.google.com/spreadsheets/d/1LpDcfAjFyqmQ8n7MTmA57T0Dxgff4VgnrsvZSnUIz7Y/edit?usp=drivesdk"
    export = google_sheet_export_url(url)
    assert "1LpDcfAjFyqmQ8n7MTmA57T0Dxgff4VgnrsvZSnUIz7Y" in export
    assert "gid=" not in export


def test_google_url_without_gid_does_not_force_first_sheet():
    export = google_sheet_export_url("https://docs.google.com/spreadsheets/d/abc_123/edit?usp=drivesdk")
    assert export == "https://docs.google.com/spreadsheets/d/abc_123/export?format=csv"


def test_date_schedule_ignores_transition_cells():
    text = """2026-2027 учебный год\n,,5а,каб,5б,каб\nсреда,1,переход,101,Математика,102\n,2,Русский язык,103,Литература,104\n,3,История,105,Биология,106\n"""
    lessons, _, _ = parse_date_schedule(text, date(2026, 9, 2))
    assert all(x.subject.lower() != "переход" for x in lessons)


def test_weekly_schedule_ignores_transition_cells():
    text = """2026-2027 учебный год\n,,5а,каб\nпонедельник,1,Переход,101\n,2,Русский язык,103\n,3,История,105\n,4,Математика,106\n,5,Биология,107\n,6,Музыка,108\n"""
    lessons, _ = parse_weekly_schedule(text)
    assert len(lessons) == 5
    assert all(x.subject.lower() != "переход" for x in lessons)
