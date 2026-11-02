import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from test_worker_ui import worker as worker_fixture

from app.db import Database
from app.messaging import TelegramAPIError
from app.services.avatars import ALL_THEMES, AVATAR_CRON, HOLIDAYS, AvatarService, theme_for_day

worker = worker_fixture


@pytest.mark.parametrize(
    "day,key",
    [
        (date(2026, 2, 20), "winter"),
        (date(2026, 3, 1), "spring"),
        (date(2026, 6, 5), "summer"),
        (date(2026, 9, 5), "autumn"),
        (date(2026, 11, 30), "autumn"),
        (date(2026, 12, 1), "winter"),
        (date(2026, 12, 28), "winter"),
        (date(2026, 12, 29), "new_year"),
        (date(2027, 1, 4), "new_year"),
        (date(2027, 1, 5), "winter"),
        (date(2026, 10, 27), "autumn"),
        (date(2026, 10, 28), "halloween"),
        (date(2026, 11, 3), "halloween"),
        (date(2026, 11, 4), "autumn"),
    ],
)
def test_seasons_holiday_boundaries_and_new_year(day, key):
    assert theme_for_day(day).key == key


def test_every_holiday_lasts_exactly_seven_days():
    for theme in HOLIDAYS:
        anchor = date(2026, theme.month, theme.day)
        for offset in range(-3, 4):
            assert theme_for_day(anchor + timedelta(days=offset)).key == theme.key
        assert theme_for_day(anchor - timedelta(days=4)).key != theme.key
        assert theme_for_day(anchor + timedelta(days=4)).key != theme.key


@pytest.mark.asyncio
async def test_only_changes_after_telegram_confirmation_and_recovers_after_restart(tmp_path):
    db = Database(tmp_path / "avatar.db")
    await db.init()
    upload = AsyncMock()
    service = AvatarService(db, ZoneInfo("Europe/Moscow"), upload)
    now = datetime(2026, 10, 19, tzinfo=UTC)
    assert await service.refresh(now)
    assert await db.get_state("avatar_current") == "strict-v1:autumn"
    assert not await AvatarService(db, service.timezone, upload).refresh(now + timedelta(hours=1))
    assert upload.await_count == 1
    # Oct 27 21:01 UTC is already Oct 28 in Moscow: holiday starts.
    assert await service.refresh(datetime(2026, 10, 27, 21, 1, tzinfo=UTC))
    assert await db.get_state("avatar_current") == "strict-v1:halloween"
    assert await service.refresh(datetime(2026, 11, 3, 21, 1, tzinfo=UTC))
    assert await db.get_state("avatar_current") == "strict-v1:autumn"


@pytest.mark.asyncio
async def test_failure_keeps_old_photo_and_retries_without_leaking_secrets(tmp_path):
    db = Database(tmp_path / "retry.db")
    await db.init()
    await db.set_state("avatar_current", "strict-v1:summer")
    upload = AsyncMock(side_effect=TelegramAPIError("private-token-bearing-url", 429, 1200))
    service = AvatarService(db, ZoneInfo("Europe/Moscow"), upload)
    now = datetime(2026, 10, 19, tzinfo=UTC)
    with pytest.raises(RuntimeError) as error:
        await service.refresh(now)
    assert "private-token" not in str(error.value)
    assert await db.get_state("avatar_current") == "strict-v1:summer"
    assert "private-token" not in (await db.get_state("avatar_last_error"))
    assert not await service.refresh(now + timedelta(minutes=10))
    upload.side_effect = None
    assert await service.refresh(now + timedelta(minutes=20))
    assert await db.get_state("avatar_current") == "strict-v1:autumn"
    assert await db.get_state("avatar_last_error") == ""
    await db.set_state("avatar_enabled", "0")
    assert not await service.refresh(now + timedelta(days=50), force=True)


@pytest.mark.asyncio
async def test_js_diagnostics_redact_urls_and_bare_bot_tokens(tmp_path):
    db = Database(tmp_path / "js-error.db")
    await db.init()
    secret = "123456:" + "s" * 35
    error_type = type("JsException", (Exception,), {})
    upload = AsyncMock(side_effect=error_type(f"Upload failed at https://api.test/bot{secret}; bot{secret}"))
    service = AvatarService(db, ZoneInfo("Europe/Moscow"), upload)
    with pytest.raises(RuntimeError, match="JsException"):
        await service.refresh(datetime(2026, 10, 21, tzinfo=UTC))
    stored = await db.get_state("avatar_last_error")
    assert secret not in stored and "https://" not in stored
    assert "<url>" in stored and "<token>" in stored
    assert not await db.get_state("avatar_current")


@pytest.mark.asyncio
async def test_avatar_cron_is_separate_from_import_and_notifications(worker, monkeypatch):
    settings = SimpleNamespace(timezone=ZoneInfo("Europe/Moscow"))
    sync, notices = SimpleNamespace(sync_next=AsyncMock()), SimpleNamespace(tick=AsyncMock())
    monkeypatch.setattr(worker, "_runtime", AsyncMock(return_value=(settings, "db", "bot", None, sync, notices)))
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_refresh_avatar", refresh)
    entry = worker.Default()
    entry.env = SimpleNamespace(ASSETS=object())
    await entry.scheduled(SimpleNamespace(cron=AVATAR_CRON), None, None)
    refresh.assert_awaited_once_with(entry.env, "db", settings, "bot")
    notices.tick.assert_not_awaited()
    sync.sync_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_request_is_consumed_before_failure_and_does_not_stall_notifications(worker, monkeypatch):
    db = SimpleNamespace(get_state=AsyncMock(return_value="1"), set_state=AsyncMock())
    notices = SimpleNamespace(tick=AsyncMock())
    monkeypatch.setattr(worker, "_runtime", AsyncMock(return_value=(None, db, "bot", None, None, notices)))
    refresh = AsyncMock(side_effect=RuntimeError("test upload failure"))
    monkeypatch.setattr(worker, "_refresh_avatar", refresh)
    entry = worker.Default()
    entry.env = object()
    await entry.scheduled(SimpleNamespace(cron="1-59/2 * * * *"), None, None)
    db.set_state.assert_awaited_once_with("avatar_manual_pending", "0")
    refresh.assert_awaited_once_with(entry.env, db, None, "bot", force=True)
    notices.tick.assert_not_awaited()
    db.get_state.return_value = "0"
    await entry.scheduled(SimpleNamespace(cron="1-59/2 * * * *"), None, None)
    notices.tick.assert_awaited_once_with("bot", user_limit=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk_wrapper", [False, True])
async def test_worker_native_multipart_upload(worker, monkeypatch, sdk_wrapper):
    parts = []
    form = SimpleNamespace(append=lambda *args: parts.append(args))
    monkeypatch.setattr(worker, "FormData", SimpleNamespace(new=lambda: form))
    monkeypatch.setattr(worker, "Object", SimpleNamespace(fromEntries=object()))
    monkeypatch.setattr(worker, "AbortSignal", SimpleNamespace(timeout=lambda value: value))
    native_parts = []
    monkeypatch.setattr(worker, "Array", SimpleNamespace(new=lambda: SimpleNamespace(push=native_parts.append)))
    photo = SimpleNamespace(size=70000, type="image/jpeg")

    def native_file(values, filename, options):
        assert filename == "autumn-strict-v1.jpg" and options == {"type": "image/jpeg"}
        return photo

    monkeypatch.setattr(worker, "File", SimpleNamespace(new=native_file))
    response = SimpleNamespace(
        ok=True, json=AsyncMock(return_value=SimpleNamespace(to_py=lambda: {"ok": True, "result": True}))
    )
    fetch = AsyncMock(return_value=response)
    monkeypatch.setattr(worker, "fetch", fetch)
    blob = object()
    supplied = SimpleNamespace(js_object=blob) if sdk_wrapper else blob
    await worker.TelegramClient("test-token").set_profile_photo(supplied, "autumn-strict-v1.jpg")
    assert json.loads(parts[0][1]) == {"type": "static", "photo": "attach://avatar"}
    assert native_parts == [blob]
    assert parts[1] == ("avatar", photo)
    options = fetch.await_args.args[1]
    assert options["body"] is form and "headers" not in options  # JS adds the multipart boundary
    assert hasattr(worker.TelegramClient, "send_message")


@pytest.mark.asyncio
async def test_profile_photo_does_not_retry_invalid_wire_format(worker, monkeypatch):
    forms = []

    def form():
        parts = []
        forms.append(parts)
        return SimpleNamespace(append=lambda *values: parts.append(values))

    monkeypatch.setattr(worker, "FormData", SimpleNamespace(new=form))
    bot = worker.TelegramClient("test-token")
    monkeypatch.setattr(worker, "Object", SimpleNamespace(fromEntries=object()))
    monkeypatch.setattr(worker, "Array", SimpleNamespace(new=lambda: SimpleNamespace(push=lambda value: None)))
    monkeypatch.setattr(worker, "File", SimpleNamespace(new=lambda *args: SimpleNamespace(size=10, type="image/jpeg")))
    bot._upload_profile_form = AsyncMock(
        side_effect=TelegramAPIError("Bad Request: photo must be uploaded as a file", 400)
    )
    blob = object()
    with pytest.raises(TelegramAPIError):
        await bot.set_profile_photo(blob, "test.jpg")
    assert bot._upload_profile_form.await_count == 1


@pytest.mark.asyncio
async def test_worker_streams_only_verified_jpeg_from_assets(worker, monkeypatch, tmp_path):
    db = Database(tmp_path / "assets.db")
    await db.init()
    blob = object()
    response = SimpleNamespace(ok=True, headers={"content-type": "image/jpeg"}, blob=AsyncMock(return_value=blob))
    assets = SimpleNamespace(fetch=AsyncMock(return_value=response))
    env = SimpleNamespace(ASSETS=assets)
    bot = SimpleNamespace(set_profile_photo=AsyncMock())
    settings = SimpleNamespace(timezone=ZoneInfo("Europe/Moscow"))
    assert await worker._refresh_avatar(env, db, settings, bot)
    assets.fetch.assert_awaited_once_with("https://assets.local/avatars/autumn-strict-v1.jpg")
    bot.set_profile_photo.assert_awaited_once_with(blob, "autumn-strict-v1.jpg")
    assert not await worker._refresh_avatar(env, db, settings, bot)
    assert assets.fetch.await_count == 1
    await db.set_state("avatar_current", "strict-v1:summer")
    response.headers["content-type"] = "text/html"
    with pytest.raises(RuntimeError):
        await worker._refresh_avatar(env, db, settings, bot)
    assert await db.get_state("avatar_current") == "strict-v1:summer"
    assert bot.set_profile_photo.await_count == 1


def test_all_avatar_files_and_cron_configuration_exist():
    root = Path(__file__).parents[1]
    assert len(ALL_THEMES) == len({theme.key for theme in ALL_THEMES}) == 14
    config = json.loads((root / "wrangler.jsonc").read_text())
    assert AVATAR_CRON in config["triggers"]["crons"]
    assert config["assets"]["binding"] == "ASSETS"
    for theme in ALL_THEMES:
        path = root / "public" / "avatars" / theme.filename
        assert path.is_file(), path.name
        assert path.read_bytes().startswith(b"\xff\xd8\xff")  # Telegram requires actual JPG, not renamed PNG
        assert 1000 < path.stat().st_size < 250_000
