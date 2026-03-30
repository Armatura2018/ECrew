import asyncio
import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton, 
                           CallbackQuery, BotCommand, BotCommandScopeDefault)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError
import aiosqlite

# === НАСТРОЙКИ ===
BOT_TOKEN = "8622961253:AAEkR6VSv3WnLKjNJ19eJkPjmM9dfLz5jB8"
CREATOR_ID = 7616343249
DB_PATH = "data/airline_bot.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# === СОСТОЯНИЯ (FSM) ===
class AddTrainee(StatesGroup):
    waiting_for_dept = State()

class ChangeDept(StatesGroup):
    waiting_for_dept = State()

class ExamMessage(StatesGroup):
    waiting_for_text = State()

class CreateEvent(StatesGroup):
    choosing_type = State()
    choosing_dept = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_location = State()
    waiting_for_description = State()
    confirming = State()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="interview", description="Запись на интервью"),
        BotCommand(command="training", description="Запись на тренинг"),
        BotCommand(command="my_bookings", description="Мои активные записи")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

def is_event_actual(date_str: str, time_str: str) -> bool:
    try:
        event_dt = datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%d.%m.%Y %H:%M")
        return event_dt > datetime.now()
    except Exception:
        return True

def escape_md(text: str) -> str:
    # Экранирование спецсимволов для MarkdownV2
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    res = str(text)
    for char in chars:
        res = res.replace(char, f'\\{char}')
    return res

# === БАЗА ДАННЫХ ===
async def init_db():
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, role TEXT, department TEXT, 
            stage TEXT, is_active INTEGER DEFAULT 1)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, department TEXT, 
            date TEXT, time TEXT, location TEXT, description TEXT, host_name TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS bookings (
            event_id INTEGER, user_id INTEGER, PRIMARY KEY(event_id, user_id))""")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('exam_text', 'Ссылка на экзамен пока не задана.')")
        await db.commit()

# === ПРОВЕРКИ ПРАВ ===
async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role, department, stage, is_active FROM users WHERE user_id = ?", (user_id,)) as c:
            return await c.fetchone()

async def is_creator(user_id: int) -> bool:
    return user_id == CREATOR_ID

async def is_head_admin(user_id: int) -> bool:
    if await is_creator(user_id): return True
    data = await get_user_data(user_id)
    return data is not None and data[0] == 'head_admin' and data[3] == 1

async def is_admin(user_id: int) -> bool:
    if await is_head_admin(user_id): return True
    data = await get_user_data(user_id)
    return data is not None and data[0] in ('admin', 'head_admin') and data[3] == 1

async def is_active_trainee(user_id: int) -> bool:
    data = await get_user_data(user_id)
    return data is not None and data[0] == 'trainee' and data[3] == 1

# === КЛАВИАТУРЫ ===
def get_departments_kb(prefix: str):
    b = InlineKeyboardBuilder()
    b.button(text="Пилоты", callback_data=f"{prefix}_pilots")
    b.button(text="Наземные службы", callback_data=f"{prefix}_ground")
    b.button(text="Бортпроводники", callback_data=f"{prefix}_cabin")
    b.adjust(1)
    return b.as_markup()

def get_pagination_kb(items: list, page: int, per_page: int, prefix: str):
    b = InlineKeyboardBuilder()
    start = page * per_page
    for item_id, item_text in items[start:start+per_page]:
        b.button(text=str(item_text), callback_data=f"{prefix}_select_{item_id}")
    b.adjust(1)
    total = max(1, (len(items) - 1) // per_page + 1)
    nav = [
        InlineKeyboardButton(text="|<|" if page > 0 else " ", callback_data=f"{prefix}_page_{page-1}" if page > 0 else "ignore"),
        InlineKeyboardButton(text=f"|{page+1}/{total}|", callback_data="ignore"),
        InlineKeyboardButton(text="|>|" if page < total - 1 else " ", callback_data=f"{prefix}_page_{page+1}" if page < total - 1 else "ignore")
    ]
    b.row(*nav)
    return b.as_markup()

def get_cancel_skip_kb(allow_skip: bool = True):
    b = InlineKeyboardBuilder()
    if allow_skip: b.button(text="Пропустить ⏭️", callback_data="create_skip")
    b.button(text="Отменить ❌", callback_data="create_cancel")
    b.adjust(1)
    return b.as_markup()

# === ОБРАБОТЧИКИ АДМИН КОМАНД ===
@dp.message(Command("add_head"), F.chat.type == "private")
async def cmd_add_head(message: types.Message):
    if not await is_creator(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit(): return await message.answer("Формат: /add_head <ID>")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, role, is_active) VALUES (?, 'head_admin', 1)", (int(args[1]),))
        await db.commit()
    await message.answer("Назначен главный администратор.")

@dp.message(Command("add_admin"), F.chat.type == "private")
async def cmd_add_admin(message: types.Message):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit(): return await message.answer("Формат: /add_admin <ID>")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, role, is_active) VALUES (?, 'admin', 1)", (int(args[1]),))
        await db.commit()
    await message.answer("Администратор добавлен.")

@dp.message(Command("add_trainee"), F.chat.type == "private")
async def cmd_add_trainee(message: types.Message, state: FSMContext):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit(): return await message.answer("Формат: /add_trainee <ID>")
    await state.update_data(target_id=int(args[1]))
    await message.answer("Выберите департамент:", reply_markup=get_departments_kb("dept"))
    await state.set_state(AddTrainee.waiting_for_dept)

@dp.callback_query(F.data.startswith("dept_"), AddTrainee.waiting_for_dept)
async def process_add_trainee_dept(call: CallbackQuery, state: FSMContext):
    dept_map = {"dept_pilots": "Пилоты", "dept_ground": "Наземные службы", "dept_cabin": "Бортпроводники"}
    dept = dept_map.get(call.data)
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, role, department, stage, is_active) VALUES (?, 'trainee', ?, 'Интервью', 1)", (data['target_id'], dept))
        await db.commit()
    await call.message.edit_text(f"Стажер добавлен в департамент: {dept}")
    await state.clear()

@dp.message(Command("advance"), F.chat.type == "private")
async def cmd_advance(message: types.Message):
    if not await is_head_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit(): return await message.answer("Формат: /advance <ID>")
    uid = int(args[1])
    data = await get_user_data(uid)
    if not data or data[0] != 'trainee': return await message.answer("Стажер не найден.")
    stages = ["Интервью", "Тренинг", "Экзамен", "Завершено"]
    try:
        next_stage = stages[stages.index(data[2]) + 1]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET stage = ? WHERE user_id = ?", (next_stage, uid))
            await db.commit()
        await message.answer(f"Новый этап: {next_stage}")
    except IndexError:
        await message.answer("Обучение уже завершено.")

@dp.message(Command("trainees"), F.chat.type == "private")
async def cmd_trainees(message: types.Message):
    if not await is_admin(message.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, department, stage FROM users WHERE role = 'trainee' AND is_active = 1") as c:
            rows = await c.fetchall()
    if not rows: return await message.answer("Активные стажеры отсутствуют.")
    lines = ["*Список стажеров:*\n"]
    for uid, dept, stage in rows:
        link = f"[Профиль стажера](tg://user?id={uid})"
        lines.append(f"👤 {link} \\(ID: `{uid}`\\)\nДепартамент: {escape_md(dept)} \\| Этап: {escape_md(stage)}\n")
    await message.answer("\n".join(lines), parse_mode="MarkdownV2")

# === МЕРОПРИЯТИЯ ===
@dp.message(Command("create"), F.chat.type == "private")
async def cmd_create(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    b = InlineKeyboardBuilder()
    b.button(text="Интервью", callback_data="ctype_interview")
    b.button(text="Тренинг", callback_data="ctype_training")
    await message.answer("Тип слота:", reply_markup=b.as_markup())
    await state.set_state(CreateEvent.choosing_type)

@dp.callback_query(F.data.startswith("ctype_"), CreateEvent.choosing_type)
async def process_create_type(call: CallbackQuery, state: FSMContext):
    etype = call.data.split("_")[1]
    await state.update_data(type=etype)
    if etype == "interview":
        await call.message.edit_text("Дата (ДД.ММ.ГГГГ):")
        await state.set_state(CreateEvent.waiting_for_date)
    else:
        await call.message.edit_text("Департамент:", reply_markup=get_departments_kb("tdept"))
        await state.set_state(CreateEvent.choosing_dept)

@dp.callback_query(F.data.startswith("tdept_"), CreateEvent.choosing_dept)
async def process_create_dept(call: CallbackQuery, state: FSMContext):
    dept_map = {"tdept_pilots": "Пилоты", "tdept_ground": "Наземные службы", "tdept_cabin": "Бортпроводники"}
    await state.update_data(department=dept_map.get(call.data))
    await call.message.edit_text("Дата (ДД.ММ.ГГГГ):")
    await state.set_state(CreateEvent.waiting_for_date)

@dp.message(StateFilter(CreateEvent.waiting_for_date, CreateEvent.waiting_for_time, 
                        CreateEvent.waiting_for_location, CreateEvent.waiting_for_description))
async def process_creation_steps(message: types.Message, state: FSMContext):
    st = await state.get_state()
    if st == CreateEvent.waiting_for_date:
        await state.update_data(date=message.text)
        await state.set_state(CreateEvent.waiting_for_time)
        await message.answer("Время (ЧЧ:ММ):")
    elif st == CreateEvent.waiting_for_time:
        await state.update_data(time=message.text)
        data = await state.get_data()
        if data['type'] == 'interview': await finalize_creation(message, state)
        else:
            await state.set_state(CreateEvent.waiting_for_location)
            await message.answer("Место:")
    elif st == CreateEvent.waiting_for_location:
        await state.update_data(location=message.text)
        await state.set_state(CreateEvent.waiting_for_description)
        await message.answer("Описание:")
    elif st == CreateEvent.waiting_for_description:
        await state.update_data(description=message.text)
        await finalize_creation(message, state)

async def finalize_creation(message: types.Message, state: FSMContext):
    d = await state.get_data()
    host = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO events (type, department, date, time, location, description, host_name) 
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (d['type'], d.get('department'), d['date'], d['time'], 
                          d.get('location', ' '), d.get('description', ' '), host))
        await db.commit()
    await message.answer("Слот успешно создан.")
    await state.clear()

# === СИСТЕМА ЗАПИСЕЙ ===
@dp.message(Command("interview"), F.chat.type == "private")
async def cmd_interview(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data or data[2] != 'Интервью' or data[3] == 0: return await message.answer("Недоступно.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, date, time FROM events WHERE type = 'interview'") as c:
            events = await c.fetchall()
    actual = [e for e in events if is_event_actual(e[1], e[2])]
    if not actual: return await message.answer("Слотов нет.")
    items = [(e[0], f"{e[1]} в {e[2]}") for e in actual]
    await message.answer("Выберите время:", reply_markup=get_pagination_kb(items, 0, 5, "book"))

@dp.message(Command("training"), F.chat.type == "private")
async def cmd_training(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data or data[2] != 'Тренинг' or data[3] == 0: return await message.answer("Недоступно.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, date, time FROM events WHERE type = 'training' AND department = ?", (data[1],)) as c:
            events = await c.fetchall()
    actual = [e for e in events if is_event_actual(e[1], e[2])]
    if not actual: return await message.answer("Слотов нет.")
    items = [(e[0], f"{e[1]} в {e[2]}") for e in actual]
    await message.answer("Выберите время:", reply_markup=get_pagination_kb(items, 0, 5, "book"))

@dp.callback_query(F.data.startswith("book_select_"))
async def process_booking(call: CallbackQuery):
    eid = int(call.data.split("_")[2])
    uid = call.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO bookings (event_id, user_id) VALUES (?, ?)", (eid, uid))
            await db.commit()
            await call.message.edit_text("Вы успешно записаны.")
        except aiosqlite.IntegrityError:
            await call.answer("Вы уже записаны!", show_alert=True)

@dp.message(Command("my_bookings"), F.chat.type == "private")
async def cmd_my_bookings(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""SELECT e.id, e.type, e.date, e.time FROM events e 
                                 JOIN bookings b ON e.id = b.event_id WHERE b.user_id = ?""", (message.from_user.id,)) as c:
            rows = await c.fetchall()
    if not rows: return await message.answer("Записей нет.")
    for eid, etype, edate, etime in rows:
        name = "Интервью" if etype == "interview" else "Тренинг"
        b = InlineKeyboardBuilder().button(text="Отменить ❌", callback_data=f"cancel_{eid}")
        await message.answer(f"{name}: {edate} в {etime}", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_book(call: CallbackQuery):
    eid = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bookings WHERE event_id = ? AND user_id = ?", (eid, call.from_user.id))
        await db.commit()
    await call.message.edit_text("Запись отменена.")

@dp.message(Command("profile"), F.chat.type == "private")
async def cmd_profile(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data: return
    await message.answer(f"👤 Профиль: {message.from_user.first_name}\nДепартамент: {data[1]}\nЭтап: {data[2]}")

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    data = await get_user_data(message.from_user.id)
    if not data and not await is_creator(message.from_user.id):
        return await message.answer("Доступ закрыт.")
    await message.answer("Система активна. Команды доступны в меню /")

@dp.callback_query(F.data == "ignore")
async def ignore_cb(call: CallbackQuery): await call.answer()

# === ЗАПУСК ===
async def main():
    await init_db()
    await set_main_menu(bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
