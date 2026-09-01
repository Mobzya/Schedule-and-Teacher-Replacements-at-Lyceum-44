import io

from openpyxl import Workbook

from app.services.sync import SyncService


def test_xlsx_sheets_reads_visible_tabs_and_integral_numbers():
    workbook = Workbook()
    visible = workbook.active
    visible.title = "5 классы"
    visible.append(["2026-2027 учебный год", 1.0, 212.0])
    hidden = workbook.create_sheet("старое")
    hidden.sheet_state = "hidden"
    hidden.append(["2020-2021 учебный год"])
    output = io.BytesIO()
    workbook.save(output)

    sheets = SyncService._xlsx_sheets(output.getvalue())

    assert sheets == [("5 классы", "2026-2027 учебный год,1,212\r\n")]
